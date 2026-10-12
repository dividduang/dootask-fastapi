from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import math
import re
import secrets
from urllib.parse import quote, urljoin
from sqlalchemy import select, delete, or_
from . import db
from .collaboration import create_personal_project
from .context import RequestContext, utcnow
from .errors import ApiError
from .security import digest, password_hash, password_verify, clear_login_failure


@dataclass
class Actor:
    userid: int
    user: dict
    credential: dict
    session: dict

    @property
    def identities(self):
        return split_values(self.user.get("identity"))


def split_values(value):
    return [item for item in (value or "").strip(",").split(",") if item]

@dataclass(frozen=True)
class EmailVerificationRequired:
    message: str = "您还没有验证邮箱，请先验证邮箱"

    @property
    def data(self):
        return {"code": "email"}


def is_disabled(user):
    return bool(user.get("disable_at") or "disable" in split_values(user.get("identity")))



def require_admin(actor):
    if "admin" not in actor.identities:
        raise ApiError("权限不足")


def lock_user(connection, userid):
    users = db.tables["users"]
    user = connection.execute(select(users).where(users.c.userid == userid).with_for_update()).mappings().first()
    if not user:
        raise ApiError("成员不存在或已被删除")
    return dict(user)


def authenticate(connection, context: RequestContext):
    if context.actor is not None:
        return context.actor
    if not context.token:
        raise ApiError("请登录后继续...", -1)
    sessions = db.tables["auth_sessions"]
    session = connection.execute(select(sessions).where(sessions.c.token_digest == digest(context.token))).mappings().first()
    now = utcnow()
    if not session or session["revoked_at"] or session["expires_at"] <= now:
        raise ApiError("身份已失效,请重新登录", -1)
    # Serialize account mutations and token rotation on the account row.
    users = db.tables["users"]
    user = connection.execute(select(users).where(users.c.userid == session["userid"]).with_for_update()).mappings().first()
    credentials = db.tables["auth_credentials"]
    cred = connection.execute(select(credentials).where(credentials.c.userid == session["userid"])).mappings().first()
    devices = db.tables["user_devices"]
    device = connection.execute(select(devices).where(devices.c.id == session["device_id"])).mappings().first()
    # Re-read after acquiring account lock: logout/change-password may have committed while waiting.
    session = connection.execute(select(sessions).where(sessions.c.token_digest == digest(context.token))).mappings().first()
    now = utcnow()
    if (not user or not cred or not session or session["revoked_at"]
            or session["expires_at"] <= now
            or cred["generation"] != session["generation"] or user["email"] != session["email"]):
        raise ApiError("身份已失效,请重新登录", -1)
    if session["purpose"] == "service":
        valid_device = session["device_id"] is None and session["expires_at"] <= session["created_at"] + timedelta(hours=1)
    else:
        valid_device = (session["purpose"] == "interactive" and device and not device["deleted_at"]
            and (not device["expired_at"] or device["expired_at"] > now) and device["userid"] == user["userid"])
    if not valid_device:
        raise ApiError("身份已失效,请重新登录", -1)
    if is_disabled(user):
        raise ApiError("帐号已停用...", -1)
    updates = {"line_ip": context.ip, "updated_at": now}
    if not user["line_at"] or user["line_at"] < now - timedelta(seconds=30):
        updates["line_at"] = now
    if context.language in ("zh", "zh-CHT", "en", "ja", "ko", "de", "fr", "id", "ru"):
        updates["lang"] = context.language
    connection.execute(users.update().where(users.c.userid == user["userid"]).values(**updates))
    data = dict(user)
    data.update(updates)
    context.actor = Actor(user["userid"], data, dict(cred), dict(session))
    return context.actor


def validate_password(connection, password):
    size = len(password.encode("utf-8"))
    if size < 6:
        raise ApiError("密码设置不能小于6位数")
    if size > 32:
        raise ApiError("密码最多只能设置32位数")
    if db.setting(connection, "system", "password_policy") == "complex":
        if any(re.fullmatch(pattern, password) for pattern in (r"[0-9]+", r"[a-zA-Z]+", r"[0-9A-Z]+", r"[0-9a-z]+")):
            raise ApiError("请设置包含大小写字母、数字或特殊字符的复杂密码")


def creation_lock(connection):
    state = db.tables["account_identity_state"]
    return connection.execute(select(state).where(state.c.id == 1).with_for_update()).mappings().one()


