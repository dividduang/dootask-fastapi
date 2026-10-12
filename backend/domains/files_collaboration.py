"""Collaboration files share message/task ACL, storage and attachment identity."""
import base64
import json
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit
from sqlalchemy import select, and_, or_
from starlette.responses import FileResponse, RedirectResponse
from ..db import tables, insert_row
from ..errors import ApiError
from ..storage import confined_path
from .files_support import actor, integer, row, decoded, store_stream
from . import files

GROUPS = {'document': 'doc docx dot dotx odt ott pdf rtf txt md', 'sheet': 'csv ods ots tsv xls xlsm xlsx xlt xltx', 'slide': 'odp otp pot potx pps ppsx ppt pptx', 'image': 'bmp gif jpeg jpg png svg tif tiff webp', 'video': '3gp avi flv mkv mov mp4 mpeg mpg rm wmv', 'archive': '7z gz rar tar tgz zip'}


def file_type(ext):
    return next((kind for kind, exts in GROUPS.items() if ext in exts.split()), 'other')


def authorize_message(connection, userid, message):
    from .dialog_core import get_dialog
    from .project_core import can_view_task, can_view_project
    dialog = get_dialog(connection, message['dialog_id'])
    if dialog['group_type'] == 'task':
        t = tables['project_tasks']
        task = connection.execute(select(t).where(t.c.dialog_id == dialog['id'], t.c.deleted_at.is_(None))).mappings().first()
        if not task or not can_view_task(connection, userid, dict(task)):
            raise ApiError('无权限访问此文件')
    elif dialog['group_type'] == 'project':
        t = tables['projects']
        project = connection.execute(select(t).where(t.c.dialog_id == dialog['id'], t.c.deleted_at.is_(None))).mappings().first()
        if not project or not can_view_project(connection, userid, dict(project)):
            raise ApiError('无权限访问此文件')
    else:
        get_dialog(connection, dialog['id'], userid)
    return dialog


