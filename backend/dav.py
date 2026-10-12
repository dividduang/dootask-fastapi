"""RFC 4918 DAV transport over the cabinet's objects, ACL and transactions."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import io
import mimetypes
import re
import secrets
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from email.utils import format_datetime
from urllib.parse import unquote, quote, urlsplit
from sqlalchemy import select, or_
from fastapi import Request
from starlette.responses import Response, FileResponse
from . import db
from .context import RequestContext, current_context, utcnow
from .errors import ApiError
from .domains import files, filedav
from .domains.files_support import integer, decoded, encoded, row, change, safe_name, store_stream, blob_path
from .domains.files_locks import serialize_namespace, enforce

DAV = 'DAV:'
ET.register_namespace('d', DAV)
METHODS = ['OPTIONS', 'PROPFIND', 'GET', 'HEAD', 'PUT', 'DELETE', 'MKCOL', 'MOVE', 'COPY', 'PROPPATCH', 'LOCK', 'UNLOCK']
LIVE = {'displayname', 'resourcetype', 'getcontentlength', 'getcontenttype', 'getetag', 'getlastmodified', 'creationdate', 'supportedlock', 'lockdiscovery'}


class DavError(Exception):
    def __init__(self, status, message, condition=None):
        self.status, self.message, self.condition = status, message, condition


def tag(name):
    return '{DAV:}' + name


def xml_response(node, status=207, headers=None):
    return Response(ET.tostring(node, encoding='utf-8', xml_declaration=True), status_code=status, media_type='application/xml', headers=headers)


def xml_parse(raw):
    if len(raw) > 1024**2 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise DavError(400, 'Unsafe XML')
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise DavError(400, 'Malformed XML')


def normalized_path(value):
    if '\x00' in value or '\\' in value:
        raise DavError(400, 'Invalid path')
    parts = value.strip('/').split('/') if value.strip('/') else []
    if any(p in ('', '.', '..') for p in parts):
        raise DavError(400, 'Invalid path')
    return '/'.join(parts)


def shared_name(item):
    return item['name'] + ' [#' + str(item['id']) + ']' + ('.' + item['ext'] if item['ext'] else '')


def virtual(name):
    return {'id': 0, 'pid': 0, 'name': name, 'type': 'folder', 'ext': '', '_virtual': name}


def list_children(connection, context, node):
    if node.get('_virtual') == '':
        return [virtual('files'), virtual('shared')]
    if '_virtual' in node:
        t = db.tables['files']
        query = select(t).where(t.c.deleted_at.is_(None))
        if node['_virtual'] == 'files':
            query = query.where(t.c.pid == 0, t.c.userid == context.actor.userid)
        else:
            query = query.where(t.c.share == 1, t.c.userid != context.actor.userid)
        values = [dict(r) for r in connection.execute(query.order_by(t.c.id)).mappings()]
        result = []
        for item in values:
            if files.permission(connection, item, context.actor.userid) >= 0:
                if node['_virtual'] == 'shared':
                    item['_dav_name'] = shared_name(item)
                    item['_shared_root'] = True
                result.append(item)
        return result
    return [f for f in files.children(connection, node['id']) if files.permission(connection, f, context.actor.userid) >= 0]


def name_of(item):
    return item.get('_dav_name') or files.full_name(item)


def resolve(connection, context, path, *, missing=False):
    parts = normalized_path(path).split('/') if normalized_path(path) else []
    node = virtual('')
    for index, part in enumerate(parts):
        if node['type'] != 'folder':
            raise DavError(409, 'Parent is not a collection')
        matches = [i for i in list_children(connection, context, node) if name_of(i) == part]
        if len(matches) > 1:
            raise DavError(409, 'Conflicting cabinet names')
        if not matches:
            if missing and index == len(parts) - 1:
                return None
            raise DavError(404, 'Not found')
        node = matches[0]
    return node


def writable_parent(connection, context, path):
    parent_path, _, name = path.rpartition('/')
    parent = resolve(connection, context, parent_path)
    safe_name(name)
    if parent['type'] != 'folder':
        raise DavError(409, 'Parent is not a collection')
    if '_virtual' in parent and parent['_virtual'] != 'files':
        raise DavError(403, 'Virtual collection is read only')
    if parent['id']:
        files.require(connection, context, parent['id'], 1, True)
    return parent, name


def etag(connection, node):
    if not node['id']:
        return '"root-' + node['name'] + '"'
    content = files.latest(connection, node['id'])
    version = content['id'] if content else 0
    return '"' + hashlib.sha256(f"{node['id']}:{version}:{node.get('updated_at')}".encode()).hexdigest() + '"'


def locks_for(connection, node, *, children=False):
    if not node or not node['id']:
        return []
    own = {node['id']}
    parents = {p['id'] for p in files.ancestors(connection, node)}
    if children:
        own.update(p['id'] for p in files.descendants(connection, node))
    t = db.tables['webdav_locks']
    return [dict(l) for l in connection.execute(select(t).where(t.c.timeout_at > utcnow(), t.c.file_id.in_(own | parents))).mappings() if l['file_id'] in own or l['depth'] == 'infinity']


def if_conditions(connection, context, path, node, header):
    """Evaluate If's tagged/untagged OR lists; only positive true tokens authorize writes."""
    if not header:
        return set()
    pos, resource, grouped, positive = 0, path, {}, set()
    while pos < len(header):
        while pos < len(header) and header[pos].isspace():
            pos += 1
        if pos == len(header):
            break
        if header[pos] == '<':
            end = header.find('>', pos)
            if end < 0:
                raise DavError(400, 'Malformed If header')
            uri = urlsplit(header[pos + 1:end])
            if uri.netloc and uri.netloc != urlsplit(context.base_url).netloc:
                raise DavError(412, 'If resource is not local')
            if not uri.path.startswith('/dav/'):
                raise DavError(412, 'If resource is outside DAV')
            resource = normalized_path(unquote(uri.path[5:]))
            pos = end + 1
            continue
        if header[pos] != '(':
            raise DavError(400, 'Malformed If header')
        end = header.find(')', pos)
        if end < 0:
            raise DavError(400, 'Malformed If header')
        terms = header[pos + 1:end]
        current = resolve(connection, context, resource, missing=True)
        existing_tokens = {l['token'] for l in locks_for(connection, current)}
        current_etag = etag(connection, current) if current else None
        cursor, truth, own_tokens, term_count = 0, True, set(), 0
        expression = re.compile(r'\s*(Not\s+)?(?:<([^>]+)>|\[([^\]]+)\])')
        while cursor < len(terms):
            if not terms[cursor:].strip():
                break
            match = expression.match(terms, cursor)
            if not match:
                raise DavError(400, 'Malformed If condition')
            negate, token, expected = match.groups()
            value = token in existing_tokens if token is not None else expected == current_etag
            truth = truth and (not value if negate else value)
            if token and not negate and value:
                own_tokens.add(token)
            cursor, term_count = match.end(), term_count + 1
        if not term_count:
            raise DavError(400, 'Empty If condition')
        grouped.setdefault(resource, []).append(truth)
        if truth:
            positive.update(own_tokens)
        pos = end + 1
    if not grouped or not all(any(states) for states in grouped.values()):
        raise DavError(412, 'If condition failed')
    return positive


