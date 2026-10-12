from __future__ import annotations
import base64
import html
import re
import secrets
from datetime import datetime, timedelta
from sqlalchemy import select, or_, and_, func
from .. import jobs
from ..context import utcnow
from ..db import tables, insert_row
from ..errors import ApiError, translate
from .files_support import actor, row, integer, decoded, encoded, change, page


def can_access(connection, userid, ident):
    report = row(connection, 'reports', integer(ident))
    if not report:
        return False
    if report['userid'] == userid:
        return True
    t = tables['report_receives']
    return bool(connection.execute(select(t.c.id).where(t.c.rid == report['id'], t.c.userid == userid)).first())


def require(connection, context, ident, *, owner=False):
    user = actor(connection, context)
    report = row(connection, 'reports', integer(ident), lock=owner)
    if not report or (report['userid'] != user.userid if owner else not can_access(connection, user.userid, ident)):
        raise ApiError('无权访问该工作汇报')
    return report


def serialize(connection, report, *, content=True):
    report = dict(report)
    t, u = tables['report_receives'], tables['users']
    receivers = []
    for receiver in connection.execute(select(t, u.c.nickname, u.c.email, u.c.userimg).join(u, u.c.userid == t.c.userid).where(t.c.rid == report['id'])).mappings():
        receivers.append({'userid': receiver['userid'], 'nickname': receiver['nickname'], 'email': receiver['email'], 'userimg': receiver['userimg'], 'pivot': {'rid': receiver['rid'], 'userid': receiver['userid'], 'read': receiver['read'], 'receive_at': receiver['receive_at']}})
    report['receives_user'] = receivers
    report['receives'] = [r['userid'] for r in receivers]
    if content and 'content' in report:
        report['content'] = html.unescape(report['content'] or '')
    else:
        report.pop('content', None)
    return report


def list_reports(connection, context, data, received=False):
    user = actor(connection, context)
    t, r, u = tables['reports'], tables['report_receives'], tables['users']
    query = select(t)
    keys = decoded(data.get('keys'))
    if received:
        query = query.join(r, r.c.rid == t.c.id).where(r.c.userid == user.userid)
        if keys.get('status') in ('read', 'unread'):
            query = query.where(r.c.read == int(keys['status'] == 'read'))
    else:
        query = query.where(t.c.userid == user.userid)
    key = str(keys.get('key', ''))
    if '@' in key:
        query = query.where(t.c.userid.in_(select(u.c.userid).where(u.c.email.contains(key))))
    elif key:
        clauses = [t.c.title.contains(key)]
        if key.isdigit():
            clauses += [t.c.id == int(key)]
            if received:
                clauses += [t.c.userid == int(key)]
        query = query.where(or_(*clauses))
    if keys.get('department_id'):
        query = query.where(t.c.userid.in_(select(u.c.userid).where(u.c.department.contains(',' + str(integer(keys['department_id'])) + ','))))
    if keys.get('type') in ('daily', 'weekly'):
        query = query.where(t.c.type == keys['type'])
    dates = keys.get('created_at', [])
    if isinstance(dates, list) and len(dates) == 2:
        for i, value in enumerate(dates):
            if value:
                parsed = parse_date(value).replace(hour=0, minute=0, second=0, microsecond=0)
                query = query.where(t.c.created_at >= parsed) if i == 0 else query.where(t.c.created_at < parsed + timedelta(days=1))
    result = page(connection, query.order_by(t.c.created_at.desc()), data, maximum=50)
    result['data'] = [serialize(connection, item, content=False) for item in result['data']]
    if received:
        for item in result['data']:
            item['receive_at'] = connection.execute(select(r.c.receive_at).where(r.c.rid == item['id'], r.c.userid == user.userid)).scalar_one_or_none()
    return result


def my(connection, context, data, settings):
    return list_reports(connection, context, data)


def receive(connection, context, data, settings):
    return list_reports(connection, context, data, True)


def parse_date(value):
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            return datetime.utcfromtimestamp(number / 1000 if number > 10**11 else number)
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ApiError('时间参数错误')


def period(kind, offset=0, base=None):
    if kind not in ('daily', 'weekly'):
        raise ApiError('汇报类型错误')
    start = (base or utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=abs(offset) * (7 if kind == 'weekly' else 1))
    if kind == 'weekly':
        start -= timedelta(days=start.weekday())
    return start, start + timedelta(days=7 if kind == 'weekly' else 1) - timedelta(microseconds=1)


