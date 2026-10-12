"""Messaging observers/tasks, explicitly registered by the integration owner."""
from collections import defaultdict
from datetime import timedelta
import hashlib
import html
import json
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
import redis
from sqlalchemy import select, func
from .. import auth, db, jobs
from ..context import RequestContext, utcnow
from ..errors import ApiError
from . import dialog_core as core
from .apps import require_installed
from .dialog_core import table, encode, decode, get_message, get_dialog, member_ids, send_message


def _short_token(connection, userid, settings):
    context = RequestContext(request_id=uuid4().hex, platform="web", version=settings.version, user_agent="DooTask transactional bot task")
    return auth.issue_service_token(connection, userid, context)


def message_effects(connection, payload, settings):
    messages = table("web_socket_dialog_msgs")
    row = connection.execute(select(messages).where(messages.c.id == payload["msg_id"], messages.c.deleted_at.is_(None))).mappings().first()
    if not row:
        return  # Withdrawal cancels not-yet-delivered effects.
    msg = core.message_data(row)
    users, members = table("users"), table("web_socket_dialog_users")
    bots = connection.execute(select(users.c.userid).join(members, members.c.userid == users.c.userid).where(
        members.c.dialog_id == msg["dialog_id"], users.c.bot == 1, users.c.userid != msg["userid"], users.c.disable_at.is_(None))).scalars().all()
    for bot_id in bots:
        jobs.enqueue(connection, "dialog.bot_prepare", {"bot_id": bot_id, "msg_id": msg["id"]}, dedupe_key=f"bot:{bot_id}:{msg['id']}")
    if db.setting(connection, "appPushSetting", "push") == "open":
        reads = table("web_socket_dialog_msg_reads")
        recipients = list(connection.execute(select(reads.c.userid).where(reads.c.msg_id == msg["id"], reads.c.silence == 0, reads.c.read_at.is_(None))).scalars())
        if recipients:
            jobs.enqueue(connection, "dialog.mobile_push", {"msg_id": msg["id"], "userids": recipients, "delayed": False})
    if msg["session_id"] and msg["type"] in ("text", "longtext") and not msg["bot"]:
        jobs.enqueue(connection, "dialog.session_title", {"session_id": msg["session_id"], "text": msg["msg"].get("text", "")}, dedupe_key=f"session-title:{msg['session_id']}")