def preconditions(connection, context, path, node, headers):
    tokens = if_conditions(connection, context, path, node, headers.get('if', ''))
    context.pgp['dav_tokens'] = list(tokens)
    current = etag(connection, node) if node else None
    match = headers.get('if-match')
    none = headers.get('if-none-match')
    if match and not (node and (match.strip() == '*' or current in [v.strip() for v in match.split(',')])):
        raise DavError(412, 'If-Match failed')
    if none and node and (none.strip() == '*' or current in [v.strip() for v in none.split(',')]):
        raise DavError(304 if headers.get('_method') in ('GET', 'HEAD') else 412, 'If-None-Match failed')


def lock_element(lock):
    active = ET.Element(tag('activelock'))
    scope = ET.SubElement(active, tag('lockscope'))
    ET.SubElement(scope, tag(lock['scope']))
    kind = ET.SubElement(active, tag('locktype'))
    ET.SubElement(kind, tag('write'))
    ET.SubElement(active, tag('depth')).text = lock['depth']
    ET.SubElement(active, tag('owner')).text = lock['owner'] or ''
    ET.SubElement(active, tag('timeout')).text = 'Second-' + str(max(1, int((lock['timeout_at'] - utcnow()).total_seconds())))
    token = ET.SubElement(active, tag('locktoken'))
    ET.SubElement(token, tag('href')).text = lock['token']
    root = ET.SubElement(active, tag('lockroot'))
    ET.SubElement(root, tag('href')).text = '/dav/' + quote(lock['uri'], safe='/')
    return active


