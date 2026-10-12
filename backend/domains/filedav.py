"""WebDAV management API and app-password authentication."""
import base64
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy import select, func, or_
from ..auth import Actor, require_admin, is_disabled
from ..context import utcnow
from ..db import tables, insert_row, setting
from ..errors import ApiError
from ..security import password_hash, password_verify, consume_limit
from .files_support import actor, row, integer, change, save_setting, page, safe_name, decoded
from . import files


def config(connection):
    value = setting(connection, 'fileSetting')
    maximum = min(3650, max(1, integer(value.get('webdav_max_expire_days'), 365)))
    return {'enabled': value.get('webdav_enabled', 'close') == 'open', 'permission_type': value.get('webdav_permission_type', 'all'),
            'permission_userids': [integer(v) for v in value.get('webdav_permission_userids', [])],
            'max_credentials': min(20, max(1, integer(value.get('webdav_max_credentials'), 5))),
            'default_expire_days': min(maximum, max(1, integer(value.get('webdav_default_expire_days'), 90))),
            'max_expire_days': maximum, 'max_file_bytes': min(1024**3, max(1024**2, integer(value.get('webdav_max_file_bytes'), 1024**3))),
            'copy_max_nodes': min(10000, max(1, integer(value.get('webdav_copy_max_nodes'), 1000))),
            'audit_retention_days': min(365, max(7, integer(value.get('webdav_audit_retention_days'), 90)))}


def allowed(user, cfg):
    return cfg['enabled'] and not is_disabled(user) and not user.get('deleted_at') and (cfg['permission_type'] == 'all' or user['userid'] in cfg['permission_userids'])


def active_count(connection, userid=None):
    t = tables['webdav_credentials']
    query = select(func.count()).select_from(t).where(t.c.revoked_at.is_(None), or_(t.c.expires_at.is_(None), t.c.expires_at > utcnow()))
    return connection.execute(query.where(t.c.userid == userid) if userid else query).scalar_one()


def dav__status(connection, context, data, settings):
    user = actor(connection, context)
    cfg = config(connection)
    return {'enabled': cfg['enabled'], 'allowed': allowed(user.user, cfg), 'https': context.base_url.startswith('https://'), 'url': context.base_url.rstrip('/') + '/dav/',
            'max_credentials': cfg['max_credentials'], 'active_credentials': active_count(connection, user.userid),
            'default_expire_days': cfg['default_expire_days'], 'max_expire_days': cfg['max_expire_days'], 'max_file_bytes': cfg['max_file_bytes']}


def credential_status(item):
    return 'revoked' if item['revoked_at'] else 'expired' if item['expires_at'] and item['expires_at'] <= utcnow() else 'active'


def dav__credentials(connection, context, data, settings):
    user = actor(connection, context)
    t = tables['webdav_credentials']
    result = []
    for value in connection.execute(select(t).where(t.c.userid == user.userid).order_by(t.c.id.desc())).mappings():
        item = dict(value)
        item.pop('password_hash')
        item.pop('userid')
        item['status'] = credential_status(item)
        result.append(item)
    return result


def dav__create(connection, context, data, settings):
    user = actor(connection, context)
    cfg = config(connection)
    if not allowed(user.user, cfg):
        raise ApiError('WebDAV 未启用或你没有使用权限')
    if not context.base_url.startswith('https://'):
        raise ApiError('WebDAV 必须通过 HTTPS 使用')
    name = str(data.get('name', '')).strip()[:100]
    if not name:
        raise ApiError('请输入设备名称')
    days = integer(data.get('expire_days'), cfg['default_expire_days'])
    if not 1 <= days <= cfg['max_expire_days']:
        raise ApiError('有效期超出允许范围')
    if active_count(connection, user.userid) >= cfg['max_credentials']:
        raise ApiError('WebDAV 应用密码数量已达上限')
    password, public = secrets.token_urlsafe(32), 'dav_' + secrets.token_hex(12)
    expires = utcnow() + timedelta(days=days)
    ident = insert_row(connection, 'webdav_credentials', userid=user.userid, public_id=public, name=name, password_hash=password_hash(password), password_suffix=password[-4:], expires_at=expires)
    return {'id': ident, 'public_id': public, 'password': password, 'password_suffix': password[-4:], 'url': context.base_url.rstrip('/') + '/dav/', 'expires_at': expires}


def credential(connection, context, ident):
    user = actor(connection, context)
    result = row(connection, 'webdav_credentials', integer(ident), lock=True)
    if not result or result['userid'] != user.userid:
        raise ApiError('WebDAV 应用密码不存在')
    return result


