"""Resumable uploads. Row locks coordinate processes; immutable chunks survive rollback."""
import io
import math
import secrets
from datetime import timedelta
from sqlalchemy import select
from ..context import utcnow
from ..db import tables, insert_row, setting
from ..errors import ApiError
from ..storage import confined_path
from .files_support import actor, integer, decoded, encoded, safe_name, row, store_stream
from . import files

CHUNK_SIZE = 5 * 1024**2
SCENES = {'file_cabinet', 'dialog_file', 'image', 'generic_file'}


def session(connection, context, ident):
    user = actor(connection, context)
    value = row(connection, 'backend_uploads', str(ident), lock=True)
    if not value or value['userid'] != user.userid or value['expires_at'] <= utcnow() or value['state'] == 'cancelled':
        raise ApiError('上传会话不存在或已过期')
    return value


def session_view(value):
    return {'done': False, 'upload_id': value['id'], 'chunk_size': CHUNK_SIZE,
            'chunk_count': math.ceil(value['size'] / CHUNK_SIZE), 'received': sorted(int(i) for i in decoded(value['received']))}


def init(connection, context, data, settings):
    import re
    user = actor(connection, context)
    digest, size = str(data.get('hash', '')).lower(), integer(data.get('size'))
    name, scene = safe_name(data.get('name')), data.get('scene')
    params = decoded(data.get('scene_params'))
    if not re.fullmatch('[0-9a-f]{32}', digest):
        raise ApiError('文件 hash 格式错误')
    limit = max(1, integer(setting(connection, 'system', 'file_upload_limit', 1024), 1024)) * 1024**2
    if not 0 < size <= min(10 * 1024**3, limit):
        raise ApiError('文件大小超限')
    if scene not in SCENES or not isinstance(params, dict):
        raise ApiError('不支持的上传场景')
    if scene == 'file_cabinet':
        files.parent_for_write(connection, context, integer(params.get('pid')))
        t = tables['files']
        candidates = connection.execute(select(t).where(t.c.userid == user.userid, t.c.hash == digest, t.c.size == size, t.c.deleted_at.is_(None))).mappings()
        for candidate in candidates:
            old = files.latest(connection, candidate['id'])
            if old:
                from .files_support import blob_path
                with blob_path(settings, old['content']).open('rb') as stream:
                    result = files.upload_path(connection, context, settings, stream, name, params)
                return {'done': True, 'instant': True, 'addItem': result, 'msg': name + ' 秒传成功'}
    t = tables['backend_uploads']
    existing = connection.execute(select(t).where(t.c.userid == user.userid, t.c.hash == digest, t.c.size == size,
        t.c.scene == scene, t.c.params == encoded(params), t.c.name == name, t.c.state == 'pending', t.c.expires_at > utcnow()).with_for_update()).mappings().first()
    if existing:
        return session_view(existing)
    ident = secrets.token_hex(16)
    insert_row(connection, 'backend_uploads', id=ident, userid=user.userid, hash=digest, size=size, name=name,
               scene=scene, params=encoded(params), received='{}', expires_at=utcnow() + timedelta(days=1))
    return session_view(row(connection, 'backend_uploads', ident))


def chunk(connection, context, data, settings):
    value = session(connection, context, data.get('upload_id'))
    if value['state'] != 'pending':
        raise ApiError('上传已完成')
    index = integer(data.get('index'), -1)
    count = math.ceil(value['size'] / CHUNK_SIZE)
    if not 0 <= index < count:
        raise ApiError('分片序号超出范围')
    upload = data.get('blob') or data.get('chunk') or data.get('file')
    if not hasattr(upload, 'file'):
        raise ApiError('分片数据无效')
    expected = CHUNK_SIZE if index < count - 1 else value['size'] - index * CHUNK_SIZE
    blob = store_stream(settings, upload.file, maximum=expected)
    if blob['size'] != expected:
        raise ApiError('分片大小不符合预期')
    received = decoded(value['received'])
    received[str(index)] = blob
    t = tables['backend_uploads']
    connection.execute(t.update().where(t.c.id == value['id']).values(received=encoded(received), updated_at=utcnow(), expires_at=utcnow() + timedelta(days=1)))
    return {'upload_id': value['id'], 'received': sorted(int(i) for i in received)}