def generate_sign(userid, kind, start):
    # The fresh database uses the selected period; avoids upstream daily-offset duplication.
    return str(userid) + (str(start.isocalendar().year) + str(start.isocalendar().week) if kind == 'weekly' else start.strftime('%Y%m%d'))


def unread_event(connection, context, users):
    if users:
        jobs.enqueue_push(connection, userids=users, ignore_fds=[context.fd] if context.fd else [], message={'type': 'report', 'action': 'unreadUpdate'})


def store(connection, context, data, settings):
    user = actor(connection, context)
    title, body, kind = str(data.get('title', '')).strip(), str(data.get('content', '')), data.get('type')
    if not title or len(title) > 255 or not body or integer(data.get('offset')) > 0:
        raise ApiError('请填写标题、汇报内容和正确周期')
    start, _ = period(kind, integer(data.get('offset')))
    ids = decoded(data.get('receive'), [])
    if not isinstance(ids, list):
        raise ApiError('请选择接收人')
    ids = sorted(set(integer(i) for i in ids) - {user.userid})
    for userid in ids:
        if not row(connection, 'users', userid, key='userid'):
            raise ApiError('用户不存在')
    ident = integer(data.get('id'))
    if ident:
        report = require(connection, context, ident, owner=True)
        change(connection, 'reports', ident, title=title, type=kind, content=html.escape(body, quote=True))
    else:
        sign = str(data.get('sign') or generate_sign(user.userid, kind, start))
        if not sign.isdigit():
            raise ApiError('工作汇报签名错误')
        t = tables['reports']
        if connection.execute(select(t.c.id).where(t.c.userid == user.userid, t.c.sign == sign, t.c.type == kind)).first():
            raise ApiError('请勿重复提交工作汇报')
        ident = insert_row(connection, 'reports', userid=user.userid, title=title, type=kind, content=html.escape(body, quote=True), sign=sign)
    # Inline data images are real browser-readable image bytes and remain in the stored rich text.
    # Reject malformed/oversized images rather than emitting a made-up URL or fake picture.
    for match in re.finditer(r'data:image/(?:png|jpg|jpeg|webp);base64,([^"\s<>]+)', body):
        import io
        from PIL import Image
        try:
            binary = base64.b64decode(match.group(1), validate=True)
            if len(binary) > 20 * 1024**2:
                raise ValueError('image too large')
            with Image.open(io.BytesIO(binary)) as image:
                image.verify()
        except (ValueError, OSError) as exc:
            raise ApiError('图片数据无效') from exc
    t = tables['report_receives']
    previous = set(connection.execute(select(t.c.userid).where(t.c.rid == ident)).scalars())
    connection.execute(t.delete().where(t.c.rid == ident))
    for userid in ids:
        insert_row(connection, 'report_receives', rid=ident, userid=userid, read=0, receive_at=utcnow())
    unread_event(connection, context, sorted(previous | set(ids)))
    return serialize(connection, row(connection, 'reports', ident))


def table_html(labels, datas):
    from markupsafe import Markup
    from ..host import render_template
    return render_template('report', {'labels': [translate(label) for label in labels],
                                     'datas': [[Markup(str(value)) for value in values] for values in datas]}).body.decode('utf-8')


