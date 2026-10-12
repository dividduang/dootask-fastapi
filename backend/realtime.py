"""Cross-worker websocket transport backed by durable MariaDB deliveries and Redis."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
import re
from uuid import uuid4
import redis.asyncio as redis
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from . import auth, crypto, db, jobs
from .context import RequestContext, utcnow
from .errors import ApiError, json_default

log = logging.getLogger(__name__)


SCOPE_CHECKS = {}


def register_scope(name, checker):
    if name in SCOPE_CHECKS and SCOPE_CHECKS[name] is not checker:
        raise ValueError(f"Duplicate delivery scope: {name}")
    SCOPE_CHECKS[name] = checker


def _authenticate(context):
    context.actor = None
    with db.transaction() as connection:
        actor = auth.authenticate(connection, context)
        if actor.session.get('purpose', 'interactive') != 'interactive':
            raise ApiError('服务Token不能建立交互连接', -1)
        return actor.userid


def _authorized(connection, userid, scope):
    if not scope:
        return True
    if "dialog_id" in scope:
        from .domains.dialog_core import can_view_dialog
        dialogs = db.tables["web_socket_dialogs"]
        row = connection.execute(select(dialogs).where(dialogs.c.id == int(scope["dialog_id"]))).mappings().first()
        return bool(row and can_view_dialog(connection, userid, row))
    if len(scope) == 1:
        name, resource_id = next(iter(scope.items()))
        if name in SCOPE_CHECKS:
            return bool(SCOPE_CHECKS[name](connection, userid, int(resource_id)))
    raise ValueError("Unsupported delivery authorization scope")


def _pending(userid, fd):
    table = db.tables["backend_deliveries"]
    with db.transaction() as connection:
        rows = connection.execute(select(table).where(table.c.userid == userid,
            table.c.transport_sent_at.is_(None), table.c.discarded_at.is_(None), table.c.expires_at > utcnow(),
            table.c.fd == fd).order_by(table.c.id).limit(200)).mappings().all()
        pending = []
        for row in rows:
            ignored = fd in json.loads(row['ignore_fds'])
            if ignored or not _authorized(connection, userid, json.loads(row['scope'] or 'null')):
                connection.execute(table.update().where(table.c.id == row['id']).values(
                    discarded_at=utcnow(), discard_reason='ignored_fd' if ignored else 'scope_revoked'))
            else:
                pending.append(dict(row))
        return pending


def _reconnect(userid, fd, settings):
    import redis as sync_redis
    table = db.tables["backend_deliveries"]
    client = sync_redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        with db.transaction() as connection:
            rows = connection.execute(select(table).where(table.c.userid == userid, table.c.reconnect == 1,
                table.c.transport_sent_at.is_(None), table.c.discarded_at.is_(None), table.c.expires_at > utcnow()).with_for_update()).mappings().all()
            # A previous fd can fail after this fd already sent the same event.
            # Include sent/discarded rows so recipient uniqueness never replays it.
            claimed = set(connection.execute(select(table.c.event_id).where(
                table.c.userid == userid, table.c.fd == fd)).scalars())
            for row in rows:
                if row["fd"] == fd or row["fd"] and client.exists(f"dootask:connection:{row['fd']}"):
                    continue
                if row["event_id"] in claimed:
                    connection.execute(table.delete().where(table.c.id == row["id"]))
                else:
                    connection.execute(table.update().where(table.c.id == row["id"]).values(fd=fd))
                    claimed.add(row["event_id"])
    finally:
        client.close()


def _transport_sent(userid, fd, msg_id):
    # Source PushTask completes at socket push, not at an application receipt.
    table = db.tables["backend_deliveries"]
    with db.transaction() as connection:
        connection.execute(table.update().where(table.c.userid == userid, table.c.fd == fd,
            table.c.msg_id == msg_id, table.c.transport_sent_at.is_(None)).values(transport_sent_at=utcnow()))


def _receipt(userid, fd, msg_id):
    if not isinstance(msg_id, str) or not msg_id:
        return
    table = db.tables["backend_deliveries"]
    with db.transaction() as connection:
        connection.execute(table.update().where(table.c.userid == userid, table.c.fd == fd,
            table.c.msg_id == str(msg_id), table.c.acknowledged_at.is_(None)).values(acknowledged_at=utcnow()))


def _connection_row(userid, fd, platform, path=None, remove=False):
    table = db.tables["web_sockets"]
    with db.transaction() as connection:
        if remove:
            connection.execute(table.delete().where(table.c.fd == fd, table.c.userid == userid))
        elif path is not None:
            connection.execute(table.update().where(table.c.fd == fd, table.c.userid == userid)
                               .values(path=path, updated_at=utcnow()))
        else:
            db.insert_row(connection, "web_sockets", key=fd, fd=fd, userid=userid, platform=platform)


def _line(userid, online):
    sockets = db.tables["web_sockets"]
    with db.transaction() as connection:
        fds = list(connection.execute(select(sockets.c.fd).where(sockets.c.userid != userid)).scalars())
        if fds:
            jobs.enqueue_push(connection, fds=fds, message={"type": "line", "data": {"userid": userid, "online": online}})


def _operation_result(userid, fd, data):
    table = db.tables["backend_operations"]
    with db.transaction() as connection:
        row = connection.execute(select(table).where(table.c.request_id == str(data.get("requestId", "")),
            table.c.userid == userid, table.c.fd == fd, table.c.expires_at > utcnow()).with_for_update()).mappings().first()
        if not row:
            raise ApiError("操作请求不存在或已过期")
        connection.execute(table.update().where(table.c.request_id == row["request_id"]).values(
            result=json.dumps({"userid": userid, "success": bool(data.get("success")),
                "result": data.get("result"), "error": data.get("error")}, ensure_ascii=False)))


async def online_users(client, userids):
    result = []
    for userid in userids:
        fds = await client.smembers(f"dootask:user:{int(userid)}")
        live = False
        for fd in fds:
            if await client.exists(f"dootask:connection:{fd}"):
                live = True
            else:
                await client.srem(f"dootask:user:{int(userid)}", fd)
        if live:
            result.append(int(userid))
    return result


async def socket_endpoint(socket: WebSocket, settings):
    await socket.accept()
    if socket.query_params.get("action") != "web":
        await socket.close(code=1008)
        return
    context = RequestContext(request_id=uuid4().hex, token=socket.query_params.get("token", ""),
        language=socket.query_params.get("language", "zh"), platform=socket.query_params.get("platform", "web"),
        ip=socket.client.host if socket.client else "")
    try:
        userid = await asyncio.to_thread(_authenticate, context)
    except ApiError as exc:
        await socket.send_json({"type": "error", "data": {"error": exc.message}})
        await socket.close(code=1008)
        return
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=5)
    fd = str(await client.incr("dootask:fd-sequence"))
    channel = client.pubsub()
    key = f"dootask:connection:{fd}"
    user_key = f"dootask:user:{userid}"
    encrypt_key = None
    send_lock = asyncio.Lock()
    path = ""

    async def send(message):
        if encrypt_key and message.get("type") == "dialog":
            value = await asyncio.to_thread(crypto.encrypt_response, message, encrypt_key)
            message = {"type": "encrypt", "encrypted": value["encrypted"]}
        async with send_lock:
            await socket.send_text(json.dumps(message, ensure_ascii=False, default=json_default))

    async def path_push(value):
        if not re.fullmatch(r"/single/file/\d+", value or ""):
            return
        peers = []
        async for peer_key in client.scan_iter("dootask:connection:*"):
            peer = await client.hgetall(peer_key)
            if peer.get("path") == value:
                peers.append(int(peer["userid"]))
        if peers:
            def persist():
                with db.transaction() as connection:
                    jobs.emit(connection, peers, "path", {"path": value, "userids": sorted(set(peers))})
            await asyncio.to_thread(persist)

    async def delivery_loop():
        while True:
            # Auth is rechecked after revocation/disable, not just at connection establishment.
            await asyncio.to_thread(_authenticate, context)
            await client.expire(key, 90)
            await asyncio.to_thread(_reconnect, userid, fd, settings)
            for row in await asyncio.to_thread(_pending, userid, fd):
                message = json.loads(row["payload"])
                message["msgId"] = row["msg_id"]
                await send(message)
                # Only successful ASGI send completion retires transport work. A send
                # or DB failure closes this socket; unsent rows survive reconnection.
                # Crash between send and this commit can duplicate, never fake ACK.
                await asyncio.to_thread(_transport_sent, userid, fd, row["msg_id"])
            await channel.get_message(ignore_subscribe_messages=True, timeout=2)

    task = None
    try:
        await client.hset(key, mapping={"userid": userid, "fd": fd, "platform": context.platform, "path": ""})
        await client.expire(key, 90)
        await client.sadd(user_key, fd)
        await channel.subscribe("dootask:deliveries")
        await asyncio.to_thread(_connection_row, userid, fd, context.platform)
        await send({"type": "open", "data": {"fd": int(fd), "ud": userid}})
        await asyncio.to_thread(_line, userid, True)
        await asyncio.to_thread(_reconnect, userid, fd, settings)
        task = asyncio.create_task(delivery_loop())
        while True:
            receive = asyncio.create_task(socket.receive_json())
            done, _ = await asyncio.wait({receive, task}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                receive.cancel()
                with suppress(asyncio.CancelledError):
                    await receive
                await task
            message = receive.result()
            if not isinstance(message, dict):
                raise ApiError("无效的消息格式")
            kind, data = message.get("type"), message.get("data") or {}
            msgid = message.get("msgId", message.get("msg_id"))
            if kind == "receipt":
                await asyncio.to_thread(_receipt, userid, fd, msgid)
                continue
            if kind == "handshake":
                await client.expire(key, 90)
                if context.platform in ("web", "win", "mac"):
                    await client.set(f"dootask:pc-active:{userid}", "1", ex=60)
            elif kind == "path":
                previous, path = path, str(data.get("path", ""))[:255]
                await client.hset(key, "path", path)
                await asyncio.to_thread(_connection_row, userid, fd, context.platform, path)
                await path_push(previous)
                await path_push(path)
                continue
            elif kind == "encrypt":
                if data.get("type") != "pgp":
                    raise ApiError("不支持的加密方式")
                encrypt_key = crypto.public_key_format(data.get("key", ""))
                # Parse/encrypt now, reject invalid material before later delivery.
                await asyncio.to_thread(crypto.encrypt_response, {"type": "handshake"}, encrypt_key)
                continue
            elif kind == "operationResult":
                await asyncio.to_thread(_operation_result, userid, fd, data)
                continue
            else:
                raise ApiError("不支持的消息类型")
            if msgid:
                await send({"type": "receipt", "msgId": msgid, "data": []})
    except WebSocketDisconnect:
        log.debug("Websocket disconnected: %s", fd)
    except Exception:
        log.exception("Websocket closed after transport/auth failure: %s", fd)
        with suppress(RuntimeError):
            await socket.close(code=1011)
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await client.delete(key)
            await client.srem(user_key, fd)
            await asyncio.to_thread(_connection_row, userid, fd, context.platform, remove=True)
            if not await online_users(client, [userid]):
                await asyncio.to_thread(_line, userid, False)
            await path_push(path)
        finally:
            await channel.aclose()
            await client.aclose()


def install_realtime(app, settings):
    async def endpoint(socket: WebSocket):
        await socket_endpoint(socket, settings)
    app.add_api_websocket_route("/ws", endpoint)


@asynccontextmanager
async def job_lifecycle(settings):
    stop = asyncio.Event()
    task = asyncio.create_task(jobs.worker(settings, stop))
    try:
        yield
    finally:
        stop.set()
        await task