def revoke(connection, item):
    change(connection, 'webdav_credentials', item['id'], revoked_at=utcnow())
    t = tables['webdav_locks']
    connection.execute(t.delete().where(t.c.credential_id == item['id']))


def dav__revoke(connection, context, data, settings):
    item = credential(connection, context, data.get('id'))
    revoke(connection, item)
    return {'id': item['id'], 'revoked_at': utcnow()}


def audit(connection, context, method, uri, status, *, credential_id=None, file_id=None, size=0, duration=0, result='success'):
    insert_row(connection, 'webdav_operation_logs', userid=context.actor.userid if context.actor else None,
               credential_id=credential_id, method=method, uri=uri[:1000], status=status, result=result[:100], file_id=file_id,
               bytes=size, duration_ms=duration, request_id=context.request_id[:64], ip=context.ip[:45], user_agent=context.user_agent[:255])


def dav__delete(connection, context, data, settings):
    item = credential(connection, context, data.get('id'))
    if credential_status(item) == 'active':
        raise ApiError('仅允许删除已失效的凭据')
    audit(connection, context, 'CREDENTIAL_DELETE', str(item['id']), 200, credential_id=item['id'])
    t = tables['webdav_credentials']
    connection.execute(t.delete().where(t.c.id == item['id']))
    return {'id': item['id']}


def conflicts_query():
    t = tables['files']
    return select(t.c.pid, t.c.userid, t.c.name, t.c.ext, func.count().label('file_count')).where(t.c.deleted_at.is_(None)).group_by(t.c.pid, t.c.userid, t.c.name, t.c.ext).having(func.count() > 1)


def conflict_count(connection):
    return connection.execute(select(func.count()).select_from(conflicts_query().subquery())).scalar_one()


def dav__adminsetting(connection, context, data, settings):
    require_admin(actor(connection, context))
    if data.get('type') == 'save':
        if data.get('webdav_enabled') == 'open' and conflict_count(connection):
            raise ApiError('存在文件路径冲突，请先处理后再启用 WebDAV')
        current = setting(connection, 'fileSetting')
        fields = ['enabled', 'permission_type', 'permission_userids', 'max_credentials', 'default_expire_days', 'max_expire_days', 'max_file_bytes', 'copy_max_nodes', 'audit_retention_days']
        current.update({f'webdav_{key}': data[f'webdav_{key}'] for key in fields if f'webdav_{key}' in data})
        current['webdav_enabled'] = 'open' if current.get('webdav_enabled') == 'open' else 'close'
        current['webdav_permission_type'] = 'appoint' if current.get('webdav_permission_type') == 'appoint' else 'all'
        current['webdav_permission_userids'] = sorted(set(integer(v) for v in decoded(current.get('webdav_permission_userids'), []) if integer(v) > 0))
        save_setting(connection, 'fileSetting', current)
    cfg = config(connection)
    return {'webdav_' + key: ('open' if value else 'close') if key == 'enabled' else value for key, value in cfg.items()}


def dav__adminstatus(connection, context, data, settings):
    require_admin(actor(connection, context))
    t = tables['webdav_locks']
    return {'config': config(connection), 'active_credentials': active_count(connection), 'active_locks': connection.execute(select(func.count()).select_from(t).where(t.c.timeout_at > utcnow())).scalar_one(), 'path_conflicts': conflict_count(connection)}


def dav__conflicts(connection, context, data, settings):
    user = actor(connection, context)
    require_admin(user)
    t = tables['files']
    result = page(connection, conflicts_query().order_by(t.c.userid, t.c.pid, t.c.name), data)
    output = []
    for group in result['data']:
        items = [dict(r) for r in connection.execute(select(t).where(t.c.pid == group['pid'], t.c.userid == group['userid'], t.c.name == group['name'], t.c.ext == group['ext'], t.c.deleted_at.is_(None))).mappings()]
        owner = row(connection, 'users', group['userid'], key='userid') or {}
        names = [files.full_name(i) for i in reversed(files.ancestors(connection, items[0]))]
        members = []
        for item in items:
            can = files.permission(connection, item, user.userid) >= 0
            members.append({'id': item['id'], 'type': item['type'], 'full_name': files.full_name(item), 'updated_at': item['updated_at'], 'can_open_location': can, 'location_board': ('mine' if item['userid'] == user.userid else 'shared') if can else None, 'location_parent_id': (0 if item['pshare'] == item['id'] and item['userid'] != user.userid else item['pid']) if can else None})
        output.append({'owner': {k: owner.get(k, '') for k in ('userid', 'nickname', 'email')}, 'parent_id': group['pid'], 'parent_path': '/' + '/'.join(names), 'path': '/' + '/'.join(names + [files.full_name(items[0])]), 'file_count': len(items), 'files': members})
    result['data'] = output
    return result