class ChunkReader:
    def __init__(self, paths):
        self.paths = iter(paths)
        self.current = None

    def read(self, size):
        while True:
            if self.current is None:
                path = next(self.paths, None)
                if path is None:
                    return b''
                self.current = path.open('rb')
            data = self.current.read(size)
            if data:
                return data
            self.current.close()
            self.current = None

    def close(self):
        if self.current is not None:
            self.current.close()


def merge(connection, context, data, settings):
    value = session(connection, context, data.get('upload_id'))
    if value['state'] == 'complete':
        return decoded(value['result'], [])
    chunks = decoded(value['received'])
    count = math.ceil(value['size'] / CHUNK_SIZE)
    if set(chunks) != {str(i) for i in range(count)}:
        raise ApiError('分片不完整，无法合并')
    reader = ChunkReader([confined_path(settings.storage_root, chunks[str(i)]['url']) for i in range(count)])
    try:
        blob = store_stream(settings, reader, maximum=value['size'], expected_hash=value['hash'])
    finally:
        reader.close()
    if blob['size'] != value['size']:
        raise ApiError('文件大小不符合预期')
    params = decoded(value['params'])
    with confined_path(settings.storage_root, blob['url']).open('rb') as stream:
        if value['scene'] == 'file_cabinet':
            result = files.upload_path(connection, context, settings, stream, value['name'], params)
        elif value['scene'] == 'dialog_file':
            from .files_collaboration import send_files_from_path
            result = send_files_from_path(connection, context, params.get('dialog_ids', []), confined_path(settings.storage_root, blob['url']), value['name'], integer(params.get('reply_id')), bool(params.get('image_attachment')), settings)
        else:
            result = generic_upload(connection, context, stream, value['name'], params, settings, image=value['scene'] == 'image')
    t = tables['backend_uploads']
    connection.execute(t.update().where(t.c.id == value['id']).values(state='complete', result=encoded(result), updated_at=utcnow()))
    return result


def generic_upload(connection, context, stream, name, params, settings, *, image=False):
    user = actor(connection, context)
    width = height = None
    if image:
        from PIL import Image, ImageOps
        try:
            with Image.open(stream) as source:
                source.load()
                source = ImageOps.exif_transpose(source)
                w, h = min(8192, max(1, integer(params.get('width'), 2160))), min(8192, max(1, integer(params.get('height'), 4160)))
                mode = str(params.get('whcut', 'percentage'))
                if mode in ('1', 'cover'):
                    source = ImageOps.fit(source, (w, h))
                else:
                    source.thumbnail((w, h))
                width, height = source.size
                output = io.BytesIO()
                source.save(output, format='PNG')
                output.seek(0)
                blob = store_stream(settings, output, maximum=64 * 1024**2)
                name = safe_name(name).rsplit('.', 1)[0] + '.png'
        except (OSError, ValueError) as exc:
            raise ApiError('图片数据无效') from exc
    else:
        blob = store_stream(settings, stream, maximum=max(1, integer(setting(connection, 'system', 'file_upload_limit', 1024))) * 1024**2)
    # These upload URLs are random bearer capabilities, not a public filesystem mount.
    token = secrets.token_hex(32)
    insert_row(connection, 'backend_file_downloads', id=token, userid=user.userid, path=blob['url'], name=name, ready=1, expires_at=utcnow() + timedelta(days=3650))
    url = '/api/file/bytes?key=' + token
    result = {'name': name, 'path': url, 'url': url, 'size': round(blob['size'] / 1024, 2), 'ext': name.rsplit('.', 1)[-1].lower() if '.' in name else '', 'hash': blob['hash']}
    if image:
        result.update(width=width, height=height)
    return result


def cancel(connection, context, data, settings):
    user = actor(connection, context)
    t = tables['backend_uploads']
    connection.execute(t.update().where(t.c.id == str(data.get('upload_id')), t.c.userid == user.userid, t.c.state == 'pending').values(state='cancelled', updated_at=utcnow()))
    return []


ROUTES = {'upload/init': init, 'upload/chunk': chunk, 'upload/merge': merge, 'upload/cancel': cancel}