def properties(connection, context, node):
    values = {}
    for name in LIVE:
        values[tag(name)] = ET.Element(tag(name))
    values[tag('displayname')].text = name_of(node)
    if node['type'] == 'folder':
        ET.SubElement(values[tag('resourcetype')], tag('collection'))
    values[tag('getcontentlength')].text = str(node.get('size') or 0)
    values[tag('getcontenttype')].text = 'httpd/unix-directory' if node['type'] == 'folder' else mimetypes.guess_type(name_of(node))[0] or 'application/octet-stream'
    values[tag('getetag')].text = etag(connection, node)
    modified = node.get('updated_at') or utcnow()
    values[tag('getlastmodified')].text = format_datetime(modified.replace(tzinfo=timezone.utc), usegmt=True)
    values[tag('creationdate')].text = (node.get('created_at') or modified).isoformat() + 'Z'
    for scope in ('exclusive', 'shared'):
        entry = ET.SubElement(values[tag('supportedlock')], tag('lockentry'))
        scope_node = ET.SubElement(entry, tag('lockscope'))
        ET.SubElement(scope_node, tag(scope))
        kind = ET.SubElement(entry, tag('locktype'))
        ET.SubElement(kind, tag('write'))
    for lock in locks_for(connection, node):
        values[tag('lockdiscovery')].append(lock_element(lock))
    if node['id']:
        t = db.tables['webdav_properties']
        for prop in connection.execute(select(t).where(t.c.path == 'file:' + str(node['id']), t.c.userid == context.actor.userid)).mappings():
            if prop['value_type'] == 2:
                values[prop['name']] = xml_parse(prop['value'].encode())
            else:
                values[prop['name']] = ET.Element(prop['name'])
                values[prop['name']].text = prop['value']
    return values


def propfind(connection, context, path, node, headers, body):
    depth = headers.get('depth', 'infinity')
    if depth not in ('0', '1', 'infinity'):
        raise DavError(400, 'Invalid Depth')
    if depth == 'infinity':
        raise DavError(403, 'Finite depth required', 'propfind-finite-depth')
    root = xml_parse(body) if body else ET.Element(tag('propfind'))
    if root.tag != tag('propfind'):
        raise DavError(400, 'Expected propfind')
    selected = root.find(tag('prop'))
    names_only = root.find(tag('propname')) is not None
    multistatus = ET.Element(tag('multistatus'))
    nodes = [(path, node)]
    if depth == '1' and node['type'] == 'folder':
        nodes.extend(((path + '/' if path else '') + name_of(child), child) for child in list_children(connection, context, node))
    for current_path, current in nodes:
        response = ET.SubElement(multistatus, tag('response'))
        ET.SubElement(response, tag('href')).text = '/dav/' + quote(current_path, safe='/') + ('/' if current['type'] == 'folder' and current_path else '')
        values = properties(connection, context, current)
        requested = [n.tag for n in selected] if selected is not None else list(values)
        included = root.find(tag('include'))
        if included is not None:
            requested += [n.tag for n in included]
        for status in (200, 404):
            names = [name for name in requested if (name in values) == (status == 200)]
            if not names:
                continue
            block = ET.SubElement(response, tag('propstat'))
            props = ET.SubElement(block, tag('prop'))
            for name in names:
                props.append(ET.Element(name) if names_only or status == 404 else values[name])
            ET.SubElement(block, tag('status')).text = 'HTTP/1.1 ' + ('200 OK' if status == 200 else '404 Not Found')
    return xml_response(multistatus)


