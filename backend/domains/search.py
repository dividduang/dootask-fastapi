"""Manticore full-text/vector search with authoritative DB ACL rechecks."""
import csv
import html
import io
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from sqlalchemy import select, or_
from .. import jobs
from ..db import tables, setting
from ..errors import ApiError
from .files_config import external_settings
from .files_external import request_json
from .files_support import actor, row, integer, decoded, encoded, blob_path, save_setting

TYPES = {'file': ('files', 'id'), 'user': ('users', 'userid'), 'project': ('projects', 'id'), 'task': ('project_tasks', 'id'), 'message': ('web_socket_dialog_msgs', 'id')}


def extract_text(path, ext, offset=0, limit=50000):
    offset, limit = max(0, offset), min(200000, max(1, limit))
    if ext in ('docx', 'xlsx', 'pptx', 'odt', 'ods', 'odp'):
        parts = []
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            wanted = [n for n in names if (ext == 'docx' and re.fullmatch(r'word/(document|header\d+|footer\d+)\.xml', n)) or (ext == 'pptx' and re.fullmatch(r'ppt/slides/slide\d+\.xml', n)) or (ext == 'xlsx' and (n == 'xl/sharedStrings.xml' or re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))) or (ext in ('odt', 'ods', 'odp') and n == 'content.xml')]
            if sum(archive.getinfo(n).file_size for n in wanted) > 100 * 1024**2:
                raise ApiError('文件解压文本过大')
            shared = []
            if ext == 'xlsx' and 'xl/sharedStrings.xml' in names:
                tree = ET.fromstring(archive.read('xl/sharedStrings.xml'))
                shared = [''.join(t.text or '' for t in node.iter() if t.tag.endswith('}t')) for node in tree]
            for name in sorted(wanted):
                if name.endswith('sharedStrings.xml'):
                    continue
                raw = archive.read(name)
                if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                    raise ApiError('文件XML包含不允许的实体')
                tree = ET.fromstring(raw)
                if ext == 'xlsx':
                    for element in tree.iter():
                        if element.tag.endswith('}row'):
                            cells = []
                            for cell in element:
                                value = ''.join(t.text or '' for t in cell.iter() if t.tag.endswith(('}v', '}t')))
                                if cell.get('t') == 's' and value.isdigit():
                                    value = shared[int(value)]
                                cells.append(value)
                            parts.append('\t'.join(cells))
                else:
                    parts.extend(n.text or '' for n in tree.iter() if n.tag.endswith(('}t', '}p')) and n.text)
        text = '\n'.join(parts)
    elif ext == 'pdf':
        from pypdf import PdfReader
        text = '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
    elif ext == 'xls':
        import xlrd
        book = xlrd.open_workbook(str(path), on_demand=True)
        try:
            text = '\n'.join('Worksheet: ' + s.name + '\n' + '\n'.join('\t'.join(str(c) for c in s.row_values(i)) for i in range(s.nrows)) for s in book.sheets())
        finally:
            book.release_resources()
    elif ext in ('doc', 'ppt'):
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        executable = external_settings().libreoffice
        if not executable:
            raise ApiError('旧版Office二进制文本提取需要已部署的LibreOffice转换服务')
        with tempfile.TemporaryDirectory(prefix='dootask-office-') as directory:
            root = Path(directory)
            source = root / ('document.' + ext)
            shutil.copyfile(path, source)
            target_ext = 'docx' if ext == 'doc' else 'pptx'
            process = subprocess.run([executable, '-env:UserInstallation=' + (root / 'profile').as_uri(), '--headless', '--convert-to', target_ext,
                                      '--outdir', directory, str(source)], capture_output=True, timeout=90, check=False)
            output = root / ('document.' + target_ext)
            if process.returncode != 0 or not output.is_file():
                raise ApiError('Office转换失败: ' + process.stderr.decode('utf-8', errors='replace')[:500])
            return extract_text(output, target_ext, offset, limit)
    else:
        if path.stat().st_size > 100 * 1024**2:
            raise ApiError('文件文本过大')
        raw = path.read_bytes()
        if b'\0' in raw[:4096] and not raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            raise ApiError('此文件格式不支持文本提取')
        try:
            text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        except UnicodeError:
            text = raw.decode('gb18030', errors='replace')
        if ext == 'rtf':
            text = re.sub(r'\\[a-z]+-?\d* ?|[{}]', '', text)
    return {'content': text[offset:offset + limit], 'total_length': len(text), 'offset': offset, 'limit': limit, 'has_more': offset + limit < len(text)}


def sql_literal(value):
    return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"


