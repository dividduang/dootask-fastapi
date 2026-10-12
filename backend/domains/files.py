"""File cabinet operations. Every mutation shares the caller's DB transaction."""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import secrets
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlsplit
from sqlalchemy import select, or_, func
from starlette.responses import FileResponse, RedirectResponse
from .. import jobs
from ..auth import Actor, require_admin
from ..context import utcnow
from ..db import tables, insert_row, setting
from ..errors import ApiError, success
from ..storage import confined_path
from .files_support import actor, integer, row, change, decoded, encoded, safe_name, store_stream, blob_path, page

OFFICE = {'word', 'excel', 'ppt', 'wps'}
TEXT = {'txt', 'code', 'document', 'drawio', 'mind'}
TYPE_EXT = {'folder': '', 'document': 'md', 'mind': 'mind', 'drawio': 'drawio', 'word': 'docx', 'excel': 'xlsx', 'ppt': 'pptx'}


def get(connection, ident, lock=False):
    result = row(connection, 'files', integer(ident), lock=lock)
    if not result:
        raise ApiError('文件不存在或已被删除')
    return result


def ancestors(connection, item):
    result, seen = [], {item['id']}
    while item['pid']:
        item = get(connection, item['pid'])
        if item['id'] in seen:
            raise ApiError('文件目录存在循环')
        seen.add(item['id'])
        result.append(item)
    return result


def permission(connection, item, userid):
    if userid in (item['userid'], item['created_id']):
        return 1000
    user = row(connection, 'users', userid, key='userid')
    ids = [userid] if user and 'temp' in str(user.get('identity', '')).split(',') else [0, userid]
    share = next((f for f in [item] + ancestors(connection, item) if f['share']), None)
    if share:
        t = tables['file_users']
        value = connection.execute(select(func.max(t.c.permission)).where(t.c.file_id == share['id'], t.c.userid.in_(ids))).scalar_one()
        if value is not None:
            return value
    return -1


def can_access(connection, userid, ident):
    item = row(connection, 'files', integer(ident))
    return bool(item and permission(connection, item, userid) >= 0)


def require(connection, context, ident, level=0, lock=False):
    user = actor(connection, context)
    if lock:
        from .files_locks import serialize_namespace
        serialize_namespace(connection)
    item = get(connection, ident, lock)
    item['permission'] = permission(connection, item, user.userid)
    if item['permission'] < level:
        raise ApiError('没有修改写入权限' if level else '没有查看访问权限')
    if lock:
        from .files_locks import enforce
        enforce(connection, context, item)
    return item


def children(connection, ident):
    t = tables['files']
    return [dict(r) for r in connection.execute(select(t).where(t.c.pid == ident, t.c.deleted_at.is_(None)).order_by(t.c.id)).mappings()]


def descendants(connection, item):
    output, stack, seen = [], [item], set()
    while stack:
        current = stack.pop()
        if current['id'] in seen:
            raise ApiError('文件目录存在循环')
        seen.add(current['id'])
        output.append(current)
        stack.extend(children(connection, current['id']))
    return output


def recipients(connection, item):
    ids = {item['userid'], item['created_id']}
    share = next((f for f in [item] + ancestors(connection, item) if f['share']), None)
    if share:
        t = tables['file_users']
        ids.update(connection.execute(select(t.c.userid).where(t.c.file_id == share['id'])).scalars())
    if 0 in ids:
        t = tables['users']
        ids.update(connection.execute(select(t.c.userid).where(t.c.disable_at.is_(None))).scalars())
    return sorted(u for u in ids if u and can_access(connection, u, item['id']))


def notify(connection, item, action, payload=None, userids=None):
    targets = recipients(connection, item) if userids is None else userids
    if targets:
        jobs.enqueue_push(connection, userids=targets, message={'type': 'file', 'action': action, 'data': payload if payload is not None else item}, scope=None if action == 'delete' else {'file_id': item['id']})
    jobs.enqueue(connection, 'search.sync', {'type': 'file', 'id': item['id']})


def latest(connection, ident, history=0):
    t = tables['file_contents']
    query = select(t).where(t.c.fid == ident, t.c.deleted_at.is_(None))
    if history:
        query = query.where(t.c.id == history)
    value = connection.execute(query.order_by(t.c.id.desc()).limit(1)).mappings().first()
    return dict(value) if value else None