def bot_prepare(connection, payload, settings):
    from .dialog import _read_ids
    msg = get_message(connection, payload["msg_id"])
    bot_id = int(payload["bot_id"])
    dialog = get_dialog(connection, msg["dialog_id"], bot_id)
    users, bots = table("users"), table("user_bots")
    bot = connection.execute(select(users).where(users.c.userid == bot_id, users.c.bot == 1, users.c.disable_at.is_(None))).mappings().first()
    if not bot:
        return
    _read_ids(connection, bot_id, [msg["id"]])
    if msg["bot"] or msg["userid"] <= 0:
        return
    text = msg["msg"].get("text", msg["msg"].get("content", msg["msg"].get("name", "")))
    text = text if isinstance(text, str) else encode(text)
    if msg["type"] == "location" and bot["email"] == "check-in@bot.system":
        jobs.enqueue(connection, "accounts.bot_checkin", {"userid": msg["userid"], "bot_id": bot_id, "dialog_id": dialog["id"], "command": "locat-checkin", "location": msg["msg"]})
        return
    if not text:
        return
    if dialog["type"] == "user" and (text.startswith("/") or bot["email"] in ("check-in@bot.system", "anon-msg@bot.system")):
        jobs.enqueue(connection, "accounts.bot_command", {"userid": msg["userid"], "bot_id": bot_id, "dialog_id": dialog["id"], "msg_id": msg["id"], "text": text})
        return
    mention = int(bot_id in {int(v) for v in re.findall(r'data-id=[\"\'](\d+)', text)})
    ai_match = re.fullmatch(r"ai-(.+)@bot\.system", bot["email"])
    extras = {"timestamp": int(time.time())}
    url, user_bot = None, None
    if ai_match:
        if dialog["type"] == "group" and not mention:
            return
        require_installed(settings, "ai")
        config, vendor = db.setting(connection, "aibotSetting") or {}, ai_match[1]
        extras = {name: config.get(vendor + "_" + source, "") for name, source in (
            ("model_name", "model"), ("system_message", "system"), ("api_key", "key"), ("base_url", "base_url"), ("agency", "agency"))}
        extras.update(model_type="qwen" if vendor == "qianwen" else vendor, server_url=settings.ai_url.rsplit("/ai", 1)[0])
        if vendor == "ollama" and extras["base_url"]:
            extras["api_key"] = extras["api_key"] or uuid4().hex[:6]
        if not extras["api_key"]:
            raise ApiError("机器人未启用")
        extras["model_name"] = msg["msg"].get("model_name") or extras["model_name"]
        if config.get(vendor + "_temperature"):
            extras["temperature"] = float(config[vendor + "_temperature"])
        if dialog["session_id"]:
            extras["context_key"] = "session_" + str(dialog["session_id"])
        if dialog["type"] == "group":
            extras["before_clear"] = 1
        configs = table("web_socket_dialog_configs")
        custom = connection.execute(select(configs.c.value).where(configs.c.dialog_id == dialog["id"], configs.c.userid == msg["userid"], configs.c.type == "ai_prompt").limit(1)).scalar_one_or_none()
        prompt = custom or extras["system_message"]
        extras["system_message"] = str(prompt) + f"\n您是：{bot['nickname']}（ID: {bot_id}）\n当前对话ID(dialog_id)：{dialog['id']}\n当前系统时间(now)：{utcnow()}"
        url = settings.ai_url.rstrip("/") + "/chat"
    else:
        user_bot = connection.execute(select(bots).where(bots.c.bot_id == bot_id).limit(1)).mappings().first()
        if not user_bot or "message" not in decode(user_bot["webhook_events"], ["message"]) or not user_bot["webhook_url"]:
            return
        url = user_bot["webhook_url"]
    reply = get_message(connection, msg["reply_id"])["msg"].get("text", "") if msg["reply_id"] else None
    sender = connection.execute(select(users).where(users.c.userid == msg["userid"])).mappings().one()
    body = {"event": "message", "text": text, "reply_text": reply, "token": _short_token(connection, bot_id, settings),
        "session_id": dialog["session_id"], "dialog_id": dialog["id"], "dialog_type": dialog["type"], "msg_id": msg["id"],
        "msg_uid": msg["userid"], "mention": mention, "bot_uid": bot_id, "extras": encode(extras), "version": settings.version,
        "timestamp": int(time.time()), "msg_user": {key: sender[key] for key in ("userid", "email", "nickname", "profession", "lang")}}
    body["msg_user"]["token"] = _short_token(connection, msg["userid"], settings)
    # Token rows commit before the HTTP callback can use them.
    jobs.enqueue(connection, "dialog.webhook", {"url": url, "body": body, "bot_id": bot_id, "dialog_id": dialog["id"],
        "user_bot_id": user_bot["id"] if user_bot else None}, dedupe_key=f"webhook:{bot_id}:{msg['id']}")


