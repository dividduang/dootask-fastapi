from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from sqlalchemy import select, update
from ..auth import authenticate
from ..context import utcnow
from ..db import tables, insert_row
from ..errors import ApiError, json_default
from ..storage import confined_path


def actor(connection, context):
    return context.actor or authenticate(connection, context)


def integer(value, default=0):
    try:
        return int(value if value not in (None, '') else default)
    except (TypeError, ValueError, OverflowError):
        raise ApiError('参数错误')


def decoded(value, default=None):
    if value is None or value == '':
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise ApiError('JSON参数错误')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=json_default)


def row(connection, name, ident, *, key='id', lock=False):
    table = tables[name]
    query = select(table).where(table.c[key] == ident)
    if 'deleted_at' in table.c:
        query = query.where(table.c.deleted_at.is_(None))
    if lock:
        query = query.with_for_update()
    result = connection.execute(query).mappings().first()
    return dict(result) if result else None


def change(connection, table_name, ident, **values):
    table = tables[table_name]
    if 'updated_at' in table.c:
        values['updated_at'] = utcnow()
    connection.execute(update(table).where(table.c.id == ident).values(**values))


def save_setting(connection, name, value):
    table = tables['settings']
    ident = connection.execute(select(table.c.id).where(table.c.name == name).with_for_update()).scalar_one_or_none()
    if ident is None:
        insert_row(connection, 'settings', name=name, setting=encoded(value))
    else:
        change(connection, 'settings', ident, setting=encoded(value))


def page(connection, query, data, *, maximum=100, default=20):
    from sqlalchemy import func
    size = max(1, min(maximum, integer(data.get('pagesize'), default)))
    number = max(1, integer(data.get('page'), 1))
    total = connection.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    rows = [dict(r) for r in connection.execute(query.limit(size).offset((number - 1) * size)).mappings()]
    return {'data': rows, 'current_page': number, 'per_page': size, 'total': total,
            'last_page': max(1, (total + size - 1) // size),
            'from': (number - 1) * size + 1 if rows else None,
            'to': (number - 1) * size + len(rows) if rows else None}


def safe_name(value, minimum=1):
    name = str(value or '').strip()
    if not minimum <= len(name) <= 255 or name in ('.', '..') or any(ord(c) < 32 or c in '\\/:*?"<>|' for c in name):
        raise ApiError('文件名称不合法')
    return name


def store_stream(settings, stream, *, maximum=10 * 1024**3, expected_hash=None):
    """Durable immutable blobs precede DB references; rollback leaves only unreachable blobs."""
    root = settings.storage_root
    staging = confined_path(root, 'staging')
    staging.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=staging)
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    try:
        with os.fdopen(fd, 'wb') as output:
            while block := stream.read(1024 * 1024):
                size += len(block)
                if size > maximum:
                    raise ApiError('文件大小超限')
                output.write(block)
                sha.update(block)
                md5.update(block)
            output.flush()
            os.fsync(output.fileno())
        digest = md5.hexdigest()
        if expected_hash and digest != expected_hash:
            raise ApiError('文件校验失败，请重试')
        relative = 'blobs/' + sha.hexdigest()[:2] + '/' + sha.hexdigest()
        destination = confined_path(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Publish immutable content without replacing a blob another reader has open.
            os.link(temporary, destination)
        except FileExistsError:
            pass
        return {'url': relative, 'size': size, 'hash': digest, 'sha256': sha.hexdigest()}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def blob_path(settings, content):
    path = confined_path(settings.storage_root, str(decoded(content).get('url', '')))
    if not path.is_file():
        raise ApiError('文件不存在或已被删除')
    return path