def full_name(item):
    return item['name'] + ('.' + item['ext'] if item['ext'] else '')


def grant(connection, context, settings, item, content=None):
    content = content or latest(connection, item['id'])
    if not content:
        return None
    token = secrets.token_hex(32)
    insert_row(connection, 'backend_file_downloads', id=token, userid=actor(connection, context).userid,
               file_id=item['id'], content_id=content['id'], name=full_name(item), ready=1, expires_at=utcnow() + timedelta(hours=1))
    return context.base_url.rstrip('/') + '/api/file/bytes?key=' + token


def view(connection, context, settings, item):
    item = dict(item)
    if item['type'] == 'picture':
        content = latest(connection, item['id'])
        if content:
            meta = decoded(content['content'])
            item.update(image_url=grant(connection, context, settings, item, content), image_width=integer(meta.get('width')), image_height=integer(meta.get('height')))
    return item


def lists(connection, context, data, settings):
    user = actor(connection, context)
    pid, scope = integer(data.get('pid')), data.get('scope', 'all')
    if pid:
        parent = require(connection, context, pid)
        result = children(connection, pid) + [parent] + ancestors(connection, parent)
    else:
        t = tables['files']
        q = select(t).where(t.c.deleted_at.is_(None), or_(t.c.pid == 0, t.c.share == 1)).order_by(t.c.id).limit(1000)
        result = [dict(r) for r in connection.execute(q).mappings()]
        result = [r for r in result if (r['userid'] == user.userid and r['pid'] == 0 and scope != 'shared') or (r['share'] and scope != 'mine')]
    output = []
    for item in result:
        p = permission(connection, item, user.userid)
        if p < 0:
            continue
        item['permission'] = p
        if item['pshare'] == item['id'] and item['userid'] != user.userid:
            item['pid'] = 0
        output.append(view(connection, context, settings, item))
    return output


def resolve(connection, context, value, level=0):
    if str(value).isdigit():
        return require(connection, context, value, level)
    t = tables['file_links']
    link = connection.execute(select(t).where(t.c.code == str(value))).mappings().first()
    if not link:
        raise ApiError('文件链接不存在')
    item = get(connection, link['file_id'])
    if not item['guest_access']:
        actor(connection, context)
    connection.execute(t.update().where(t.c.id == link['id']).values(num=t.c.num + 1))
    return item


def one(connection, context, data, settings):
    item = resolve(connection, context, data.get('id'), 1 if data.get('with_url') == 'yes' else 0)
    item.setdefault('permission', 0)
    if data.get('with_url') == 'yes':
        # A guest link is already an explicit capability; its content URL retains that capability.
        item['content_url'] = (context.base_url.rstrip('/') + '/api/file/content?' + urlencode({'id': data['id'], 'down': 'yes'})) if context.actor is None else grant(connection, context, settings, item)
    if data.get('with_text') == 'yes':
        from .search import extract_text
        content = latest(connection, item['id'])
        item['text_content'] = extract_text(blob_path(settings, content['content']), item['ext'], integer(data.get('text_offset')), integer(data.get('text_limit'), 50000)) if content else {'content': '', 'total_length': 0, 'offset': 0, 'limit': 50000, 'has_more': False}
    return item


def fetch(connection, context, data, settings):
    from .search import extract_text
    user = actor(connection, context)
    path = str(data.get('path', ''))
    parsed = urlsplit(path)
    if parsed.scheme:
        if parsed.netloc != urlsplit(context.base_url).netloc:
            raise ApiError('没有查看访问权限')
        path = parsed.path.lstrip('/')
    t = tables['file_contents']
    candidates = connection.execute(select(t).where(t.c.deleted_at.is_(None), func.json_unquote(func.json_extract(t.c.content, '$.url')) == path)).mappings()
    for content_row in candidates:
        if can_access(connection, user.userid, content_row['fid']):
            item = get(connection, content_row['fid'])
            return extract_text(blob_path(settings, content_row['content']), item['ext'], integer(data.get('offset')), integer(data.get('limit'), 50000))
    raise ApiError('没有查看访问权限')


