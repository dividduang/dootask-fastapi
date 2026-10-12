"""Durable project notifications, BIFF8 exports and scheduled task lifecycle."""
import io
import re
import secrets
import zipfile
from datetime import timedelta
from sqlalchemy import select, or_
from .. import db, jobs
from ..auth import Actor, require_admin
from ..context import RequestContext, utcnow
from ..errors import ApiError
from .project_core import (actor, table, row, rows, by_id, update, count, ids, integer, decode, date,
    member_map, task_allowed_users, observe_task, log, task_snapshot, handoff_record)


def service_context(connection, task=None, userid=0):
    users = table('users')
    if task and not userid:
        members = member_map(connection, task['project_id'])
        userid = task['userid'] if task['userid'] in members else next((u for u, r in members.items() if r == 1), 0)
    user = row(connection, 'users', users.c.userid == userid) if userid else {'userid': 0, 'identity': ''}
    if not user:
        raise ApiError('任务操作成员不存在')
    return RequestContext('project-job', actor=Actor(userid, user, {}, {}))


def notify(connection, payload, settings):
    task = by_id(connection, 'project_tasks', payload['task_id'])
    if not task or task['deleted_at']:
        return
    wanted = set(ids(payload.get('userids'))) & set(task_allowed_users(connection, task))
    if not wanted:
        return
    from .account_meetings import bot_get
    from .dialog_core import ensure_user_dialog, send_message
    bot = bot_get(connection, 'task-alert')
    tu, pl, users = table('project_task_users'), table('project_task_push_logs'), table('users')
    roles = {u['userid']: u['owner'] for u in rows(connection, 'project_task_users', tu.c.task_id == task['id'])}
    kind = integer(payload.get('type'))
    for userid in wanted:
        if not row(connection, 'users', users.c.userid == userid, users.c.disable_at.is_(None)):
            continue
        if kind in (1, 2) and row(connection, 'project_task_push_logs', pl.c.userid == userid, pl.c.task_id == task['id'], pl.c.type == kind):
            continue
        responsible = roles.get(userid) == 1
        title = {0: '您有一个新任务' if responsible else '您有一个新协助任务',
            1: '您负责的任务即将超时' if responsible else '您协助的任务即将超时',
            2: '您负责的任务已经超时' if responsible else '您协助的任务已经超时',
            3: '您负责的任务时间已修改' if responsible else '您协助的任务时间已修改'}[kind]
        dialog_id = ensure_user_dialog(connection, bot['userid'], userid)
        db.insert_row(connection, 'project_task_push_logs', userid=userid, task_id=task['id'], type=kind)
        send_message(connection, dialog_id=dialog_id, sender_id=bot['userid'], msg_type='template',
            msg={'type': 'task_list', 'title': title + str(payload.get('suffix', '')), 'list': [{'id': task['parent_id'] or task['id'], 'name': task['name']}]})


def request_export(connection, context, data, *, overdue=False):
    user = actor(connection, context)
    require_admin(user)
    people = ids(data.get('userid'))
    period = data.get('time')
    if not overdue:
        if not people or len(people) > 100:
            raise ApiError('导出成员限制为1至100个')
        if not isinstance(period, list) or len(period) != 2:
            raise ApiError('时间选择错误')
        start, end = date(period[0]), date(period[1])
        if not start or not end or end < start or end - start > timedelta(days=90):
            raise ApiError('时间范围限制最大90天')
    from .account_meetings import bot_get
    from .dialog_core import ensure_user_dialog, send_message
    bot = bot_get(connection, 'system-msg')
    dialog_id = ensure_user_dialog(connection, bot['userid'], user.userid)
    jobs.enqueue(connection, 'projects.export', {'userid': user.userid, 'userids': people, 'time': period,
        'type': data.get('type', 'taskTime'), 'overdue': overdue, 'base_url': context.base_url,
        'dialog_id': dialog_id, 'bot_id': bot['userid']})
    send_message(connection, dialog_id=dialog_id, sender_id=bot['userid'], msg_type='template',
        msg={'type': 'content', 'content': '正在导出超期任务，请稍等...' if overdue else '正在导出任务统计，请稍等...'})
    return []


def task_export(connection, context, data, settings):
    return request_export(connection, context, data)


def task_exportoverdue(connection, context, data, settings):
    return request_export(connection, context, data, overdue=True)


def task_down(connection, context, data, settings):
    from .files import download_bytes
    return download_bytes(connection, context, data, settings)


def duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return ''.join(str(n) + unit for n, unit in ((days, '天'), (hours, '小时'), (minutes, '分钟'), (seconds, '秒')) if n) or '0秒'


