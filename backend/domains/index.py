"""Non-API IndexController routes, installed explicitly before the SPA host."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlsplit, parse_qs
from fastapi import Request
from starlette.responses import Response, RedirectResponse, FileResponse, JSONResponse, HTMLResponse
from .. import db, jobs
from ..context import RequestContext, current_context, utcnow
from ..errors import ApiError, success, failure, json_default
from ..host import render_template
from ..storage import confined_path
from .files_config import external_settings
from .files_external import request_json
from .files_support import actor, row, safe_name, encoded, decoded, store_stream, save_setting, integer
from . import files


def api(connection, context, data, settings):
    return RedirectResponse(context.base_url.rstrip('/') + '/docs/index.html', status_code=301)


def to_preview_url(context, data):
    name = data['name']
    ext = data.get('ext', '')
    if ext and not name.lower().endswith('.' + ext.lower()):
        name += '.' + ext
    return context.base_url.rstrip('/') + '/online/preview/' + quote(name) + '?' + urlencode({'key': data['path'], 'name': name, 'ext': ext})


def online__preview(connection, context, data, settings):
    key = str(data.get('key', ''))
    name = str(data.get('name') or data.get('_child') or '')
    ext = str(data.get('ext') or PurePosixPath(name).suffix.lstrip('.')).lower()
    parsed = urlsplit(key)
    query = parse_qs(parsed.query)
    name = query.get('name', [name])[0]
    ext = query.get('ext', [ext])[0].lower()
    download_url, path = key, None
    grant_key = query.get('key', [''])[0]
    if grant_key:
        path, grant = files.resolve_download(connection, context, grant_key, settings)
        download_url = context.base_url.rstrip('/') + '/api/file/bytes?key=' + grant_key
        name = name or grant['name']
        ext = ext or PurePosixPath(name).suffix.lstrip('.').lower()
    elif data.get('id'):
        item = files.resolve(connection, context, data['id'])
        content = files.latest(connection, item['id'])
        if content:
            path = confined_path(settings.storage_root, decoded(content['content'])['url'])
        elif item['type'] in files.OFFICE:
            path = settings.frontend_root / 'public/assets/office' / ('empty.' + item['ext'])
        name, ext = files.full_name(item), item['ext']
        download_url = context.base_url.rstrip('/') + '/api/file/content?' + urlencode({'id': data['id'], 'down': 'yes', 'token': context.token})
    elif key.startswith('assets/office/empty.'):
        path = confined_path(settings.frontend_root / 'public', parsed.path)
        download_url = context.base_url.rstrip('/') + '/' + parsed.path
    else:
        # Cabinet relative paths are not public: fetch requires authenticated object ACL.
        user = actor(connection, context)
        t = db.tables['file_contents']
        for content in connection.execute(__import__('sqlalchemy').select(t).where(t.c.deleted_at.is_(None))).mappings():
            if decoded(content['content']).get('url') == parsed.path.lstrip('/') and files.can_access(connection, user.userid, content['fid']):
                item = files.get(connection, content['fid'])
                path = confined_path(settings.storage_root, decoded(content['content'])['url'])
                name, ext = name or files.full_name(item), ext or item['ext']
                download_url = files.grant(connection, context, settings, item, dict(content))
                break
    if path is None or not path.is_file():
        raise ApiError('文件不存在或已被删除')
    if ext == 'pdf':
        if path.stat().st_size > 10 * 1024**2:
            return render_template('download', {'system_alias': settings.system_alias, 'name': name, 'size': readable(path.stat().st_size), 'url': download_url, 'button': '点击下载', 'csrf_token': ''})
        agent = context.user_agent.lower()
        if 'electron' in agent or 'chrome' in agent or 'safari' in agent and 'ios_kuaifan_eeui' not in agent:
            return FileResponse(path, filename=name, media_type='application/pdf', content_disposition_type='inline')
        if 'ios_kuaifan_eeui' in agent:
            message = {'action': 'eeuiAppSendMessage', 'data': [{'action': 'setPageData', 'data': {'showProgress': True, 'titleFixed': True, 'urlFixed': True}}, {'action': 'createTarget', 'url': download_url}]}
            return HTMLResponse('<script>window.top.postMessage(' + encoded(message).replace('<', '\\u003c') + ',"*")</script>')
    target = external_settings().preview_url
    if not target:
        raise ApiError('文件预览服务未配置')
    url = download_url + ('&' if '?' in download_url else '?') + urlencode({'fullfilename': PurePosixPath(name).stem + '_' + str(int(path.stat().st_mtime)) + '.' + ext})
    return RedirectResponse(target.rstrip('/') + '/onlinePreview?' + urlencode({'url': base64.b64encode(url.encode()).decode()}), status_code=301)


def drawio__iconsearch(connection, context, data, settings):
    params = {'q': str(data.get('q', '')).strip(), 'p': str(data.get('p', '')).strip(), 'c': str(data.get('c', '')).strip()}
    return request_json('https://app.diagrams.net/iconSearch?' + urlencode(params), timeout=30)


def readable(size):
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.2f} {unit}'
        value /= 1024


def desktop__publish(connection, context, data, settings):
    from .files_locks import serialize_namespace
    version = context.headers.get('publish-version', '')
    child = str(data.get('_child') or '')
    value = db.setting(connection, 'desktopPublish')
    releases, drafts = value.get('releases', {}), value.get('drafts', {})
    latest = value.get('latest', '0.0.1')
    if re.fullmatch(r'\d+\.\d+\.\d+', version):
        key = external_settings().publish_key
        if not key or not hmac.compare_digest(context.headers.get('publish-key', ''), key):
            raise ApiError('key error')
        serialize_namespace(connection)
        value = db.setting(connection, 'desktopPublish')
        releases, drafts = value.get('releases', {}), value.get('drafts', {})
        if data.get('action') == 'release':
            if not drafts.get(version):
                raise ApiError('draft version not exists')
            releases[version] = {'files': drafts[version], 'released_at': utcnow().isoformat()}
            latest = version
            # Keep newest five and anything published in the last 30 days.
            ordered = sorted(releases, key=lambda v: tuple(int(p) for p in v.split('.')), reverse=True)
            cutoff = utcnow() - timedelta(days=30)
            releases = {v: releases[v] for i, v in enumerate(ordered) if i < 5 or datetime.fromisoformat(releases[v]['released_at']) >= cutoff}
            save_setting(connection, 'desktopPublish', {'latest': latest, 'releases': releases, 'drafts': {}})
            return success([])
        upload = data.get('file')
        if not hasattr(upload, 'file'):
            raise ApiError('请选择上传文件')
        name = safe_name(upload.filename)
        blob = store_stream(settings, upload.file, maximum=4 * 1024**3)
        draft = drafts.setdefault(version, {})
        draft[name] = {**blob, 'name': name, 'time': utcnow().isoformat()}
        save_setting(connection, 'desktopPublish', {**value, 'releases': releases, 'drafts': drafts})
        return success({'name': name, 'path': '/uploads/desktop-draft/' + version + '/' + quote(name), 'size': blob['size'] / 1024, 'ext': PurePosixPath(name).suffix.lstrip('.')})
    if child.lower() == 'latest':
        child = latest
    if re.fullmatch(r'v?\d+\.\d+\.\d+', child):
        selected = child.lstrip('v')
        is_draft = selected not in releases
        items = drafts.get(selected) if is_draft else releases[selected]['files']
        if items is None:
            return Response(status_code=404)
        entries = []
        for name, item in items.items():
            if re.search(r'\.(zip|yml|yaml|blockmap)$', name, re.I) or name.endswith('-win.exe'):
                continue
            entries.append({'name': name, 'time': item['time'].replace('T', ' ')[:19], 'size': readable(item['size']), 'url': '/uploads/' + ('desktop-draft' if is_draft else 'desktop') + '/' + selected + '/' + quote(name)})
        return render_template('desktop', {'system_alias': settings.system_alias, 'version': selected, 'files': entries, 'is_draft': is_draft, 'latest_version': latest, 'other_version': [{'version': v, 'url': '/desktop/publish/' + v} for v in sorted(releases, reverse=True) if v != selected], 'csrf_token': ''})
    item = releases.get(latest, {}).get('files', {}).get(child)
    return FileResponse(confined_path(settings.storage_root, item['url']), filename=child) if item else Response(status_code=404)


def release_file(connection, context, data, settings):
    value = db.setting(connection, 'desktopPublish')
    version, name = data['version'], safe_name(data['name'])
    items = value.get('drafts', {}).get(version, {}) if data['draft'] else value.get('releases', {}).get(version, {}).get('files', {})
    item = items.get(name)
    return FileResponse(confined_path(settings.storage_root, item['url']), filename=name) if item else Response(status_code=404)


def crontab(connection, context, data, settings):
    try:
        address = ipaddress.ip_address(context.ip)
    except ValueError:
        return Response('Forbidden Access', media_type='text/plain')
    cfg = external_settings()
    if not address.is_private and not address.is_loopback:
        return Response('Forbidden Access', media_type='text/plain')
    if cfg.crontab_key and not hmac.compare_digest(context.headers.get('x-crontab-key', ''), cfg.crontab_key):
        return Response('Forbidden Access', media_type='text/plain')
    # The concrete owning modules register their real jobs and schedules. Unknown
    # required kinds fail instead of announcing that unimplemented cron work ran.
    required = ('files.expire', 'files.attachment_backfill', 'accounts.expire', 'projects.maintenance', 'projects.ai_scan', 'checkin.remind', 'accounts.close_meetings', 'dialog.maintenance', 'dialog.todo_remind', 'dialog.delete_bot_messages', 'dialog.email_notice', 'dialog.joke_soup')
    missing = [kind for kind in required if kind not in jobs.HANDLERS]
    if missing:
        raise ApiError('定时任务尚未集成: ' + ', '.join(missing))
    slot = int(utcnow().replace(tzinfo=__import__('datetime').timezone.utc).timestamp()) // 60
    for kind, (_, payload) in jobs.SCHEDULES.items():
        jobs.enqueue(connection, kind, payload, dedupe_key=f'index-cron:{kind}:{slot}')
    if external_settings().manticore_url:
        jobs.enqueue(connection, 'search.initialize', {}, dedupe_key=f'index-cron:search:{slot}')
    return Response('success', media_type='text/plain')


ROUTES = {'api': api, 'crontab': crontab, 'desktop/publish': desktop__publish,
          'drawio/iconsearch': drawio__iconsearch, 'online/preview': online__preview}
# main/version/health/avatar are owned and installed by the shared host/ avatar module.


def install_index(app, settings):
    async def run(request: Request, handler, extra=None):
        from ..main import read_inputs, token_input
        from ..host import verify_csrf
        data = await read_inputs(request)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.path.rstrip('/') != '/desktop/publish':
            if not verify_csrf(request, settings.csrf_secret, data.get('_token') or request.headers.get('x-csrf-token')):
                if hasattr(request.state, 'upload_form'):
                    await request.state.upload_form.close()
                return Response('CSRF token mismatch', status_code=419)
        data.update(extra or {})
        context = RequestContext(request_id=request.headers.get('x-request-id') or uuid.uuid4().hex,
            language=request.headers.get('language', 'zh'), token=token_input(request, data),
            ip=request.client.host if request.client else '', user_agent=request.headers.get('user-agent', ''),
            base_url=str(request.base_url).rstrip('/'), headers=dict(request.headers))
        def execute():
            token = current_context.set(context)
            try:
                with db.transaction() as connection:
                    result = handler(connection, context, data, settings)
                return result if isinstance(result, Response) else JSONResponse(json.loads(encoded(result)))
            except ApiError as exc:
                return JSONResponse(json.loads(encoded(failure(exc))))
            finally:
                current_context.reset(token)
        try:
            return await asyncio.to_thread(execute)
        finally:
            if hasattr(request.state, 'upload_form'):
                await request.state.upload_form.close()
    async def api_route(request: Request):
        return await run(request, api)
    async def cron_route(request: Request):
        return await run(request, crontab)
    async def icon_route(request: Request):
        return await run(request, drawio__iconsearch)
    async def preview_route(request: Request, name: str = ''):
        return await run(request, online__preview, {'_child': name})
    async def publish_route(request: Request, name: str = ''):
        return await run(request, desktop__publish, {'_child': name})
    async def published_file(request: Request, version: str, name: str):
        return await run(request, release_file, {'version': version, 'name': name, 'draft': False})
    async def draft_file(request: Request, version: str, name: str):
        return await run(request, release_file, {'version': version, 'name': name, 'draft': True})
    methods = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']
    for path, endpoint in [('/api', api_route), ('/crontab', cron_route), ('/drawio/iconsearch', icon_route), ('/online/preview', preview_route), ('/online/preview/{name:path}', preview_route), ('/desktop/publish', publish_route), ('/desktop/publish/{name:path}', publish_route)]:
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
    app.add_api_route('/uploads/desktop/{version}/{name}', published_file, methods=['GET', 'HEAD'], include_in_schema=False)
    app.add_api_route('/uploads/desktop-draft/{version}/{name}', draft_file, methods=['GET', 'HEAD'], include_in_schema=False)