def create_account(connection, context, email, password, *, nickname="", identity="", bot=False,
                   email_verity=False, changepass=False, profession="", departments=()):
    """Internal creation primitive; public callers enforce registration/admin/directory policy."""
    from pypinyin import lazy_pinyin
    from .domains import account_admin
    from .domains.dialog_core import sync_members, member_ids, send_message
    from . import jobs
    if len(email) > 100 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ApiError("请输入正确的邮箱地址")
    if not bot and email.casefold().endswith("@bot.system"):
        raise ApiError("此邮箱域仅用于系统机器人")
    validate_password(connection, password)
    state = creation_lock(connection)
    users = db.tables["users"]
    if connection.execute(select(users.c.userid).where(users.c.email == email)).first():
        raise ApiError("邮箱地址已存在")
    first = not bot and state["owner_userid"] is None
    if first:
        identity = ",admin," + ("ldap," if "ldap" in split_values(identity) else "")
    name = nickname or email.split("@")[0]
    pinyin = "".join(lazy_pinyin(name))
    userid = db.insert_row(connection, "users", email=email, nickname=nickname, identity=identity,
        bot=int(bot), email_verity=int(email_verity), changepass=int(changepass), profession=profession,
        encrypt=secrets.token_hex(16), password="", created_ip=context.ip,
        lang=context.language, az=(pinyin[:1] or "#").upper(), pinyin=pinyin)
    db.insert_row(connection, "auth_credentials", userid=userid, password_hash=password_hash(password),
                  generation=secrets.token_hex(32))
    if first:
        state_table = db.tables["account_identity_state"]
        connection.execute(state_table.update().where(state_table.c.id == 1).values(owner_userid=userid))
    user = dict(connection.execute(select(users).where(users.c.userid == userid)).mappings().one())
    if not bot:
        from .domains.project_core import push_project, by_id
        from .domains.projects import project_data
        project_id = create_personal_project(connection, userid)
        push_project(connection, context, project_id, "add", project_data(connection, by_id(connection, "projects", project_id), userid))
        jobs.enqueue(connection, "search.sync", {"type":"project","id":project_id})
        if departments:
            user = account_admin.set_departments(connection, context, user, departments)
        dialogs = db.tables["web_socket_dialogs"]
        if db.setting(connection, "system", "all_group_autoin", "yes") == "yes":
            group = connection.execute(select(dialogs.c.id).where(dialogs.c.group_type == "all",
                dialogs.c.deleted_at.is_(None)).order_by(dialogs.c.id.desc()).limit(1).with_for_update()).first()
            if group:
                sync_members(connection, group.id, set(member_ids(connection, group.id)) | {userid})
                send_message(connection, dialog_id=group.id, sender_id=0, msg_type="notice",
                    msg={"notice": name + " 已加入群组"}, context=context)
                jobs.enqueue(connection, "dialog.member_hook", {"dialog_id":group.id,"userid":0,"userids":[userid],"event":"member_join"})
        jobs.enqueue(connection, "accounts.search", {"userid": userid})
        jobs.enqueue(connection, "accounts.created", {"userid": userid})
    return user


def register(connection, context, email, password, invite=""):
    if len(email) > 32 or len(password) > 32:
        raise ApiError("账号密码最多可输入32位字符")
    mode = db.setting(connection, "system", "reg", "open")
    if mode == "close":
        raise ApiError("未开放注册")
    if mode == "invite" and (not invite or not secrets.compare_digest(invite, str(db.setting(connection, "system", "reg_invite", "")))):
        raise ApiError("请输入正确的邀请码")
    identity = ",temp," if db.setting(connection, "system", "reg_identity", "normal") == "temp" else ""
    return create_account(connection, context, email, password, identity=identity)["userid"]


def issue_token(connection, user, credential, context):
    current = lock_user(connection, user["userid"])
    if is_disabled(current):
        raise ApiError("帐号已停用...")
    now = utcnow()
    days = max(1, int(db.setting(connection, "system", "token_valid_days", 30)))
    expires = now + timedelta(days=days)
    token = secrets.token_urlsafe(48)
    device_hash = hashlib.md5(token.encode(), usedforsecurity=False).hexdigest()
    detail = {"ip": context.ip, "type": "手机" if context.platform in ("ios", "android") else "电脑",
              "os": "Unknown", "browser": "Unknown", "version": "", "app_name": "",
              "app_type": context.platform, "app_version": context.version, "user_agent": context.user_agent}
    device_id = db.insert_row(connection, "user_devices", userid=user["userid"], hash=device_hash,
                              detail=json.dumps(detail, ensure_ascii=False), expired_at=expires)
    db.insert_row(connection, "auth_sessions", token_digest=digest(token), userid=user["userid"], device_id=device_id,
                  generation=credential["generation"], email=user["email"], expires_at=expires)
    devices = db.tables["user_devices"]
    old = connection.execute(select(devices.c.id).where(devices.c.userid == user["userid"], devices.c.deleted_at.is_(None))
                             .order_by(devices.c.id.desc()).offset(200)).scalars().all()
    if old:
        revoke_devices(connection, old)
    return token