def export_job(connection, payload, settings):
    import xlwt
    from .files_support import store_stream
    from .dialog_core import send_message
    user_context = service_context(connection, userid=payload['userid'])
    require_admin(user_context.actor)
    t, tu, u = table('project_tasks'), table('project_task_users'), table('users')
    query = select(t).where(t.c.deleted_at.is_(None))
    now = utcnow()
    overdue = payload['overdue']
    if overdue:
        query = query.where(t.c.complete_at.is_(None), t.c.end_at <= now).order_by(t.c.end_at)
    else:
        start, end = date(payload['time'][0]).replace(hour=0, minute=0, second=0), date(payload['time'][1]).replace(hour=23, minute=59, second=59)
        if payload['type'] == 'createdTime':
            query = query.where(t.c.created_at.between(start, end))
        else:
            query = query.where(or_((t.c.start_at <= end) & (t.c.end_at >= start),
                (or_(t.c.start_at.is_(None), t.c.end_at.is_(None))) & t.c.complete_at.between(start, end)))
        query = query.where(t.c.id.in_(select(tu.c.task_id).where(tu.c.userid.in_(payload['userids']), tu.c.owner == 1))).order_by(t.c.id.desc())
    task_rows = [dict(v) for v in connection.execute(query).mappings()]
    if not task_rows:
        send_message(connection, dialog_id=payload['dialog_id'], sender_id=payload['bot_id'], msg_type='template',
            msg={'type': 'content', 'title': '导出超期任务已完成' if overdue else '导出任务统计已完成', 'content': [{'content': '没有任何数据', 'style': 'color: #ff0000;'}]})
        return
    names = {v['userid']: v['nickname'] for v in rows(connection, 'users')}
    workbook = xlwt.Workbook(encoding='utf-8')
    headings = ['任务ID', '父级任务ID', '所属项目', '任务标题', '任务标签', '任务开始时间', '任务结束时间']
    headings += ['任务计划用时', '超时时间', '负责人', '创建人'] if overdue else ['完成时间', '归档时间', '任务计划用时', '实际完成用时', '超时时间', '开发用时', '验收/测试用时', '负责人', '创建人', '状态']
    sheet_ids = [0] if overdue else payload['userids']
    owner_map = {}
    for owner in rows(connection, 'project_task_users', tu.c.task_id.in_([v['id'] for v in task_rows]), tu.c.owner == 1):
        owner_map.setdefault(owner['task_id'], []).append(owner['userid'])
    bold, red, blue = xlwt.easyxf('font: bold on;'), xlwt.easyxf('font: colour red;'), xlwt.easyxf('font: colour blue;')
    for index, owner_id in enumerate(sheet_ids):
        title = '超期任务' if overdue else f'{index+1}.{names.get(owner_id) or owner_id}'
        title = re.sub(r'[\[\]:*?/\\]', '_', title)[:31]
        sheet = workbook.add_sheet(title)
        for col, heading in enumerate(headings):
            sheet.write(0, col, heading, bold)
        line = 1
        for task in task_rows:
            owners = owner_map.get(task['id'], [])
            if not overdue and owner_id not in owners:
                continue
            if line > 65535:
                raise ApiError('单个成员任务数量超出XLS格式行数限制65535')
            project = by_id(connection, 'projects', task['project_id'])
            tags = rows(connection, 'project_task_tags', table('project_task_tags').c.task_id == task['id'])
            start_at = task['start_at'] or task['created_at']
            finish = task['complete_at'] or now
            total = (finish - start_at).total_seconds()
            planned = (task['end_at'] - start_at).total_seconds() if task['end_at'] else 0
            overtime = duration(total - planned) if task['end_at'] and total > planned else '-'
            actual = total if task['complete_at'] else 0
            changes = rows(connection, 'project_task_flow_changes', table('project_task_flow_changes').c.task_id == task['id'], order=[table('project_task_flow_changes').c.id])
            test_seconds = 0
            previous = None
            for change in changes:
                if previous and str(change['before_flow_item_name']).startswith('test'):
                    test_seconds += (change['created_at'] - previous['created_at']).total_seconds()
                previous = change
            if previous and not task['complete_at'] and str(previous['after_flow_item_name']).startswith('test'):
                test_seconds += (now - previous['created_at']).total_seconds()
            development = total - test_seconds
            state = '已完成' if task['complete_at'] or str(task['flow_item_name']).startswith('end') else '未完成'
            if str(task['flow_item_name']).startswith('end') and re.search(r'cancel|取消', task['flow_item_name'], re.I):
                state, actual, test_seconds, development, overtime = '已取消', 0, 0, 0, '-'
            values = [task['id'], task['parent_id'] or '-', project['name'] if project else '-', task['name'],
                ', '.join(v['name'] for v in tags) or '-', task['start_at'] or '-', task['end_at'] or '-']
            owner_names = ', '.join(f'{names.get(v, "")} (ID: {v})' for v in (owners if overdue else [owner_id]))
            creator = f'{names.get(task["userid"], "")} (ID: {task["userid"]})'
            if overdue:
                values += [duration(planned) if planned else '-', overtime, owner_names, creator]
            else:
                values += [task['complete_at'] or '-', task['archived_at'] or '-', duration(planned) if planned else '-',
                    duration(actual) if actual else '-', overtime, duration(development) if development > 0 else '-',
                    duration(test_seconds) if test_seconds > 0 else '-', owner_names, creator, state]
            for col, value in enumerate(values):
                value = value.strftime('%Y-%m-%d %H:%M:%S') if hasattr(value, 'strftime') else value
                style = red if not overdue and col == 16 and state == '未完成' else blue if not overdue and col == 16 and state == '已完成' and task['end_at'] and task['complete_at'] and task['complete_at'] > task['end_at'] else xlwt.Style.default_style
                # xlwt writes text as string cells, never formulas from user-controlled task names.
                sheet.write(line, col, value, style)
            line += 1
    xls = io.BytesIO()
    workbook.save(xls)
    title = '超期任务' if overdue else str(names.get(payload['userids'][0]) or payload['userids'][0]) + ('等' + str(len(payload['userids'])) + '位成员的任务统计' if len(payload['userids']) > 1 else '的任务统计')
    filename = re.sub(r'[<>:"/\\|?*]', '_', title) + '_' + str(int(now.timestamp())) + '.xls'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(filename, xls.getvalue())
    archive.seek(0)
    blob = store_stream(settings, archive)
    key = secrets.token_hex(32)
    db.insert_row(connection, 'backend_file_downloads', id=key, userid=payload['userid'], path=blob['url'],
        name=filename[:-4] + '.zip', ready=1, expires_at=now + timedelta(days=1))
    url = payload['base_url'].rstrip('/') + '/api/project/task/down?key=' + key
    send_message(connection, dialog_id=payload['dialog_id'], sender_id=payload['bot_id'], msg_type='template',
        msg={'type': 'file_download', 'title': '导出超期任务已完成' if overdue else '导出任务统计已完成',
             'name': filename, 'size': blob['size'], 'url': url})