def search(connection, context, data, settings):
    user = actor(connection, context)
    t = tables['files']
    key, link = str(data.get('key', '')).strip(), str(data.get('link', '')).strip()
    q = select(t).where(t.c.deleted_at.is_(None))
    if link:
        links = tables['file_links']
        q = q.where(t.c.id.in_(select(links.c.file_id).where(links.c.code == link.rsplit('/', 1)[-1])))
    elif key:
        q = q.where(or_(t.c.name.contains(key), t.c.id == integer(key) if key.isdigit() else False))
    else:
        q = q.where(t.c.userid == user.userid)
    output, take = [], max(1, min(100, integer(data.get('take'), 50)))
    for r in connection.execute(q.order_by((t.c.userid == user.userid).desc(), t.c.id)).mappings():
        item = dict(r)
        if permission(connection, item, user.userid) >= 0:
            if item['pshare'] == item['id'] and item['userid'] != user.userid:
                item['pid'] = 0
            output.append(item)
            if len(output) >= take:
                break
    return output


def parent_for_write(connection, context, pid):
    user = actor(connection, context)
    from .files_locks import serialize_namespace
    serialize_namespace(connection)
    if not pid:
        # Account row is held by authenticate: serializes all own-root namespace changes.
        return user.userid, '', 0
    parent = require(connection, context, pid, 1, True)
    if parent['type'] != 'folder':
        raise ApiError('移动位置错误')
    return parent['userid'], parent['pids'] + str(pid) + ',' if parent['pids'] else ',' + str(pid) + ',', parent['pshare']


def unique_name(connection, pid, userid, name, ext, exclude=0):
    t = tables['files']
    names = set(connection.execute(select(t.c.name).where(t.c.pid == pid, t.c.userid == userid, t.c.ext == ext, t.c.id != exclude, t.c.deleted_at.is_(None))).scalars())
    base, number = name, 2
    while name in names:
        name = f'{base} ({number})'
        number += 1
    return name


def create_file(connection, context, pid, name, kind, ext, *, deduplicate=True):
    user = actor(connection, context)
    owner, pids, pshare = parent_for_write(connection, context, pid)
    t = tables['files']
    count = connection.execute(select(func.count()).select_from(t).where(t.c.pid == pid, t.c.userid == owner, t.c.deleted_at.is_(None))).scalar_one()
    if count >= 300:
        raise ApiError('每个文件夹里最多只能创建300个文件或文件夹')
    if len(pids) > 255:
        raise ApiError('目录层级过深')
    name = safe_name(name)
    if deduplicate:
        name = unique_name(connection, pid, owner, name, ext)
    ident = insert_row(connection, 'files', pid=pid, pids=pids, pshare=pshare, name=name, type=kind, ext=ext, userid=owner, created_id=user.userid)
    return get(connection, ident)


def add(connection, context, data, settings):
    name = safe_name(data.get('name'), 2)
    if len(name) > 100:
        raise ApiError('文件名称最多只能设置100个字')
    if integer(data.get('id')):
        item = require(connection, context, data['id'], 1, True)
        parent_for_write(connection, context, item['pid'])
        name = unique_name(connection, item['pid'], item['userid'], name, item['ext'], item['id'])
        change(connection, 'files', item['id'], name=name)
        notify(connection, get(connection, item['id']), 'update', {'id': item['id'], 'name': name})
        return {'id': item['id'], 'name': name}
    kind = data.get('type')
    if kind not in TYPE_EXT:
        raise ApiError('类型错误')
    item = create_file(connection, context, integer(data.get('pid')), name, kind, TYPE_EXT[kind])
    notify(connection, item, 'add')
    return item


def copy(connection, context, data, settings):
    source = require(connection, context, data.get('id'))
    if source['type'] == 'folder':
        raise ApiError('不支持复制文件夹')
    item = create_file(connection, context, source['pid'], source['name'], source['type'], source['ext'])
    change(connection, 'files', item['id'], cid=source['id'], size=source['size'], hash=source['hash'])
    old = latest(connection, source['id'])
    if old:
        insert_row(connection, 'file_contents', fid=item['id'], content=old['content'], text=old['text'], size=old['size'], userid=actor(connection, context).userid)
    item = get(connection, item['id'])
    notify(connection, item, 'add')
    return item


def selected(data):
    ids = decoded(data.get('ids'), [])
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
        raise ApiError('请选择文件或文件夹（最多100个）')
    return list(dict.fromkeys(integer(i) for i in ids))