def proppatch(connection, context, path, node, body):
    if not node['id']:
        raise DavError(403, 'Virtual collection is read only')
    files.require(connection, context, node['id'], 1, True)
    root = xml_parse(body)
    if root.tag != tag('propertyupdate'):
        raise DavError(400, 'Expected propertyupdate')
    ops = []
    for operation in root:
        if operation.tag not in (tag('set'), tag('remove')):
            raise DavError(400, 'Invalid property operation')
        props = operation.find(tag('prop'))
        if props is None:
            raise DavError(400, 'Missing prop')
        ops.extend((operation.tag == tag('set'), prop) for prop in props)
    protected = any(p.tag in {tag(n) for n in LIVE} for _, p in ops)
    result, response = ET.Element(tag('multistatus')), ET.Element(tag('response'))
    result.append(response)
    ET.SubElement(response, tag('href')).text = '/dav/' + quote(path, safe='/')
    t = db.tables['webdav_properties']
    storage_path = 'file:' + str(node['id'])
    for put, prop in ops:
        block = ET.SubElement(response, tag('propstat'))
        ET.SubElement(block, tag('prop')).append(ET.Element(prop.tag))
        status = 403 if prop.tag in {tag(n) for n in LIVE} else 424 if protected else 200
        ET.SubElement(block, tag('status')).text = {200: 'HTTP/1.1 200 OK', 403: 'HTTP/1.1 403 Forbidden', 424: 'HTTP/1.1 424 Failed Dependency'}[status]
        if not protected:
            connection.execute(t.delete().where(t.c.path == storage_path, t.c.userid == context.actor.userid, t.c.name == prop.tag))
            if put:
                db.insert_row(connection, 'webdav_properties', userid=context.actor.userid, path=storage_path,
                    path_hash=hashlib.sha256(storage_path.encode()).hexdigest(), name=prop.tag,
                    property_hash=hashlib.sha256((storage_path + '\0' + prop.tag).encode()).hexdigest(),
                    value=ET.tostring(prop, encoding='unicode') if len(prop) or prop.attrib else (prop.text or ''),
                    value_type=2 if len(prop) or prop.attrib else 1)
    return xml_response(result)


def new_object(connection, context, path, folder):
    parent, name = writable_parent(connection, context, path)
    suffix = '' if folder or '.' not in name else name.rsplit('.', 1)[1]
    stem = name if not suffix else name[:-(len(suffix) + 1)]
    return files.create_file(connection, context, parent['id'], stem, 'folder' if folder else files.upload_type(suffix.lower()), suffix.lower(), deduplicate=False)