def maintenance(connection, payload, settings):
    from .project_tasks import archive_task, copy_task, next_loop, create_relation
    now = utcnow()
    system = db.setting(connection, 'system', default={})
    push_enabled = db.setting(connection, 'appPushSetting', 'push') == 'open'
    t, p, tu = table('project_tasks'), table('projects'), table('project_task_users')
    projects = {v['id']: v for v in rows(connection, 'projects', p.c.deleted_at.is_(None), p.c.archived_at.is_(None))}
    tasks = rows(connection, 'project_tasks', t.c.project_id.in_(list(projects)), t.c.deleted_at.is_(None))
    for task in tasks:
        project = projects[task['project_id']]
        days = integer(project['archive_days']) if project['archive_method'] == 'custom' else integer(system.get('archived_day')) if system.get('auto_archived') == 'open' else 0
        if not task['parent_id'] and days > 0 and task['complete_at'] and not task['archived_at'] and not task['archived_userid'] and task['complete_at'] <= now - timedelta(days=min(days, 365)):
            archive_task(connection, service_context(connection), task, automatic=True)
        if not task['parent_id'] and task['loop_at'] and now - timedelta(minutes=10) <= task['loop_at'] <= now:
            jobs.enqueue(connection, 'projects.recurring', {'task_id': task['id'], 'loop_at': task['loop_at']},
                dedupe_key=f'project-loop:{task["id"]}:{task["loop_at"].isoformat()}')
        if push_enabled and not task['complete_at'] and not task['archived_at'] and task['end_at']:
            minutes = (task['end_at'] - now).total_seconds() / 60
            kind = 1 if 60 <= minutes <= 70 else 2 if -70 <= minutes <= -60 else None
            if kind:
                users = list(connection.execute(select(tu.c.userid).where(tu.c.task_id == task['id'])).scalars())
                jobs.enqueue(connection, 'projects.notify', {'task_id': task['id'], 'userids': users, 'type': kind},
                    dedupe_key=f'task-reminder:{task["id"]}:{kind}:{task["end_at"].isoformat()}')
    clock = str(system.get('unclaimed_task_reminder_time', ''))
    if system.get('unclaimed_task_reminder') == 'open' and re.fullmatch(r'\d\d:\d\d', clock):
        hour, minute = map(int, clock.split(':'))
        if now.hour == hour and abs(now.minute - minute) <= 1:
            for pid in projects:
                jobs.enqueue(connection, 'projects.unclaimed', {'project_id': pid}, dedupe_key=f'unclaimed:{now.date()}:{pid}')