def relocate(connection, context, item, pid, *, name=None):
    from .files_locks import enforce
    enforce(connection, context, item, subtree=True)
    owner, pids, pshare = parent_for_write(connection, context, pid)
    branch = descendants(connection, item)
    if pid in {r['id'] for r in branch}:
        raise ApiError('移动位置错误')
    if pshare and any(r['share'] for r in branch):
        raise ApiError('内含有共享文件，无法移动到另一个共享文件夹内')
    old_recipients = recipients(connection, item)
    change(connection, 'files', item['id'], pid=pid, name=unique_name(connection, pid, owner, name or item['name'], item['ext'], item['id']))
    for current in branch:
        current = get(connection, current['id'], True)
        chain = ancestors(connection, current)
        parents = ',' + ','.join(str(p['id']) for p in reversed(chain)) + ',' if chain else ''
        if len(parents) > 255:
            raise ApiError('目录层级过深')
        shared = next((p['id'] for p in [current] + chain if p['share']), 0)
        change(connection, 'files', current['id'], userid=owner, pids=parents, pshare=shared)
        jobs.enqueue(connection, 'search.sync', {'type': 'file', 'id': current['id']})
    item = get(connection, item['id'])
    removed = set(old_recipients) - set(recipients(connection, item))
    if removed:
        notify(connection, item, 'delete', {'id': item['id']}, list(removed))
    notify(connection, item, 'update')
    return item


def move(connection, context, data, settings):
    return [relocate(connection, context, require(connection, context, ident, 1000, True), integer(data.get('pid'))) for ident in selected(data)]


def delete_tree(connection, context, item):
    from .files_locks import enforce
    enforce(connection, context, item, subtree=True)
    for current in reversed(descendants(connection, item)):
        targets = recipients(connection, current)
        change(connection, 'files', current['id'], deleted_at=utcnow())
        links = tables['file_links']
        connection.execute(links.delete().where(links.c.file_id == current['id']))
        notify(connection, current, 'delete', {'id': current['id']}, targets)


def remove(connection, context, data, settings):
    result = []
    for ident in selected(data):
        item = require(connection, context, ident, 1000, True)
        delete_tree(connection, context, item)
        result.append(item)
    return result


def save_blob(connection, context, item, blob, *, text='', extra=None):
    meta = {'type': item['ext'], 'ext': item['ext'], 'url': blob['url'], 'sha256': blob['sha256'], **(extra or {})}
    ident = insert_row(connection, 'file_contents', fid=item['id'], content=encoded(meta), text=text, size=blob['size'], userid=actor(connection, context).userid)
    change(connection, 'files', item['id'], size=blob['size'], hash=blob['hash'])
    notify(connection, get(connection, item['id']), 'content', {'id': item['id']})
    result = row(connection, 'file_contents', ident)
    result['content'] = meta
    return result


def require_editor(settings, kind):
    from .apps import require_installed
    appid = 'office' if kind in OFFICE else 'drawio' if kind == 'drawio' else 'minder' if kind == 'mind' else None
    if appid:
        require_installed(settings, appid)


def content__save(connection, context, data, settings):
    item = require(connection, context, data.get('id'), 1, True)
    require_editor(settings, item['type'])
    if item['type'] not in TEXT:
        raise ApiError('参数错误')
    raw = data.get('content', '')
    values = decoded(raw) if item['type'] != 'mind' else None
    body = raw if item['type'] == 'mind' else values.get('xml' if item['type'] == 'drawio' else 'content', '')
    if not isinstance(body, str):
        body = encoded(body)
    text = ''
    if item['type'] == 'document':
        def replace_image(match):
            from PIL import Image
            try:
                binary = base64.b64decode(match.group(2), validate=True)
                if len(binary) > 20 * 1024**2:
                    raise ApiError('图片大小超限')
                with Image.open(io.BytesIO(binary)) as image:
                    image.verify()
                return match.group(0)
            except (ValueError, OSError) as exc:
                raise ApiError('图片数据无效') from exc
        body = re.sub(r'<img\s+src="data:image/(png|jpg|jpeg|webp);base64,([^" ]+)"', replace_image, body)
        item['ext'] = 'md' if values.get('type') == 'md' else 'text'
        change(connection, 'files', item['id'], ext=item['ext'])
        text = re.sub('<[^>]+>', '', body)
    blob = store_stream(settings, io.BytesIO(body.encode('utf-8')))
    return save_blob(connection, context, item, blob, text=text)