def template(connection, context, data, settings):
    from .project_core import can_view_task
    user = actor(connection, context)
    kind, offset = data.get('type'), integer(data.get('offset'))
    start, end = period(kind, offset)
    sign = generate_sign(user.userid, kind, start)
    t = tables['reports']
    existing = connection.execute(select(t).where(t.c.userid == user.userid, t.c.type == kind, t.c.sign == sign)).mappings().first()
    if existing and offset > 0:
        return {k: html.unescape(existing[k] or '') if k == 'content' else existing[k] for k in ('id', 'sign', 'title', 'content')}
    task, members, projects, users = tables['project_tasks'], tables['project_task_users'], tables['projects'], tables['users']
    query = select(task).where(task.c.deleted_at.is_(None), task.c.id.in_(select(members.c.task_id).where(members.c.userid == user.userid))).order_by(task.c.id.desc())
    completed, unfinished, plans = [], [], []
    now = parse_date(data['date']) if data.get('date') else utcnow()
    if offset:
        now = end
    next_start, next_end = start + timedelta(days=7), end + timedelta(days=7)
    def unfinished_in(item, lo, hi, include_updated):
        if item['complete_at']:
            return False
        a, b = item['start_at'], item['end_at']
        if a and b:
            return a <= hi and b >= lo
        return not a and not b and (lo <= item['created_at'] <= hi or include_updated and lo <= item['updated_at'] <= hi)
    for value in connection.execute(query).mappings():
        item = dict(value)
        if not can_view_task(connection, user.userid, item):
            continue
        project = row(connection, 'projects', item['project_id'])
        if not project:
            continue
        names = connection.execute(select(users.c.nickname).join(members, members.c.userid == users.c.userid).where(members.c.task_id == item['id'], members.c.owner == 1)).scalars()
        owner = html.escape(', '.join(names))
        base = [html.escape(project['name']), html.escape(item['name']), owner]
        if item['complete_at'] and start <= item['complete_at'] <= end and not re.search('已取消|Cancelled|취소됨|キャンセル済み|Abgebrochen|Annulé|Dibatalkan|Отменено', item.get('flow_item_name') or ''):
            weekday = translate('周' + '一二三四五六日'[item['complete_at'].weekday()])
            completed.append(base + [f'<div style="text-align:center">[{weekday}]</div>' if kind == 'weekly' else '&nbsp;'])
        if not project['archived_at']:
            if unfinished_in(item, start, end, True):
                unfinished.append(base + ['<div style="color:#ff0000;text-align:center">[' + translate('超期') + ']</div>' if item['end_at'] and item['end_at'] < now else '&nbsp;'])
            if kind == 'weekly' and unfinished_in(item, next_start, next_end, False):
                timing = ((item['start_at'].strftime('%Y-%m-%d %H:%M') if item['start_at'] else '') + (' ~ ' + item['end_at'].strftime('%Y-%m-%d %H:%M') if item['end_at'] else '')).strip() or '-'
                plans.append(['[' + html.escape(project['name']) + '] ' + html.escape(item['name']), timing, owner])
    labels = ['项目', '任务', '负责人', '备注']
    title = user.user.get('nickname', '') + (f'的周报[{start:%m/%d}-{end:%m/%d}][{start.month}月第{(start.day - 1)//7 + 1}周]' if kind == 'weekly' else f'的日报[{start:%Y/%m/%d}]')
    body = '<h2>' + translate('已完成工作') + '</h2>' + table_html(labels, completed)
    body += '<p>&nbsp;</p><h2>' + translate('本周未完成的工作' if kind == 'weekly' else '今日未完成的工作') + '</h2>' + table_html(labels, unfinished)
    if kind == 'weekly':
        body += '<p>&nbsp;</p><h2>' + translate('下周拟定计划') + f'[{next_start:%m/%d}-{next_end:%m/%d}]</h2>' + table_html(['计划描述', '计划时间', '负责人'], plans)
    result = {'time': start, 'sign': sign, 'title': translate(title), 'content': body}
    if existing:
        result['id'] = existing['id']
    return result


def detail(connection, context, data, settings):
    user = actor(connection, context)
    link = None
    if integer(data.get('id')):
        report = require(connection, context, data['id'])
    else:
        t = tables['report_links']
        link = connection.execute(select(t).where(t.c.code == str(data.get('code', '')))).mappings().first()
        if not link:
            raise ApiError('报告不存在或已被删除')
        report = row(connection, 'reports', link['rid'])
        if not report:
            raise ApiError('报告不存在或已被删除')
        connection.execute(t.update().where(t.c.id == link['id']).values(num=t.c.num + 1))
    result = serialize(connection, report)
    result['type_val'] = report['type']
    if link:
        result['report_link'] = dict(link)
    t = tables['report_receives']
    connection.execute(t.update().where(t.c.rid == report['id'], t.c.userid == user.userid).values(read=1))
    a = tables['report_ai_analyses']
    analysis = connection.execute(select(a).where(a.c.rid == report['id'], a.c.userid == user.userid)).mappings().first()
    result['ai_analysis'] = {'id': analysis['id'], 'text': analysis['analysis_text'], 'model': analysis['model'], 'updated_at': analysis['updated_at']} if analysis else None
    unread_event(connection, context, [user.userid])
    return result


def analysave(connection, context, data, settings):
    user = actor(connection, context)
    report = require(connection, context, data.get('id'))
    text = str(data.get('text', '')).strip()
    if not text:
        raise ApiError('分析内容不能为空')
    t = tables['report_ai_analyses']
    ident = connection.execute(select(t.c.id).where(t.c.rid == report['id'], t.c.userid == user.userid).with_for_update()).scalar_one_or_none()
    focus = data.get('focus', [])
    focus = [focus] if isinstance(focus, str) else focus
    if not isinstance(focus, list):
        raise ApiError('参数错误')
    values = {'model': str(data.get('model', ''))[:255], 'analysis_text': text, 'meta': encoded({'viewer_role': user.user.get('profession') or '/'.join(user.identities), 'viewer_name': user.user.get('nickname'), 'focus': [str(f).strip() for f in focus if str(f).strip()]})}
    if ident is None:
        ident = insert_row(connection, 'report_ai_analyses', rid=report['id'], userid=user.userid, **values)
    else:
        change(connection, 'report_ai_analyses', ident, **values)
    saved = row(connection, 'report_ai_analyses', ident)
    return {'id': ident, 'text': text, 'model': saved['model'], 'updated_at': saved['updated_at']}