def lock_request(connection, context, path, node, headers, body, credential, settings):
    depth = headers.get('depth', 'infinity')
    if depth not in ('0', 'infinity'):
        raise DavError(400, 'Invalid lock depth')
    seconds = 1800
    for offered in headers.get('timeout', 'Second-1800').split(','):
        offered = offered.strip()
        if offered == 'Infinite':
            seconds = 7200
            break
        if re.fullmatch('Second-[0-9]+', offered):
            seconds = min(7200, max(1, int(offered[7:])))
            break
    tokens = set(context.pgp.get('dav_tokens', []))
    t = db.tables['webdav_locks']
    if not body:
        if node is None:
            raise DavError(412, 'Lock token required')
        matched = [l for l in locks_for(connection, node) if l['token'] in tokens and l['userid'] == context.actor.userid and l['file_id'] == node['id']]
        if len(matched) != 1:
            raise DavError(412, 'Lock token required')
        lock = matched[0]
        change(connection, 'webdav_locks', lock['id'], timeout_at=utcnow() + timedelta(seconds=seconds))
        lock['timeout_at'] = utcnow() + timedelta(seconds=seconds)
        status = 200
    else:
        root = xml_parse(body)
        if root.tag != tag('lockinfo') or root.find('./' + tag('locktype') + '/' + tag('write')) is None:
            raise DavError(400, 'Unsupported lock type')
        scope_node = root.find(tag('lockscope'))
        if scope_node is None or len(scope_node) != 1 or scope_node[0].tag not in (tag('exclusive'), tag('shared')):
            raise DavError(400, 'Invalid lock scope')
        scope = scope_node[0].tag.split('}')[-1]
        existing = locks_for(connection, node, children=depth == 'infinity')
        if existing and (scope == 'exclusive' or any(l['scope'] == 'exclusive' for l in existing)):
            raise DavError(423, 'Locked')
        status = 200
        if node is None:
            node = new_object(connection, context, path, False)
            blob = store_stream(settings, io.BytesIO(b''))
            files.save_blob(connection, context, node, blob)
            status = 201
        else:
            if not node['id'] or files.permission(connection, node, context.actor.userid) < 1:
                raise DavError(403, 'Forbidden')
        owner = root.find(tag('owner'))
        owner_text = ''.join(owner.itertext())[:255] if owner is not None else ''
        token = 'opaquelocktoken:' + str(uuid.uuid4())
        ident = db.insert_row(connection, 'webdav_locks', file_id=node['id'], userid=context.actor.userid, credential_id=credential['id'], uri=path,
            uri_hash=hashlib.sha256(path.encode()).hexdigest(), owner=owner_text, scope=scope, depth=depth, token=token, timeout_at=utcnow() + timedelta(seconds=seconds))
        lock = row(connection, 'webdav_locks', ident)
    prop = ET.Element(tag('prop'))
    discovery = ET.SubElement(prop, tag('lockdiscovery'))
    discovery.append(lock_element(lock))
    return xml_response(prop, status, {'Lock-Token': '<' + lock['token'] + '>'})


def copy_tree(connection, context, source, destination, depth, settings, maximum):
    nodes = files.descendants(connection, source) if depth == 'infinity' else [source]
    if len(nodes) > maximum:
        raise DavError(507, 'Copy node limit exceeded')
    ids = {}
    for old in nodes:
        if files.permission(connection, old, context.actor.userid) < 0:
            raise DavError(403, 'Source permission denied')
        if old['id'] == source['id']:
            new = new_object(connection, context, destination, old['type'] == 'folder')
        else:
            new = files.create_file(connection, context, ids[old['pid']], old['name'], old['type'], old['ext'], deduplicate=False)
        ids[old['id']] = new['id']
        content = files.latest(connection, old['id'])
        if content:
            db.insert_row(connection, 'file_contents', fid=new['id'], content=content['content'], text=content['text'], size=content['size'], userid=context.actor.userid)
            change(connection, 'files', new['id'], size=old['size'], hash=old['hash'])
        props = db.tables['webdav_properties']
        for prop in connection.execute(select(props).where(props.c.path == 'file:' + str(old['id']), props.c.userid == context.actor.userid)).mappings():
            path = 'file:' + str(new['id'])
            db.insert_row(connection, 'webdav_properties', userid=context.actor.userid, path=path, path_hash=hashlib.sha256(path.encode()).hexdigest(), name=prop['name'], property_hash=hashlib.sha256((path + '\0' + prop['name']).encode()).hexdigest(), value=prop['value'], value_type=prop['value_type'])
        files.notify(connection, files.get(connection, new['id']), 'add')