def content(connection, context, data, settings):
    item = resolve(connection, context, data.get('id'), 1 if data.get('down') == 'yes' else 0)
    if data.get('down', 'no') == 'no':
        require_editor(settings, item['type'])
    if data.get('only_update_at') == 'yes':
        return {'id': item['id'], 'update_at': item['updated_at']}
    stored = latest(connection, item['id'], integer(data.get('history_id')))
    if integer(data.get('history_id')) and not stored:
        raise ApiError('历史数据不存在或已被删除')
    path = blob_path(settings, stored['content']) if stored else None
    if path is None and item['type'] in OFFICE:
        path = confined_path(settings.frontend_root / 'public', 'assets/office/empty.' + item['ext'])
    if data.get('down') == 'preview':
        query = urlencode({'key': grant(connection, context, settings, item, stored) if context.actor else str(data['id']), 'id': data['id']})
        return RedirectResponse('/online/preview/' + quote(full_name(item)) + '?' + query, status_code=302)
    if data.get('down') == 'yes' or item['type'] in OFFICE:
        if path is None or not path.is_file():
            raise ApiError('This file is empty.')
        return FileResponse(path, filename=full_name(item))
    if not stored:
        return {'content': {'type': item['ext'], 'content': ''} if item['type'] == 'document' else {}}
    if item['type'] in TEXT and (item['type'] not in ('code', 'txt') or stored['size'] < 2 * 1024**2):
        value = path.read_text(encoding='utf-8', errors='replace')
        if item['type'] == 'mind':
            result = decoded(value)
        elif item['type'] == 'drawio':
            result = {'xml': value}
        else:
            result = {'content': value}
            if item['type'] == 'document':
                result['type'] = item['ext']
        return {'content': result}
    return {'content': {'preview': True, 'name': full_name(item), 'key': grant(connection, context, settings, item) if context.actor else data['id']}}


def content__history(connection, context, data, settings):
    item = require(connection, context, data.get('id'))
    t = tables['file_contents']
    return page(connection, select(t.c.id, t.c.size, t.c.userid, t.c.created_at).where(t.c.fid == item['id'], t.c.deleted_at.is_(None)).order_by(t.c.id.desc()), data)


def content__restore(connection, context, data, settings):
    item = require(connection, context, data.get('id'), 1, True)
    require_editor(settings, item['type'])
    old = latest(connection, item['id'], integer(data.get('history_id')))
    if not old:
        raise ApiError('历史数据不存在或已被删除')
    with blob_path(settings, old['content']).open('rb') as stream:
        blob = store_stream(settings, stream)
    save_blob(connection, context, item, blob, text=old['text'] or '', extra=decoded(old['content']))
    return []


def share(connection, context, data, settings):
    item = require(connection, context, data.get('id'), 1000)
    if item['userid'] != actor(connection, context).userid:
        raise ApiError('仅限所有者操作')
    t = tables['file_users']
    return {'id': item['id'], 'list': [dict(r) for r in connection.execute(select(t).where(t.c.file_id == item['id'])).mappings()]}


def share__update(connection, context, data, settings):
    item = require(connection, context, data.get('id'), 1000, True)
    user = actor(connection, context)
    if item['userid'] != user.userid:
        raise ApiError('仅限所有者操作')
    if any(p['share'] for p in ancestors(connection, item)):
        raise ApiError('此文件已经处于共享文件夹中，无法重复共享')
    value, users = integer(data.get('permission')), decoded(data.get('userids'), [])
    if value not in (-1, 0, 1) or not isinstance(users, list) or not users:
        raise ApiError('请选择共享对象')
    users = sorted(set(integer(u) for u in users))
    branch = descendants(connection, item)
    if value >= 0 and not integer(data.get('force')) and any(f['share'] for f in branch[1:]):
        raise ApiError('此文件夹内已有共享文件夹', ret=-3001)
    t = tables['file_users']
    existing = set(connection.execute(select(t.c.userid).where(t.c.file_id == item['id'])).scalars())
    if value >= 0 and len(existing | set(users)) > 100:
        raise ApiError('共享人数上限100个成员')
    previous = recipients(connection, item)
    for userid in users:
        if userid and not row(connection, 'users', userid, key='userid'):
            raise ApiError('用户不存在')
        connection.execute(t.delete().where(t.c.file_id == item['id'], t.c.userid == userid))
        if value >= 0:
            insert_row(connection, 'file_users', file_id=item['id'], userid=userid, permission=value)
    enabled = connection.execute(select(func.count()).select_from(t).where(t.c.file_id == item['id'])).scalar_one() > 0
    for child in branch:
        if child['id'] != item['id']:
            connection.execute(t.delete().where(t.c.file_id == child['id']))
        change(connection, 'files', child['id'], share=int(enabled and child['id'] == item['id']), pshare=item['id'] if enabled else 0)
        jobs.enqueue(connection, 'search.sync', {'type': 'file', 'id': child['id']})
    item = get(connection, item['id'])
    revoked = set(previous) - set(recipients(connection, item))
    if revoked:
        notify(connection, item, 'delete', {'id': item['id']}, list(revoked))
    notify(connection, item, 'update')
    return item


