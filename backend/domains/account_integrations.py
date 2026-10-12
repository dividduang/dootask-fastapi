"""Real SMTP/LDAP integrations. No credentials or successful results are synthesized."""
import html
import json
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from sqlalchemy import select, delete
from .. import auth, db
from ..context import utcnow
from ..errors import ApiError
from ..storage import atomic_write
from .account_common import decoded, row, update, email_valid


def send_mail(config, to, subject, content, alias='Task'):
    if not email_valid(to):
        raise ApiError('请输入正确的收件人地址')
    if not config.get('smtp_server') or not config.get('account'):
        raise ApiError('邮件服务未配置')
    message = EmailMessage()
    message['From'] = f"{alias} <{config['account']}>"
    message['To'] = to
    message['Subject'] = subject
    message.set_content(content, subtype='html')
    port = int(config.get('port') or 25)
    try:
        client = smtplib.SMTP_SSL(config['smtp_server'], port, timeout=30, context=ssl.create_default_context()) if port == 465 else smtplib.SMTP(config['smtp_server'], port, timeout=30)
        with client:
            client.ehlo()
            if port != 465 and client.has_extn('starttls'):
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if config.get('password'):
                client.login(config['account'], config['password'])
            client.send_message(message)
    except TimeoutError as exc:
        raise ApiError('邮件发送超时，请检查邮箱配置是否正确') from exc
    except smtplib.SMTPResponseException as exc:
        if exc.smtp_code == 550:
            raise ApiError('邮件内容被拒绝，请检查邮箱是否开启接收功能') from exc
        raise ApiError('邮件发送失败：' + exc.smtp_error.decode('utf-8', errors='replace')) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise ApiError('邮件发送失败：' + str(exc)) from exc


def send_verification(connection, context, user, type=1, email=None, *, deferred=False):
    email = user['email'] if type == 1 else email
    if not email_valid(email or ''):
        raise ApiError('邮箱地址错误')
    t = db.tables['user_email_verifications']
    old = connection.execute(select(t).where(t.c.email == email, t.c.type == type, t.c.created_at > utcnow()-timedelta(minutes=30))).mappings().first()
    if old and type == 1:
        return
    code = secrets.token_hex(32) if type == 1 else str(secrets.randbelow(900000)+100000)
    alias = db.setting(connection, 'system', 'system_alias', 'Task') or 'Task'
    verb = {1:'绑定邮箱验证',2:'修改邮箱验证',3:'注销帐号验证'}.get(type)
    if not verb:
        raise ApiError('参数错误')
    content = html.escape(user.get('nickname') or user['email']) + ' 您好，请在30分钟内完成验证。'
    if type == 1:
        link = context.base_url.rstrip('/') + '/single/valid/email?code=' + code
        content += '<p><a href="' + html.escape(link, quote=True) + '">' + html.escape(link) + '</a></p>'
    else:
        content += '<p>' + code + '</p><p>如果不是本人操作，您的帐号可能存在风险，请及时修改密码！</p>'
    config = db.setting(connection, 'emailSetting')
    if not config.get('smtp_server') or not config.get('account'):
        raise ApiError('邮件服务未配置')
    if not deferred:
        send_mail(config, email, alias+verb, content, alias)
    connection.execute(delete(t).where(t.c.userid == user['userid'], t.c.email == email, t.c.type == type))
    verification_id = db.insert_row(connection, 'user_email_verifications', userid=user['userid'], email=email, code=code, status=0, type=type)
    if deferred:
        from ..jobs import enqueue
        enqueue(connection, 'accounts.verify_email', {'verification_id':verification_id, 'subject':alias+verb, 'content':content, 'alias':alias})


def verify_email(connection, email, code, type, *, consume=True):
    if not code:
        raise ApiError('请输入验证码')
    t = db.tables['user_email_verifications']
    result = connection.execute(select(t).where(t.c.email == email, t.c.type == type).order_by(t.c.id.desc()).with_for_update()).mappings().first()
    if not result or result['status'] or not secrets.compare_digest(str(result['code']), str(code)):
        raise ApiError('验证码错误')
    if abs((utcnow()-result['created_at']).total_seconds()) > 1800:
        raise ApiError('验证码已失效')
    if consume:
        update(connection, 'user_email_verifications', {'id':result['id']}, {'status':1})
    return result


def verification_job(connection, payload, settings):
    value = row(connection, 'user_email_verifications', id=payload['verification_id'])
    if not value or value['status'] or value['created_at'] < utcnow()-timedelta(minutes=30):
        return
    user = row(connection, 'users', userid=value['userid'])
    if not user or auth.is_disabled(user) or user['email'] != value['email']:
        return
    send_mail(db.setting(connection, 'emailSetting'), value['email'], payload['subject'], payload['content'], payload['alias'])


def ldap_connection(config):
    from ldap3 import Server, Connection, Tls
    if not config.get('ldap_host') or not config.get('ldap_user_dn') or not config.get('ldap_password'):
        raise ApiError('LDAP服务未配置')
    host = config['ldap_host']
    tls = host.startswith('ldaps://')
    server = Server(host, port=int(config.get('ldap_port') or (636 if tls else 389)), use_ssl=tls,
                    tls=Tls(validate=ssl.CERT_REQUIRED), connect_timeout=15)
    try:
        return Connection(server, user=config['ldap_user_dn'], password=config['ldap_password'], auto_bind=True, raise_exceptions=True, receive_timeout=20)
    except Exception as exc:
        raise ApiError('LDAP连接或认证失败') from exc