def process(connection, context, path, method, headers, body, stream, settings):
    credential = filedav.authenticate_dav(connection, context, headers.get('authorization', ''))
    if method not in ('GET', 'HEAD', 'OPTIONS', 'PROPFIND'):
        serialize_namespace(connection)
    node = resolve(connection, context, path, missing=method in ('PUT', 'MKCOL', 'LOCK'))
    headers = {**headers, '_method': method}
    preconditions(connection, context, path, node, headers)
    if method == 'OPTIONS':
        return Response(status_code=200, headers={'DAV': '1, 2', 'Allow': ', '.join(METHODS), 'MS-Author-Via': 'DAV'})
    if method in ('GET', 'HEAD'):
        if node['type'] == 'folder':
            raise DavError(405, 'Collection has no GET representation')
        stored = files.latest(connection, node['id'])
        path_obj = blob_path(settings, stored['content']) if stored else settings.frontend_root / 'public' / 'assets' / 'office' / ('empty.' + node['ext']) if node['type'] in files.OFFICE else None
        if path_obj is None or not path_obj.is_file():
            raise DavError(404, 'No content')
        return FileResponse(path_obj, filename=name_of(node), headers={'ETag': etag(connection, node)}, content_disposition_type='inline')
    if method == 'PROPFIND':
        return propfind(connection, context, path, node, headers, body)
    if method == 'PROPPATCH':
        return proppatch(connection, context, path, node, body)
    if method == 'LOCK':
        return lock_request(connection, context, path, node, headers, body, credential, settings)
    if method == 'UNLOCK':
        token = headers.get('lock-token', '').strip('<> ')
        t = db.tables['webdav_locks']
        result = connection.execute(t.delete().where(t.c.file_id == node['id'], t.c.token == token, t.c.userid == context.actor.userid))
        if not result.rowcount:
            raise DavError(409, 'Lock token does not match this resource')
        return Response(status_code=204)
    if method == 'MKCOL':
        if body:
            raise DavError(415, 'MKCOL body is unsupported')
        if node:
            raise DavError(405, 'Already exists')
        created = new_object(connection, context, path, True)
        files.notify(connection, created, 'add')
        return Response(status_code=201)
    if method == 'PUT':
        if headers.get('content-range'):
            raise DavError(400, 'Partial PUT is not supported')
        created = node is None
        if node is None:
            node = new_object(connection, context, path, False)
        if node['type'] == 'folder':
            raise DavError(405, 'Cannot PUT a collection')
        files.require(connection, context, node['id'], 1, True)
        blob = store_stream(settings, stream, maximum=filedav.config(connection)['max_file_bytes'])
        files.save_blob(connection, context, node, blob)
        return Response(status_code=201 if created else 204, headers={'ETag': etag(connection, files.get(connection, node['id']))})
    if method == 'DELETE':
        if not node['id'] or node.get('_shared_root'):
            raise DavError(403, 'Virtual/shared root cannot be deleted')
        files.require(connection, context, node['id'], 1000, True)
        enforce(connection, context, node, subtree=True)
        ids = [n['id'] for n in files.descendants(connection, node)]
        files.delete_tree(connection, context, node)
        locks = db.tables['webdav_locks']
        connection.execute(locks.delete().where(locks.c.file_id.in_(ids)))
        props = db.tables['webdav_properties']
        connection.execute(props.delete().where(props.c.path.in_(['file:' + str(i) for i in ids])))
        return Response(status_code=204)
    if method in ('MOVE', 'COPY'):
        target = urlsplit(headers.get('destination', ''))
        if target.netloc and target.netloc != urlsplit(context.base_url).netloc:
            raise DavError(502, 'Cross-server destinations are unsupported')
        if target.query or target.fragment or not target.path.startswith('/dav/'):
            raise DavError(400, 'Invalid Destination')
        destination = normalized_path(unquote(target.path[5:]))
        if destination == path or destination.startswith(path.rstrip('/') + '/'):
            raise DavError(403, 'Destination is within source')
        if not node['id'] or node.get('_shared_root') and method == 'MOVE':
            raise DavError(403, 'Virtual/shared root cannot be moved')
        depth = headers.get('depth', 'infinity')
        if depth not in ('0', 'infinity') or method == 'MOVE' and depth != 'infinity':
            raise DavError(400, 'Invalid Depth')
        existing = resolve(connection, context, destination, missing=True)
        if headers.get('overwrite', 'T') not in ('T', 'F'):
            raise DavError(400, 'Invalid Overwrite')
        if existing and headers.get('overwrite', 'T') == 'F':
            raise DavError(412, 'Destination exists')
        parent, name = writable_parent(connection, context, destination)
        if parent['id'] in {n['id'] for n in files.descendants(connection, node)}:
            raise DavError(403, 'Destination is within source')
        if existing:
            if not existing['id']:
                raise DavError(403, 'Cannot overwrite virtual collection')
            files.require(connection, context, existing['id'], 1000, True)
            files.delete_tree(connection, context, existing)
        if method == 'MOVE':
            files.require(connection, context, node['id'], 1000, True)
            suffix = '.' + node['ext'] if node['ext'] else ''
            if suffix and not name.lower().endswith(suffix.lower()):
                raise DavError(409, 'Extension change is unsupported')
            files.relocate(connection, context, node, parent['id'], name=name[:-len(suffix)] if suffix else name)
            t = db.tables['webdav_locks']
            for lock in locks_for(connection, node, children=True):
                updated = destination + lock['uri'][len(path):] if lock['uri'].startswith(path) else lock['uri']
                change(connection, 'webdav_locks', lock['id'], uri=updated, uri_hash=hashlib.sha256(updated.encode()).hexdigest())
        else:
            copy_tree(connection, context, node, destination, depth, settings, filedav.config(connection)['copy_max_nodes'])
        return Response(status_code=204 if existing else 201)
    raise DavError(405, 'Method not allowed')