def share__out(connection, context, data, settings):
    item = require(connection, context, data.get('id'))
    user = actor(connection, context)
    if item['userid'] == user.userid:
        raise ApiError('不能退出自己共享的文件')
    t = tables['file_users']
    ident = item['pshare'] or item['id']
    if connection.execute(select(t.c.id).where(t.c.file_id == ident, t.c.userid == 0)).first():
        raise ApiError('无法退出共享所有人的文件或文件夹')
    connection.execute(t.delete().where(t.c.file_id == ident, t.c.userid == user.userid))
    for child in descendants(connection, get(connection, ident)):
        jobs.enqueue(connection, 'search.sync', {'type': 'file', 'id': child['id']})
    notify(connection, item, 'delete', {'id': item['id']}, [user.userid])
    return []


def link(connection, context, data, settings):
    item = require(connection, context, data.get('id'), 0, True)
    if item['type'] == 'folder':
        raise ApiError('文件夹不支持分享')
    user = actor(connection, context)
    guest_access = int(data.get('guest_access') == 'yes') if 'guest_access' in data else item['guest_access']
    if guest_access != item['guest_access'] and item['permission'] < 1:
        raise ApiError('没有修改写入权限')
    t = tables['file_links']
    if data.get('refresh') == 'yes':
        connection.execute(t.delete().where(t.c.file_id == item['id'], t.c.userid == user.userid))
    result = connection.execute(select(t).where(t.c.file_id == item['id'], t.c.userid == user.userid).order_by(t.c.id.desc()).limit(1)).mappings().first()
    if result is None:
        code = base64.b64encode(f"{item['id']},{user.userid},{secrets.token_urlsafe(24)}".encode()).decode()
        ident = insert_row(connection, 'file_links', file_id=item['id'], userid=user.userid, code=code)
        result = row(connection, 'file_links', ident)
    else:
        result = dict(result)
    change(connection, 'files', item['id'], guest_access=guest_access)
    return {'id': item['id'], 'code': result['code'], 'num': result['num'],
            'guest_access': guest_access,
            'url': context.base_url.rstrip('/') + '/single/file/' + result['code']}


def upload_type(ext):
    groups = {'document': 'md markdown text', 'word': 'doc docx dot dotx odt ott rtf', 'excel': 'xls xlsx xlsm xlt xltx ods ots csv tsv', 'ppt': 'ppt pptx pps ppsx pot potx odp otp', 'picture': 'jpg jpeg webp png gif bmp ico raw svg', 'archive': 'rar zip jar 7z tar gzip gz apk dmg', 'media': 'mp3 wav mp4 flv avi mov wmv mkv 3gp rm', 'code': 'py js ts json html css xml sh c cpp h java rs go php rb sql yaml yml ini log conf ps1 bat vue'}
    return next((kind for kind, exts in groups.items() if ext in exts.split()), ext if ext in {'txt', 'pdf', 'drawio', 'mind', 'wps', 'ofd', 'xmind', 'axure', 'cad'} else '')


