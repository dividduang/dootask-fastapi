"""Cross-protocol write locks: cabinet writes cannot evade DAV locks."""
from sqlalchemy import select
from ..context import utcnow
from ..db import tables
from ..errors import ApiError


def serialize_namespace(connection):
    # ponytail: global namespace row; shard by immutable owner only after measuring contention.
    t = tables['backend_file_mutex']
    connection.execute(select(t.c.id).where(t.c.id == 1).with_for_update()).scalar_one()


def enforce(connection, context, item, *, subtree=False):
    from .files import ancestors, descendants
    serialize_namespace(connection)
    ids = {item['id']}
    parents = {p['id'] for p in ancestors(connection, item)}
    if subtree:
        ids.update(p['id'] for p in descendants(connection, item))
    t = tables['webdav_locks']
    tokens = set(context.pgp.get('dav_tokens', []))
    grouped = {}
    for lock in connection.execute(select(t).where(t.c.timeout_at > utcnow(), t.c.file_id.in_(ids | parents))).mappings():
        if lock['file_id'] in ids or lock['depth'] == 'infinity':
            grouped.setdefault(lock['file_id'], []).append(lock)
    for locks in grouped.values():
        applicable = [lock for lock in locks if lock['scope'] == 'exclusive'] or locks
        if not any(lock['token'] in tokens and lock['userid'] == context.actor.userid for lock in applicable):
            raise ApiError('文件已被WebDAV锁定', data={'http_status': 423})