def attachment_lists(connection, context, data, settings):
    user = actor(connection, context)
    scope, conversation, source, kind = data.get('scope', 'all'), data.get('conversation_type', 'all'), data.get('project_source', 'all'), data.get('file_type', 'all')
    if scope not in ('all', 'conversation', 'project') or conversation not in ('all', 'private', 'group') or source not in ('all', 'project_chat', 'task') or kind not in ('all', *GROUPS, 'other'):
        raise ApiError('参数错误')
    a, m = tables['web_socket_dialog_msg_attachments'], tables['web_socket_dialog_msgs']
    query = select(a, m.c.userid.label('sender_id'), m.c.created_at.label('message_created_at')).join(m, m.c.id == a.c.msg_id).where(m.c.deleted_at.is_(None))
    if integer(data.get('sender_id')):
        query = query.where(m.c.userid == integer(data['sender_id']))
    if data.get('key'):
        query = query.where(a.c.name.contains(str(data['key'])[:100]))
    if kind in GROUPS:
        query = query.where(a.c.ext.in_(GROUPS[kind].split()))
    elif kind == 'other':
        query = query.where(a.c.ext.not_in(' '.join(GROUPS.values()).split()))
    cursor = str(data.get('cursor') or '')
    if cursor:
        try:
            prefix, msg, position, ident = cursor.split(':')
            if prefix != 'a':
                raise ValueError()
            msg, position, ident = int(msg), int(position), int(ident)
        except ValueError:
            raise ApiError('游标无效')
        query = query.where(or_(a.c.cursor_msg_id > -msg, and_(a.c.cursor_msg_id == -msg, a.c.position > position), and_(a.c.cursor_msg_id == -msg, a.c.position == position, a.c.id > ident)))
    take = max(1, min(100, integer(data.get('take'), 50)))
    result = []
    for value in connection.execute(query.order_by(a.c.cursor_msg_id, a.c.position, a.c.id)).mappings():
        attachment = dict(value)
        message = row(connection, 'web_socket_dialog_msgs', attachment['msg_id'])
        try:
            dialog = authorize_message(connection, user.userid, message)
        except ApiError:
            continue
        is_project = dialog['group_type'] in ('project', 'task')
        if scope == 'project' and not is_project or scope == 'conversation' and is_project:
            continue
        source_type = 'private' if dialog['type'] == 'user' else 'project_chat' if dialog['group_type'] == 'project' else 'task' if dialog['group_type'] == 'task' else 'group'
        if scope == 'conversation' and conversation != 'all' and source_type != conversation:
            continue
        if scope == 'project' and source != 'all' and source_type != source:
            continue
        project = task = None
        if source_type == 'task':
            t = tables['project_tasks']
            task = connection.execute(select(t).where(t.c.dialog_id == dialog['id'], t.c.deleted_at.is_(None))).mappings().first()
            project = row(connection, 'projects', task['project_id']) if task else None
        elif source_type == 'project_chat':
            t = tables['projects']
            project = connection.execute(select(t).where(t.c.dialog_id == dialog['id'], t.c.deleted_at.is_(None))).mappings().first()
        if scope == 'project' and (not project or (integer(data.get('project_id')) and project['id'] != integer(data['project_id'])) or (not integer(data.get('project_id')) and project['archived_at'])):
            continue
        name = attachment['name'] or PurePosixPath(urlsplit(attachment['path']).path).name or f"image-{attachment['msg_id']}-{attachment['position'] + 1}.{attachment['ext']}"
        sender = row(connection, 'users', attachment['sender_id'], key='userid') or {'userid': attachment['sender_id']}
        source_name = (task['name'] if task else project['name'] if project else dialog['name']) or ''
        if source_type == 'private':
            u, du = tables['users'], tables['web_socket_dialog_users']
            source_name = connection.execute(select(u.c.nickname).join(du, du.c.userid == u.c.userid).where(du.c.dialog_id == dialog['id'], du.c.userid != user.userid).limit(1)).scalar_one_or_none() or user.user.get('nickname', '')
        download = context.base_url.rstrip('/') + '/api/file/collaboration/download?attachment_id=' + str(attachment['id']) + '&token=' + context.token
        result.append({'attachment_id': attachment['id'], 'attachment_source': attachment['source_type'], 'attachment_position': attachment['position'], 'generated_name': not bool(attachment['name']), 'msg_id': attachment['msg_id'], 'dialog_id': dialog['id'], 'name': name, 'ext': attachment['ext'], 'size': attachment['size'], 'thumb': attachment['thumb'] or '', 'image_url': download if file_type(attachment['ext']) == 'image' else '', 'width': attachment['width'], 'height': attachment['height'], 'file_type': file_type(attachment['ext']), 'source_type': source_type, 'source_name': source_name, 'project_id': project['id'] if project else 0, 'project_name': project['name'] if project else '', 'task_id': task['id'] if task else 0, 'task_name': task['name'] if task else '', 'task_status': ('archived' if task['archived_at'] else 'completed' if task['complete_at'] else 'active') if task else '', 'sender': {k: sender.get(k) for k in ('userid', 'nickname', 'email', 'userimg')}, 'created_at': attachment['message_created_at']})
        if len(result) > take:
            break
    more = len(result) > take
    result = result[:take]
    last = result[-1] if result else None
    return {'list': result, 'has_more': more, 'next_cursor': f"a:{last['msg_id']}:{last['attachment_position']}:{last['attachment_id']}" if more else 0}


def resolve_attachment_path(connection, context, value, settings):
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc != urlsplit(context.base_url).netloc:
        return None
    if parsed.path == '/api/file/bytes':
        key = parse_qs(parsed.query).get('key', [''])[0]
        path, grant = files.resolve_download(connection, context, key, settings)
        return path
    return confined_path(settings.storage_root, value)