def webhook(connection, payload, settings):
    get_dialog(connection, payload["dialog_id"], payload["bot_id"])
    url = payload["url"]
    if not re.match(r"^https?://", url):
        raise ApiError("Webhook地址无效")
    # Source Ihttp::ihttp_post uses form fields, nested user encoded as JSON here.
    body = {key: encode(value) if isinstance(value, (dict, list)) else value for key, value in payload["body"].items()}
    request = Request(url, data=urlencode(body).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Idempotency-Key": str(payload["_event_id"])})
    with urlopen(request, timeout=30) as response:
        raw = response.read(2 * 1024 * 1024)
    result = json.loads(raw) if raw else {}
    if payload.get("user_bot_id"):
        bots = table("user_bots")
        connection.execute(bots.update().where(bots.c.id == payload["user_bot_id"]).values(webhook_num=bots.c.webhook_num + 1))
    if result.get("code") == 200 and result.get("message"):
        send_message(connection, dialog_id=payload["dialog_id"], sender_id=payload["bot_id"], msg_type="text", msg={"text": str(result["message"])}, silence=True)
    elif result.get("code") not in (None, 200):
        raise ApiError(result.get("error") or "机器人Webhook失败", data=result)


def member_hook(connection, payload, settings):
    bots, members = table("user_bots"), table("web_socket_dialog_users")
    dialog = get_dialog(connection, payload["dialog_id"])
    rows = connection.execute(select(bots).join(members, members.c.userid == bots.c.bot_id).where(members.c.dialog_id == dialog["id"])).mappings()
    for row in rows:
        if payload["event"] not in decode(row["webhook_events"], ["message"]) or not row["webhook_url"]:
            continue
        body = {"event": payload["event"], "dialog_id": dialog["id"], "dialog_type": dialog["type"], "userid": payload["userid"],
            "userids": payload.get("userids", [payload["userid"]]), "bot_uid": row["bot_id"], "token": _short_token(connection, row["bot_id"], settings),
            "timestamp": int(time.time()), "version": settings.version}
        jobs.enqueue(connection, "dialog.webhook", {"url": row["webhook_url"], "body": body, "bot_id": row["bot_id"], "dialog_id": dialog["id"], "user_bot_id": row["id"]})


def todo_remind(connection, payload, settings):
    from .dialog import _bot
    todos = table("web_socket_dialog_msg_todos")
    rows = connection.execute(select(todos).where(todos.c.done_at.is_(None), todos.c.reminded_at.is_(None), todos.c.remind_at <= utcnow())
        .order_by(todos.c.id).limit(500).with_for_update(skip_locked=True)).mappings().all()
    groups = defaultdict(list)
    for row in rows:
        groups[row["msg_id"]].append(row)
    for msg_id, group in groups.items():
        rowids = [r["id"] for r in group]
        try:
            msg = get_message(connection, msg_id)
        except ApiError:
            connection.execute(todos.update().where(todos.c.id.in_(rowids)).values(reminded_at=utcnow()))
            continue
        recipients = sorted(set(r["userid"] for r in group) & set(member_ids(connection, msg["dialog_id"])))
        if recipients:
            bot = _bot(connection, "todo-alert")
            users = table("users")
            names = dict(connection.execute(select(users.c.userid, users.c.nickname).where(users.c.userid.in_(recipients))).all())
            text = " ".join(f'<span class="mention user" data-id="{id}">@{html.escape(names[id])}</span>' for id in recipients) + " 你有一条待办到提醒时间啦"
            send_message(connection, dialog_id=msg["dialog_id"], sender_id=bot["userid"], msg_type="text", msg={"text": text}, reply_id=msg_id, silence=False, trusted_sender=True)
        connection.execute(todos.update().where(todos.c.id.in_(rowids)).values(reminded_at=utcnow()))


def session_title(connection, payload, settings):
    from .messaging_ai import invoke
    sessions = table("web_socket_dialog_sessions")
    row = connection.execute(select(sessions).where(sessions.c.id == payload["session_id"])).mappings().first()
    if not row or not payload["text"]:
        return
    configs = table("web_socket_dialog_configs")
    if connection.execute(select(configs.c.id).where(configs.c.dialog_id == row["dialog_id"], configs.c.type == "session_title:" + str(row["id"])).limit(1)).first():
        return
    title = invoke(connection, settings, [{"role": "system", "content": "为项目任务管理系统对话生成精准简洁标题，只返回标题。"}, {"role": "user", "content": payload["text"]}])[:100]
    connection.execute(sessions.update().where(sessions.c.id == row["id"]).values(title=title, updated_at=utcnow()))


def delete_bot_messages(connection, payload, settings):
    from .project_tasks import sync_message_relations
    bots, messages = table("user_bots"), table("web_socket_dialog_msgs")
    due = connection.execute(select(bots).where(bots.c.clear_day > 0, bots.c.clear_at <= utcnow()).limit(100).with_for_update(skip_locked=True)).mappings().all()
    for bot in due:
        rows = connection.execute(select(messages).where(messages.c.userid == bot["bot_id"], messages.c.deleted_at.is_(None),
            messages.c.created_at <= utcnow() - timedelta(days=bot["clear_day"]))).mappings().all()
        for row in rows:
            msg = core.message_data(row)
            connection.execute(messages.update().where(messages.c.id == msg["id"]).values(deleted_at=utcnow(), updated_at=utcnow()))
            for name in ("web_socket_dialog_msg_reads", "web_socket_dialog_msg_todos", "web_socket_dialog_msg_attachments"):
                child = table(name)
                connection.execute(child.delete().where(child.c.msg_id == msg["id"]))
            sync_message_relations(connection, {**msg, "deleted_at": utcnow()})
            core.push_dialog(connection, msg["dialog_id"], "delete", {"id": msg["id"], "dialog_id": msg["dialog_id"]})
            jobs.enqueue(connection, "search.sync", {"type": "message", "id": msg["id"]})
        connection.execute(bots.update().where(bots.c.id == bot["id"]).values(clear_at=utcnow() + timedelta(days=bot["clear_day"])))


def maintenance(connection, payload, settings):
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    sockets = table("web_sockets")
    try:
        for row in connection.execute(select(sockets)).mappings():
            if not client.exists(f"dootask:connection:{row['fd']}"):
                connection.execute(sockets.delete().where(sockets.c.id == row["id"]))
                client.srem(f"dootask:user:{row['userid']}", row["fd"])
    finally:
        client.close()
    for name, field in (("backend_operations", "expires_at"), ("backend_deliveries", "expires_at")):
        target = table(name)
        connection.execute(target.delete().where(target.c[field] < utcnow()))
    outbox = table("backend_outbox")
    connection.execute(outbox.delete().where(outbox.c.completed_at < utcnow() - timedelta(days=7)))


def mobile_push(connection, payload, settings):
    from .messaging_push import push_to_users
    config = db.setting(connection, "appPushSetting") or {}
    if config.get("push") != "open":
        return
    msg = get_message(connection, payload["msg_id"])
    reads = table("web_socket_dialog_msg_reads")
    pending = list(connection.execute(select(reads.c.userid).where(reads.c.msg_id == msg["id"], reads.c.userid.in_(payload["userids"]), reads.c.read_at.is_(None), reads.c.silence == 0)).scalars())
    direct, delayed = [], []
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        for userid in pending:
            if not payload.get("delayed") and client.exists(f"dootask:pc-active:{userid}"):
                delayed.append(userid)
            else:
                direct.append(userid)
    finally:
        client.close()
    if delayed:
        jobs.enqueue(connection, "dialog.mobile_push", {"msg_id": msg["id"], "userids": delayed, "delayed": True}, available_at=utcnow() + timedelta(seconds=10))
    if direct:
        from .account_history import preview_message
        users = table("users")
        nickname = connection.execute(select(users.c.nickname).where(users.c.userid == msg["userid"])).scalar_one_or_none() or "AI 助手"
        dialog = get_dialog(connection, msg["dialog_id"])
        body = preview_message(msg)
        title = nickname
        if dialog["type"] == "group":
            body, title = nickname + ": " + body, dialog["name"]
        push_to_users(connection, direct, {"title": title, "body": body, "description": f"MID:{msg['id']}", "seconds": 3600,
            "extra": {"dialog_id": msg["dialog_id"], "msg_id": msg["id"]}}, settings)


def email_notice(connection, payload, settings):
    config = db.setting(connection, "emailSetting") or {}
    if config.get("notice_msg") != "open":
        return
    ranges = config.get("msg_unread_time_ranges") or []
    current = utcnow().strftime("%H:%M")
    if ranges and not isinstance(ranges[0], (list, tuple)):
        ranges = [ranges]
    if not any(isinstance(item, (list, tuple)) and len(item) == 2 and item[0] <= current <= item[1] for item in ranges):
        return
    messages, reads, users = table("web_socket_dialog_msgs"), table("web_socket_dialog_msg_reads"), table("users")
    from .account_history import preview_message
    from .account_integrations import send_mail
    for kind in ("user", "group"):
        minute = int(config.get("msg_unread_" + kind + "_minute", -1))
        if minute < 0:
            continue
        rows = connection.execute(select(messages, reads.c.id.label("read_id"), reads.c.userid.label("recipient"))
            .join(reads, reads.c.msg_id == messages.c.id).where(messages.c.dialog_type == kind,
                messages.c.type.in_(("text", "file", "record", "meeting")), messages.c.deleted_at.is_(None),
                messages.c.created_at <= utcnow() - timedelta(minutes=minute), reads.c.read_at.is_(None), reads.c.silence == 0, reads.c.email == 0)
            .order_by(reads.c.userid, messages.c.created_at).limit(1000).with_for_update(skip_locked=True)).mappings().all()
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["recipient"]].append(row)
        for userid, items in grouped.items():
            recipient = connection.execute(select(users).where(users.c.userid == userid, users.c.disable_at.is_(None), users.c.bot == 0)).mappings().first()
            if not recipient:
                continue
            allowed = []
            for item in items[:100]:
                try:
                    get_dialog(connection, item["dialog_id"], userid)
                except ApiError:
                    continue
                allowed.append(item)
            if not allowed:
                continue
            content = "<h2>" + html.escape(recipient["nickname"] or recipient["email"]) + "，您好。</h2><p>您有未读消息，请及时处理。</p>"
            for item in allowed:
                content += "<p>" + html.escape(preview_message(core.message_data(item))) + "</p>"
            send_mail(config, recipient["email"], "未读消息提醒", content, settings.system_alias)
            connection.execute(reads.update().where(reads.c.id.in_([item["read_id"] for item in allowed])).values(email=1))