def manticore_sql(query):
    cfg = external_settings()
    if not cfg.manticore_url:
        raise ApiError('Manticore搜索服务未配置')
    request = Request(cfg.manticore_url.rstrip('/') + '/sql?mode=raw', data=urlencode({'query': query}).encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
    except (OSError, ValueError) as exc:
        raise ApiError('Manticore搜索服务请求失败: ' + str(exc)) from exc
    blocks = result if isinstance(result, list) else [result]
    for block in blocks:
        if block.get('error'):
            raise ApiError('Manticore: ' + str(block['error']))
    return blocks[0].get('data', []) if blocks else []


def embedding(text):
    cfg = external_settings()
    if not cfg.embedding_url or not cfg.embedding_model:
        raise ApiError('语义搜索Embedding服务未配置')
    result = request_json(cfg.embedding_url, {'model': cfg.embedding_model, 'input': text}, headers={'Authorization': 'Bearer ' + cfg.embedding_key} if cfg.embedding_key else {})
    try:
        vector = [float(v) for v in result['data'][0]['embedding']]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ApiError('Embedding服务响应格式错误') from exc
    if not vector or len(vector) > 65536 or not all(math.isfinite(v) for v in vector):
        raise ApiError('Embedding向量无效')
    return vector


def permitted(connection, kind, userid, item):
    if not item:
        return False
    if kind == 'user':
        return not item.get('disable_at') and not item.get('bot')
    if kind == 'file':
        from .files import permission
        return permission(connection, item, userid) >= 0
    if kind in ('project', 'task'):
        from .project_core import can_view_project, can_view_task
        return (can_view_task if kind == 'task' else can_view_project)(connection, userid, item)
    from .dialog_core import get_message
    try:
        get_message(connection, item['id'], userid)
        return not item.get('bot')
    except ApiError:
        return False


def allowed_users(connection, kind, item):
    if kind == 'user':
        return [0]
    if kind == 'file':
        from .files import recipients
        return recipients(connection, item)
    if kind == 'task':
        from .project_core import task_allowed_users
        return task_allowed_users(connection, item)
    if kind == 'message':
        from .dialog_core import member_ids
        return member_ids(connection, item['dialog_id'])
    users = tables['users']
    return [uid for uid in connection.execute(select(users.c.userid).where(users.c.disable_at.is_(None))).scalars() if permitted(connection, kind, uid, item)]


def document(connection, kind, item, settings):
    title, content = item.get('name', item.get('nickname', '')), ''
    if kind == 'file':
        from .files import latest
        stored = latest(connection, item['id'])
        if stored:
            if stored['text']:
                content = stored['text']
            elif item['type'] in ('document', 'txt', 'code', 'word', 'excel', 'ppt', 'pdf', 'mind', 'drawio'):
                content = extract_text(blob_path(settings, stored['content']), item['ext'], 0, 200000)['content']
    elif kind == 'user':
        content = ' '.join(str(item.get(k) or '') for k in ('email', 'profession', 'introduction'))
        tags = tables['user_tags']
        content += ' ' + ' '.join(connection.execute(select(tags.c.name).where(tags.c.user_id == item['userid'])).scalars())
    elif kind == 'project':
        content = str(item.get('desc') or '')
    elif kind == 'task':
        t = tables['project_task_contents']
        values = connection.execute(select(t.c.content).where(t.c.task_id == item['id'])).scalars()
        content = str(item.get('desc') or '') + ' ' + ' '.join(str(v or '') for v in values)
    else:
        msg = decoded(item.get('msg'))
        content = msg.get('text', msg.get('name', '')) if isinstance(msg, dict) else str(msg)
        title = item.get('key') or ''
    return {'title': title, 'content': html.unescape(re.sub('<[^>]+>', ' ', content)), 'allowed_users': allowed_users(connection, kind, item), 'dialog_id': item.get('dialog_id', 0)}


def search_enabled(settings):
    from .apps import installed_config
    installed = installed_config(settings, 'manticore') is not None
    if installed and not external_settings().manticore_url:
        raise ApiError('Manticore应用已安装但MANTICORE_URL未配置')
    return installed or bool(external_settings().manticore_url)


def initialize(connection, payload, settings):
    cfg = external_settings()
    vector = embedding('DooTask search schema') if cfg.embedding_url else None
    dims = len(vector) if vector else 0
    saved = setting(connection, 'fastapiSearchSchema')
    signature = {'dimensions': dims, 'embedding_model': cfg.embedding_model, 'embedding_url': cfg.embedding_url, 'schema': 1}
    changed = saved != signature
    for kind in TYPES:
        name = 'backend_' + kind
        if changed:
            manticore_sql('DROP TABLE IF EXISTS ' + name)
        ddl = f"CREATE TABLE IF NOT EXISTS {name} (title text, content text, allowed_users multi64, dialog_id bigint"
        if dims:
            ddl += f", vector float_vector knn_type='hnsw' knn_dims='{dims}' hnsw_similarity='cosine'"
        manticore_sql(ddl + ") charset_table='non_cjk,cjk'")
    save_setting(connection, 'fastapiSearchSchema', signature)
    if changed or payload.get('rebuild'):
        for kind in TYPES:
            jobs.enqueue(connection, 'search.rebuild', {'type': kind, 'after': 0})


def sync(connection, payload, settings):
    kind = payload['type']
    if kind not in TYPES:
        raise ApiError('未知搜索对象类型')
    cfg = external_settings()
    if not search_enabled(settings):
        return
    if not setting(connection, 'fastapiSearchSchema'):
        initialize(connection, {}, settings)
    table_name, key = TYPES[kind]
    ident = integer(payload['id'])
    item = row(connection, table_name, ident, key=key)
    if not item or kind == 'message' and item.get('bot'):
        manticore_sql(f'DELETE FROM backend_{kind} WHERE id={ident}')
        return
    if kind == 'message':
        # Serialize per-message replacement with membership MVA refresh.
        dialogs = tables['web_socket_dialogs']
        connection.execute(select(dialogs.c.id).where(dialogs.c.id == item['dialog_id']).with_for_update()).first()
    doc = document(connection, kind, item, settings)
    if cfg.embedding_url:
        doc['vector'] = embedding(doc['title'] + '\n' + doc['content'])
    result = request_json(cfg.manticore_url.rstrip('/') + '/replace', {'index': 'backend_' + kind, 'id': ident, 'doc': doc})
    if result.get('error'):
        raise ApiError('Manticore: ' + str(result['error']))


def sync_dialog_members(connection, payload, settings):
    if not search_enabled(settings):
        return
    if not setting(connection, 'fastapiSearchSchema'):
        initialize(connection, {}, settings)
    from .dialog_core import member_ids
    dialog_id = integer(payload['dialog_id'])
    dialogs = tables['web_socket_dialogs']
    dialog = connection.execute(select(dialogs).where(dialogs.c.id == dialog_id).with_for_update()).mappings().first()
    members = member_ids(connection, dialog_id) if dialog and not dialog['deleted_at'] else []
    allowed = ','.join(str(int(uid)) for uid in sorted(set(members)))
    # Update existing documents without re-embedding all historical content.
    # Searches still recheck current MariaDB ACL after provider candidate filtering.
    manticore_sql(f'UPDATE backend_message SET allowed_users=({allowed}) WHERE dialog_id={dialog_id}')


def rebuild(connection, payload, settings):
    kind = payload['type']
    table_name, key = TYPES[kind]
    table = tables[table_name]
    ids = list(connection.execute(select(table.c[key]).where(table.c[key] > integer(payload.get('after'))).order_by(table.c[key]).limit(100)).scalars())
    for ident in ids:
        sync(connection, {'type': kind, 'id': ident}, settings)
    if len(ids) == 100:
        jobs.enqueue(connection, 'search.rebuild', {'type': kind, 'after': ids[-1]})


def mysql_candidates(connection, kind, key):
    name, pk = TYPES[kind]
    t = tables[name]
    query = select(t)
    if 'deleted_at' in t.c:
        query = query.where(t.c.deleted_at.is_(None))
    columns = ('nickname', 'email', 'profession') if kind == 'user' else ('msg', 'key') if kind == 'message' else ('name', 'desc') if 'desc' in t.c else ('name',)
    conditions = [t.c[c].contains(key) for c in columns if c in t.c]
    if key.isdigit():
        conditions.append(t.c[pk] == int(key))
    return connection.execute(query.where(or_(*conditions)).order_by(t.c[pk].desc())).mappings()


def run_search(connection, context, data, settings, kind):
    user = actor(connection, context)
    key = str(data.get('key', '')).strip()
    if not key:
        return []
    take = min(50, max(1, integer(data.get('take'), 20)))
    mode = data.get('search_type', 'hybrid')
    if mode not in ('fulltext', 'vector', 'hybrid', 'text'):
        raise ApiError('搜索类型错误')
    cfg = external_settings()
    dialog_id = integer(data.get('dialog_id')) if kind == 'message' else 0
    if dialog_id:
        from .dialog_core import get_dialog
        get_dialog(connection, dialog_id, user.userid)
    candidates = []
    if not search_enabled(settings):
        candidates = [(dict(item), 0, None) for item in mysql_candidates(connection, kind, key)]
    else:
        escaped = re.sub(r'([\\()\[\]|!@~"&/^$=<>:\-])', r'\\\1', key)
        acl = f'(allowed_users=0 OR allowed_users={user.userid})' + (f' AND dialog_id={dialog_id}' if dialog_id else '')
        groups = []
        if mode in ('text', 'fulltext', 'hybrid'):
            groups.append(manticore_sql(f'SELECT id,content,WEIGHT() AS relevance FROM backend_{kind} WHERE MATCH({sql_literal(escaped)}) AND {acl} ORDER BY relevance DESC LIMIT 250'))
        if mode in ('vector', 'hybrid'):
            vector = embedding(key)
            groups.append(manticore_sql(f"SELECT id,content,KNN_DIST() AS distance FROM backend_{kind} WHERE KNN(vector,250,({','.join(str(v) for v in vector)})) AND {acl} LIMIT 250"))
        ranked, previews = {}, {}
        for group in groups:
            for rank, result in enumerate(group):
                ident = integer(result['id'])
                ranked[ident] = ranked.get(ident, 0) + 1 / (60 + rank + 1)
                previews[ident] = result.get('content', '')[:300]
        table_name, pk = TYPES[kind]
        candidates = [(row(connection, table_name, ident, key=pk), score, previews[ident]) for ident, score in sorted(ranked.items(), key=lambda p: p[1], reverse=True)]
    output = []
    for item, relevance, preview in candidates:
        if not permitted(connection, kind, user.userid, item) or dialog_id and item['dialog_id'] != dialog_id:
            continue
        if kind == 'user':
            item = {k: item.get(k) for k in ('userid', 'nickname', 'email', 'userimg', 'profession', 'department', 'identity', 'introduction')}
            tags = tables['user_tags']
            item['search_tags'] = list(connection.execute(select(tags.c.name).where(tags.c.user_id == item['userid']).limit(10)).scalars())
            item['introduction_preview'] = preview
        elif kind == 'message':
            item['msg_id'] = item['id']
            item['msg'] = decoded(item['msg'])
            sender = row(connection, 'users', item['userid'], key='userid') or {}
            item['user'] = {k: sender.get(k) for k in ('userid', 'nickname', 'email', 'userimg')}
            item['content_preview'] = preview
        else:
            item['content_preview' if kind == 'file' else 'desc_preview'] = preview
        item['relevance'] = relevance
        output.append(item)
        if len(output) == take:
            break
    if kind == 'message' and data.get('mode') == 'position':
        return {'data': [r['id'] for r in output]}
    if kind == 'message' and data.get('mode') == 'dialog':
        from .dialog_core import get_dialog
        dialogs, seen = [], set()
        for result in output:
            if result['dialog_id'] in seen:
                continue
            seen.add(result['dialog_id'])
            dialog = get_dialog(connection, result['dialog_id'], user.userid)
            dialog['search_msg_id'] = result['id']
            dialogs.append(dialog)
        return {'data': dialogs}
    return output


def contact(connection, context, data, settings):
    return run_search(connection, context, data, settings, 'user')


def project(connection, context, data, settings):
    return run_search(connection, context, data, settings, 'project')


def task(connection, context, data, settings):
    return run_search(connection, context, data, settings, 'task')


def file(connection, context, data, settings):
    return run_search(connection, context, data, settings, 'file')


def message(connection, context, data, settings):
    return run_search(connection, context, data, settings, 'message')


def task_vector_search(connection, vector, userid, limit=200, *, project_id=0, exclude_id=0):
    values = [float(v) for v in vector]
    if not values or not all(math.isfinite(v) for v in values):
        raise ApiError('Embedding向量无效')
    limit = max(1, min(1000, integer(limit, 200)))
    results = manticore_sql(f"SELECT id,KNN_DIST() AS distance FROM backend_task WHERE KNN(vector,{limit * 5},({','.join(str(v) for v in values)})) AND (allowed_users=0 OR allowed_users={integer(userid)}) LIMIT {limit * 5}")
    output = []
    for candidate in results:
        item = row(connection, 'project_tasks', integer(candidate['id']))
        if item and item['id'] != exclude_id and (not project_id or item['project_id'] == project_id) and permitted(connection, 'task', userid, item):
            output.append({**item, 'task_id': item['id'], 'distance': float(candidate['distance'])})
            if len(output) >= limit:
                break
    return output


def register_jobs():
    jobs.register('search.sync', sync)
    jobs.register('search.rebuild', rebuild)
    jobs.register('search.initialize', initialize)
    jobs.register('search.dialog_members', sync_dialog_members)


ROUTES = {'search/contact': contact, 'search/project': project, 'search/task': task, 'search/file': file, 'search/message': message}