def recurring(connection, payload, settings):
    from .project_tasks import copy_task, descendants, next_loop, create_relation
    task = by_id(connection, 'project_tasks', payload['task_id'], lock=True)
    if not task or task['deleted_at'] or task['parent_id'] or not task['loop_at'] or task['loop_at'] != date(payload['loop_at']):
        return
    project = by_id(connection, 'projects', task['project_id'])
    if not project or project['deleted_at'] or project['archived_at']:
        return
    context = service_context(connection, task)
    column = by_id(connection, 'project_columns', task['column_id'])
    if not column or column['deleted_at']:
        raise ApiError('周期任务所在列表已删除')
    f = table('project_flow_items')
    options = rows(connection, 'project_flow_items', f.c.project_id == project['id'], f.c.status == 'start', order=[f.c.sort, f.c.id])
    flow = options[0] if options else None
    tu = table('project_task_users')
    members = rows(connection, 'project_task_users', tu.c.task_id == task['id'])
    owners = [v['userid'] for v in members if v['owner'] == 1]
    owners += ids(decode(flow['userids'])) if flow else []
    clone = copy_task(connection, context, task, project, column, flow, {'owner': owners,
        'assist': [v['userid'] for v in members if v['owner'] == 0], 'completed': False}, settings)
    start, end = task['loop_at'], task['loop_at'] + (task['end_at'] - task['start_at']) if task['start_at'] and task['end_at'] else None
    values = {'loop': task['loop'], 'loop_at': next_loop(task['loop'], start if task['start_at'] else None), 'complete_at': None}
    if task['start_at']:
        values.update(start_at=start, end_at=end)
    update(connection, 'project_tasks', clone['id'], values)
    for child in descendants(connection, clone['id']):
        update(connection, 'project_tasks', child['id'], {'start_at': start if task['start_at'] else None, 'end_at': end, 'complete_at': None})
        observe_task(connection, context, by_id(connection, 'project_tasks', child['id']), 'update')
    update(connection, 'project_tasks', task['id'], {'loop': '', 'loop_at': None})
    fresh = by_id(connection, 'project_tasks', clone['id'])
    log(connection, context, task['project_id'], '创建任务来自周期任务ID：' + str(task['id']), task=fresh)
    log(connection, context, task['project_id'], '已创建新的周期任务ID：' + str(clone['id']) + '，此任务关闭周期', task=task)
    handoff_record(connection, context, fresh, task_snapshot(connection, clone), 'recurring')
    observe_task(connection, context, fresh, 'update')
    observe_task(connection, context, by_id(connection, 'project_tasks', task['id']), 'update')
    create_relation(connection, context, task, fresh)


def unclaimed(connection, payload, settings):
    from .account_meetings import bot_get
    from .dialog_core import send_message
    project = by_id(connection, 'projects', payload['project_id'])
    if not project or project['deleted_at'] or project['archived_at'] or not project['dialog_id']:
        return
    t, tu = table('project_tasks'), table('project_task_users')
    query = select(t.c.id, t.c.name).where(t.c.project_id == project['id'], t.c.deleted_at.is_(None),
        t.c.archived_at.is_(None), t.c.visibility == 1, t.c.id.not_in(select(tu.c.task_id))).limit(10)
    tasks = [dict(v) for v in connection.execute(query).mappings()]
    if not tasks:
        return
    bot = bot_get(connection, 'task-alert')
    send_message(connection, dialog_id=project['dialog_id'], sender_id=bot['userid'], msg_type='template', trusted_sender=True,
        msg={'type': 'task_list', 'title': '任务待领取', 'list': tasks})


def register_jobs():
    from ..realtime import register_scope
    from .project_core import can_view_project, can_view_task
    jobs.register('projects.notify', notify)
    jobs.register('projects.export', export_job)
    jobs.register('projects.maintenance', maintenance)
    jobs.register('projects.recurring', recurring)
    jobs.register('projects.unclaimed', unclaimed)
    jobs.schedule('projects.maintenance', 60)
    register_scope('project_id', lambda c, u, i: can_view_project(c, u, by_id(c, 'projects', i)))
    register_scope('task_id', lambda c, u, i: can_view_task(c, u, by_id(c, 'project_tasks', i)))


ROUTES = {'project/task/export': task_export, 'project/task/exportoverdue': task_exportoverdue, 'project/task/down': task_down}
RESPONSE_KINDS = {'project/task/down': 'Response'}
PUBLIC_ROUTES = {'project/task/down'}
