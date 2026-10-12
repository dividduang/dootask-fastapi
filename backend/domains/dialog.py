"""Explicit source dialog API handlers. Every handler returns data, not an envelope."""
import base64
from datetime import datetime, timedelta
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
import redis
from PIL import Image
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select, func, and_, or_
from .. import auth, db, jobs
from ..context import utcnow
from ..errors import ApiError
from ..storage import atomic_write, confined_path
from . import dialog_core as core
from .dialog_core import table, ids, encode, decode, get_dialog, get_message, message_data, dialog_data, member_ids, send_message, push_dialog
from .apps import on


def _actor(connection, context):
    return auth.authenticate(connection, context)


def _dialog(connection, context, data, *, lock=False):
    actor = _actor(connection, context)
    return actor, get_dialog(connection, int(data.get("dialog_id", 0)), actor.userid, lock=lock)


def _message(connection, context, data, *, lock=False):
    actor = _actor(connection, context)
    return actor, get_message(connection, int(data.get("msg_id", data.get("id", 0))), actor.userid, lock=lock)


def _date(value):
    if value in (None, "", False):
        return None
    try:
        if str(value).isdigit():
            number = int(value)
            return datetime.utcfromtimestamp(number / 1000 if number > 100000000000 else number)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, OverflowError) as exc:
        raise ApiError("时间格式错误") from exc


def _page(connection, statement, data, *, default=20, transform=dict):
    size = max(1, min(100, int(data.get("pagesize", data.get("take", default)))))
    page = max(1, int(data.get("page", 1)))
    total = connection.execute(select(func.count()).select_from(statement.order_by(None).subquery())).scalar_one()
    rows = connection.execute(statement.limit(size).offset((page - 1) * size)).mappings()
    return {"data": [transform(row) for row in rows], "total": total, "current_page": page,
        "per_page": size, "last_page": max(1, math.ceil(total / size)), "from": (page - 1) * size + 1 if total else None,
        "to": min(page * size, total) if total else None}


def _member(connection, dialog_id, userid):
    members = table("web_socket_dialog_users")
    row = connection.execute(select(members).where(members.c.dialog_id == dialog_id, members.c.userid == userid).with_for_update()).mappings().first()
    if not row:
        raise ApiError("会话不存在")
    return row


def _set_member(connection, dialog_id, userid, **values):
    members = table("web_socket_dialog_users")
    connection.execute(members.update().where(members.c.dialog_id == dialog_id, members.c.userid == userid).values(**values, updated_at=utcnow()))
    return {"id": dialog_id, **values}


def _group_owner(connection, actor, dialog, *, primary=False, personal=False):
    if dialog["type"] != "group" or personal and dialog["group_type"] != "user":
        raise ApiError("此群组不允许操作")
    role = _member(connection, dialog["id"], actor.userid)["role"]
    if role not in ((1,) if primary else (1, 2)):
        raise ApiError("仅群主可操作" if primary else "仅群主或群管理员可操作")


def lists(connection, context, data, settings):
    actor = _actor(connection, context)
    dialogs, members = table("web_socket_dialogs"), table("web_socket_dialog_users")
    query = select(dialogs.c.id).join(members, members.c.dialog_id == dialogs.c.id).where(members.c.userid == actor.userid, dialogs.c.deleted_at.is_(None))
    if data.get("updated") or data.get("updated_at"):
        query = query.where(members.c.updated_at > _date(data.get("updated", data.get("updated_at"))))
    result = _page(connection, query.order_by(members.c.top_at.desc(), members.c.last_at.desc()), data, default=50,
                   transform=lambda row: dialog_data(connection, row["id"], actor.userid))
    if result["current_page"] == 1:
        deleted = table("deleteds")
        query = select(deleted.c.did).where(deleted.c.type == "dialog", deleted.c.userid == actor.userid)
        if data.get("deleted"):
            query = query.where(deleted.c.created_at > _date(data["deleted"]))
        result["deleted_id"] = list(connection.execute(query).scalars())
    return result


def beyond(connection, context, data, settings):
    actor = _actor(connection, context)
    members, reads, todos = table("web_socket_dialog_users"), table("web_socket_dialog_msg_reads"), table("web_socket_dialog_msg_todos")
    found = set()
    if data.get("unread_at"):
        query = select(members.c.dialog_id).where(members.c.userid == actor.userid, members.c.last_at < _date(data["unread_at"]),
            or_(members.c.mark_unread == 1, members.c.dialog_id.in_(select(reads.c.dialog_id).where(reads.c.userid == actor.userid, reads.c.read_at.is_(None)))))
        found.update(connection.execute(query.limit(40)).scalars())
    if data.get("todo_at"):
        query = select(members.c.dialog_id).where(members.c.userid == actor.userid, members.c.last_at < _date(data["todo_at"]),
            members.c.dialog_id.in_(select(todos.c.dialog_id).where(todos.c.userid == actor.userid, todos.c.done_at.is_(None))))
        found.update(connection.execute(query.limit(20)).scalars())
    return [dialog_data(connection, item, actor.userid) for item in sorted(found)]


