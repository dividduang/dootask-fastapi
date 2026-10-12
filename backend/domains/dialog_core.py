"""Chat collaboration primitives; callers supply their existing business transaction."""
import html
import json
import re
from datetime import timedelta
from sqlalchemy import select, func
from sqlalchemy.dialects.mysql import insert
from .. import db, jobs
from ..context import utcnow
from ..errors import ApiError, json_default


def table(name):
    return db.tables[name]


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=json_default)


def decode(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ApiError("参数格式错误") from exc


def ids(value):
    if value is None or value == "":
        return []
    if not isinstance(value, (list, tuple, set, dict)):
        value = str(value).split(",")
    try:
        return sorted(set(int(v) for v in value if int(v) > 0))
    except (ValueError, TypeError) as exc:
        raise ApiError("参数错误") from exc


def get_dialog(connection, dialog_id, userid=None, *, lock=False):
    dialogs = table("web_socket_dialogs")
    stmt = select(dialogs).where(dialogs.c.id == int(dialog_id), dialogs.c.deleted_at.is_(None))
    row = connection.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if not row:
        raise ApiError("会话不存在")
    if userid is not None:
        if not can_view_dialog(connection, userid, row):
            raise ApiError("您不在此会话内")
    return dict(row)


def can_view_dialog(connection, userid, dialog):
    if dialog["deleted_at"]:
        return False
    if dialog["group_type"] in ("project", "task"):
        from .project_core import can_view_project, can_view_task
        resource = table("projects" if dialog["group_type"] == "project" else "project_tasks")
        row = connection.execute(select(resource).where(resource.c.dialog_id == dialog["id"], resource.c.deleted_at.is_(None)).limit(1)).mappings().first()
        if row:
            checker = can_view_project if dialog["group_type"] == "project" else can_view_task
            return checker(connection, userid, dict(row))
        return False
    members = table("web_socket_dialog_users")
    return connection.execute(select(members.c.id).where(members.c.dialog_id == dialog["id"], members.c.userid == userid).limit(1)).first() is not None


def member_ids(connection, dialog_id):
    members = table("web_socket_dialog_users")
    return list(connection.execute(select(members.c.userid).where(members.c.dialog_id == dialog_id)).scalars())


def create_group(connection, *, owner_id, userids, name="", group_type="", link_id=0):
    users = table("users")
    wanted = sorted(set(ids(userids) + [int(owner_id)]))
    found = connection.execute(select(users.c.userid).where(users.c.userid.in_(wanted),
        users.c.disable_at.is_(None)).order_by(users.c.userid).with_for_update()).scalars().all()
    if found != wanted:
        raise ApiError("成员不存在或已停用")
    dialog_id = db.insert_row(connection, "web_socket_dialogs", type="group", group_type=group_type,
        owner_id=owner_id, link_id=link_id, name=str(name)[:255])
    for userid in wanted:
        db.insert_row(connection, "web_socket_dialog_users", dialog_id=dialog_id, userid=userid,
            role=1 if userid == owner_id else 0, inviter=owner_id,
            important=int(group_type in ("project", "task", "department")))
    return dialog_id


def sync_members(connection, dialog_id, userids, *, owner_id=None, important=True):
    dialog = get_dialog(connection, dialog_id, lock=True)
    members = table("web_socket_dialog_users")
    current = member_ids(connection, dialog_id)
    wanted = ids(userids)
    users = table("users")
    valid = connection.execute(select(users.c.userid).where(users.c.userid.in_(wanted), users.c.disable_at.is_(None))).scalars().all()
    if set(valid) != set(wanted):
        raise ApiError("成员不存在或已停用")
    removed = sorted(set(current) - set(wanted))
    if removed:
        # These users were captured under the dialog lock before access revocation.
        jobs.enqueue_push(connection, userids=removed, message={"type": "dialog", "mode": "groupExit", "data": {"id": dialog_id}})
        from .project_core import deleted_record
        deleted_record(connection, "dialog", dialog_id, removed)
        connection.execute(members.delete().where(members.c.dialog_id == dialog_id, members.c.userid.in_(removed)))
    for userid in set(wanted) - set(current):
        db.insert_row(connection, "web_socket_dialog_users", dialog_id=dialog_id, userid=userid,
            inviter=owner_id or dialog["owner_id"], important=int(important))
        deleteds = table("deleteds")
        connection.execute(deleteds.delete().where(deleteds.c.type == "dialog", deleteds.c.did == dialog_id, deleteds.c.userid == userid))
    if owner_id is not None:
        if owner_id not in wanted:
            raise ApiError("群主必须是会话成员")
        connection.execute(members.update().where(members.c.dialog_id == dialog_id, members.c.role == 1).values(role=0))
        connection.execute(members.update().where(members.c.dialog_id == dialog_id, members.c.userid == owner_id).values(role=1))
        connection.execute(table("web_socket_dialogs").update().where(table("web_socket_dialogs").c.id == dialog_id)
                           .values(owner_id=owner_id, updated_at=utcnow()))
    if wanted:
        jobs.enqueue_push(connection, userids=wanted, message={"type": "dialog", "mode": "groupUpdate", "data": {"id": dialog_id, "people": len(wanted)}}, scope={"dialog_id": dialog_id})
        joined = sorted(set(wanted) - set(current))
        if joined:
            jobs.enqueue_push(connection, userids=joined, message={"type": "dialog", "mode": "groupJoin", "data": {"id": dialog_id}}, scope={"dialog_id": dialog_id})
    if set(current) != set(wanted):
        jobs.enqueue(connection, 'search.dialog_members', {'dialog_id': dialog_id})
    return wanted


def ensure_user_dialog(connection, userid, other_userid):
    users = table("users")
    wanted = sorted(set([int(userid), int(other_userid)]))
    found = connection.execute(select(users.c.userid).where(users.c.userid.in_(wanted),
        users.c.disable_at.is_(None)).order_by(users.c.userid).with_for_update()).scalars().all()
    if wanted != found:
        raise ApiError("会员不存在或已停用")
    members, dialogs = table("web_socket_dialog_users"), table("web_socket_dialogs")
    own = select(members.c.dialog_id).where(members.c.userid == userid)
    other = select(members.c.dialog_id).where(members.c.userid == other_userid)
    candidates = connection.execute(select(dialogs).where(dialogs.c.type == "user", dialogs.c.deleted_at.is_(None),
        dialogs.c.id.in_(own), dialogs.c.id.in_(other))).mappings().all()
    for row in candidates:
        if set(member_ids(connection, row["id"])) == set(wanted):
            return row["id"]
    dialog_id = db.insert_row(connection, "web_socket_dialogs", type="user")
    for uid in wanted:
        db.insert_row(connection, "web_socket_dialog_users", dialog_id=dialog_id, userid=uid)
    return dialog_id


def message_data(row):
    result = dict(row)
    result["msg"] = decode(result.get("msg"))
    result["emoji"] = decode(result.get("emoji"), [])
    result["percentage"] = 100 if not result.get("send") else min(100, round(100 * result.get("read", 0) / result["send"]))
    return result


def get_message(connection, msg_id, userid=None, *, lock=False):
    messages = table("web_socket_dialog_msgs")
    stmt = select(messages).where(messages.c.id == int(msg_id), messages.c.deleted_at.is_(None))
    row = connection.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if not row:
        raise ApiError("消息不存在或已被删除")
    get_dialog(connection, row["dialog_id"], userid)
    return message_data(row)


def _attachments(connection, row):
    attachments = table("web_socket_dialog_msg_attachments")
    connection.execute(attachments.delete().where(attachments.c.msg_id == row["id"]))
    payload = row["msg"]
    files = []
    if row["type"] in ("file", "record"):
        files.append(("file_message", payload))
    elif row["type"] == "longtext" and payload.get("file"):
        files.append(("file_message", payload["file"]))
    if row["type"] == "text":
        for path in re.findall(r'<img\b[^>]*\bsrc=[\"\']([^\"\']+)', payload.get("text", ""), re.I):
            if path.startswith(("uploads/", "/uploads/", "/api/file/bytes?key=")):
                files.append(("inline_image", {"path": path, "name": path.rsplit("/", 1)[-1]}))
    for position, (source, item) in enumerate(files):
        ext = item.get("ext") or item.get("name", "").rsplit(".", 1)[-1].lower()
        db.insert_row(connection, "web_socket_dialog_msg_attachments", msg_id=row["id"], dialog_id=row["dialog_id"],
            source_type=source, kind="image" if ext in ("png", "jpg", "jpeg", "webp", "gif") else "file",
            position=position, name=str(item.get("name", ""))[:255], ext=ext[:20], path=item.get("path", ""),
            thumb=item.get("thumb", ""), size=max(0, int(item.get("size", 0))),
            width=max(0, int(item.get("width", 0))), height=max(0, int(item.get("height", 0))))


def scope_assets(connection, message, dialog_id, userid):
    from secrets import token_hex
    grants = table("backend_file_downloads")
    replacements = {}
    def replace(match):
        key = match.group(1)
        if key in replacements:
            return replacements[key]
        row = connection.execute(select(grants).where(grants.c.id == key, grants.c.ready == 1, grants.c.expires_at > utcnow())).mappings().first()
        if not row:
            raise ApiError("附件已失效")
        if row["userid"] != userid:
            if not row["dialog_id"]:
                raise ApiError("无权限发送此附件")
            get_dialog(connection, row["dialog_id"], userid)
        if row["dialog_id"] == dialog_id:
            return match[0]
        new_key = token_hex(32)
        db.insert_row(connection, "backend_file_downloads", id=new_key, userid=userid, dialog_id=dialog_id,
            file_id=row["file_id"], content_id=row["content_id"], file_ids=row["file_ids"], path=row["path"],
            name=row["name"], ready=1, expires_at=row["expires_at"])
        replacements[key] = "/api/file/bytes?key=" + new_key
        return replacements[key]
    def visit(value):
        if isinstance(value, dict):
            return {key: visit(child) for key, child in value.items()}
        if isinstance(value, list):
            return [visit(child) for child in value]
        if isinstance(value, str):
            return re.sub(r"/api/file/bytes\?key=([a-fA-F0-9]{64})", replace, value)
        return value
    return visit(message)


def send_message(connection, *, dialog_id, sender_id, msg_type, msg, context=None,
                 reply_id=0, update_id=0, forward_id=0, silence=None, search_key=None, trusted_sender=False):
    dialog = get_dialog(connection, dialog_id, sender_id if sender_id > 0 and not trusted_sender else None, lock=True)
    if sender_id > 0 and not trusted_sender and dialog["group_type"] in ("project", "task"):
        resource = table("projects" if dialog["group_type"] == "project" else "project_tasks")
        relation = connection.execute(select(resource).where(resource.c.dialog_id == dialog_id)).mappings().first()
        project_id = relation["id"] if dialog["group_type"] == "project" else relation["project_id"]
        members = table("project_users")
        if not connection.execute(select(members.c.id).where(members.c.project_id == project_id, members.c.userid == sender_id)).first():
            raise ApiError("部门只读视角不能发送消息")
    system = db.setting(connection, "system") or {}
    if sender_id > 0:
        users = table("users")
        sender = connection.execute(select(users).where(users.c.userid == sender_id)).mappings().one()
        if not sender["bot"]:
            if system.get("chat_information") == "required":
                if not sender["nickname"]:
                    raise ApiError("请设置昵称", -2)
                if not sender["tel"]:
                    raise ApiError("请设置联系电话", -3)
            mute_key = "user_private_chat_mute" if dialog["type"] == "user" else "user_group_chat_mute" if dialog["group_type"] == "user" else "all_group_mute" if dialog["group_type"] == "all" else ""
            if mute_key and system.get(mute_key) == "close" and "admin" not in str(sender["identity"]).split(","):
                raise ApiError("当前会话禁言")
    asset_owner = context.actor.userid if context and context.actor else sender_id
    if asset_owner > 0:
        msg = scope_assets(connection, msg, dialog_id, asset_owner)
    messages = table("web_socket_dialog_msgs")
    text = msg.get("text", msg.get("title", msg.get("notice", "")))
    mtype = "image" if msg_type == "text" and "<img" in text or msg_type == "file" and msg.get("ext") in ("png", "jpg", "jpeg", "gif", "webp") else msg_type
    key = search_key or html.unescape(re.sub("<[^>]+>", "", text))
    now = utcnow()
    if msg_type == "text" and dialog["type"] == "group" and dialog["owner_id"] in (0, sender_id):
        mentioned = {int(value) for value in re.findall(r'<span\s+class="mention user"\s+data-id="(\d+)"', text) if int(value) > 0}
        current = set(member_ids(connection, dialog_id))
        extra = mentioned - current
        if dialog["group_type"] in ("task", "project"):
            extra = {userid for userid in extra if can_view_dialog(connection, userid, dialog)}
        if extra:
            sync_members(connection, dialog_id, current | extra, important=False)
            jobs.enqueue(connection, "dialog.member_hook", {"dialog_id": dialog_id, "userid": sender_id, "userids": sorted(extra), "event": "member_join"})
    if update_id:
        old = get_message(connection, update_id, sender_id if sender_id > 0 and not trusted_sender else None, lock=True)
        if old["dialog_id"] != dialog_id or old["userid"] != sender_id or old["type"] not in ("text", "template", "longtext", "vote", "wordchain"):
            raise ApiError("仅支持修改自己的消息")
        limit = int(system.get("msg_edit_limit", 0) or 0)
        if limit > 0 and sender_id > 0 and not sender["bot"] and len(member_ids(connection, dialog_id)) > 1 and old["created_at"] + timedelta(minutes=limit) < now:
            raise ApiError("此消息已超过修改时间限制")
        values = dict(msg=encode({**old["msg"], **msg}), modify=1, key=key, updated_at=now)
        connection.execute(messages.update().where(messages.c.id == update_id).values(**values))
        row = get_message(connection, update_id)
        _attachments(connection, row)
        push_dialog(connection, dialog_id, "update", row)
    else:
        if reply_id:
            reply = get_message(connection, reply_id, sender_id if sender_id > 0 and not trusted_sender else None)
            if reply["dialog_id"] != dialog_id:
                raise ApiError("回复的消息不存在")
            msg = {**msg, "reply_data": {k: reply[k] for k in ("id", "userid", "type", "msg")}}
            connection.execute(messages.update().where(messages.c.id == reply_id).values(reply_num=messages.c.reply_num + 1))
        users = table("users")
        bot = connection.execute(select(users.c.bot).where(users.c.userid == sender_id)).scalar_one_or_none() or 0
        msg_id = db.insert_row(connection, "web_socket_dialog_msgs", dialog_id=dialog_id, dialog_type=dialog["type"],
            session_id=dialog["session_id"], userid=sender_id, type=msg_type, mtype=mtype, msg=encode(msg),
            key=key, link=int(bool(re.search(r"https?://|<a\s", text))), reply_id=reply_id, forward_id=forward_id, bot=bot)
        row = get_message(connection, msg_id)
        _attachments(connection, row)
        members = table("web_socket_dialog_users")
        connection.execute(members.update().where(members.c.dialog_id == dialog_id).values(hide=0, last_at=now, updated_at=now))
        if forward_id:
            connection.execute(messages.update().where(messages.c.id == forward_id).values(forward_num=messages.c.forward_num + 1))
        if msg_type == "meeting":
            db.insert_row(connection, "meeting_msgs", meetingid=msg["meetingid"], dialog_id=dialog_id, msg_id=msg_id)
        if dialog["session_id"]:
            sessions = table("web_socket_dialog_sessions")
            connection.execute(sessions.update().where(sessions.c.id == dialog["session_id"], sessions.c.title == "").values(title=key[:255], updated_at=now))
        # Reads are durable in the caller's transaction, never fabricated by WS delivery.
        recipients = connection.execute(select(members).where(members.c.dialog_id == dialog_id)).mappings().all()
        mentions = set(int(v) for v in re.findall(r'data-id=[\"\'](\d+)', text))
        reads = table("web_socket_dialog_msg_reads")
        row["send"] = sum(recipient["userid"] != sender_id for recipient in recipients)
        for recipient in recipients:
            uid = recipient["userid"]
            mentioned = int(uid in mentions or 0 in mentions)
            quiet = int(not mentioned and (recipient["silence"] or (silence if silence is not None else msg_type not in ("text", "file", "record", "meeting"))))
            if uid != sender_id:
                statement = insert(reads).values(dialog_id=dialog_id, msg_id=msg_id, userid=uid,
                    mention=mentioned, silence=quiet, dot=int(msg_type == "record"))
                connection.execute(statement.on_duplicate_key_update(id=reads.c.id))
            jobs.enqueue_push(connection, userids=[uid], ignore_fds=[context.fd] if context and context.fd else (),
                message={"type": "dialog", "mode": "add", "silence": quiet,
                         "data": {**row, "mention": mentioned, "dot": int(msg_type == "record"), "user_at": now, "user_ms": int(now.timestamp() * 1000)}}, scope={"dialog_id": dialog_id})
        send_count = connection.execute(select(func.count()).select_from(reads).where(reads.c.msg_id == msg_id)).scalar_one()
        connection.execute(messages.update().where(messages.c.id == msg_id).values(send=send_count))
        row["send"] = send_count
        jobs.enqueue(connection, "dialog.message_effects", {"msg_id": msg_id, "sender_id": sender_id}, dedupe_key=f"dialog-effects:{msg_id}")
    from .project_tasks import sync_message_relations
    sync_message_relations(connection, row)
    jobs.enqueue(connection, "search.sync", {"type": "message", "id": row["id"]})
    return row


def send_system_message(connection, dialog_id, text, *, sender_id=0):
    return send_message(connection, dialog_id=dialog_id, sender_id=sender_id, msg_type="notice", msg={"notice": text, "source": "system"}, silence=True)


def push_dialog(connection, dialog_id, mode, data, *, ignore_fds=()):
    users = member_ids(connection, dialog_id)
    if users:
        jobs.enqueue_push(connection, userids=users, ignore_fds=ignore_fds,
            message={"type": "dialog", "mode": mode, "data": data}, scope={"dialog_id": dialog_id})


def unread(connection, dialog_id, userid):
    reads = table("web_socket_dialog_msg_reads")
    rows = connection.execute(select(reads).where(reads.c.dialog_id == dialog_id,
        reads.c.userid == userid, reads.c.read_at.is_(None)).order_by(reads.c.msg_id)).mappings().all()
    return {"id": dialog_id, "unread": len(rows), "unread_one": rows[0]["msg_id"] if rows else 0,
            "mention": sum(bool(r["mention"]) for r in rows), "mention_ids": [r["msg_id"] for r in rows if r["mention"]]}


def dialog_data(connection, dialog_id, userid):
    dialog = get_dialog(connection, dialog_id, userid)
    members, messages, todos = table("web_socket_dialog_users"), table("web_socket_dialog_msgs"), table("web_socket_dialog_msg_todos")
    member = connection.execute(select(members).where(members.c.dialog_id == dialog_id, members.c.userid == userid)).mappings().first()
    if member is None:
        member = {"top_at": None, "last_at": None, "mark_unread": 0, "silence": 0, "hide": 0, "color": "", "updated_at": dialog["updated_at"]}
    result = {**dialog, **{key: member[key] for key in ("top_at", "last_at", "mark_unread", "silence", "hide", "color")}, **unread(connection, dialog_id, userid)}
    result["user_at"] = member["updated_at"]
    result["user_ms"] = int(member["updated_at"].timestamp() * 1000) if member["updated_at"] else 0
    result["todo_num"] = connection.execute(select(func.count()).select_from(todos).where(todos.c.dialog_id == dialog_id, todos.c.userid == userid, todos.c.done_at.is_(None))).scalar_one()
    last = connection.execute(select(messages).where(messages.c.dialog_id == dialog_id, messages.c.deleted_at.is_(None)).order_by(messages.c.id.desc()).limit(1)).mappings().first()
    from .account_history import preview_message
    last_data = message_data(last) if last else None
    result["last_msg"] = ({**{key: last_data[key] for key in ("id", "userid", "percentage", "created_at")},
        "type": "preview", "msg": {"preview": preview_message(last_data)}, "emoji": [{"symbol": e["symbol"]} for e in last_data["emoji"]]} if last_data else None)
    users = table("users")
    people = connection.execute(select(users.c.bot, func.count()).join(members, members.c.userid == users.c.userid)
        .where(members.c.dialog_id == dialog_id, users.c.disable_at.is_(None)).group_by(users.c.bot)).all()
    counts = dict(people)
    result.update(people=sum(counts.values()), people_user=counts.get(0, 0), people_bot=counts.get(1, 0),
        deputy_ids=list(connection.execute(select(members.c.userid).where(members.c.dialog_id == dialog_id, members.c.role == 2)).scalars()),
        quick_msgs=[], dialog_user=None, group_info=None, bot=0)
    for kind, predicate in (("tag", messages.c.tag > 0), ("todo", messages.c.todo > 0), ("image", messages.c.mtype == "image"), ("file", messages.c.mtype == "file"), ("link", messages.c.link == 1)):
        result["has_" + kind] = connection.execute(select(messages.c.id).where(messages.c.dialog_id == dialog_id, messages.c.deleted_at.is_(None), predicate).limit(1)).first() is not None
    if dialog["type"] == "user":
        users = table("users")
        other = next((u for u in member_ids(connection, dialog_id) if u != userid), userid)
        row = connection.execute(select(users.c.userid, users.c.nickname, users.c.email, users.c.userimg, users.c.bot).where(users.c.userid == other)).mappings().first()
        result["dialog_user"] = dict(row) if row else None
        result["name"] = row["nickname"] if row else ""
        if row:
            result.update(email=row["email"], userimg=row["userimg"], bot=row["bot"], is_disable=0)
        result["dialog_mute"] = db.setting(connection, "system", "user_private_chat_mute")
    elif dialog["group_type"] in ("task", "project"):
        resource = table("project_tasks" if dialog["group_type"] == "task" else "projects")
        columns = [resource.c.id, resource.c.name, resource.c.archived_at, resource.c.deleted_at]
        if dialog["group_type"] == "task":
            columns.append(resource.c.complete_at)
        group = connection.execute(select(*columns).where(resource.c.dialog_id == dialog_id).limit(1)).mappings().first()
        result["group_info"] = dict(group) if group else None
        result["name"] = group["name"] if group else "[Delete]"
    else:
        result["dialog_mute"] = db.setting(connection, "system", "all_group_mute" if dialog["group_type"] == "all" else "user_group_chat_mute")
    return result