def ldap_find(client, config, value, attributes=None):
    from ldap3.utils.conv import escape_filter_chars
    allowed = ('cn','uid','mail','sAMAccountName','userPrincipalName')
    attr = config.get('ldap_login_attr', 'cn')
    attr = attr if attr in allowed else 'cn'
    client.search(config.get('ldap_base_dn',''), f'(&(objectClass=person)({attr}={escape_filter_chars(value)}))', attributes=attributes or ['mail','cn','uid','userPrincipalName','displayName','jpegPhoto'])
    if len(client.entries) > 1:
        raise ApiError('LDAP 登录属性不唯一')
    return client.entries[0] if client.entries else None


def ldap_login(connection, context, email, password, settings):
    from ldap3.core.exceptions import LDAPException
    config = db.setting(connection, 'thirdAccessSetting')
    if config.get('ldap_open') != 'open' or not password:
        raise ApiError('帐号或密码错误')
    client = ldap_connection(config)
    try:
        entry = ldap_find(client, config, email)
        if entry is None or not client.rebind(user=entry.entry_dn, password=password):
            raise ApiError('帐号或密码错误')
        attrs = entry.entry_attributes_as_dict
        address = next((str(attrs[k][0]) for k in ('mail','cn','uid','userPrincipalName') if attrs.get(k) and email_valid(str(attrs[k][0]))), None)
        user = row(connection, 'users', email=email)
        if not user:
            if not address:
                raise ApiError('LDAP 用户缺少邮箱属性，请联系管理员配置')
            user = row(connection, 'users', email=address)
        if user and 'ldap' not in auth.split_values(user['identity']):
            raise ApiError('LDAP 邮箱与本地帐号冲突，请联系管理员')
        if not user:
            user = auth.create_account(connection, context, address, password, identity=',ldap,',
                nickname=str((attrs.get('displayName') or attrs.get('uid') or [''])[0]))
        user = auth.lock_user(connection, user['userid'])
        if auth.is_disabled(user):
            raise ApiError('帐号已停用...')
        changes = {'nickname':str((attrs.get('displayName') or attrs.get('uid') or [''])[0]),
                   'identity':','+','.join(dict.fromkeys(auth.split_values(user['identity'])+['ldap']))+','}
        photo = attrs.get('jpegPhoto')
        if photo and isinstance(photo[0], bytes):
            from PIL import Image
            import io
            image = Image.open(io.BytesIO(photo[0]))
            image.verify()
            path = f"uploads/user/ldap/{user['userid']}.jpeg"
            atomic_write(settings.storage_root, path, photo[0])
            changes['userimg'] = path
        update(connection, 'users', {'userid':user['userid']}, changes)
        user.update(changes)
        return user
    except LDAPException as exc:
        raise ApiError('帐号或密码错误') from exc
    finally:
        client.unbind()


def ldap_update(connection, email, changes, delete_user=False, *, required=False):
    from ldap3.core.exceptions import LDAPException
    config = db.setting(connection, 'thirdAccessSetting')
    if config.get('ldap_open') != 'open':
        if required:
            raise ApiError('LDAP服务未配置')
        return
    if not changes and not delete_user:
        return
    from ldap3 import MODIFY_REPLACE
    from ldap3.utils.conv import escape_filter_chars
    client = ldap_connection(config)
    try:
        for attr in ('mail','cn','uid','userPrincipalName'):
            client.search(config.get('ldap_base_dn',''), f'({attr}={escape_filter_chars(email)})', attributes=['cn'])
            if client.entries:
                dn = client.entries[0].entry_dn
                if delete_user:
                    ok = client.delete(dn)
                else:
                    ok = client.modify(dn, {k:[(MODIFY_REPLACE,[v])] for k,v in changes.items()})
                if not ok:
                    raise ApiError('LDAP更新失败')
                return
        if required:
            raise ApiError('LDAP帐号不存在')
    except LDAPException as exc:
        raise ApiError('LDAP更新失败') from exc
    finally:
        client.unbind()


def ldap_sync_local(connection, user, password, settings):
    """Post-login LDAP synchronization; caller records source failure separately."""
    from ldap3.core.exceptions import LDAPException
    config = db.setting(connection, 'thirdAccessSetting')
    if config.get('ldap_open') != 'open' or config.get('ldap_sync_local') != 'open' or 'ldap' in auth.split_values(user.get('identity')):
        return
    from ldap3.utils.dn import escape_rdn
    from ldap3.utils.conv import escape_filter_chars
    client = ldap_connection(config)
    try:
        for attr in ('mail', 'cn', 'uid', 'userPrincipalName'):
            client.search(config.get('ldap_base_dn', ''), f'({attr}={escape_filter_chars(user["email"])})', attributes=['cn'])
            if client.entries:
                return
        attrs = {'cn': user['email'], 'sn': user['email'], 'uid': user['email'],
                 'userPassword': password, 'displayName': user.get('nickname') or user['email'], 'mail': user['email']}
        if not client.add('cn=' + escape_rdn(user['email']) + ',' + config['ldap_base_dn'], ['person', 'top'], attrs):
            raise ApiError('LDAP同步失败')
        update(connection, 'users', {'userid': user['userid']}, {'identity': ',' + ','.join(auth.split_values(user.get('identity')) + ['ldap']) + ','})
    except LDAPException as exc:
        raise ApiError('LDAP同步失败') from exc
    finally:
        client.unbind()