def issue_service_token(connection, userid, context):
    users, credentials = db.tables["users"], db.tables["auth_credentials"]
    user = connection.execute(select(users).where(users.c.userid == userid).with_for_update()).mappings().first()
    credential = connection.execute(select(credentials).where(credentials.c.userid == userid)).mappings().first()
    if not user or not credential or is_disabled(user):
        raise ApiError("机器人或发送成员身份不可用")
    now = utcnow()
    token = secrets.token_urlsafe(48)
    db.insert_row(connection, "auth_sessions", token_digest=digest(token), userid=userid, device_id=None,
        purpose="service", generation=credential["generation"], email=user["email"],
        created_at=now, expires_at=now+timedelta(hours=1))
    return token


def revoke_devices(connection, ids):
    if not ids:
        return
    devices, sessions = db.tables["user_devices"], db.tables["auth_sessions"]
    users = db.tables["users"]
    owners = select(devices.c.userid).where(devices.c.id.in_(ids))
    connection.execute(select(users.c.userid).where(users.c.userid.in_(owners)).order_by(users.c.userid).with_for_update()).all()
    hashes = connection.execute(select(devices.c.hash).where(devices.c.id.in_(ids))).scalars().all()
    userids = select(devices.c.userid).where(devices.c.id.in_(ids))
    connection.execute(sessions.update().where(sessions.c.userid.in_(userids), sessions.c.purpose == "service").values(revoked_at=utcnow()))
    now = utcnow()
    connection.execute(devices.update().where(devices.c.id.in_(ids)).values(deleted_at=now, updated_at=now))
    connection.execute(sessions.update().where(sessions.c.device_id.in_(ids)).values(revoked_at=now))
    aliases = db.tables["umeng_alias"]
    connection.execute(delete(aliases).where(aliases.c.device_hash.in_(hashes)))


def login(connection, context, email, password, *, settings, registration=False, invite=""):
    from .domains.account_integrations import ldap_login, ldap_sync_local, send_verification
    if len(email) > 100 or len(password.encode("utf-8")) > 32:
        raise ApiError("帐号或密码错误", data={"code": "no"})
    if registration:
        register(connection, context, email, password, invite)
    users, credentials = db.tables["users"], db.tables["auth_credentials"]
    user = connection.execute(select(users).where(users.c.email == email).with_for_update()).mappings().first()
    directory = not registration and (not user or "ldap" in split_values(user["identity"])) and db.setting(connection, "thirdAccessSetting", "ldap_open") == "open"
    if directory:
        user = ldap_login(connection, context, email, password, settings)
        user = connection.execute(select(users).where(users.c.userid == user["userid"]).with_for_update()).mappings().one()
    credential = connection.execute(select(credentials).where(credentials.c.userid == user["userid"])).mappings().first() if user else None
    if not credential or (not directory and ("ldap" in split_values(user["identity"]) or not password_verify(password, credential["password_hash"]))):
        if not credential:
            password_hash(password[:32])
        raise ApiError("帐号或密码错误", data={"code": "no"})
    if is_disabled(user):
        raise ApiError("帐号已停用...")
    if db.setting(connection, "emailSetting", "reg_verify") == "open" and not user["email_verity"]:
        send_verification(connection, context, user, deferred=True)
        return EmailVerificationRequired("注册成功，请验证邮箱后登录" if registration else "您还没有验证邮箱，请先验证邮箱")
    if not directory:
        try:
            ldap_sync_local(connection, user, password, settings)
        except ApiError:
            logging.getLogger(__name__).exception("LDAP synchronization failed for user %s", user["userid"])
    now = utcnow()
    changes = {"login_num": (user["login_num"] or 0) + 1, "last_ip": context.ip, "last_at": now,
               "line_ip": context.ip, "line_at": now, "updated_at": now}
    connection.execute(users.update().where(users.c.userid == user["userid"]).values(**changes))
    user = dict(user)
    user.update(changes)
    token = issue_token(connection, user, credential, context)
    create_personal_project(connection, user["userid"])
    clear_login_failure(connection, email)
    return user_data(connection, user, token, context)