def upload_path(connection, context, settings, stream, name, params):
    name = safe_name(name)
    pid = integer(params.get('pid'))
    parent_for_write(connection, context, pid)
    result = []
    relative = str(params.get('webkitRelativePath', params.get('webkit_relative_path', name)) or name)
    parts = relative.replace('\\', '/').split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ApiError('文件路径不合法')
    for component in parts[:-1]:
        component = safe_name(component)
        found = next((c for c in children(connection, pid) if c['type'] == 'folder' and c['name'] == component and permission(connection, c, actor(connection, context).userid) >= 1), None)
        if found is None:
            found = create_file(connection, context, pid, component, 'folder', '')
            notify(connection, found, 'add')
            result.append(found)
        pid = found['id']
    owner, _, _ = parent_for_write(connection, context, pid)
    suffix = PurePosixPath(name).suffix
    ext, stem = suffix.lstrip('.').lower(), name[:-len(suffix)] if suffix else name
    item = None
    overwrite = bool(integer(params.get('cover', params.get('overwrite', 0))))
    if overwrite:
        item = next((c for c in children(connection, pid) if c['userid'] == owner and c['name'] == stem and c['ext'] == ext), None)
        if item:
            item = require(connection, context, item['id'], 1, True)
    if item is None:
        overwrite = False
        item = create_file(connection, context, pid, stem, upload_type(ext), ext)
    limit = integer(setting(connection, 'system', 'file_upload_limit', 1024), 1024)
    blob = store_stream(settings, stream, maximum=max(1, limit) * 1024**2)
    extra = {}
    if item['type'] == 'picture' and ext != 'svg':
        from PIL import Image
        with Image.open(confined_path(settings.storage_root, blob['url'])) as image:
            image.verify()
            extra = {'width': image.width, 'height': image.height}
    save_blob(connection, context, item, blob, extra=extra)
    item = get(connection, item['id'])
    notify(connection, item, 'add')
    item = view(connection, context, settings, item)
    item.update(full_name=relative, overwrite=int(overwrite))
    return result + [item]


def content__upload(connection, context, data, settings):
    upload = data.get('files') or data.get('file')
    if not hasattr(upload, 'file'):
        raise ApiError('请选择上传文件')
    return upload_path(connection, context, settings, upload.file, upload.filename, data)


def office__token(connection, context, data, settings):
    from .files_external import office_token
    return {'token': office_token(connection, context, data.get('config'), settings)}


def content__office(connection, context, data, settings):
    from .files_external import office_save
    return office_save(connection, context, data, settings)


def collaboration__lists(connection, context, data, settings):
    from .files_collaboration import attachment_lists
    return attachment_lists(connection, context, data, settings)


def collaboration__download(connection, context, data, settings):
    from .files_collaboration import attachment_download
    return attachment_download(connection, context, data, settings)


def download__pack(connection, context, data, settings):
    if data.get('key'):
        return download_bytes(connection, context, data, settings)
    user = actor(connection, context)
    if 'temp' in user.identities:
        raise ApiError('无法打包下载')
    config = setting(connection, 'fileSetting')
    mode, people = config.get('permission_pack_type'), config.get('permission_pack_userids', [])
    if mode == 'admin':
        require_admin(user)
    if (mode == 'appointAllow' and user.userid not in people) or (mode == 'appointProhibit' and user.userid in people):
        raise ApiError('此功能已禁止使用')
    ids, files, total = selected(data), [], 0
    for ident in ids:
        root = require(connection, context, ident, 1)
        for item in descendants(connection, root):
            require(connection, context, item['id'], 1)
            total += item['size'] or 0
            files.append(item['id'])
    if total > 1024**3:
        raise ApiError('文件总大小已超过1GB，请分批下载')
    token = secrets.token_hex(32)
    name = safe_name(data.get('name') or f'Package_{user.userid}') + '.zip'
    insert_row(connection, 'backend_file_downloads', id=token, userid=user.userid, name=name, file_ids=encoded(files), expires_at=utcnow() + timedelta(days=1))
    url = context.base_url.rstrip('/') + '/api/file/download/pack?key=' + token
    jobs.enqueue(connection, 'file.pack', {'key': token, 'ids': ids, 'userid': user.userid, 'url': url, 'name': name})
    return {'name': name, 'url': url}


