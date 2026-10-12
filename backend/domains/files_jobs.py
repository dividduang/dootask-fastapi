"""Durable cabinet jobs. ZIP publication is content-addressed and transactionally advertised."""
import io
import tempfile
import zipfile
from datetime import timedelta
from sqlalchemy import select, or_
from .. import jobs
from ..auth import Actor, is_disabled
from ..context import RequestContext, utcnow
from ..db import tables
from ..errors import ApiError
from ..storage import confined_path
from .files_support import row, decoded, store_stream, change
from . import files


def pack(connection, payload, settings):
    from .dialog_core import ensure_user_dialog, send_message
    grant = row(connection, 'backend_file_downloads', payload['key'], lock=True)
    if not grant or grant['ready']:
        return
    user = row(connection, 'users', payload['userid'], key='userid')
    if not user or is_disabled(user):
        raise ApiError('用户不可用')
    context = RequestContext(request_id='pack-' + str(payload['_event_id']), actor=Actor(user['userid'], user, {}, {}))
    entries, seen, total = [], set(), 0
    def visit(item, prefix):
        nonlocal total
        files.require(connection, context, item['id'], 1)
        relative = prefix + files.full_name(item)
        if relative in seen:
            relative = prefix + item['name'] + ' [#' + str(item['id']) + ']' + ('.' + item['ext'] if item['ext'] else '')
        seen.add(relative)
        if item['type'] == 'folder':
            entries.append((relative + '/', None))
            for child in files.children(connection, item['id']):
                visit(child, relative + '/')
        else:
            content = files.latest(connection, item['id'])
            path = confined_path(settings.storage_root, decoded(content['content'])['url']) if content else settings.frontend_root / 'public/assets/office' / ('empty.' + item['ext']) if item['type'] in files.OFFICE else None
            if path:
                total += path.stat().st_size
                if total > 1024**3:
                    raise ApiError('文件总大小已超过1GB，请分批下载')
            entries.append((relative, path))
    for ident in payload['ids']:
        visit(files.require(connection, context, ident, 1), '')
    with tempfile.TemporaryFile() as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for index, (name, path) in enumerate(entries):
                if path:
                    archive.write(path, name)
                else:
                    archive.writestr(name, b'')
                if index % max(1, len(entries) // 20) == 0:
                    jobs.enqueue_push(connection, userids=[user['userid']], message={'type': 'file', 'action': 'compress', 'data': {'name': payload['name'], 'url': payload['url'], 'progress': int((index + 1) * 100 / max(1, len(entries)))}})
        stream.seek(0)
        blob = store_stream(settings, stream, maximum=2 * 1024**3)
    change(connection, 'backend_file_downloads', grant['id'], path=blob['url'], ready=1)
    jobs.enqueue_push(connection, userids=[user['userid']], message={'type': 'file', 'action': 'compress', 'data': {'name': payload['name'], 'url': payload['url'], 'progress': 100}})
    from .account_meetings import bot_get
    bot = bot_get(connection, 'system-msg')
    dialog = ensure_user_dialog(connection, bot['userid'], user['userid'])
    send_message(connection, dialog_id=dialog, sender_id=bot['userid'], msg_type='template', msg={'type': 'file_download', 'title': '文件下载打包已完成', 'name': payload['name'], 'size': blob['size'], 'url': payload['url']}, context=context)


def expire(connection, payload, settings):
    now = utcnow()
    from .files_config import external_settings
    from .files_locks import serialize_namespace
    serialize_namespace(connection)
    for name, column, hours in [('web_socket_tmp_msgs', 'created_at', 1), ('tmps', 'created_at', 24), ('task_workers', 'deleted_at', 12)]:
        table = tables[name]
        connection.execute(table.delete().where(table.c[column] < now - timedelta(hours=hours)))
    cfg = external_settings()
    if cfg.recycle_days > 0:
        table = tables['files']
        expired = list(connection.execute(select(table.c.id).where(table.c.deleted_at < now - timedelta(days=cfg.recycle_days))).scalars())
        if expired:
            for name, column in [('file_contents', 'fid'), ('file_links', 'file_id'), ('file_users', 'file_id'), ('webdav_locks', 'file_id')]:
                related = tables[name]
                connection.execute(related.delete().where(related.c[column].in_(expired)))
            props = tables['webdav_properties']
            connection.execute(props.delete().where(props.c.path.in_(['file:' + str(i) for i in expired])))
            connection.execute(table.delete().where(table.c.id.in_(expired)))
    for name in ('backend_uploads', 'backend_file_downloads'):
        table = tables[name]
        connection.execute(table.delete().where(table.c.expires_at < now))
    locks = tables['webdav_locks']
    connection.execute(locks.delete().where(locks.c.timeout_at < now))
    from .filedav import config
    logs = tables['webdav_operation_logs']
    connection.execute(logs.delete().where(logs.c.created_at < now - timedelta(days=config(connection)['audit_retention_days'])))
    # Only abandoned staging files are removed here. Immutable blobs may still be
    # referenced by retained histories, messages or reports and are never age-deleted.
    staging = confined_path(settings.storage_root, 'staging')
    if staging.is_dir() and cfg.temporary_days > 0:
        from datetime import timezone
        cutoff = now.replace(tzinfo=timezone.utc).timestamp() - 86400 * cfg.temporary_days
        for path in staging.iterdir():
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink()


def attachment_backfill(connection, payload, settings):
    from ..db import setting
    from .files_support import save_setting
    from .dialog_core import _attachments, message_data
    table = tables['web_socket_dialog_msgs']
    progress = setting(connection, 'attachmentBackfill')
    after = int(progress.get('after', 0))
    rows = list(connection.execute(select(table).where(table.c.id > after, table.c.deleted_at.is_(None)).order_by(table.c.id).limit(200).with_for_update()).mappings())
    attachments = tables['web_socket_dialog_msg_attachments']
    for message in rows:
        if not connection.execute(select(attachments.c.id).where(attachments.c.msg_id == message['id']).limit(1)).first():
            _attachments(connection, message_data(message))
    if rows:
        save_setting(connection, 'attachmentBackfill', {'after': rows[-1]['id'], 'processed': int(progress.get('processed', 0)) + len(rows)})


def register_jobs():
    jobs.register('file.pack', pack)
    jobs.register('files.expire', expire)
    jobs.schedule('files.expire', 3600)
    jobs.register('files.attachment_backfill', attachment_backfill)
    jobs.schedule('files.attachment_backfill', 60)
