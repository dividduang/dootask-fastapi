"""Complaint persistence and administrator notification."""
from sqlalchemy import select
from .. import db, jobs
from ..auth import require_admin
from ..errors import ApiError
from .project_core import actor, table, integer, page, decode, encode, update
from .dialog_core import get_dialog, ensure_user_dialog, send_message


def lists(connection, context, data, settings):
    require_admin(actor(connection, context))
    t = table('complaints')
    query = select(t)
    if integer(data.get('type')):
        query = query.where(t.c.type == integer(data['type']))
    if data.get('status') not in (None, ''):
        query = query.where(t.c.status == integer(data['status']))
    result = page(connection, query.order_by(t.c.id.desc()), data)
    for item in result['data']:
        item['imgs'] = decode(item['imgs'])
    return result


def submit(connection, context, data, settings):
    user = actor(connection, context)
    did = integer(data.get('dialog_id'))
    get_dialog(connection, did, user.userid)
    kind, reason = integer(data.get('type')), str(data.get('reason', '')).strip()
    if not kind:
        raise ApiError('请选择举报类型')
    if not reason or len(reason) > 500:
        raise ApiError('请填写举报原因，最多500个字')
    images = []
    for item in decode(data.get('imgs')):
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise ApiError('举报图片参数错误')
        path = item['path']
        if path.startswith(context.base_url.rstrip('/') + '/'):
            path = path[len(context.base_url.rstrip('/'))+1:]
        images.append(path)
    cid = db.insert_row(connection, 'complaints', dialog_id=did, userid=user.userid, type=kind, reason=reason, imgs=encode(images))
    jobs.enqueue(connection, 'complaints.notify', {'id': cid})
    return []


def action(connection, context, data, settings):
    require_admin(actor(connection, context))
    cid = integer(data.get('id'))
    if data.get('type') == 'handle':
        update(connection, 'complaints', cid, {'status': 1})
    elif data.get('type') == 'delete':
        t = table('complaints')
        connection.execute(t.delete().where(t.c.id == cid))
    else:
        raise ApiError('操作类型错误')
    return []


def notify(connection, payload, settings):
    from .account_meetings import bot_get
    from .project_core import by_id
    complaint = by_id(connection, 'complaints', payload['id'])
    if not complaint:
        return
    bot = bot_get(connection, 'system-msg')
    users = table('users')
    administrators = connection.execute(select(users.c.userid).where(users.c.identity.like('%,admin,%'), users.c.disable_at.is_(None))
        .order_by(users.c.line_at.desc()).limit(10)).scalars()
    for uid in administrators:
        dialog_id = ensure_user_dialog(connection, bot['userid'], uid)
        send_message(connection, dialog_id=dialog_id, sender_id=bot['userid'], msg_type='template', msg={
            'type': 'content', 'title': '收到新的举报信息', 'content': '收到新的举报信息：' + complaint['reason'] + ' (请前往应用查看详情)'})


def register_jobs():
    jobs.register('complaints.notify', notify)


ROUTES = {'complaint/lists': lists, 'complaint/submit': submit, 'complaint/action': action}