def dav__conflictrename(connection, context, data, settings):
    require_admin(actor(connection, context))
    from .files_locks import serialize_namespace, enforce
    serialize_namespace(connection)
    item = files.get(connection, integer(data.get('id')), True)
    enforce(connection, context, item, subtree=True)
    t = tables['files']
    count = connection.execute(select(func.count()).select_from(t).where(t.c.pid == item['pid'], t.c.userid == item['userid'], t.c.name == item['name'], t.c.ext == item['ext'], t.c.deleted_at.is_(None))).scalar_one()
    if count < 2:
        raise ApiError('目标不再属于路径冲突组')
    full = safe_name(data.get('name'))
    suffix = '.' + item['ext'] if item['ext'] else ''
    if suffix and not full.lower().endswith(suffix.lower()):
        raise ApiError('文件扩展名不可更改')
    name = full[:-len(suffix)] if suffix else full
    if files.unique_name(connection, item['pid'], item['userid'], name, item['ext'], item['id']) != name:
        raise ApiError('文件名称已存在')
    change(connection, 'files', item['id'], name=name)
    files.notify(connection, files.get(connection, item['id']), 'update')
    audit(connection, context, 'CONFLICT_RENAME', full, 200, file_id=item['id'])
    return {'id': item['id'], 'name': full, 'path_conflicts': conflict_count(connection)}


def dav__userrevoke(connection, context, data, settings):
    require_admin(actor(connection, context))
    userid = integer(data.get('userid'))
    if not row(connection, 'users', userid, key='userid'):
        raise ApiError('用户不存在')
    t = tables['webdav_credentials']
    values = list(connection.execute(select(t).where(t.c.userid == userid, t.c.revoked_at.is_(None)).with_for_update()).mappings())
    for item in values:
        revoke(connection, item)
    return {'userid': userid, 'revoked_count': len(values), 'revoked_at': utcnow()}


def authenticate_dav(connection, context, authorization):
    if not context.base_url.startswith('https://'):
        raise ApiError('WebDAV 必须通过 HTTPS 使用')
    try:
        scheme, secret = authorization.split(' ', 1)
        if scheme.lower() != 'basic':
            raise ValueError()
        public, password = base64.b64decode(secret, validate=True).decode('utf-8').split(':', 1)
    except (ValueError, UnicodeError):
        raise ApiError('WebDAV authentication required', ret=-1)
    consume_limit('webdav:' + hashlib.sha256((context.ip + '|' + public).encode()).hexdigest(), 120, 60)
    t = tables['webdav_credentials']
    # Discover owner without a lock, then follow HTTP's user -> credential order.
    owner = connection.execute(select(t.c.userid).where(t.c.public_id == public)).scalar_one_or_none()
    user = row(connection, 'users', owner, key='userid', lock=True) if owner else None
    item = connection.execute(select(t).where(t.c.public_id == public).with_for_update()).mappings().first()
    if not item or not user or item['userid'] != user['userid'] or credential_status(item) != 'active' or not password_verify(password, item['password_hash']):
        consume_limit('webdav-failure:' + hashlib.sha256((context.ip + '|' + public).encode()).hexdigest(), 10, 60)
        raise ApiError('WebDAV authentication required', ret=-1)
    if not user or not allowed(user, config(connection)):
        raise ApiError('WebDAV 未启用或你没有使用权限', ret=-1)
    context.actor = Actor(user['userid'], user, {}, {})
    context.pgp['dav_credential_id'] = item['id']
    if not item['last_used_at'] or item['last_used_at'] < utcnow() - timedelta(minutes=5):
        change(connection, 'webdav_credentials', item['id'], last_used_at=utcnow(), last_used_ip=context.ip[:45], last_user_agent=context.user_agent[:255])
    return dict(item)


ROUTES = {'file/dav/status': dav__status, 'file/dav/credentials': dav__credentials, 'file/dav/create': dav__create,
          'file/dav/revoke': dav__revoke, 'file/dav/delete': dav__delete, 'file/dav/adminsetting': dav__adminsetting,
          'file/dav/adminstatus': dav__adminstatus, 'file/dav/conflicts': dav__conflicts,
          'file/dav/conflictrename': dav__conflictrename, 'file/dav/userrevoke': dav__userrevoke}