def joke_soup(connection, payload, settings):
    from .messaging_ai import invoke
    from .apps import installed_config
    if not installed_config(settings, "ai"):
        return
    content = invoke(connection, settings, [{"role": "system", "content": '生成适合团队协作环境的笑话与心灵鸡汤，返回JSON对象：{"jokes":[字符串],"soups":[字符串]}。不要Markdown标记。'},
        {"role": "user", "content": "各生成5条简短、不重复的内容。"}])
    result = json.loads(content)
    if not all(isinstance(result.get(key), list) and all(isinstance(item, str) for item in result[key]) for key in ("jokes", "soups")):
        raise ApiError("AI 返回笑话和心灵鸡汤格式错误")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        for key in ("jokes", "soups"):
            name = "JokeSoupTask-v2:" + key
            values = decode(client.get(name), [])
            client.set(name, encode((values + result[key])[-200:]))
    finally:
        client.close()


def register_jobs():
    for name, handler in (("dialog.message_effects", message_effects), ("dialog.bot_prepare", bot_prepare),
        ("dialog.webhook", webhook), ("dialog.member_hook", member_hook), ("dialog.todo_remind", todo_remind),
        ("dialog.session_title", session_title), ("dialog.delete_bot_messages", delete_bot_messages),
        ("dialog.maintenance", maintenance), ("dialog.mobile_push", mobile_push),
        ("dialog.email_notice", email_notice), ("dialog.joke_soup", joke_soup)):
        jobs.register(name, handler)
    jobs.schedule("dialog.todo_remind", 60)
    jobs.schedule("dialog.delete_bot_messages", 3600)
    jobs.schedule("dialog.maintenance", 60)
    jobs.schedule("dialog.email_notice", 60)
    jobs.schedule("dialog.joke_soup", 3600)