def one(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    return dialog_data(connection, dialog["id"], actor.userid)


def search(connection, context, data, settings):
    actor = _actor(connection, context)
    key = str(data.get("key", "")).strip()
    if not key:
        raise ApiError("请输入搜索关键词")
    dialogs, members = table("web_socket_dialogs"), table("web_socket_dialog_users")
    dialog_ids = connection.execute(select(dialogs.c.id).join(members, members.c.dialog_id == dialogs.c.id).where(
        members.c.userid == actor.userid, dialogs.c.deleted_at.is_(None), dialogs.c.name.contains(key)).limit(20)).scalars().all()
    result = [dialog_data(connection, id, actor.userid) for id in dialog_ids]
    users = table("users")
    for row in connection.execute(select(users.c.userid, users.c.nickname, users.c.email, users.c.userimg, users.c.bot)
            .where(users.c.disable_at.is_(None), or_(users.c.nickname.contains(key), users.c.email.contains(key))).limit(20 - len(result))).mappings():
        result.append({"id": "u:" + str(row["userid"]), "type": "user", "name": row["nickname"], "dialog_user": dict(row), "last_at": None, "last_msg": None})
    if "dialog_only" not in data and len(result) < 20:
        messages = table("web_socket_dialog_msgs")
        hits = connection.execute(select(messages.c.dialog_id).join(members, members.c.dialog_id == messages.c.dialog_id)
            .where(members.c.userid == actor.userid, messages.c.deleted_at.is_(None), messages.c.key.contains(key),
                messages.c.dialog_id.not_in(dialog_ids)).distinct().limit(20 - len(result))).scalars()
        result.extend(dialog_data(connection, id, actor.userid) for id in hits)
    return result


def search__tag(connection, context, data, settings):
    actor = _actor(connection, context)
    messages, members = table("web_socket_dialog_msgs"), table("web_socket_dialog_users")
    rows = connection.execute(select(messages.c.id, messages.c.dialog_id).join(members, members.c.dialog_id == messages.c.dialog_id)
        .where(members.c.userid == actor.userid, messages.c.tag > 0, messages.c.deleted_at.is_(None)).order_by(messages.c.id.desc()).limit(50)).mappings()
    return [{**dialog_data(connection, r["dialog_id"], actor.userid), "search_msg_id": r["id"]} for r in rows]


def user(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    members, users = table("web_socket_dialog_users"), table("users")
    cols = [members, users.c.nickname, users.c.email, users.c.userimg] if int(data.get("getuser", 0)) else [members]
    rows = [dict(row) for row in connection.execute(select(*cols).join(users, users.c.userid == members.c.userid)
        .where(members.c.dialog_id == dialog["id"], users.c.disable_at.is_(None)).order_by(members.c.id)).mappings()]
    if int(data.get("getuser", 0)):
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
        try:
            for row in rows:
                row["online"] = bool(row["bot"]) or any(client.exists(f"dootask:connection:{fd}") for fd in client.smembers(f"dootask:user:{row['userid']}"))
        finally:
            client.close()
    return rows


def todo(connection, context, data, settings):
    actor = _actor(connection, context)
    todos = table("web_socket_dialog_msg_todos")
    query = select(todos).where(todos.c.userid == actor.userid, todos.c.done_at.is_(None))
    if int(data.get("dialog_id", 0)):
        get_dialog(connection, int(data["dialog_id"]), actor.userid)
        query = query.where(todos.c.dialog_id == int(data["dialog_id"]))
    return [dict(row) for row in connection.execute(query.order_by(todos.c.id.desc()).limit(50)).mappings()]


def top(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    member = _member(connection, dialog["id"], actor.userid)
    return _set_member(connection, dialog["id"], actor.userid, top_at=None if member["top_at"] else utcnow())


def hide(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    if _member(connection, dialog["id"], actor.userid)["top_at"]:
        raise ApiError("置顶会话无法隐藏")
    return _set_member(connection, dialog["id"], actor.userid, hide=1)


def tel(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    if dialog["type"] != "user":
        raise ApiError("仅支持个人会话")
    users = table("users")
    others = [id for id in member_ids(connection, dialog["id"]) if id != actor.userid]
    recipient = connection.execute(select(users.c.tel, users.c.nickname).where(users.c.userid.in_(others)).limit(1)).mappings().first()
    if not recipient:
        raise ApiError("会话对象不存在")
    if not recipient["tel"]:
        raise ApiError("对方未设置联系电话")
    if "temp" in actor.identities:
        raise ApiError("无法查看联系电话")
    added = send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="notice",
        msg={"notice": actor.user["nickname"] + " 查看了 " + recipient["nickname"] + " 的联系电话"})
    return {"tel": recipient["tel"], "add": added}


def open__user(connection, context, data, settings):
    actor = _actor(connection, context)
    dialog_id = core.ensure_user_dialog(connection, actor.userid, int(data.get("userid", 0)))
    return dialog_data(connection, dialog_id, actor.userid)


def open__event(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    jobs.enqueue(connection, "dialog.member_hook", {"dialog_id": dialog["id"], "userid": actor.userid, "event": "dialog_open"},
        dedupe_key=f"dialog-open:{dialog['id']}:{actor.userid}:{int(utcnow().timestamp()) // 60}")
    return []


def msg__one(connection, context, data, settings):
    return _message(connection, context, data)[1]


def msg__list(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    messages, reads, todos = table("web_socket_dialog_msgs"), table("web_socket_dialog_msg_reads"), table("web_socket_dialog_msg_todos")
    query = select(messages, reads.c.mention, reads.c.dot, reads.c.read_at).outerjoin(reads,
        and_(reads.c.msg_id == messages.c.id, reads.c.userid == actor.userid)).where(messages.c.dialog_id == dialog["id"], messages.c.deleted_at.is_(None))
    if dialog["session_id"]:
        query = query.where(messages.c.session_id == dialog["session_id"])
    kind = data.get("msg_type")
    if kind in ("tag", "todo", "link"):
        query = query.where(messages.c[kind] > 0)
    elif kind in ("text", "image", "file", "record", "meeting"):
        query = query.where(messages.c.mtype == kind)
    elif kind:
        raise ApiError("参数错误")
    if int(data.get("msg_id", 0)):
        query = query.where(messages.c.reply_id == int(data["msg_id"]))
    take = max(1, min(100, int(data.get("take", 50))))
    prev, next_id = int(data.get("prev_id", 0)), int(data.get("next_id", 0))
    if int(data.get("position_id", 0)):
        upper = connection.execute(query.where(messages.c.id >= int(data["position_id"])).order_by(messages.c.id).limit(max(1, take // 2))).mappings().all()
        prev = upper[-1]["id"] if upper else int(data["position_id"])
    selected = query.where(messages.c.id <= prev).order_by(messages.c.id.desc()) if prev else query.where(messages.c.id >= next_id).order_by(messages.c.id) if next_id else query.order_by(messages.c.id.desc())
    rows = sorted([message_data(row) for row in connection.execute(selected.limit(take)).mappings()], key=lambda row: row["id"], reverse=True)
    for row in rows:
        row.update(next_id=0, prev_id=0)
        row["todo_done"] = int(not connection.execute(select(todos.c.id).where(todos.c.msg_id == row["id"], todos.c.done_at.is_(None)).limit(1)).first())
    if rows:
        rows[0]["next_id"] = connection.execute(query.with_only_columns(messages.c.id).where(messages.c.id > rows[0]["id"]).order_by(messages.c.id).limit(1)).scalar_one_or_none() or 0
        rows[-1]["prev_id"] = connection.execute(query.with_only_columns(messages.c.id).where(messages.c.id < rows[-1]["id"]).order_by(messages.c.id.desc()).limit(1)).scalar_one_or_none() or 0
    _set_member(connection, dialog["id"], actor.userid, mark_unread=0)
    result = {"list": rows, "time": int(utcnow().timestamp() * 1000)}
    if not any((kind, data.get("msg_id"), prev, next_id)):
        result.update(dialog=dialog_data(connection, dialog["id"], actor.userid), todo=todo(connection, context, data, settings),
            top=get_message(connection, dialog["top_msg_id"], actor.userid) if dialog["top_msg_id"] else None)
    if dialog["group_type"] == "task":
        users = table("users")
        connection.execute(users.update().where(users.c.userid == actor.userid).values(task_dialog_id=dialog["id"]))
    return result


def msg__latest(connection, context, data, settings):
    actor = _actor(connection, context)
    if not isinstance(data.get("dialogs"), list) or not data["dialogs"]:
        raise ApiError("参数错误")
    messages = table("web_socket_dialog_msgs")
    result = []
    for item in data["dialogs"][:5]:
        get_dialog(connection, int(item["id"]), actor.userid)
        rows = connection.execute(select(messages).where(messages.c.dialog_id == int(item["id"]), messages.c.id > int(item.get("latest_id", 0)),
            messages.c.deleted_at.is_(None)).order_by(messages.c.id.desc()).limit(max(1, min(50, int(data.get("take", 25)))))).mappings()
        result.extend(message_data(row) for row in rows)
    return {"data": result}


def msg__dot(connection, context, data, settings):
    actor, msg = _message(connection, context, data)
    reads = table("web_socket_dialog_msg_reads")
    connection.execute(reads.update().where(reads.c.msg_id == msg["id"], reads.c.userid == actor.userid).values(dot=0))
    return {"id": msg["id"], "dot": 0}


def _read_ids(connection, userid, msg_ids):
    reads, messages = table("web_socket_dialog_msg_reads"), table("web_socket_dialog_msgs")
    connection.execute(reads.update().where(reads.c.userid == userid, reads.c.msg_id.in_(msg_ids), reads.c.read_at.is_(None)).values(read_at=utcnow()))
    for msg_id in msg_ids:
        msg = get_message(connection, msg_id, userid)
        count = connection.execute(select(func.count()).select_from(reads).where(reads.c.msg_id == msg_id, reads.c.read_at.is_not(None))).scalar_one()
        connection.execute(messages.update().where(messages.c.id == msg_id).values(read=count))
        if msg["userid"] > 0:
            jobs.enqueue_push(connection, userids=[msg["userid"]], message={"type": "dialog", "mode": "readed", "data": {
                "id": msg_id, "read": count, "percentage": min(100, round(100 * count / msg["send"])) if msg["send"] else 100}}, scope={"dialog_id": msg["dialog_id"]})


def msg__read(connection, context, data, settings):
    actor = _actor(connection, context)
    values = data.get("id", "")
    mapping = values if isinstance(values, dict) else {id: 0 for id in ids(values)}
    found, msgids = set(), set()
    reads = table("web_socket_dialog_msg_reads")
    for id, dialog_id in mapping.items():
        msg = get_message(connection, int(id), actor.userid)
        msgids.add(msg["id"])
        found.add(msg["dialog_id"])
        if int(dialog_id or 0) == msg["dialog_id"]:
            msgids.update(connection.execute(select(reads.c.msg_id).where(reads.c.dialog_id == msg["dialog_id"], reads.c.userid == actor.userid,
                reads.c.msg_id >= msg["id"], reads.c.read_at.is_(None))).scalars())
    _read_ids(connection, actor.userid, msgids)
    return [msg__unread(connection, context, {"dialog_id": id}, settings) for id in sorted(found)]


def msg__unread(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    member = _member(connection, dialog["id"], actor.userid)
    return {**core.unread(connection, dialog["id"], actor.userid), "user_at": member["updated_at"],
            "user_ms": int(member["updated_at"].timestamp() * 1000) if member["updated_at"] else 0}


def msg__checked(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    if msg["userid"] != actor.userid or msg["type"] != "text":
        raise ApiError("仅支持修改自己的文本消息")
    index = int(data.get("index", 0))
    count = -1
    def change(match):
        nonlocal count
        count += 1
        return '<li data-list="' + ("checked" if on(data.get("checked")) else "unchecked") + '">' if count == index else match[0]
    payload = {**msg["msg"], "text": re.sub(r"<li[^>]*>", change, msg["msg"].get("text", ""), flags=re.I)}
    result = send_message(connection, dialog_id=msg["dialog_id"], sender_id=actor.userid, msg_type="text", msg=payload, update_id=msg["id"], context=context)
    return {"id": result["id"], "msg": result["msg"]}


def msg__stream(connection, context, data, settings):
    actor = _actor(connection, context)
    userid = int(data.get("userid", 0))
    if userid != actor.userid and not actor.user.get("bot"):
        auth.require_admin(actor)
    stream_url = str(data.get("stream_url", ""))
    if not stream_url.startswith("/ai/") or ".." in stream_url or "\\" in stream_url:
        raise ApiError("无效的流地址")
    jobs.enqueue_push(connection, userids=[userid], message={"type": "msgStream", "stream_url": stream_url})
    return []


def _targets(connection, actor, data):
    targets = ids(data.get("dialogids", data.get("dialog_ids", data.get("dialog_id"))))
    for userid in ids(data.get("userids", data.get("user_ids"))):
        targets.append(core.ensure_user_dialog(connection, actor.userid, userid))
    targets = sorted(set(targets))
    if not targets:
        raise ApiError("请选择对话或成员")
    for dialog_id in targets:
        get_dialog(connection, dialog_id, actor.userid)
    return targets


def _text(value, maximum=200000):
    text = str(value or "").strip()
    if not text:
        raise ApiError("消息内容不能为空")
    if len(re.sub(r"<img[^>]*>", "", text, flags=re.I)) > maximum:
        raise ApiError(f"消息内容最大不能超过{maximum}字")
    return text


def msg__sendtext(connection, context, data, settings):
    actor = _actor(connection, context)
    text = _text(data.get("text"))
    if str(data.get("text_type", "")).lower() not in ("md", "markdown"):
        from .messaging_markup import normalize_html
        text = normalize_html(connection, context, text, settings)
    result = None
    for dialog_id in _targets(connection, actor, data):
        payload = {"text": text}
        if str(data.get("text_type", "")).lower() in ("md", "markdown"):
            payload["type"] = "md"
        if data.get("model_name"):
            payload["model_name"] = data["model_name"]
        kind = "text"
        if len(re.sub(r"<img[^>]*>", "", text, flags=re.I)) > 5000:
            ext = payload.get("type", "htm")
            from .upload import generic_upload
            raw = text.encode()
            file = generic_upload(connection, context, io.BytesIO(raw), f"LongText-{len(text)}.{ext}", {}, settings)
            file["size"] = len(raw)
            payload = {**payload, "type": ext, "desc": re.sub("<[^>]+>", "", text)[:200], "text": text[:500], "file": file}
            kind = "longtext"
        result = send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type=kind, msg=payload, context=context,
            reply_id=int(data.get("reply_id", 0)), update_id=int(data.get("update_id", 0)), silence=on(data.get("silence", False)), search_key=data.get("key"))
    return result


def msg__sendnotice(connection, context, data, settings):
    actor = _actor(connection, context)
    notice = _text(data.get("notice"), 500)
    result = None
    for dialog_id in _targets(connection, actor, data):
        result = send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type="notice",
            msg={"notice": notice, "source": data.get("source", "api")}, context=context, silence=on(data.get("silence", False)))
    return result


def msg__sendtemplate(connection, context, data, settings):
    actor = _actor(connection, context)
    content = decode(data.get("content"), [])
    if not isinstance(content, list) or not content:
        raise ApiError("模板内容不能为空")
    for item in content:
        _text(item.get("content"), 300)
        if len(str(item.get("style", ""))) > 300:
            raise ApiError("模板消息样式过长")
    title = str(data.get("title") or content[0]["content"][:50])
    result = None
    for dialog_id in _targets(connection, actor, data):
        result = send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type="template", msg={
            "type": "content", "title": title, "content": content, "source": data.get("source", "api")}, context=context, silence=on(data.get("silence", False)))
    return result


def _bot(connection, kind):
    from .account_meetings import SYSTEM_BOTS, bot_get
    if kind in SYSTEM_BOTS:
        return bot_get(connection, kind)
    users = table("users")
    row = connection.execute(select(users).where(users.c.email == kind + "@bot.system", users.c.bot == 1)).mappings().first()
    if not row or auth.is_disabled(row):
        raise ApiError("机器人不存在或已停用")
    return row


def msg__sendapprove(connection, context, data, settings):
    _actor(connection, context)
    kind = data.get("type")
    if int(data.get("to_userid", 0)) <= 0 or kind not in ("approve_reviewer", "approve_notifier", "approve_submitter", "approve_comment_notifier"):
        raise ApiError("参数错误")
    bot = _bot(connection, "approval-alert")
    dialog_id = core.ensure_user_dialog(connection, bot["userid"], int(data["to_userid"]))
    return send_message(connection, dialog_id=dialog_id, sender_id=bot["userid"], msg_type="template",
        msg={"type": kind, "action": data.get("action") or None, "is_finished": int(data.get("is_finished", 0)),
             "data": decode(data.get("data")), "title": data.get("title", "")}, silence=True)


def _record(connection, context, settings, dialog_id, data):
    duration = int(data.get("duration", 0))
    if duration < 600:
        raise ApiError("说话时间太短")
    value = str(data.get("base64", ""))
    match = re.fullmatch(r"(?:data:audio/([\w.+-]+);base64,)?([A-Za-z0-9+/=\r\n]+)", value)
    if not match:
        raise ApiError("录音格式错误")
    raw = base64.b64decode(match[2], validate=True)
    if len(raw) > 30 * 1024 * 1024 or not raw:
        raise ApiError("录音文件大小错误")
    ext = {"mpeg": "mp3", "mp3": "mp3", "wav": "wav", "webm": "webm", "ogg": "ogg", "mp4": "m4a"}.get(match[1] or "mpeg")
    if not ext:
        raise ApiError("录音格式错误")
    from .upload import generic_upload
    record = generic_upload(connection, context, io.BytesIO(raw), uuid4().hex + "." + ext, {}, settings)
    return {**record, "size": len(raw), "duration": duration}


def msg__sendrecord(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    return send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="record",
        msg=_record(connection, context, settings, dialog["id"], data), reply_id=int(data.get("reply_id", 0)), context=context)


def msg__sendfile(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    return _send_uploads(connection, context, data, settings, actor, [dialog["id"]])


def msg__sendfiles(connection, context, data, settings):
    actor = _actor(connection, context)
    return _send_uploads(connection, context, data, settings, actor, _targets(connection, actor, data))


def _send_uploads(connection, context, data, settings, actor, targets):
    from .files_support import store_stream
    from .files_collaboration import send_files_from_path
    for dialog_id in targets:
        get_dialog(connection, dialog_id, actor.userid)
    uploads = data.get("files", [])
    if not isinstance(uploads, list):
        uploads = [uploads]
    sources = []
    for upload in uploads:
        if not hasattr(upload, "file"):
            raise ApiError("请选择文件")
        blob = store_stream(settings, upload.file, maximum=200 * 1024 * 1024)
        sources.append((confined_path(settings.storage_root, blob["url"]), str(data.get("filename") or upload.filename or "file")))
    if data.get("image64"):
        match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)", str(data["image64"]))
        if not match:
            raise ApiError("图片格式错误")
        raw = base64.b64decode(match[2], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        blob = store_stream(settings, io.BytesIO(raw), maximum=20 * 1024 * 1024)
        sources.append((confined_path(settings.storage_root, blob["url"]), str(data.get("filename") or "image." + match[1])))
    if not sources:
        raise ApiError("请选择文件")
    result = None
    for path, name in sources:
        result = send_files_from_path(connection, context, targets, path, name, int(data.get("reply_id", 0)), int(data.get("image_attachment", 0)), settings)
    return result


def msg__sendanon(connection, context, data, settings):
    _actor(connection, context)
    if db.setting(connection, "system", "anon_message", "open") != "open":
        raise ApiError("匿名消息功能暂停使用")
    text = _text(data.get("text"), 2000)
    bot = _bot(connection, "anon-msg")
    users = table("users")
    recipient = connection.execute(select(users).where(users.c.userid == int(data.get("userid", 0)), users.c.disable_at.is_(None), users.c.bot == 0)).mappings().first()
    if not recipient:
        raise ApiError("匿名消息仅允许发送给个人")
    dialog_id = core.ensure_user_dialog(connection, bot["userid"], recipient["userid"])
    return send_message(connection, dialog_id=dialog_id, sender_id=bot["userid"], msg_type="template", msg={"type": "content", "content": text})


def msg__sendbot(connection, context, data, settings):
    actor = _actor(connection, context)
    kind = str(data.get("bot_type", "system-msg"))
    if kind not in ("system-msg", "task-alert", "todo-alert", "check-in", "approval-alert", "meeting-alert", "bot-manager"):
        if not 6 <= len(kind) <= 20:
            raise ApiError("机器人类型由6-20个字符组成。")
        kind = "user-auto-" + kind
    bot = _bot(connection, kind)
    if kind.startswith("user-auto-"):
        bots = table("user_bots")
        if not connection.execute(select(bots.c.id).where(bots.c.bot_id == bot["userid"], bots.c.userid == actor.userid)).first():
            raise ApiError("无权限操作此机器人")
    text = _text(data.get("text"), 2000)
    dialog_id = core.ensure_user_dialog(connection, bot["userid"], int(data.get("userid", 0)))
    return send_message(connection, dialog_id=dialog_id, sender_id=bot["userid"], msg_type="text", msg={"type": "md", "text": text}, silence=on(data.get("silence", False)))


def msg__sendlocation(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    lng, lat = float(data.get("lng", 0)), float(data.get("lat", 0))
    if not lng or not lat or not -180 <= lng <= 180 or not -90 <= lat <= 90:
        raise ApiError("经纬度错误")
    if data.get("type") not in ("baidu", "amap", "tencent"):
        raise ApiError("参数错误")
    title = _text(data.get("title"), 255)
    payload = {"type": data["type"], "lng": lng, "lat": lat, "title": title, "distance": int(data.get("distance", 0)),
        "address": str(data.get("address", "")), "thumb": str(data.get("thumb", ""))}
    if payload["thumb"].startswith(("https://", "http://")):
        # Only the map provider's public image hosts, never arbitrary internal URLs.
        host = urlparse(payload["thumb"]).hostname or ""
        if not any(host == suffix or host.endswith("." + suffix) for suffix in ("baidu.com", "amap.com", "qq.com", "gtimg.com")):
            raise ApiError("地图快照来源无效")
        from .messaging_markup import public_image
        raw = public_image(payload["thumb"])
        if len(raw) > 10 * 1024 * 1024:
            raise ApiError("地图快照过大")
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if min(width, height) < 20:
                raise ApiError("地图快照尺寸太小")
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG")
        from .upload import generic_upload
        image = generic_upload(connection, context, io.BytesIO(output.getvalue()), uuid4().hex + ".jpg", {}, settings, image=True)
        payload.update(thumb_original=payload["thumb"], thumb=image["path"], width=width, height=height)
    return send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="location", msg=payload, context=context)


def msg__readlist(connection, context, data, settings):
    actor, msg = _message(connection, context, data)
    if msg["userid"] != actor.userid:
        raise ApiError("不是发送人")
    reads = table("web_socket_dialog_msg_reads")
    return [dict(row) for row in connection.execute(select(reads).where(reads.c.msg_id == msg["id"])).mappings()]


def msg__withdraw(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    if msg["userid"] != actor.userid:
        raise ApiError("仅支持撤回自己的消息")
    limit = int(db.setting(connection, "system", "msg_rev_limit", 0) or 0)
    if limit and not actor.user["bot"] and len(member_ids(connection, msg["dialog_id"])) > 1 and msg["created_at"] + timedelta(minutes=limit) < utcnow():
        raise ApiError("此消息已超过撤回时间限制")
    messages, reads = table("web_socket_dialog_msgs"), table("web_socket_dialog_msg_reads")
    connection.execute(messages.update().where(messages.c.id == msg["id"]).values(deleted_at=utcnow(), updated_at=utcnow()))
    removed = connection.execute(reads.delete().where(reads.c.msg_id == msg["id"], reads.c.read_at.is_(None))).rowcount
    for name in ("web_socket_dialog_msg_todos", "web_socket_dialog_msg_attachments"):
        child = table(name)
        connection.execute(child.delete().where(child.c.msg_id == msg["id"]))
    if msg["reply_id"]:
        connection.execute(messages.update().where(messages.c.id == msg["reply_id"]).values(reply_num=func.greatest(0, messages.c.reply_num - 1)))
    last = connection.execute(select(messages).where(messages.c.dialog_id == msg["dialog_id"], messages.c.deleted_at.is_(None)).order_by(messages.c.id.desc()).limit(1)).mappings().first()
    members = table("web_socket_dialog_users")
    connection.execute(members.update().where(members.c.dialog_id == msg["dialog_id"]).values(last_at=last["created_at"] if last else None, updated_at=utcnow()))
    push_dialog(connection, msg["dialog_id"], "delete", {"id": msg["id"], "dialog_id": msg["dialog_id"], "last_msg": message_data(last) if last else None, "update_read": int(bool(removed))})
    from .project_tasks import sync_message_relations
    sync_message_relations(connection, {**msg, "deleted_at": utcnow()})
    jobs.enqueue(connection, "search.sync", {"type": "message", "id": msg["id"]})
    return {"id": msg["id"]}


def msg__mark(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    kind = data.get("type")
    if kind not in ("read", "unread"):
        raise ApiError("参数错误")
    if kind == "read":
        reads = table("web_socket_dialog_msg_reads")
        selected = connection.execute(select(reads.c.msg_id).where(reads.c.userid == actor.userid, reads.c.dialog_id == dialog["id"],
            reads.c.read_at.is_(None), reads.c.msg_id >= int(data.get("after_msg_id", 0)))).scalars().all()
        _read_ids(connection, actor.userid, selected)
    _set_member(connection, dialog["id"], actor.userid, mark_unread=int(kind == "unread"))
    return {**msg__unread(connection, context, data, settings), "mark_unread": int(kind == "unread")}


def msg__silence(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    if dialog["type"] == "group" and dialog["group_type"] != "user":
        raise ApiError("此会话不允许设置免打扰")
    if data.get("type") not in ("set", "cancel"):
        raise ApiError("参数错误")
    if data["type"] == "set":
        msg__mark(connection, context, {**data, "type": "read"}, settings)
    return _set_member(connection, dialog["id"], actor.userid, silence=int(data["type"] == "set"))


def msg__color(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    return _set_member(connection, dialog["id"], actor.userid, color=str(data.get("color", ""))[:20])


def msg__emoji(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    symbol = str(data.get("symbol", ""))
    if len(symbol) > 32 or not re.search("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u20e3]", symbol):
        raise ApiError("参数错误")
    values = msg["emoji"]
    found = next((entry for entry in values if entry["symbol"] == symbol), None)
    if found:
        if actor.userid in found["userids"]:
            found["userids"].remove(actor.userid)
        else:
            found["userids"].insert(0, actor.userid)
        values = [entry for entry in values if entry["userids"]]
    else:
        values.insert(0, {"symbol": symbol, "userids": [actor.userid]})
    messages = table("web_socket_dialog_msgs")
    connection.execute(messages.update().where(messages.c.id == msg["id"]).values(emoji=encode(values), updated_at=utcnow()))
    result = {"id": msg["id"], "emoji": values}
    push_dialog(connection, msg["dialog_id"], "update", result)
    return result



def msg__forward(connection, context, data, settings):
    actor = _actor(connection, context)
    msg_ids = ids(data.get("msg_ids") or data.get("msg_id"))
    if not msg_ids or len(msg_ids) > 100:
        raise ApiError("最多转发100条消息")
    sources = [get_message(connection, id, actor.userid) for id in msg_ids]
    targets, result = _targets(connection, actor, data), []
    for source in sources:
        if source["type"] in ("tag", "top", "todo", "notice", "word-chain", "vote", "template"):
            raise ApiError("此类型消息不支持转发")
        payload = dict(source["msg"])
        payload.pop("reply_data", None)
        if int(data.get("show_source", 0)):
            payload["forward_data"] = {"userid": source["userid"], "created_at": source["created_at"]}
        for dialog_id in targets:
            result.append(send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type=source["type"],
                msg=payload, forward_id=source["id"], context=context))
    if data.get("leave_message"):
        for dialog_id in targets:
            result.append(send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type="text", msg={"text": _text(data["leave_message"])}, context=context))
    return {"msgs": result}


def msg__mergeforward(connection, context, data, settings):
    actor = _actor(connection, context)
    wanted = ids(data.get("msg_ids"))
    if not wanted or len(wanted) > 100:
        raise ApiError("请选择1到100条消息")
    sources = [get_message(connection, id, actor.userid) for id in wanted]
    sources = [row for row in sources if row["type"] not in ("tag", "top", "todo", "notice", "word-chain", "vote", "template")]
    if not sources:
        raise ApiError("所选消息均不支持转发")
    if len({row["dialog_id"] for row in sources}) != 1:
        raise ApiError("只能合并转发同一对话的消息")
    sender_ids = list(dict.fromkeys(row["userid"] for row in sources))
    users = table("users")
    sender_names = list(connection.execute(select(users.c.nickname).where(users.c.userid.in_(sender_ids[:2]))).scalars())
    result = []
    for dialog_id in _targets(connection, actor, data):
        payload = {"sender_names": sender_names, "sender_total": len(sender_ids), "msg_ids": [row["id"] for row in sources],
            "preview": [{"userid": row["userid"], "type": row["type"], "msg": {key: row["msg"].get(key, "") for key in
                ({"text": ("text",), "file": ("name", "ext"), "location": ("title",)}.get(row["type"], ()))}} for row in sources[:4]],
            "count": len(sources), "forward_data": {"show": int(data.get("show_source", 0)), "leave": int(bool(data.get("leave_message")))}}
        result.append(send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type="merge-forward", msg=payload, context=context))
        if data.get("leave_message"):
            result.append(send_message(connection, dialog_id=dialog_id, sender_id=actor.userid, msg_type="text", msg={"text": _text(data["leave_message"])}, context=context))
    return {"msgs": result}


def msg__mergedetail(connection, context, data, settings):
    actor, msg = _message(connection, context, data)
    if msg["type"] != "merge-forward":
        raise ApiError("消息不存在或已被删除")
    messages = table("web_socket_dialog_msgs")
    rows = connection.execute(select(messages).where(messages.c.id.in_(ids(msg["msg"].get("msg_ids")))).order_by(messages.c.created_at)).mappings()
    return {"msgs": [{key: message_data(row)[key] for key in ("id", "userid", "type", "msg", "created_at")} for row in rows]}


def _quote(msg):
    return str(msg["msg"].get("text", msg["msg"].get("title", msg["msg"].get("name", msg["type"]))))[:500]


def msg__tag(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    if msg["type"] in ("tag", "todo", "notice"):
        raise ApiError("此消息不支持标注")
    value, messages = 0 if msg["tag"] else actor.userid, table("web_socket_dialog_msgs")
    connection.execute(messages.update().where(messages.c.id == msg["id"]).values(tag=value, updated_at=utcnow()))
    update = {"id": msg["id"], "tag": value}
    added = send_message(connection, dialog_id=msg["dialog_id"], sender_id=actor.userid, msg_type="tag", msg={
        "action": "add" if value else "remove", "data": {"id": msg["id"], "type": msg["type"], "msg": _quote(msg)}}, context=context)
    push_dialog(connection, msg["dialog_id"], "update", update)
    return {"add": added, "update": update}


def _todo_permission(connection, actor, msg, affected):
    if db.setting(connection, "system", "todo_set_permission") == "close" and set(affected) - {actor.userid}:
        role = _member(connection, msg["dialog_id"], actor.userid)["role"]
        if role != 1 and "admin" not in actor.identities:
            raise ApiError("仅群主、项目/任务负责人或系统管理员可设置或取消他人待办")


def _todo_update(connection, msg):
    todos = table("web_socket_dialog_msg_todos")
    outstanding = connection.execute(select(todos.c.id).where(todos.c.msg_id == msg["id"], todos.c.done_at.is_(None)).limit(1)).first()
    result = {"id": msg["id"], "todo": msg["todo"], "todo_done": int(not outstanding), "dialog_id": msg["dialog_id"]}
    push_dialog(connection, msg["dialog_id"], "update", result)
    return result


def msg__todo(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    if msg["type"] in ("tag", "todo", "notice"):
        raise ApiError("此消息不支持设待办")
    members = set(member_ids(connection, msg["dialog_id"]))
    wanted = members if data.get("type", "all") == "all" else set(ids(data.get("userids")))
    if wanted - members:
        raise ApiError("待办成员不在会话内")
    todos = table("web_socket_dialog_msg_todos")
    current = set(connection.execute(select(todos.c.userid).where(todos.c.msg_id == msg["id"])).scalars())
    _todo_permission(connection, actor, msg, current ^ wanted)
    added = []
    for action, affected in (("remove", current - wanted), ("add", wanted - current)):
        if not affected:
            continue
        added.append(send_message(connection, dialog_id=msg["dialog_id"], sender_id=actor.userid, msg_type="todo", msg={
            "action": action, "data": {"id": msg["id"], "type": msg["type"], "msg": _quote(msg), "userids": ",".join(map(str, sorted(affected)))}}, context=context))
        if action == "remove":
            connection.execute(todos.delete().where(todos.c.msg_id == msg["id"], todos.c.userid.in_(affected)))
        else:
            for userid in affected:
                db.insert_row(connection, "web_socket_dialog_msg_todos", msg_id=msg["id"], dialog_id=msg["dialog_id"], userid=userid)
    if "remind_at" in data:
        connection.execute(todos.update().where(todos.c.msg_id == msg["id"], todos.c.userid.in_(wanted)).values(remind_at=_date(data["remind_at"]), reminded_at=None))
    msg["todo"] = actor.userid if wanted else 0
    messages = table("web_socket_dialog_msgs")
    connection.execute(messages.update().where(messages.c.id == msg["id"]).values(todo=msg["todo"], updated_at=utcnow()))
    return {"add": added, "update": _todo_update(connection, msg)}


def msg__todolist(connection, context, data, settings):
    actor, msg = _message(connection, context, data)
    todos = table("web_socket_dialog_msg_todos")
    return [dict(row) for row in connection.execute(select(todos).where(todos.c.msg_id == msg["id"])).mappings()]


def msg__todoremind(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    wanted = ids(data.get("userids"))
    if not wanted:
        raise ApiError("请选择成员")
    _todo_permission(connection, actor, msg, wanted)
    todos = table("web_socket_dialog_msg_todos")
    connection.execute(todos.update().where(todos.c.msg_id == msg["id"], todos.c.userid.in_(wanted)).values(remind_at=_date(data.get("remind_at")), reminded_at=None))
    return _todo_update(connection, msg)


def msg__done(connection, context, data, settings):
    actor = _actor(connection, context)
    todos = table("web_socket_dialog_msg_todos")
    row = connection.execute(select(todos).where(todos.c.id == int(data.get("id", 0)), todos.c.userid == actor.userid).with_for_update()).mappings().first()
    if not row or row["done_at"]:
        return {"add": None, "update": None}
    msg = get_message(connection, row["msg_id"], actor.userid, lock=True)
    connection.execute(todos.update().where(todos.c.id == row["id"]).values(done_at=utcnow()))
    done_ids = list(connection.execute(select(todos.c.userid).where(todos.c.msg_id == msg["id"], todos.c.done_at.is_not(None)).order_by(todos.c.done_at.desc(), todos.c.id.desc())).scalars())
    added = send_message(connection, dialog_id=msg["dialog_id"], sender_id=actor.userid, msg_type="todo", msg={
        "action": "done", "data": {"id": msg["id"], "type": msg["type"], "msg": _quote(msg), "done_userids": done_ids}}, context=context)
    _todo_update(connection, msg)
    return {"add": added, "update": None}


def group__add(connection, context, data, settings):
    actor = _actor(connection, context)
    users = sorted(set(ids(data.get("userids")) + [actor.userid]))
    if len(users) < 2:
        raise ApiError("群成员至少2人")
    if "temp" in actor.identities:
        raise ApiError("无法创建群组")
    name = str(data.get("chat_name", "")).strip()
    if not name:
        account = table("users")
        name = ", ".join(connection.execute(select(account.c.nickname).where(account.c.userid.in_(users)).limit(8)).scalars())[:100]
    dialog_id = core.create_group(connection, owner_id=actor.userid, userids=users, name=name, group_type="user")
    if data.get("avatar"):
        dialogs = table("web_socket_dialogs")
        avatar = data["avatar"][0]["path"] if isinstance(data["avatar"], list) else data["avatar"]
        connection.execute(dialogs.update().where(dialogs.c.id == dialog_id).values(avatar=str(avatar)[:255]))
    push_dialog(connection, dialog_id, "groupAdd", {"id": dialog_id})
    return dialog_data(connection, dialog_id, actor.userid)


def group__edit(connection, context, data, settings):
    actor = _actor(connection, context)
    admin = int(data.get("admin", 0)) == 1
    if admin:
        auth.require_admin(actor)
    dialog = get_dialog(connection, int(data.get("dialog_id", 0)), None if admin else actor.userid, lock=True)
    if not admin:
        _group_owner(connection, actor, dialog)
    changes = {}
    if "avatar" in data:
        avatar = data["avatar"]
        changes["avatar"] = str(avatar[0]["path"] if isinstance(avatar, list) and avatar else avatar or "")[:255]
    if ("chat_name" in data or "name" in data) and (dialog["group_type"] == "user" or dialog["group_type"] == "all" and admin):
        name = str(data.get("chat_name") or data.get("name", "")).strip()
        if not 2 <= len(name) <= 100:
            raise ApiError("群名称由2-100个字组成")
        changes["name"] = name
    dialogs = table("web_socket_dialogs")
    connection.execute(dialogs.update().where(dialogs.c.id == dialog["id"]).values(**changes, updated_at=utcnow()))
    push_dialog(connection, dialog["id"], "groupUpdate", {"id": dialog["id"], **changes})
    return {"id": dialog["id"], **changes}


def group__adduser(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    if dialog["owner_id"]:
        _group_owner(connection, actor, dialog)
    elif dialog["type"] != "group":
        raise ApiError("仅支持群组")
    wanted = ids(decode(data.get("userids"), []))
    core.sync_members(connection, dialog["id"], member_ids(connection, dialog["id"]) + wanted, important=False)
    jobs.enqueue(connection, "dialog.member_hook", {"dialog_id": dialog["id"], "userid": actor.userid, "userids": wanted, "event": "member_join"})
    return []


def group__deluser(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    if dialog["type"] != "group":
        raise ApiError("仅支持群组")
    wanted = ids(decode(data.get("userids"), [])) or [actor.userid]
    role = _member(connection, dialog["id"], actor.userid)["role"]
    for userid in wanted:
        member = _member(connection, dialog["id"], userid)
        if member["role"] == 1 or member["important"]:
            raise ApiError("群主及项目任务成员不可移出或退出")
        if userid != actor.userid and role not in (1, 2) and member["inviter"] != actor.userid:
            raise ApiError("无权限移出成员")
        if role == 2 and member["role"] == 2 and userid != actor.userid:
            raise ApiError("无权限移出群管理员")
    core.sync_members(connection, dialog["id"], set(member_ids(connection, dialog["id"])) - set(wanted), important=False)
    jobs.enqueue(connection, "dialog.member_hook", {"dialog_id": dialog["id"], "userid": actor.userid, "userids": wanted, "event": "member_leave"})
    return []


def group__transfer(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    _group_owner(connection, actor, dialog, primary=True, personal=True)
    userid = int(data.get("userid", 0))
    if userid == actor.userid:
        raise ApiError("你已经是群主")
    core.sync_members(connection, dialog["id"], member_ids(connection, dialog["id"]) + [userid], owner_id=userid, important=False)
    push_dialog(connection, dialog["id"], "groupUpdate", {"id": dialog["id"], "owner_id": userid})
    return []


def _deputy(connection, context, data, settings, role):
    actor, dialog = _dialog(connection, context, data, lock=True)
    _group_owner(connection, actor, dialog, primary=True, personal=True)
    userid = int(data.get("userid", 0))
    member = _member(connection, dialog["id"], userid)
    if member["role"] == 1:
        raise ApiError("不能修改群主角色")
    _set_member(connection, dialog["id"], userid, role=role)
    members = table("web_socket_dialog_users")
    deputies = list(connection.execute(select(members.c.userid).where(members.c.dialog_id == dialog["id"], members.c.role == 2)).scalars())
    push_dialog(connection, dialog["id"], "groupUpdate", {"id": dialog["id"], "deputy_ids": deputies})
    return []


def group__adddeputy(connection, context, data, settings):
    return _deputy(connection, context, data, settings, 2)


def group__deldeputy(connection, context, data, settings):
    return _deputy(connection, context, data, settings, 0)


def group__disband(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    _group_owner(connection, actor, dialog, primary=True, personal=True)
    users = member_ids(connection, dialog["id"])
    dialogs = table("web_socket_dialogs")
    connection.execute(dialogs.update().where(dialogs.c.id == dialog["id"]).values(deleted_at=utcnow(), updated_at=utcnow()))
    for userid in users:
        db.insert_row(connection, "deleteds", type="dialog", did=dialog["id"], userid=userid)
    if users:
        jobs.enqueue_push(connection, userids=users, message={"type": "dialog", "mode": "groupDelete", "data": {"id": dialog["id"]}})
    return []


def group__searchuser(connection, context, data, settings):
    auth.require_admin(_actor(connection, context))
    dialogs = table("web_socket_dialogs")
    query = select(dialogs).where(dialogs.c.type == "group", dialogs.c.group_type == "user", dialogs.c.deleted_at.is_(None))
    if data.get("key"):
        query = query.where(dialogs.c.name.contains(str(data["key"])))
    return {"list": [dict(row) for row in connection.execute(query.limit(20)).mappings()]}


def common__list(connection, context, data, settings):
    actor = _actor(connection, context)
    members, dialogs = table("web_socket_dialog_users"), table("web_socket_dialogs")
    query = select(dialogs.c.id).join(members, members.c.dialog_id == dialogs.c.id).where(members.c.userid == actor.userid,
        dialogs.c.type == "group", dialogs.c.group_type == "user", dialogs.c.deleted_at.is_(None))
    if int(data.get("target_userid", 0)):
        other = members.alias("other")
        query = query.where(dialogs.c.id.in_(select(other.c.dialog_id).where(other.c.userid == int(data["target_userid"]))))
    if data.get("only_count") == "yes":
        return {"total": connection.execute(select(func.count()).select_from(query.subquery())).scalar_one()}
    return _page(connection, query.order_by(members.c.top_at.desc(), members.c.last_at.desc()), data,
        transform=lambda row: dialog_data(connection, row["id"], actor.userid))


def okr__add(connection, context, data, settings):
    actor = _actor(connection, context)
    dialog_id = core.create_group(connection, owner_id=actor.userid, userids=ids(data.get("userids")),
        name=_text(data.get("name"), 255), group_type="okr", link_id=int(data.get("link_id", 0)))
    return get_dialog(connection, dialog_id, actor.userid)


def okr__push(connection, context, data, settings):
    _actor(connection, context)
    bot = _bot(connection, "okr-alert")
    dialog_id = core.ensure_user_dialog(connection, bot["userid"], int(data.get("userid", 0)))
    return send_message(connection, dialog_id=dialog_id, sender_id=bot["userid"], msg_type="text", msg={"text": _text(data.get("text"))}, silence=True)



def msg__sendfileid(connection, context, data, settings):
    from . import files
    actor = _actor(connection, context)
    file_id = int(data.get("file_id", 0))
    file = files.require(connection, context, file_id)
    link = files.link(connection, context, {"id": file_id, "refresh": "no", "guest_access": "no"}, settings)
    text = f'<p><a class="mention file" href="{{{{RemoteURL}}}}single/file/{html.escape(link["code"])}" target="_blank">~{html.escape(file["name"])}</a></p>'
    if data.get("leave_message"):
        text += "<p>" + html.escape(str(data["leave_message"])) + "</p>"
    return {"msgs": [send_message(connection, dialog_id=id, sender_id=actor.userid, msg_type="text", msg={"text": text}, context=context) for id in _targets(connection, actor, data)]}


def msg__sendtaskid(connection, context, data, settings):
    from .project_core import can_view_task
    actor = _actor(connection, context)
    tasks = table("project_tasks")
    task = connection.execute(select(tasks).where(tasks.c.id == int(data.get("task_id", 0)), tasks.c.deleted_at.is_(None))).mappings().first()
    if not task or not can_view_task(connection, actor.userid, dict(task)):
        raise ApiError("没有权限操作此任务")
    text = f'<p><span class="mention task" data-id="{task["id"]}">#{html.escape(task["name"])}</span></p>'
    if data.get("leave_message"):
        text += "<p>" + html.escape(str(data["leave_message"])) + "</p>"
    return {"msgs": [send_message(connection, dialog_id=id, sender_id=actor.userid, msg_type="text", msg={"text": text}, context=context) for id in _targets(connection, actor, data)]}


def msg__detail(connection, context, data, settings):
    from .files_collaboration import authorize_message, format_file_data
    actor = _actor(connection, context)
    msg = get_message(connection, int(data.get("msg_id", 0)))
    authorize_message(connection, actor.userid, msg)
    if data.get("only_update_at") == "yes":
        return {"id": msg["id"], "update_at": msg["updated_at"]}
    if msg["type"] == "file":
        formatted = format_file_data(connection, context, msg["msg"], settings)
        msg.update(content=formatted["content"], file_mode=formatted["file_mode"])
    elif msg["type"] == "longtext":
        from .files_collaboration import resolve_attachment_path
        path = resolve_attachment_path(connection, context, msg["msg"]["file"]["path"], settings)
        msg["content"] = {"type": msg["msg"].get("type", "htm"), "content": path.read_text(encoding="utf-8")}
    return msg


def msg__download(connection, context, data, settings):
    from .files_collaboration import authorize_message, resolve_attachment_path
    actor = _actor(connection, context)
    msg = get_message(connection, int(data.get("msg_id", 0)))
    authorize_message(connection, actor.userid, msg)
    if msg["type"] != "file":
        raise ApiError("此消息不支持下载")
    path = resolve_attachment_path(connection, context, msg["msg"]["path"], settings)
    if not path or not path.is_file():
        raise ApiError("文件不存在")
    if data.get("down") == "preview":
        return RedirectResponse(context.base_url.rstrip("/") + "/single/file/msg-" + str(msg["id"]), status_code=302)
    return FileResponse(path, filename=msg["msg"]["name"])


def msg__wordchain(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    text, entries = _text(data.get("text")), data.get("list", [])
    if not isinstance(entries, list) or not entries:
        raise ApiError("内容不能为空")
    identifier = str(data.get("uuid", ""))
    existing = _card_messages(connection, dialog["id"], "word-chain", identifier) if identifier else []
    previous = existing[0]["msg"] if existing else {}
    old_entries = previous.get("list", [])
    indexed = {int(item["id"]): dict(item) for item in old_entries}
    now_id = int(utcnow().timestamp() * 1000)
    for offset, entry in enumerate(entries):
        old = indexed.get(int(entry.get("id", 0)))
        if old and old["userid"] != actor.userid:
            continue
        key = int(entry.get("id", 0)) if old else now_id + offset
        indexed[key] = {**entry, "id": key, "userid": actor.userid}
    payload = {"text": text, "list": sorted(indexed.values(), key=lambda item: item["id"]), "userid": actor.userid,
        "createid": previous.get("createid", actor.userid), "uuid": identifier or uuid4().hex}
    return send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="word-chain", msg=payload, context=context)


def _card_messages(connection, dialog_id, kind, identifier):
    messages = table("web_socket_dialog_msgs")
    rows = connection.execute(select(messages).where(messages.c.dialog_id == dialog_id, messages.c.type == kind,
        messages.c.deleted_at.is_(None), messages.c.msg.contains(identifier)).order_by(messages.c.id.desc()).with_for_update()).mappings()
    return [message_data(row) for row in rows if decode(row["msg"]).get("uuid") == identifier]


def msg__vote(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    action = str(data.get("type", "create"))
    if action == "create":
        choices = data.get("list", [])
        if not isinstance(choices, list) or len(choices) < 2:
            raise ApiError("请设置投票选项")
        payload = {"text": _text(data.get("text")), "list": choices, "userid": actor.userid, "uuid": str(data.get("uuid") or uuid4().hex),
            "multiple": int(data.get("multiple", 0)), "anonymous": int(data.get("anonymous", 0)), "votes": [], "state": 1}
        return [send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="vote", msg=payload, context=context)]
    if action not in ("vote", "again", "finish") or not data.get("uuid"):
        raise ApiError("参数错误")
    rows = _card_messages(connection, dialog["id"], "vote", str(data["uuid"]))
    if not rows:
        raise ApiError("投票不存在")
    if action == "again":
        return [send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="vote", msg=rows[0]["msg"], context=context)]
    votes = data.get("vote", [])
    votes = votes if isinstance(votes, list) else [votes]
    result = []
    for row in rows:
        payload = row["msg"]
        if action == "finish":
            if payload.get("userid") != actor.userid:
                raise ApiError("只有发起人可以结束投票")
            payload["state"] = 0
        else:
            if not payload.get("state"):
                raise ApiError("投票已结束")
            if any(v["userid"] == actor.userid for v in payload.get("votes", [])):
                raise ApiError("不能重复投票")
            if not votes or not payload.get("multiple") and len(votes) != 1:
                raise ApiError("参数错误")
            valid = {str(item.get("id", index)) if isinstance(item, dict) else str(index) for index, item in enumerate(payload["list"])}
            if any(str(v) not in valid for v in votes):
                raise ApiError("投票选项无效")
            payload.setdefault("votes", []).append({"userid": actor.userid, "votes": votes})
        messages = table("web_socket_dialog_msgs")
        connection.execute(messages.update().where(messages.c.id == row["id"]).values(msg=encode(payload), updated_at=utcnow()))
        row["msg"] = payload
        push_dialog(connection, dialog["id"], "update", row)
        result.append(row)
    return result


def msg__top(connection, context, data, settings):
    actor, msg = _message(connection, context, data, lock=True)
    dialog = get_dialog(connection, msg["dialog_id"], actor.userid, lock=True)
    top_id = 0 if dialog["top_msg_id"] == msg["id"] else msg["id"]
    update = {"dialog_id": dialog["id"], "top_msg_id": top_id, "top_userid": actor.userid if top_id else 0}
    dialogs = table("web_socket_dialogs")
    connection.execute(dialogs.update().where(dialogs.c.id == dialog["id"]).values(top_msg_id=top_id, top_userid=update["top_userid"], updated_at=utcnow()))
    added = send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="top", msg={
        "action": "add" if top_id else "remove", "data": {"id": msg["id"], "type": msg["type"], "msg": _quote(msg)}}, context=context)
    push_dialog(connection, dialog["id"], "updateTopMsg", update)
    return {"add": added, "update": update}


def msg__topinfo(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    return get_message(connection, dialog["top_msg_id"], actor.userid) if dialog["top_msg_id"] else None


def sticker__search(connection, context, data, settings):
    _actor(connection, context)
    key = str(data.get("key", ""))
    payload = {"initQuery": key + " 表情", "queryFrom": "wap", "ie": "utf8", "keyword": key + " 表情", "showMode": 0,
        "start": 1, "reqType": "client", "reqFrom": "wap_result", "prevIsRedis": "n", "pagetype": 0, "amsParams": []}
    request = Request("https://pic.sogou.com/napi/wap/searchlist", data=encode(payload).encode(), headers={"Content-Type": "application/json", "Referer": "https://pic.sogou.com/"})
    with urlopen(request, timeout=15) as response:
        result = json.loads(response.read())
    return {"list": [{"name": item["title"], "src": item["thumbUrl"], "height": item["thumbHeight"], "width": item["thumbWidth"]}
        for item in result.get("data", {}).get("picResult", {}).get("items", []) if int(item["thumbHeight"]) > 10 and int(item["thumbWidth"]) > 10]}


def config(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    kind = str(data.get("type", ""))
    if not kind:
        raise ApiError("参数错误")
    configs = table("web_socket_dialog_configs")
    value = connection.execute(select(configs.c.value).where(configs.c.dialog_id == dialog["id"], configs.c.userid == actor.userid, configs.c.type == kind).limit(1)).scalar_one_or_none()
    return {"value": value}


def config__save(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data, lock=True)
    kind, value = str(data.get("type", "")), str(data.get("value", ""))
    if not kind or len(kind) > 50:
        raise ApiError("参数错误")
    configs = table("web_socket_dialog_configs")
    row = connection.execute(select(configs.c.id).where(configs.c.dialog_id == dialog["id"], configs.c.userid == actor.userid, configs.c.type == kind)).first()
    if row:
        connection.execute(configs.update().where(configs.c.id == row.id).values(value=value, updated_at=utcnow()))
    else:
        db.insert_row(connection, "web_socket_dialog_configs", dialog_id=dialog["id"], userid=actor.userid, type=kind, value=value)
    send_message(connection, dialog_id=dialog["id"], sender_id=actor.userid, msg_type="notice", msg={"notice": "修改提示词：" + value[:100] if value else "取消提示词"})
    return []


def _session_dialog(connection, dialog):
    users = table("users")
    emails = connection.execute(select(users.c.email).where(users.c.userid.in_(member_ids(connection, dialog["id"])))).scalars()
    if dialog["type"] != "user" or not any(re.match(r"^(ai-|user-session-).*@bot\.system$", email) for email in emails):
        raise ApiError("当前对话不支持")


def session__create(connection, context, data, settings):
    actor = _actor(connection, context)
    dialog_id = core.ensure_user_dialog(connection, actor.userid, int(data["userid"])) if int(data.get("userid", 0)) else int(data.get("dialog_id", 0))
    dialog = get_dialog(connection, dialog_id, actor.userid, lock=True)
    _session_dialog(connection, dialog)
    sessions, dialogs = table("web_socket_dialog_sessions"), table("web_socket_dialogs")
    row = connection.execute(select(sessions).where(sessions.c.dialog_id == dialog_id, sessions.c.title == "").limit(1)).mappings().first()
    if not row:
        id = db.insert_row(connection, "web_socket_dialog_sessions", dialog_id=dialog_id, title="")
        row = connection.execute(select(sessions).where(sessions.c.id == id)).mappings().one()
    connection.execute(dialogs.update().where(dialogs.c.id == dialog_id).values(session_id=row["id"], updated_at=utcnow()))
    reads, messages = table("web_socket_dialog_msg_reads"), table("web_socket_dialog_msgs")
    connection.execute(reads.update().where(reads.c.dialog_id == dialog_id, reads.c.read_at.is_(None), reads.c.msg_id.in_(
        select(messages.c.id).where(messages.c.session_id == dialog["session_id"], messages.c.dialog_id == dialog_id))).values(read_at=utcnow()))
    return dict(row)


def session__list(connection, context, data, settings):
    actor, dialog = _dialog(connection, context, data)
    sessions = table("web_socket_dialog_sessions")
    return _page(connection, select(sessions).where(sessions.c.dialog_id == dialog["id"]).order_by(sessions.c.id.desc()), data, default=10,
        transform=lambda row: {**dict(row), "is_open": int(row["id"] == dialog["session_id"])})


def session__open(connection, context, data, settings):
    actor = _actor(connection, context)
    sessions, dialogs = table("web_socket_dialog_sessions"), table("web_socket_dialogs")
    row = connection.execute(select(sessions).where(sessions.c.id == int(data.get("session_id", 0)))).mappings().first()
    if not row:
        raise ApiError("会话不存在或已被删除")
    get_dialog(connection, row["dialog_id"], actor.userid, lock=True)
    connection.execute(dialogs.update().where(dialogs.c.id == row["dialog_id"]).values(session_id=row["id"], updated_at=utcnow()))
    return dict(row)


def session__rename(connection, context, data, settings):
    actor = _actor(connection, context)
    sessions = table("web_socket_dialog_sessions")
    row = connection.execute(select(sessions).where(sessions.c.id == int(data.get("session_id", 0))).with_for_update()).mappings().first()
    if not row:
        raise ApiError("会话不存在或已被删除")
    dialog = get_dialog(connection, row["dialog_id"], actor.userid)
    _session_dialog(connection, dialog)
    title = _text(data.get("title"), 100)
    connection.execute(sessions.update().where(sessions.c.id == row["id"]).values(title=title, updated_at=utcnow()))
    configs = table("web_socket_dialog_configs")
    marker = "session_title:" + str(row["id"])
    connection.execute(configs.delete().where(configs.c.dialog_id == row["dialog_id"], configs.c.type == marker))
    db.insert_row(connection, "web_socket_dialog_configs", dialog_id=row["dialog_id"], userid=actor.userid, type=marker, value="renamed")
    return {**dict(row), "title": title, "updated_at": utcnow()}


def msg__ai_generate(connection, context, data, settings):
    return _retired(context)


def _retired(context):
    version = tuple(int(v) for v in re.findall(r"\d+", context.version)[:3])
    if version and version < (1, 4, 35):
        raise ApiError("客户端版本过低，请升级客户端")
    return Response(content="", media_type="text/html")


def msg__webhookmsg2ai(connection, context, data, settings):
    return _retired(context)


def msg__applied(connection, context, data, settings):
    return _retired(context)



def msg__send_ai_assistant(connection, context, data, settings):
    actor = _actor(connection, context)
    dialog_id = int(data.get("dialog_id", 0))
    if not dialog_id:
        from .project_tasks import task_dialog
        dialog_id = task_dialog(connection, context, {"task_id": int(data.get("task_id", 0))}, settings)["dialog_id"]
    get_dialog(connection, dialog_id, actor.userid)
    nickname = str(data.get("nickname", ""))
    if len(nickname) > 20:
        raise ApiError("发送者昵称最多不能超过20字")
    payload = {"text": _text(data.get("text"))}
    if data.get("text_type", "md") in ("md", "markdown"):
        payload["type"] = "md"
    if nickname:
        payload["nickname"] = nickname
    return send_message(connection, dialog_id=dialog_id, sender_id=-1, msg_type="text", msg=payload, silence=on(data.get("silence", False)))


def msg__convertrecord(connection, context, data, settings):
    from .messaging_ai import transcribe, translate
    actor = _actor(connection, context)
    dialog_id = int(data.get("dialog_id", 0))
    if dialog_id:
        get_dialog(connection, dialog_id, actor.userid)
    record = _record(connection, context, settings, dialog_id, data)
    prompt = "如果识别到中文，优先使用简体中文输出" if context.language == "zh" else "如果識別到中文，優先使用繁體中文輸出" if context.language == "zh-CHT" else ""
    from .files_collaboration import resolve_attachment_path
    text = transcribe(connection, settings, resolve_attachment_path(connection, context, record["path"], settings), prompt)
    result = {"text": text}
    if data.get("translate"):
        result["text"] = translate(connection, settings, text, str(data["translate"]))
    return result


def msg__voice2text(connection, context, data, settings):
    from .messaging_ai import transcribe
    actor, msg = _message(connection, context, data, lock=True)
    if msg["type"] != "record":
        raise ApiError("仅支持语音消息")
    payload = msg["msg"]
    if not payload.get("text"):
        from .files_collaboration import resolve_attachment_path
        payload["text"] = transcribe(connection, settings, resolve_attachment_path(connection, context, payload["path"], settings),
            "如果识别到中文，优先使用简体中文输出" if context.language == "zh" else "")
    payload["text_userid"] = sorted(set(payload.get("text_userid", []) + [actor.userid]))
    messages = table("web_socket_dialog_msgs")
    connection.execute(messages.update().where(messages.c.id == msg["id"]).values(msg=encode(payload), updated_at=utcnow()))
    return {**msg, "msg": payload}


def msg__translation(connection, context, data, settings):
    from .messaging_ai import translate
    actor, msg = _message(connection, context, data, lock=True)
    if msg["type"] not in ("text", "record"):
        raise ApiError("此消息不支持翻译")
    language = str(data.get("language") or context.language)
    if not re.fullmatch(r"[A-Za-z-]{2,20}", language):
        raise ApiError("参数错误")
    translations = table("web_socket_dialog_msg_translates")
    row = connection.execute(select(translations).where(translations.c.msg_id == msg["id"], translations.c.language == language)).mappings().first()
    if row and not int(data.get("force", 0)):
        return {key: row[key] for key in ("msg_id", "language", "content")}
    text = _text(msg["msg"].get("text"))
    if msg["msg"].get("type") == "md":
        text = re.sub(r":::\s*reasoning.*?:::", "", text, flags=re.S)
    content = translate(connection, settings, text, language)
    if row:
        connection.execute(translations.update().where(translations.c.id == row["id"]).values(content=content, updated_at=utcnow()))
    else:
        db.insert_row(connection, "web_socket_dialog_msg_translates", dialog_id=msg["dialog_id"], msg_id=msg["id"], language=language, content=content)
    return {"msg_id": msg["id"], "language": language, "content": content}


ROUTES = {
    "dialog/lists": lists, "dialog/beyond": beyond, "dialog/search": search, "dialog/search__tag": search__tag,
    "dialog/one": one, "dialog/user": user, "dialog/todo": todo, "dialog/top": top, "dialog/hide": hide, "dialog/tel": tel,
    "dialog/open__user": open__user, "dialog/open__event": open__event,
    "dialog/msg__list": msg__list, "dialog/msg__latest": msg__latest, "dialog/msg__one": msg__one,
    "dialog/msg__dot": msg__dot, "dialog/msg__read": msg__read, "dialog/msg__unread": msg__unread,
    "dialog/msg__checked": msg__checked, "dialog/msg__stream": msg__stream, "dialog/msg__ai_generate": msg__ai_generate,
    "dialog/msg__sendtext": msg__sendtext, "dialog/msg__sendnotice": msg__sendnotice, "dialog/msg__sendtemplate": msg__sendtemplate,
    "dialog/msg__sendapprove": msg__sendapprove, "dialog/msg__sendrecord": msg__sendrecord, "dialog/msg__convertrecord": msg__convertrecord,
    "dialog/msg__sendfile": msg__sendfile, "dialog/msg__sendfiles": msg__sendfiles, "dialog/msg__sendfileid": msg__sendfileid,
    "dialog/msg__sendtaskid": msg__sendtaskid, "dialog/msg__sendanon": msg__sendanon, "dialog/msg__sendbot": msg__sendbot,
    "dialog/msg__send_ai_assistant": msg__send_ai_assistant, "dialog/msg__sendlocation": msg__sendlocation,
    "dialog/msg__readlist": msg__readlist, "dialog/msg__detail": msg__detail, "dialog/msg__download": msg__download,
    "dialog/msg__withdraw": msg__withdraw, "dialog/msg__voice2text": msg__voice2text, "dialog/msg__translation": msg__translation,
    "dialog/msg__mark": msg__mark, "dialog/msg__silence": msg__silence, "dialog/msg__forward": msg__forward,
    "dialog/msg__mergeforward": msg__mergeforward, "dialog/msg__mergedetail": msg__mergedetail, "dialog/msg__emoji": msg__emoji,
    "dialog/msg__tag": msg__tag, "dialog/msg__todo": msg__todo, "dialog/msg__todolist": msg__todolist,
    "dialog/msg__todoremind": msg__todoremind, "dialog/msg__done": msg__done, "dialog/msg__color": msg__color,
    "dialog/msg__webhookmsg2ai": msg__webhookmsg2ai, "dialog/group__add": group__add, "dialog/group__edit": group__edit,
    "dialog/group__adduser": group__adduser, "dialog/group__deluser": group__deluser, "dialog/group__transfer": group__transfer,
    "dialog/group__adddeputy": group__adddeputy, "dialog/group__deldeputy": group__deldeputy, "dialog/group__disband": group__disband,
    "dialog/group__searchuser": group__searchuser, "dialog/common__list": common__list, "dialog/okr__add": okr__add,
    "dialog/okr__push": okr__push, "dialog/msg__wordchain": msg__wordchain, "dialog/msg__vote": msg__vote,
    "dialog/msg__top": msg__top, "dialog/msg__topinfo": msg__topinfo, "dialog/msg__applied": msg__applied,
    "dialog/sticker__search": sticker__search, "dialog/config": config, "dialog/config__save": config__save,
    "dialog/session__create": session__create, "dialog/session__list": session__list,
    "dialog/session__open": session__open, "dialog/session__rename": session__rename,
}
RESPONSE_MODE = "data"
BINARY_ROUTES = {"dialog/msg__download", "dialog/msg__ai_generate", "dialog/msg__webhookmsg2ai", "dialog/msg__applied"}


def register_jobs():
    from .messaging_jobs import register_jobs as register
    register()