def run_request(context, path, method, headers, body, stream, settings):
    started = time.monotonic()
    try:
        with db.transaction() as connection:
            response = process(connection, context, path, method, headers, body, stream, settings)
            filedav.audit(connection, context, method, path, response.status_code,
                credential_id=context.pgp.get('dav_credential_id'), size=integer(headers.get('content-length')),
                duration=int((time.monotonic() - started) * 1000))
            return response
    except ApiError as exc:
        status = 429 if exc.message == '请求过于频繁，请稍后重试' else 401 if exc.ret == -1 else 423 if isinstance(exc.data, dict) and exc.data.get('http_status') == 423 else 403
        error = DavError(status, exc.message)
    except DavError as exc:
        error = exc
    with db.transaction() as connection:
        filedav.audit(connection, context, method, path, error.status, credential_id=context.pgp.get('dav_credential_id'), result=error.message, duration=int((time.monotonic() - started) * 1000))
    root = ET.Element(tag('error'))
    if error.condition:
        ET.SubElement(root, tag(error.condition))
    ET.SubElement(root, tag('responsedescription')).text = error.message
    headers = {'WWW-Authenticate': 'Basic realm="DooTask WebDAV", charset="UTF-8"'} if error.status == 401 else {}
    if error.status == 429:
        headers['Retry-After'] = '60'
    if error.status == 304:
        return Response(status_code=304)
    return xml_response(root, error.status, headers)


def install_dav(app, settings):
    async def endpoint(request: Request, path: str = ''):
        context = RequestContext(request_id=request.headers.get('x-request-id') or uuid.uuid4().hex,
            ip=request.client.host if request.client else '', user_agent=request.headers.get('user-agent', ''),
            base_url=str(request.base_url).rstrip('/'), headers=dict(request.headers))
        token = current_context.set(context)
        try:
            with tempfile.SpooledTemporaryFile(max_size=5 * 1024**2) as stream:
                maximum = 1024**3 if request.method == 'PUT' else 1024**2
                total = 0
                async for block in request.stream():
                    total += len(block)
                    if total > maximum:
                        return Response('Payload Too Large', status_code=413)
                    stream.write(block)
                stream.seek(0)
                body = b'' if request.method == 'PUT' else stream.read()
                stream.seek(0)
                return await asyncio.to_thread(run_request, context, normalized_path(path), request.method, dict(request.headers), body, stream, settings)
        except DavError as error:
            return Response(error.message, status_code=error.status)
        finally:
            current_context.reset(token)
    app.add_api_route('/dav', endpoint, methods=METHODS, include_in_schema=False)
    app.add_api_route('/dav/{path:path}', endpoint, methods=METHODS, include_in_schema=False)
