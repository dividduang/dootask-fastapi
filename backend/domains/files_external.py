import base64
import hashlib
import hmac
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from ..errors import ApiError
from .files_config import external_settings
from .files_support import decoded, encoded, actor, store_stream


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload=None, *, headers=None, timeout=30, method=None):
    request = Request(url, data=encoded(payload).encode() if payload is not None else None,
                      headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(16 * 1024**2 + 1)
            if len(raw) > 16 * 1024**2:
                raise ApiError('外部服务响应过大')
            value = json.loads(raw)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise ApiError('外部服务请求失败: ' + str(exc)) from exc
    if not isinstance(value, (dict, list)):
        raise ApiError('外部服务响应格式错误')
    return value


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def office_token(connection, context, config, settings):
    from .files import require, resolve
    from .files_support import row, integer
    from .apps import require_installed
    from urllib.parse import parse_qs
    require_installed(settings, 'office')
    config = decoded(config)
    if not isinstance(config, dict):
        raise ApiError('参数错误')
    cfg = external_settings()
    if not cfg.office_url or not cfg.office_secret:
        raise ApiError('Office服务未配置')
    document, editor = config.get('document', {}), config.get('editorConfig', {})
    if not isinstance(document, dict) or not isinstance(editor, dict):
        raise ApiError('参数错误')
    url = urlsplit(str(document.get('url', '')))
    base = urlsplit(context.base_url)
    # Unchanged OnlyOffice.vue hardcodes this internal reverse-proxy origin.
    origins = {(base.scheme, base.netloc), ('http', 'nginx')}
    if (url.scheme, url.netloc) not in origins:
        raise ApiError('Office文件地址不合法')
    query = parse_qs(url.query)
    view_only = editor.get('mode') == 'view'
    resource_id = None
    path = url.path.lower().rstrip('/')
    if query.get('id') and path == '/api/file/content':
        resource_id = query['id'][0]
        if str(resource_id).isdigit():
            require(connection, context, resource_id, 0 if view_only else 1)
        elif view_only:
            resolve(connection, context, resource_id)
        else:
            raise ApiError('分享文档仅支持查看')
    elif query.get('key') and path == '/api/file/bytes':
        user = actor(connection, context)
        download = row(connection, 'backend_file_downloads', query['key'][0])
        if not download or download['userid'] != user.userid or not download['file_id']:
            raise ApiError('Office文件地址不合法')
        resource_id = download['file_id']
        require(connection, context, resource_id, 0 if view_only else 1)
    elif path == '/api/dialog/msg/download' and query.get('msg_id') and view_only:
        from .dialog_core import get_message
        get_message(connection, integer(query['msg_id'][0]), actor(connection, context).userid)
    elif path == '/api/project/task/filedown' and query.get('file_id') and view_only:
        from .project_core import can_view_task
        attachment = row(connection, 'project_task_files', integer(query['file_id'][0]))
        task = row(connection, 'project_tasks', attachment['task_id']) if attachment else None
        if not task or not can_view_task(connection, actor(connection, context).userid, task):
            raise ApiError('没有查看访问权限')
    else:
        raise ApiError('Office文件地址不合法')
    callback_value = editor.get('callbackUrl')
    if not view_only:
        callback = urlsplit(str(callback_value or ''))
        if (callback.scheme, callback.netloc) not in origins or callback.path.lower() not in ('/api/file/content/office', '/api/file/content__office'):
            raise ApiError('Office回调地址不合法')
        if parse_qs(callback.query).get('id', [''])[0] != str(resource_id):
            raise ApiError('Office回调文件不一致')
    elif callback_value:
        raise ApiError('只读Office文档不允许写入回调')
    body = b64(b'{"alg":"HS256","typ":"JWT"}') + '.' + b64(encoded(config).encode())
    return body + '.' + b64(hmac.new(cfg.office_secret.encode(), body.encode(), hashlib.sha256).digest())


def verify_office_token(token, secret):
    try:
        header, body, signature = token.split('.')
        expected = b64(hmac.new(secret.encode(), (header + '.' + body).encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        if json.loads(base64.urlsafe_b64decode(header + '=' * (-len(header) % 4))).get('alg') != 'HS256':
            raise ValueError()
        return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except (ValueError, TypeError, KeyError) as exc:
        raise ApiError('Office回调签名无效') from exc


def office_save(connection, context, data, settings):
    from .files import require, save_blob
    from .apps import require_installed
    require_installed(settings, 'office')
    item = require(connection, context, data.get('id'), 1, True)
    cfg = external_settings()
    if not cfg.office_url or not cfg.office_secret:
        raise ApiError('Office服务未配置')
    payload = verify_office_token(str(data.get('token', '')), cfg.office_secret)
    if 'payload' in payload:
        payload = payload['payload']
    for name in ('status', 'key'):
        if name not in payload or str(payload[name]) != str(data.get(name, '')):
            raise ApiError('Office回调签名数据不一致')
    if int(data.get('status', 0)) in (2, 6) and ('url' not in payload or payload['url'] != data.get('url')):
        raise ApiError('Office回调签名数据不一致')
    if int(data.get('status', 0)) in (2, 6):
        supplied, target = urlsplit(str(data.get('url', ''))), urlsplit(cfg.office_url)
        if supplied.scheme not in ('http', 'https') or not supplied.path.startswith('/'):
            raise ApiError('Office下载地址无效')
        url = urlunsplit((target.scheme, target.netloc, supplied.path, supplied.query, ''))
        try:
            with build_opener(NoRedirect).open(Request(url), timeout=60) as response:
                blob = store_stream(settings, response)
        except (OSError, HTTPError, URLError) as exc:
            raise ApiError('Office文件下载失败') from exc
        save_blob(connection, context, item, blob, extra={'from': url})
    return {'error': 0}