def resolve_download(connection, context, key, settings):
    grant_row = row(connection, 'backend_file_downloads', str(key))
    if not grant_row or grant_row['expires_at'] <= utcnow() or not grant_row['ready']:
        raise ApiError('文件不存在或已过期')
    owner = row(connection, 'users', grant_row['userid'], key='userid')
    if not owner or owner.get('disable_at'):
        raise ApiError('没有查看访问权限')
    if grant_row.get('dialog_id'):
        from .dialog_core import get_dialog
        get_dialog(connection, grant_row['dialog_id'], grant_row['userid'])
    if any(not can_access(connection, grant_row['userid'], ident) for ident in decoded(grant_row.get('file_ids'), [])):
        raise ApiError('没有查看访问权限')
    if grant_row['file_id']:
        if not can_access(connection, grant_row['userid'], grant_row['file_id']):
            raise ApiError('没有查看访问权限')
        content_row = row(connection, 'file_contents', grant_row['content_id'])
        if not content_row or content_row['fid'] != grant_row['file_id']:
            raise ApiError('文件不存在或已被删除')
        path = blob_path(settings, content_row['content'])
    else:
        path = confined_path(settings.storage_root, grant_row['path'])
    return path, grant_row


def download_bytes(connection, context, data, settings):
    path, grant_row = resolve_download(connection, context, data.get('key', ''), settings)
    return FileResponse(path, filename=grant_row['name'])


def transfer_user_files(connection, context, original_userid, new_userid):
    from .files_locks import serialize_namespace
    user = actor(connection, context)
    if user.userid != original_userid:
        require_admin(user)
    original = row(connection, 'users', original_userid, key='userid')
    target = row(connection, 'users', new_userid, key='userid', lock=True)
    if not original or not target or original_userid == new_userid:
        raise ApiError('移交用户无效')
    serialize_namespace(connection)
    t = tables['files']
    owned = [dict(r) for r in connection.execute(select(t).where(t.c.userid == original_userid, t.c.deleted_at.is_(None)).order_by(t.c.id).with_for_update()).mappings()]
    if not owned:
        return []
    name = unique_name(connection, 0, new_userid, '【' + (original['nickname'] or str(original_userid)) + '】移交的文件', '')
    folder_id = insert_row(connection, 'files', pid=0, userid=new_userid, created_id=0, name=name, type='folder', ext='')
    for item in owned:
        change(connection, 'files', item['id'], userid=new_userid, pid=folder_id if item['pid'] == 0 else item['pid'])
    for item in owned:
        item = get(connection, item['id'])
        chain = ancestors(connection, item)
        pids = ',' + ','.join(str(p['id']) for p in reversed(chain)) + ','
        if len(pids) > 255:
            raise ApiError('目录层级过深')
        change(connection, 'files', item['id'], pids=pids, pshare=next((p['id'] for p in [item] + chain if p['share']), 0))
        notify(connection, get(connection, item['id']), 'update')
    shares = tables['file_users']
    for old in connection.execute(select(shares).where(shares.c.userid == original_userid).with_for_update()).mappings():
        existing = connection.execute(select(shares).where(shares.c.userid == new_userid, shares.c.file_id == old['file_id'])).mappings().first()
        if existing:
            change(connection, 'file_users', existing['id'], permission=max(existing['permission'], old['permission']))
            connection.execute(shares.delete().where(shares.c.id == old['id']))
        else:
            change(connection, 'file_users', old['id'], userid=new_userid)
        for child in descendants(connection, get(connection, old['file_id'])):
            jobs.enqueue(connection, 'search.sync', {'type': 'file', 'id': child['id']})
    notify(connection, get(connection, folder_id), 'add')
    return [get(connection, folder_id)]


def register_jobs():
    from .files_jobs import register_jobs as register_file_jobs
    from ..realtime import register_scope
    register_file_jobs()
    register_scope('file_id', can_access)


ROUTES = {
    'file/collaboration/lists': collaboration__lists, 'file/collaboration/download': collaboration__download,
    'file/lists': lists, 'file/one': one, 'file/fetch': fetch, 'file/search': search,
    'file/add': add, 'file/copy': copy, 'file/move': move, 'file/remove': remove,
    'file/content': content, 'file/content/save': content__save, 'file/office/token': office__token,
    'file/content/office': content__office, 'file/content/upload': content__upload,
    'file/content/history': content__history, 'file/content/restore': content__restore,
    'file/share': share, 'file/share/update': share__update, 'file/share/out': share__out,
    'file/link': link, 'file/download/pack': download__pack,
}
# Ordinary mappings return unwrapped data; content/office is an unwrapped external callback.
RAW_ROUTES = {'file/content/office'}
BINARY_ROUTES = {'file/content', 'file/collaboration/download', 'file/download/pack'}
EXTRA_ROUTES = {'file/bytes': download_bytes}