def user_data(connection, user, token, context, *, detailed=False):
    data = {key: value for key, value in user.items() if key not in ("password", "encrypt", "updated_at")}
    data["identity"] = split_values(user.get("identity"))
    data["department"] = [int(item) for item in split_values(user.get("department")) if item.isdecimal()]
    data["nickname"] = user.get("nickname") or user["email"].split("@")[0]
    data["token"] = token
    image = user.get("userimg")
    data["userimg"] = urljoin(context.base_url, image) if image and "avatar/" not in image else context.base_url.rstrip("/") + "/avatar/" + quote(data["nickname"], safe="") + ".png"
    if detailed:
        data["nickname_original"] = user.get("nickname") or ""
        deps, owners = db.tables["user_departments"], db.tables["user_department_owners"]
        names = connection.execute(select(deps.c.name, deps.c.owner_userid).where(deps.c.id.in_(data["department"])).limit(10)).all()
        data["department_name"] = ", ".join(row.name + (" (M)" if row.owner_userid == user["userid"] else "") for row in names)
        data["department_owner"] = connection.execute(select(deps.c.id).where(deps.c.owner_userid == user["userid"], deps.c.parent_id == 0).limit(1)).first() is not None
        deputies = select(owners.c.department_id).where(owners.c.userid == user["userid"])
        data["managed_departments"] = [dict(row) for row in connection.execute(select(deps.c.id, deps.c.name, deps.c.parent_id, deps.c.owner_userid)
            .where(or_(deps.c.owner_userid == user["userid"], deps.c.id.in_(deputies))).order_by(deps.c.id)).mappings()]
    return data


def info(connection, context):
    actor = authenticate(connection, context)
    token = context.token
    if context.platform in ("ios", "android") and should_refresh(connection, actor):
        token = issue_token(connection, actor.user, actor.credential, context)
    return user_data(connection, actor.user, token, context, detailed=True)


def should_refresh(connection, actor):
    if actor.session["purpose"] != "interactive":
        return False
    days = max(1, int(db.setting(connection, "system", "token_valid_days", 30)))
    return actor.session["expires_at"] < utcnow() + timedelta(days=math.ceil(days / 3))


def token_expire(connection, context, refresh=False):
    actor = authenticate(connection, context)
    now = utcnow()
    expires = actor.session["expires_at"]
    data = {"expired_at": expires, "remaining_seconds": int((expires - now).total_seconds()),
            "expired": expires <= now, "server_time": now}
    if refresh and should_refresh(connection, actor):
        data["token"] = issue_token(connection, actor.user, actor.credential, context)
    return data


def logout(connection, context):
    # Idempotent source behavior: missing/expired token may still be logged out.
    sessions = db.tables["auth_sessions"]
    row = connection.execute(select(sessions).where(sessions.c.token_digest == digest(context.token))).mappings().first()
    if row:
        users = db.tables["users"]
        connection.execute(select(users.c.userid).where(users.c.userid == row["userid"]).with_for_update()).first()
        if row["purpose"] == "service":
            connection.execute(sessions.update().where(sessions.c.token_digest == row["token_digest"]).values(revoked_at=utcnow()))
        else:
            revoke_devices(connection, [row["device_id"]])


def change_password(connection, context, old_password, new_password, *, settings):
    actor = authenticate(connection, context)
    if actor.session["purpose"] == "service":
        raise ApiError("服务身份不能修改密码")
    if actor.user.get("bot"):
        raise ApiError("当前帐号不支持此操作")
    if old_password == new_password:
        raise ApiError("新旧密码一致")
    validate_password(connection, new_password)
    if "ldap" in actor.identities:
        from .domains.account_integrations import ldap_login, ldap_update
        verified = ldap_login(connection, context, actor.user["email"], old_password, settings)
        if verified["userid"] != actor.userid:
            raise ApiError("请填写正确的旧密码")
        ldap_update(connection, actor.user["email"], {"userPassword": new_password}, required=True)
    elif not password_verify(old_password, actor.credential["password_hash"]):
        raise ApiError("请填写正确的旧密码")
    else:
        from .domains.account_integrations import ldap_update
        ldap_update(connection, actor.user["email"], {"userPassword": new_password})
    cred = dict(actor.credential, password_hash=password_hash(new_password), generation=secrets.token_hex(32), updated_at=utcnow())
    credentials, users, devices = db.tables["auth_credentials"], db.tables["users"], db.tables["user_devices"]
    connection.execute(credentials.update().where(credentials.c.userid == actor.userid).values(**cred))
    connection.execute(users.update().where(users.c.userid == actor.userid).values(changepass=0, encrypt=secrets.token_hex(16), updated_at=utcnow()))
    ids = connection.execute(select(devices.c.id).where(devices.c.userid == actor.userid, devices.c.deleted_at.is_(None))).scalars().all()
    revoke_devices(connection, ids)
    actor.user["changepass"] = 0
    token = issue_token(connection, actor.user, cred, context)
    return user_data(connection, actor.user, token, context)