def attachment_download(connection, context, data, settings):
    user = actor(connection, context)
    attachment = row(connection, 'web_socket_dialog_msg_attachments', integer(data.get('attachment_id')))
    if not attachment:
        raise ApiError('文件不存在或已被删除')
    message = row(connection, 'web_socket_dialog_msgs', attachment['msg_id'])
    if not message or message['dialog_id'] != attachment['dialog_id']:
        raise ApiError('文件不存在或已被删除')
    authorize_message(connection, user.userid, message)
    path = resolve_attachment_path(connection, context, attachment['path'], settings)
    if path is None:
        return RedirectResponse(attachment['path'], status_code=302)
    return FileResponse(path, filename=attachment['name'] or path.name)


def send_files_from_path(connection, context, dialog_ids, local_path, name, reply_id, image_attachment, settings):
    from .dialog_core import get_dialog, send_message
    from .upload import generic_upload
    user = actor(connection, context)
    if not isinstance(dialog_ids, list):
        dialog_ids = [dialog_ids]
    ids = sorted(set(integer(d) for d in dialog_ids))
    if not ids or len(ids) > 100:
        raise ApiError('dialog_ids 不能为空')
    dialogs = [get_dialog(connection, ident, user.userid) for ident in ids]
    with local_path.open('rb') as stream:
        metadata = generic_upload(connection, context, stream, name, {}, settings)
    metadata['size'] = local_path.stat().st_size
    metadata['thumb'] = ''
    if file_type(metadata['ext']) == 'image' and metadata['ext'] != 'svg':
        from PIL import Image
        with Image.open(local_path) as image:
            image.verify()
            metadata.update(width=image.width, height=image.height, thumb=metadata['path'])
    result = None
    for dialog in dialogs:
        from datetime import timedelta
        from ..context import utcnow
        import secrets
        original_key = parse_qs(urlsplit(metadata['path']).query)['key'][0]
        original_grant = row(connection, 'backend_file_downloads', original_key)
        scoped_key = secrets.token_hex(32)
        insert_row(connection, 'backend_file_downloads', id=scoped_key, userid=user.userid, dialog_id=dialog['id'],
                   path=original_grant['path'], name=metadata['name'], ready=1, expires_at=utcnow() + timedelta(days=3650))
        metadata = {**metadata, 'path': '/api/file/bytes?key=' + scoped_key, 'url': '/api/file/bytes?key=' + scoped_key}
        if metadata['thumb']:
            metadata['thumb'] = metadata['path']
        task = None
        if dialog['group_type'] == 'task' and (image_attachment or file_type(metadata['ext']) != 'image'):
            t = tables['project_tasks']
            task = connection.execute(select(t).where(t.c.dialog_id == dialog['id'], t.c.deleted_at.is_(None))).mappings().first()
            if task:
                from .project_core import can_view_task
                if not can_view_task(connection, user.userid, dict(task)):
                    raise ApiError('无权限访问此文件')
                insert_row(connection, 'project_task_files', project_id=task['project_id'], task_id=task['id'], name=metadata['name'], size=metadata['size'], ext=metadata['ext'], path=metadata['path'], thumb=metadata['thumb'], userid=user.userid)
        result = send_message(connection, dialog_id=dialog['id'], sender_id=user.userid, msg_type='file', msg=metadata, context=context, reply_id=reply_id)
        if task:
            result['task_id'] = task['id']
    return result


def format_file_data(connection, context, data, settings):
    result = dict(data)
    path = resolve_attachment_path(connection, context, result['path'], settings)
    ext = result.get('ext', '')
    if path is not None and (ext in ('md', 'text', 'drawio', 'mind') or files.upload_type(ext) in ('txt', 'code') and path.stat().st_size < 2 * 1024**2):
        text = path.read_text(encoding='utf-8', errors='replace')
        result['content'] = decoded(text) if ext == 'mind' else {'xml': text} if ext == 'drawio' else {'type': ext, 'content': text} if ext in ('md', 'text') else {'content': text}
        result['file_mode'] = ext if ext in ('md', 'text', 'drawio', 'mind') else 'code'
    elif files.upload_type(ext) in files.OFFICE:
        result.update(content={}, file_mode='office')
    else:
        result.update(content={'preview': True, 'name': result['name'], 'key': result['path']}, file_mode='preview')
    return result
