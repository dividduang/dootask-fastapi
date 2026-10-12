"""Transactional jobs. Delivery is at least once; handlers must be idempotent.

All domain handlers receive the worker's SQLAlchemy Connection. Never commit it.
Registration is explicit at startup; unknown kinds fail rather than disappear.
"""
import asyncio
import json
import logging
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, or_
from sqlalchemy.dialects.mysql import insert
from . import db
from .context import utcnow
from .errors import json_default

log = logging.getLogger(__name__)
HANDLERS = {}
SCHEDULES = {}


def register(kind, handler):
    if kind in HANDLERS and HANDLERS[kind] is not handler:
        raise ValueError(f"Duplicate job handler: {kind}")
    HANDLERS[kind] = handler


def schedule(kind, seconds, payload=None):
    if kind not in HANDLERS or seconds < 1:
        raise ValueError("Schedule requires a registered handler and positive interval")
    SCHEDULES[kind] = (int(seconds), payload or {})


def enqueue(connection, kind, payload, *, dedupe_key=None, available_at=None):
    if not connection.in_transaction():
        raise RuntimeError("Outbox requires the business transaction")
    table = db.tables["backend_outbox"]
    values = dict(kind=kind, payload=json.dumps(payload, ensure_ascii=False, default=json_default),
                  dedupe_key=dedupe_key, available_at=available_at or utcnow(), created_at=utcnow())
    statement = insert(table).values(**values)
    if dedupe_key:
        connection.execute(statement.on_duplicate_key_update(id=table.c.id))
        return connection.execute(select(table.c.id).where(table.c.dedupe_key == dedupe_key)).scalar_one()
    return connection.execute(statement).inserted_primary_key[0]


def enqueue_push(connection, *, userids=(), fds=(), ignore_fds=(), message, scope=None):
    if not isinstance(message, dict) or not message.get("type"):
        raise ValueError("Push requires a source-compatible type/data message")
    users = sorted(set(int(v) for v in userids if int(v) > 0))
    targets = sorted(set(str(v) for v in fds if str(v)))
    if not users and not targets:
        raise ValueError("Push requires authorized recipients")
    return enqueue(connection, "realtime.push", dict(userids=users, fds=targets,
                   ignore_fds=[str(v) for v in ignore_fds if str(v)], message=message, scope=scope))


def emit(connection, userids, type, data, *, ignore_fds=(), scope=None):
    return enqueue_push(connection, userids=userids, ignore_fds=ignore_fds,
                        message={"type": type, "data": data}, scope=scope)


def _push(connection, payload, settings):
    # Store recipients before Redis wakeup. Reconnect reads these same durable rows.
    import redis
    event_id = payload["_event_id"]
    deliveries = db.tables["backend_deliveries"]
    recipients = []
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                 socket_connect_timeout=5, socket_timeout=5)
    try:
        for userid in payload["userids"]:
            live = [str(fd) for fd in client.smembers(f"dootask:user:{userid}")
                    if client.exists(f"dootask:connection:{fd}") and str(fd) not in payload["ignore_fds"]]
            recipients.extend((userid, fd) for fd in live)
            if not live:
                recipients.append((userid, ""))
        for fd in payload["fds"]:
            owner = client.hget(f"dootask:connection:{fd}", "userid")
            if owner:
                recipients.append((int(owner), fd))
        for userid, fd in set(recipients):
            statement = insert(deliveries).values(event_id=event_id, userid=userid, fd=fd,
                reconnect=int(userid in payload["userids"]),
                msg_id=uuid4().hex, payload=json.dumps(payload["message"], ensure_ascii=False, default=json_default),
                scope=json.dumps(payload.get("scope")), ignore_fds=json.dumps(payload["ignore_fds"]),
                expires_at=utcnow() + timedelta(days=1), created_at=utcnow())
            connection.execute(statement.on_duplicate_key_update(id=deliveries.c.id))
        # A wakeup is only a hint: sockets poll the committed durable rows as well.
        client.publish("dootask:deliveries", json.dumps([u for u, _ in recipients]))
    finally:
        client.close()


def run_once(settings):
    table = db.tables["backend_outbox"]
    with db.transaction() as connection:
        row = connection.execute(select(table).where(table.c.completed_at.is_(None),
            table.c.available_at <= utcnow()).order_by(table.c.id).limit(1)
            .with_for_update(skip_locked=True)).mappings().first()
        if not row:
            return False
        try:
            # Roll back *all* handler writes on failure while preserving failure evidence.
            with connection.begin_nested():
                handler = HANDLERS.get(row["kind"])
                if handler is None:
                    raise RuntimeError(f"Unregistered job kind: {row['kind']}")
                payload = json.loads(row["payload"])
                payload["_event_id"] = row["id"]
                handler(connection, payload, settings)
            connection.execute(table.update().where(table.c.id == row["id"]).values(
                completed_at=utcnow(), attempts=row["attempts"] + 1, last_error=None))
        except Exception as exc:
            attempts = row["attempts"] + 1
            connection.execute(table.update().where(table.c.id == row["id"]).values(
                attempts=attempts, last_error=f"{type(exc).__name__}: {exc}"[:65535],
                available_at=utcnow() + timedelta(seconds=min(3600, 2 ** min(attempts, 12)))))
            log.exception("Outbox job %s (%s) failed", row["id"], row["kind"])
    return True


def tick():
    now = utcnow()
    epoch = int(now.timestamp())
    with db.transaction() as connection:
        for kind, (seconds, payload) in SCHEDULES.items():
            enqueue(connection, kind, payload, dedupe_key=f"schedule:{kind}:{epoch // seconds}")


async def worker(settings, stop):
    # One worker per ASGI process is safe: row locks/SKIP LOCKED arbitrate jobs.
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
            busy = await asyncio.to_thread(run_once, settings)
        except Exception:
            log.exception("Outbox worker database failure")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                continue


register("realtime.push", _push)