def ids_input(value, limit=100):
    if isinstance(value, str) and ',' in value:
        value = value.split(',')
    if not isinstance(value, list):
        value = [value]
    ids = sorted(set(integer(v) for v in value))
    if not ids or len(ids) > limit or min(ids) <= 0:
        raise ApiError('请传入正确的工作汇报Id')
    return ids


def mark(connection, context, data, settings):
    user = actor(connection, context)
    ids = ids_input(data.get('id'))
    t = tables['report_receives']
    connection.execute(t.update().where(t.c.rid.in_(ids), t.c.userid == user.userid).values(read=int(data.get('action') != 'unread')))
    unread_event(connection, context, [user.userid])
    return []


def read(connection, context, data, settings):
    ids = ids_input(data.get('ids'))
    mark(connection, context, {'id': ids, 'action': 'read'}, settings)
    return [serialize(connection, require(connection, context, ident)) for ident in ids]


def unread(connection, context, data, settings):
    user = actor(connection, context)
    t = tables['report_receives']
    return {'total': connection.execute(select(func.count()).select_from(t).where(t.c.userid == user.userid, t.c.read == 0)).scalar_one()}


def last_submitter(connection, context, data, settings):
    user = actor(connection, context)
    t = tables['reports']
    ident = connection.execute(select(t.c.id).where(t.c.userid == user.userid).order_by(t.c.created_at.desc()).limit(1)).scalar_one_or_none()
    if ident is None:
        raise ApiError('记录不存在')
    r = tables['report_receives']
    return list(connection.execute(select(r.c.userid).where(r.c.rid == ident)).scalars())


def generate_link(connection, context, ident):
    report = require(connection, context, ident)
    user = actor(connection, context)
    table = tables['report_links']
    link = connection.execute(select(table).where(table.c.rid == report['id'], table.c.userid == user.userid)).mappings().first()
    if not link:
        code = base64.b64encode(f"{report['id']},{user.userid},{secrets.token_urlsafe(24)}".encode()).decode()
        key = insert_row(connection, 'report_links', rid=report['id'], userid=user.userid, code=code)
        link = row(connection, 'report_links', key)
    return {'id': report['id'], 'rid': report['id'], 'userid': user.userid, 'code': link['code'], 'num': link['num'],
            'url': context.base_url.rstrip('/') + '/single/report/detail/' + link['code']}


def share(connection, context, data, settings):
    from .dialog_core import ensure_user_dialog, get_dialog, send_message
    user = actor(connection, context)
    ids = ids_input(data.get('id'), 20)
    snippets = []
    for ident in ids:
        report = require(connection, context, ident)
        code = generate_link(connection, context, ident)['code']
        snippets.append('<a class="mention report" href="{{RemoteURL}}single/report/detail/' + code + '" target="_blank">%' + html.escape(report['title']) + '</a>')
    body = '<ol>' + ''.join('<li data-list="ordered">' + s + '</li>' for s in snippets) + '</ol>' if len(snippets) > 1 else '<p>' + snippets[0] + '</p>'
    if data.get('leave_message'):
        body += '<p>' + html.escape(str(data['leave_message'])) + '</p>'
    dialogs = set(integer(i) for i in decoded(data.get('dialogids'), []))
    for userid in decoded(data.get('userids'), []):
        dialogs.add(ensure_user_dialog(connection, user.userid, integer(userid)))
    if not dialogs or len(dialogs) > 100:
        raise ApiError('请选择接收人')
    result = []
    for dialog in dialogs:
        get_dialog(connection, dialog, user.userid)
        result.append(send_message(connection, dialog_id=dialog, sender_id=user.userid, msg_type='text', msg={'text': body}, context=context))
    return result


def register_jobs():
    from ..realtime import register_scope
    register_scope('report_id', can_access)


ROUTES = {'report/my': my, 'report/receive': receive, 'report/store': store, 'report/template': template,
          'report/detail': detail, 'report/analysave': analysave, 'report/mark': mark, 'report/share': share,
          'report/last_submitter': last_submitter, 'report/unread': unread, 'report/read': read}
