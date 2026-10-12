"""Task mutations and observer orchestration; caller owns the SQL transaction."""
import calendar
import html
import re
from datetime import timedelta
from sqlalchemy import select, or_, and_
from sqlalchemy.dialects.mysql import insert
from .. import db
from ..context import utcnow, RequestContext
from ..auth import Actor
from ..errors import ApiError
from ..jobs import enqueue, enqueue_push
from .project_core import (table, actor, row, rows, by_id, update, count, ids, integer, encode, decode,
    date, require_project, require_task, require_action, member_map, root_task, task_allowed_users,
    can_view_task, task_data, task_snapshot, handoff_record, observe_task, log, deleted_record)


def descendants(connection, task_id, *, deleted=False):
    t = table('project_tasks')
    result, pending, seen = [], [task_id], {task_id}
    while pending:
        children = rows(connection, 'project_tasks', t.c.parent_id.in_(pending), *([] if deleted else [t.c.deleted_at.is_(None)]))
        pending = []
        for child in children:
            if child['id'] in seen:
                raise ApiError('任务父子关系异常')
            seen.add(child['id'])
            pending.append(child['id'])
            result.append(child)
    return result


def description(content):
    text = re.sub(r'<li[^>]*tox-checklist--checked[^>]*>', '[√]', str(content), flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', html.unescape(text)).strip()[:500]


def save_content(connection, context, task, content, settings):
    content = str(content)
    if len(content.encode('utf-8')) > 20 * 1024 * 1024:
        raise ApiError('任务详细描述过大')
    # New database stores the actual HTML in the existing LONGTEXT; no public unauthenticated content file.
    identifier = db.insert_row(connection, 'project_task_contents', project_id=task['project_id'], task_id=task['id'],
        userid=actor(connection, context).userid, desc=description(content), content=content)
    update(connection, 'project_tasks', task['id'], {'desc': description(content)})
    log(connection, context, task['project_id'], '修改{任务}详细描述', task=task,
        record={'link': {'title': '查看历史', 'url': f'single/task/content/{task["id"]}?history_id={identifier}'}})
    return identifier


def normalize_times(data, task=None):
    data = dict(data)
    if 'times' not in data and ('start_at' in data or 'end_at' in data):
        start, end = data.get('start_at'), data.get('end_at')
        if end and not start:
            start = (task or {}).get('start_at') or utcnow()
        elif start and not end:
            end = (task or {}).get('end_at')
            if not end:
                raise ApiError('请设置结束时间')
        data['times'] = [start, end]
    return data


def times(value):
    if isinstance(value, str):
        value = value.split(',')
    if not value or not isinstance(value, (list, tuple)) or len(value) < 2:
        return None, None
    start, end = date(value[0]), date(value[1])
    if not start or not end or start == end:
        return None, None
    if start > end:
        raise ApiError('开始时间不能晚于结束时间')
    return start, end


def next_loop(loop, start, now=None):
    now = now or utcnow()
    base = start or now.replace(hour=0, minute=0, second=0, microsecond=0)
    if base.date() < now.date():
        base = base.replace(year=now.year, month=now.month, day=now.day)
    if loop in ('', 'never', None):
        return None
    days = {'day': 1, 'week': 7, 'twoweeks': 14}.get(str(loop))
    if days:
        return base + timedelta(days=days)
    if loop == 'weekdays':
        base += timedelta(days=1)
        while base.weekday() > 4:
            base += timedelta(days=1)
        return base
    if loop in ('month', 'year'):
        month = base.month + (1 if loop == 'month' else 12)
        year = base.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        # Carbon's addMonth/addYear overflows dates (January 31 -> March 3).
        return base.replace(year=year, month=month, day=1) + timedelta(days=base.day - 1)
    days = integer(loop)
    if days <= 0:
        raise ApiError('重复周期选择错误')
    return base + timedelta(days=days)


def _participants(connection, task, owners, assists):
    members = member_map(connection, task['project_id'])
    owners, assists = ids(owners), ids(assists)
    assists = [uid for uid in assists if uid not in owners]
    if len(owners) > 10 or len(assists) > 10:
        raise ApiError('任务负责人或协助人员最多不能超过10个')
    if (set(owners) | set(assists)) - set(members):
        raise ApiError('任务负责人或协助人员不在项目内')
    t = table('project_task_users')
    connection.execute(t.delete().where(t.c.task_id == task['id']))
    for role, users in ((1, owners), (0, assists)):
        for uid in users:
            db.insert_row(connection, 'project_task_users', project_id=task['project_id'], task_id=task['id'],
                task_pid=task['parent_id'] or task['id'], userid=uid, owner=role)
    sync_task_dialog(connection, task)


def sync_task_dialog(connection, task):
    from .dialog_core import sync_members
    root = root_task(connection, task)
    if not root['dialog_id']:
        return
    tu = table('project_task_users')
    related = [root['id']] + [child['id'] for child in descendants(connection, root['id']) if not child['archived_at']]
    wanted = list(connection.execute(select(tu.c.userid).where(tu.c.task_id.in_(related)).distinct()).scalars())
    dialog = by_id(connection, 'web_socket_dialogs', root['dialog_id'])
    if dialog and not dialog['deleted_at']:
        sync_members(connection, root['dialog_id'], wanted, owner_id=dialog['owner_id'] if dialog['owner_id'] in wanted else (wanted[0] if wanted else None))


def create_task(connection, context, data, settings):
    uid = actor(connection, context).userid
    data = normalize_times(data)
    project_id, column_id, parent_id = (integer(data.get(k)) for k in ('project_id', 'column_id', 'parent_id'))
    project = require_project(connection, context, project_id, write=True)
    require_action(connection, context, project, 'task_add')
    column = by_id(connection, 'project_columns', column_id)
    if not column or column['project_id'] != project_id or column['deleted_at']:
        raise ApiError('任务列表不存在或已被删除')
    name = str(data.get('name', '')).strip()
    if not name or len(name) > 255:
        raise ApiError('任务描述不能为空且最多255个字')
    parent = require_task(connection, context, parent_id, write=True) if parent_id else None
    if parent and (parent['project_id'] != project_id or parent['column_id'] != column_id or parent['complete_at']):
        raise ApiError('主任务已完成或不在此列表')
    inherited_before = {}
    if parent:
        family_root = root_task(connection, parent)
        for related in [family_root] + descendants(connection, family_root['id']):
            inherited_before[related['id']] = task_allowed_users(connection, related)
    t = table('project_tasks')
    active = [t.c.deleted_at.is_(None), t.c.archived_at.is_(None), t.c.complete_at.is_(None)]
    for condition, limit, message in ((t.c.project_id == project_id, 2000, '项目内未完成任务最多不能超过2000个'),
            (t.c.column_id == column_id, 500, '单个列表未完成任务最多不能超过500个')):
        if count(connection, 'project_tasks', *active, condition) >= limit:
            raise ApiError(message)
    if parent and count(connection, 'project_tasks', *active, t.c.parent_id == parent_id) >= 50:
        raise ApiError('每个任务的子任务最多不能超过50个')
    owners, assists = ids(data.get('owner')), ids(data.get('assist'))
    if not assists and integer(data.get('add_assist')) and uid not in owners:
        assists = [uid]
    limit = integer(db.setting(connection, 'system', 'task_user_limit', 1000), 1000)
    tu = table('project_task_users')
    for owner in owners:
        if limit > 0 and count(connection, 'project_tasks', *active, t.c.id.in_(select(tu.c.task_id).where(tu.c.userid == owner))) >= limit:
            raise ApiError(f'负责或参与的未完成任务最多不能超过{limit}个')
    start, end = times(data.get('times'))
    if parent:
        if start and parent['start_at'] and start < parent['start_at']:
            raise ApiError('子任务开始时间不能小于主任务开始时间')
        if end and parent['end_at'] and end > parent['end_at']:
            raise ApiError('子任务结束时间不能大于主任务结束时间')
        if not start:
            start, end = parent['start_at'], parent['end_at']
    visibility = parent['visibility'] if parent else integer(data.get('visibility_appoint', data.get('visibility', 1)), 1)
    if visibility not in (1, 2, 3):
        raise ApiError('任务可见性设置错误')
    priorities = db.setting(connection, 'priority', default=[])
    priority = next((p for p in priorities if integer(p.get('priority')) == integer(data.get('p_level'))), None)
    if not priority and not integer(data.get('p_level')):
        priority = next((p for p in priorities if p.get('default')), priorities[0] if priorities else {})
    priority = priority or {}
    existing = connection.execute(select(t.c.sort).where(t.c.column_id == column_id, t.c.deleted_at.is_(None))).scalars().all()
    sort_value = min(existing or [0]) - 1 if integer(data.get('top')) else max(existing or [0]) + 1
    values = dict(parent_id=parent_id, project_id=project_id, column_id=column_id, name=name, userid=uid,
        start_at=start, end_at=end, visibility=visibility, sort=sort_value,
        p_level=integer(data.get('p_level')) or integer(priority.get('priority')),
        p_name=str(data.get('p_name') or priority.get('name') or ''),
        p_color=str(data.get('p_color') or priority.get('color') or ''))
    f = table('project_flow_items')
    flows = rows(connection, 'project_flow_items', f.c.project_id == project_id, f.c.status == 'start', order=[f.c.sort, f.c.id])
    if flows:
        flow = flows[0]
        values.update(flow_item_id=flow['id'], flow_item_name='|'.join([flow['status'], flow['name'], flow['color'] or '']))
        owners = list(dict.fromkeys(owners + ids(decode(flow['userids']))))
    identifier = db.insert_row(connection, 'project_tasks', **values)
    task = by_id(connection, 'project_tasks', identifier)
    _participants(connection, task, owners, assists)
    visible = ids(data.get('visibility_appointor'))
    if set(visible) - set(member_map(connection, project_id)):
        raise ApiError('可见成员不在项目内')
    for viewer in visible:
        db.insert_row(connection, 'project_task_visibility_users', project_id=project_id, task_id=identifier, userid=viewer)
    if data.get('content'):
        save_content(connection, context, task, data['content'], settings)
    for sub in decode(data.get('subtasks'), []):
        if not isinstance(sub, dict):
            raise ApiError('子任务参数错误')
        create_task(connection, context, {**sub, 'parent_id': identifier, 'project_id': project_id,
            'column_id': column_id, 'visibility': visibility, 'times': sub.get('times') or [start, end]}, settings)
    task = by_id(connection, 'project_tasks', identifier)
    log(connection, context, project_id, '创建{任务}', task=task)
    handoff_record(connection, context, task, None, 'create')
    observe_task(connection, context, task, 'add')
    for related_id, previous_users in inherited_before.items():
        observe_task(connection, context, by_id(connection, 'project_tasks', related_id), 'update', previous_users)
    enqueue(connection, 'projects.notify', {'task_id': identifier, 'userids': owners + assists, 'type': 0})
    return task


def task_add(connection, context, data, settings):
    from .projects import create_column
    p = require_project(connection, context, data.get('project_id'), write=True)
    require_action(connection, context, p, 'task_add')
    c = table('project_columns')
    candidates = rows(connection, 'project_columns', c.c.project_id == p['id'], c.c.deleted_at.is_(None), order=[c.c.id])
    selected = data.get('column_id')
    column = next((r for r in candidates if r['id'] == integer(selected) or r['name'] == str(selected)), None) if selected else next(iter(candidates), None)
    new_column = None
    if not column:
        column = new_column = create_column(connection, context, p, selected or 'Default')
    task = create_task(connection, context, {**data, 'parent_id': 0, 'project_id': p['id'], 'column_id': column['id']}, settings)
    result = task_data(connection, task, context.actor.userid)
    result['is_visible'] = int(can_view_task(connection, context.actor.userid, task))
    if new_column:
        result['new_column'] = new_column
    template = by_id(connection, 'project_task_templates', data.get('template_id'))
    if template and context.actor.userid in member_map(connection, template['project_id']) and (
        template['project_id'] == p['id'] or p['task_template_share'] != 'close'):
        update(connection, 'project_task_templates', template['id'], {'use_count': template['use_count'] + 1, 'last_used_at': utcnow()})
    return result


def task_addsub(connection, context, data, settings):
    parent = require_task(connection, context, data.get('task_id'), write=True)
    task = create_task(connection, context, {'name': data.get('name'), 'parent_id': parent['id'], 'project_id': parent['project_id'],
        'column_id': parent['column_id'], 'owner': [context.actor.userid], 'times': [parent['start_at'], parent['end_at']],
        'visibility': parent['visibility']}, settings)
    return task_data(connection, task, context.actor.userid)


def mutate_task(connection, context, task, data, settings, *, source='update', note='', check_permissions=True):
    data = normalize_times(data, task)
    p = require_project(connection, context, task['project_id'], write=True)
    tu = table('project_task_users')
    users = rows(connection, 'project_task_users', tu.c.task_id == task['id'])
    owners = [u['userid'] for u in users if u['owner'] == 1]
    assists = [u['userid'] for u in users if u['owner'] == 0]
    # Validate every requested action, not the source's first matching field only.
    if check_permissions and owners:
        ordinary = set(data) - {'task_id', 'times', 'start_at', 'end_at', 'complete_at', 'flow_item_id'}
        if ordinary:
            require_action(connection, context, p, 'task_update', task)
        if 'times' in data:
            require_action(connection, context, p, 'task_time', task)
        if 'flow_item_id' in data or 'complete_at' in data:
            require_action(connection, context, p, 'task_status', task)
    before = task_snapshot(connection, task)
    root = root_task(connection, task)
    affected = [root] + descendants(connection, root['id'])
    previous = {item['id']: task_allowed_users(connection, item) for item in affected}
    values, marking = {}, {}
    if 'flow_item_id' in data:
        new_flow = by_id(connection, 'project_flow_items', data['flow_item_id'])
        old_flow = by_id(connection, 'project_flow_items', task['flow_item_id'])
        if not new_flow or new_flow['project_id'] != p['id']:
            raise ApiError('任务状态不存在')
        if new_flow['id'] == task['flow_item_id']:
            raise ApiError('任务状态未发生改变')
        if old_flow:
            if new_flow['id'] not in ids(decode(old_flow['turns'])):
                raise ApiError(f'当前状态[{old_flow["name"]}]不可流转到[{new_flow["name"]}]')
            if old_flow['userlimit'] and not p['owner'] and context.actor.userid not in ids(decode(old_flow['userids'])):
                raise ApiError('当前状态仅限状态负责人或项目负责人修改')
        values.update(flow_item_id=new_flow['id'], flow_item_name='|'.join([new_flow['status'], new_flow['name'], new_flow['color'] or '']))
        data['complete_at'] = utcnow() if new_flow['status'] == 'end' else None
        flow_users = [u for u in ids(decode(new_flow['userids'])) if u in member_map(connection, p['id'])]
        if flow_users:
            if new_flow['usertype'] in ('replace', 'merge'):
                data['assist'] = list(dict.fromkeys(assists + owners)) if not task['parent_id'] else assists
                data['owner'] = flow_users + ([context.actor.userid] if new_flow['usertype'] == 'merge' else [])
            else:
                data['owner'] = list(dict.fromkeys(owners + flow_users))
        if new_flow['columnid']:
            data['column_id'] = new_flow['columnid']
        log(connection, context, p['id'], '修改{任务}状态', task=task, record={
            'flow': {**before, 'owner': owners, 'assist': assists}, 'change': [old_flow['name'] if old_flow else '', new_flow['name']]})
        db.insert_row(connection, 'project_task_flow_changes', task_id=task['id'], userid=context.actor.userid,
            before_flow_item_id=task['flow_item_id'], before_flow_item_name=task['flow_item_name'],
            after_flow_item_id=values['flow_item_id'], after_flow_item_name=values['flow_item_name'])
    if 'complete_at' in data:
        complete = bool(date(data['complete_at']))
        if task['parent_id']:
            parent = by_id(connection, 'project_tasks', task['parent_id'])
            if parent and parent['complete_at']:
                raise ApiError('主任务已完成，无法修改子任务状态')
        if complete and any(not child['complete_at'] for child in descendants(connection, task['id'])):
            raise ApiError('子任务未完成', -4004, {'task_id': task['id']})
        if complete and not ids(data.get('owner', owners)):
            raise ApiError('请先领取任务')
        if 'flow_item_id' not in data:
            f = table('project_flow_items')
            options = rows(connection, 'project_flow_items', f.c.project_id == p['id'], f.c.status == ('end' if complete else 'start'))
            if len(options) > 1:
                raise ApiError('请选择任务状态', -4005 if complete else -4006, {'task_id': task['id']})
            if options:
                f = options[0]
                values.update(flow_item_id=f['id'], flow_item_name='|'.join([f['status'], f['name'], f['color'] or '']))
        values['complete_at'] = utcnow() if complete else None
        marking['is_update_project'] = True
        log(connection, context, p['id'], '标记{任务}已完成' if complete else '标记{任务}未完成', task=task)
        if task['dialog_id'] and not task['parent_id']:
            from .dialog_core import send_system_message
            send_system_message(connection, task['dialog_id'], '标记任务已完成' if complete else '标记任务未完成')
    if 'name' in data:
        name = str(data['name']).strip()
        if not name or len(name) > 255:
            raise ApiError('任务描述不能为空且最多255个字')
        values['name'] = name
        if name != task['name']:
            log(connection, context, p['id'], '修改{任务}标题', task=task, record={'change': [task['name'], name]})
            if task['dialog_id']:
                update(connection, 'web_socket_dialogs', task['dialog_id'], {'name': name})
    if 'owner' in data or ('assist' in data and not task['parent_id']):
        new_owners = ids(data.get('owner', owners))
        new_assists = ids(data.get('assist', assists)) if not task['parent_id'] else assists
        _participants(connection, task, new_owners, new_assists)
        log(connection, context, p['id'], '修改{任务}负责人', task=task, record={'userid': new_owners})
        enqueue(connection, 'projects.notify', {'task_id': task['id'], 'userids': list(set(new_owners) - set(owners)), 'type': 0})
        marking['is_update_project'] = True
    if 'visibility' in data or 'visibility_appointor' in data:
        if task['parent_id']:
            raise ApiError('子任务继承主任务可见性')
        visibility = integer(data.get('visibility', task['visibility']))
        if visibility not in (1, 2, 3):
            raise ApiError('任务可见性设置错误')
        viewers = ids(data.get('visibility_appointor'))
        if set(viewers) - set(member_map(connection, p['id'])):
            raise ApiError('可见成员不在项目内')
        values['visibility'] = visibility
        vu = table('project_task_visibility_users')
        connection.execute(vu.delete().where(vu.c.task_id == task['id']))
        for uid in viewers:
            db.insert_row(connection, 'project_task_visibility_users', project_id=p['id'], task_id=task['id'], userid=uid)
        for child in descendants(connection, task['id']):
            update(connection, 'project_tasks', child['id'], {'visibility': visibility})
    if 'times' in data:
        start, end = times(data['times'])
        if task['parent_id']:
            parent = by_id(connection, 'project_tasks', task['parent_id'])
            if not start:
                start, end = parent['start_at'], parent['end_at']
            elif parent:
                parent_values = {}
                if not parent['start_at'] or start < parent['start_at']:
                    parent_values['start_at'] = start
                if not parent['end_at'] or end > parent['end_at']:
                    parent_values['end_at'] = end
                if parent_values:
                    update(connection, 'project_tasks', parent['id'], parent_values)
                    log(connection, context, p['id'], '同步修改{任务}时间', task=parent)
                    marking['is_update_maintask'] = True
        else:
            for child in descendants(connection, task['id']):
                cs, ce = child['start_at'], child['end_at']
                cs = start if not cs or cs == task['start_at'] or not start or cs < start else cs
                ce = end if not ce or ce == task['end_at'] or not end or ce > end else ce
                if cs and ce and cs > ce:
                    cs = start
                if (cs, ce) != (child['start_at'], child['end_at']):
                    update(connection, 'project_tasks', child['id'], {'start_at': cs, 'end_at': ce})
                    log(connection, context, p['id'], '同步修改{任务}时间', task=child)
                    marking['is_update_subtask'] = True
        values.update(start_at=start, end_at=end)
        if task['end_at'] and task['end_at'] < utcnow() and not task['complete_at']:
            log(connection, context, p['id'], '{任务}超期未完成', task=task, record={'cache': {
                'task_at': f'{task["start_at"]}~{task["end_at"]}', 'change_at': f'{start}~{end}',
                'over_sec': int((min(end, utcnow()) if end else utcnow()).timestamp() - task['end_at'].timestamp()),
                'owners': owners, 'assists': assists}})
        log(connection, context, p['id'], '修改{任务}时间', task=task, record={'change': [[task['start_at'], task['end_at']], [start, end]]})
        enqueue(connection, 'projects.notify', {'task_id': task['id'], 'userids': owners + assists, 'type': 3})
    if not task['parent_id']:
        for key, maximum in (('color', 20), ('p_name', 50), ('p_color', 20)):
            if key in data:
                if len(str(data[key])) > maximum:
                    raise ApiError('参数过长')
                values[key] = str(data[key])
        if 'p_level' in data:
            values['p_level'] = integer(data['p_level'])
        if 'column_id' in data:
            column = by_id(connection, 'project_columns', data['column_id'])
            if not column or column['project_id'] != p['id'] or column['deleted_at']:
                raise ApiError('请选择正确的列表')
            values['column_id'] = column['id']
            for child in descendants(connection, task['id']):
                update(connection, 'project_tasks', child['id'], {'column_id': column['id']})
        if 'loop' in data or 'times' in data:
            values['loop'] = str(data.get('loop', task['loop']) or '')
            values['loop_at'] = next_loop(values['loop'], values.get('start_at', task['start_at']))
        if 'content' in data:
            save_content(connection, context, task, data['content'], settings)
            values['desc'] = description(data['content'])
            marking['is_update_content'] = True
        if 'task_tag' in data:
            tags = decode(data['task_tag'])
            if not isinstance(tags, list):
                raise ApiError('标签参数错误')
            tt = table('project_task_tags')
            connection.execute(tt.delete().where(tt.c.task_id == task['id']))
            for tag in tags:
                name, color = str(tag.get('name', '')).strip(), str(tag.get('color', ''))
                if not name or len(name) > 50 or len(color) > 10:
                    raise ApiError('标签设置错误')
                db.insert_row(connection, 'project_task_tags', project_id=p['id'], task_id=task['id'], name=name, color=color)
            log(connection, context, p['id'], '修改{任务}标签', task=task, record={'tags': tags})
    update(connection, 'project_tasks', task['id'], values)
    result = by_id(connection, 'project_tasks', task['id'])
    handoff_record(connection, context, result, before, source, note)
    for item in affected:
        fresh = by_id(connection, 'project_tasks', item['id'])
        observe_task(connection, context, fresh, 'update', previous[item['id']])
    payload = task_data(connection, result, context.actor.userid)
    payload['update_marking'] = marking
    return payload


def task_update(connection, context, data, settings):
    return mutate_task(connection, context, require_task(connection, context, data.get('task_id'), write=True), data, settings)


def delete_task(connection, context, task, *, restore=False):
    affected = [task] + descendants(connection, task['id'], deleted=True)
    for item in affected:
        previous = task_allowed_users(connection, item)
        values = {'deleted_at': None if restore else utcnow()}
        if not restore:
            values['deleted_userid'] = actor(connection, context).userid
        update(connection, 'project_tasks', item['id'], values)
        if item['dialog_id']:
            update(connection, 'web_socket_dialogs', item['dialog_id'], {'deleted_at': values['deleted_at']})
            recipients = task_allowed_users(connection, item)
            if recipients:
                enqueue_push(connection, userids=recipients, message={'type': 'dialog', 'mode': 'groupRestore' if restore else 'delete',
                    'data': {'id': item['dialog_id']}}, scope={'task_id': item['id']} if restore else None)
        log(connection, context, item['project_id'], '还原{任务}' if restore else '删除{任务}', task=item)
        observe_task(connection, context, by_id(connection, 'project_tasks', item['id']), 'restore' if restore else 'delete', previous)


def task_remove(connection, context, data, settings):
    restore = data.get('type') == 'recovery'
    task = require_task(connection, context, data.get('task_id'), archived=None, deleted=restore)
    try:
        project = require_project(connection, context, task['project_id'], write=True)
        require_action(connection, context, project, 'task_remove', task)
    except ApiError:
        project = by_id(connection, 'projects', task['project_id'])
        if restore or (project and not project['deleted_at'] and not project['archived_at']):
            raise
        uid = context.actor.userid
        tu = table('project_task_users')
        participant = row(connection, 'project_task_users', tu.c.userid == uid, tu.c.task_id.in_([task['id'], root_task(connection, task)['id']]))
        if member_map(connection, task['project_id']).get(uid) not in (1, 2) and not participant and task['userid'] != uid:
            raise ApiError('仅项目负责人或任务相关成员删除')
    delete_task(connection, context, task, restore=restore)
    return {'id': task['id']}


def archive_task(connection, context, task, *, recover=False, automatic=False):
    if not task['complete_at']:
        raise ApiError('仅限已完成状态的任务归档')
    for item in [task] + descendants(connection, task['id']):
        before = task_snapshot(connection, item)
        if recover:
            column = by_id(connection, 'project_columns', item['column_id'])
            if column and column['deleted_at']:
                update(connection, 'project_columns', column['id'], {'deleted_at': None})
                from .projects import push_column
                push_column(connection, context, by_id(connection, 'project_columns', column['id']), 'recovery')
        update(connection, 'project_tasks', item['id'], {'archived_at': None if recover else utcnow(),
            'archived_userid': 0 if automatic else actor(connection, context).userid, 'archived_follow': 0})
        fresh = by_id(connection, 'project_tasks', item['id'])
        handoff_record(connection, context, fresh, before, 'auto_archive' if automatic else 'archive')
        log(connection, context, item['project_id'], '任务取消归档' if recover else '任务自动归档' if automatic else '任务归档', task=item)
        observe_task(connection, context, fresh, 'recovery' if recover else 'archived')


def task_archived(connection, context, data, settings):
    recover = data.get('type') == 'recovery'
    task = require_task(connection, context, data.get('task_id'), write=True, archived=not recover)
    if task['parent_id']:
        raise ApiError('子任务不支持此功能')
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_archived', task)
    archive_task(connection, context, task, recover=recover)
    fresh = by_id(connection, 'project_tasks', task['id'])
    return {k: fresh[k] for k in ('id', 'archived_at', 'archived_userid')}


def task_upgrade(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), write=True)
    if not task['parent_id']:
        raise ApiError('当前任务已是主任务')
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_move', task)
    root = root_task(connection, task)
    parent = by_id(connection, 'project_tasks', task['parent_id'])
    previous = task_allowed_users(connection, task)
    values = {k: root[k] for k in ('p_level', 'p_name', 'p_color')}
    values['parent_id'] = 0
    update(connection, 'project_tasks', task['id'], values)
    tu = table('project_task_users')
    connection.execute(tu.update().where(tu.c.task_id == task['id']).values(task_pid=task['id']))
    if task['visibility'] == 3:
        vu = table('project_task_visibility_users')
        for viewer in rows(connection, 'project_task_visibility_users', vu.c.task_id == root['id']):
            if not row(connection, 'project_task_visibility_users', vu.c.task_id == task['id'], vu.c.userid == viewer['userid']):
                db.insert_row(connection, 'project_task_visibility_users', project_id=p['id'], task_id=task['id'], userid=viewer['userid'])
    log(connection, context, p['id'], '子任务升级为主任务', task=parent, record={'subtask': {'id': task['id'], 'name': task['name']}})
    fresh = by_id(connection, 'project_tasks', task['id'])
    observe_task(connection, context, fresh, 'update', previous)
    parent = by_id(connection, 'project_tasks', parent['id'])
    observe_task(connection, context, parent, 'update', previous)
    for sibling in descendants(connection, root['id']):
        if sibling['id'] != fresh['id']:
            observe_task(connection, context, sibling, 'update', previous)
    create_relation(connection, context, parent, fresh)
    return {'task': task_data(connection, fresh, context.actor.userid), 'parent': task_data(connection, parent, context.actor.userid)}


def task_dialog(connection, context, data, settings):
    from .dialog_core import create_group
    task = require_task(connection, context, data.get('task_id'), write=True)
    if task['parent_id']:
        raise ApiError('子任务不支持此功能')
    if not task['dialog_id']:
        tu = table('project_task_users')
        participants = list(connection.execute(select(tu.c.userid).where(tu.c.task_id.in_([task['id']] + [t['id'] for t in descendants(connection, task['id'])])).distinct()).scalars())
        dialog_id = create_group(connection, owner_id=context.actor.userid, userids=participants, name=task['name'], group_type='task', link_id=task['id'])
        update(connection, 'project_tasks', task['id'], {'dialog_id': dialog_id})
        task['dialog_id'] = dialog_id
        observe_task(connection, context, task, 'dialog')
    from .dialog_core import dialog_data
    return {'id': task['id'], 'dialog_id': task['dialog_id'], 'dialog_data': dialog_data(connection, task['dialog_id'], context.actor.userid)}


def _target(connection, context, task, data):
    target = require_project(connection, context, data.get('project_id'), write=True)
    require_action(connection, context, target, 'task_add')
    column = by_id(connection, 'project_columns', data.get('column_id'))
    if not column or column['project_id'] != target['id'] or column['deleted_at']:
        raise ApiError('列表不存在')
    flow = by_id(connection, 'project_flow_items', data.get('flow_item_id'))
    if integer(data.get('flow_item_id')) and (not flow or flow['project_id'] != target['id']):
        raise ApiError('任务状态不存在')
    if not flow and count(connection, 'project_flow_items', table('project_flow_items').c.project_id == target['id']):
        raise ApiError('请选择移动后状态', 102)
    return target, column, flow


def task_move(connection, context, data, settings):
    if tuple(integer(part) for part in str(context.version or '0.0.1').split('.')[:3]) < (0, 42, 0):
        raise ApiError('当前客户端版本过低，最低版本要求 (0.42.0)。')
    task = require_task(connection, context, data.get('task_id'), write=True)
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_move', task)
    target, column, flow = _target(connection, context, task, data)
    if task['project_id'] == target['id'] and task['column_id'] == column['id']:
        return {'id': task['id']}
    affected = [task] + descendants(connection, task['id'])
    old_root = root_task(connection, task)
    old_family_users = task_allowed_users(connection, old_root)
    previous_by_id = {item['id']: task_allowed_users(connection, item) for item in affected}
    snapshots = {item['id']: task_snapshot(connection, item) for item in affected}
    results = []
    for item in affected:
        before = snapshots[item['id']]
        values = {'project_id': target['id'], 'column_id': column['id'], 'flow_item_id': flow['id'] if flow else 0,
                  'flow_item_name': '|'.join([flow['status'], flow['name'], flow['color'] or '']) if flow else ''}
        if item['id'] == task['id'] and task['parent_id']:
            values['parent_id'] = 0
        if flow:
            values['complete_at'] = utcnow() if flow['status'] == 'end' else None
        elif 'completed' in data:
            values['complete_at'] = utcnow() if str(data['completed']).lower() not in ('0', 'false', '') else None
        update(connection, 'project_tasks', item['id'], values)
        fresh = by_id(connection, 'project_tasks', item['id'])
        for name in ('project_task_users', 'project_task_visibility_users', 'project_task_tags', 'project_task_files', 'project_task_contents'):
            t = table(name)
            connection.execute(t.update().where(t.c.task_id == item['id']).values(project_id=target['id']))
        members = member_map(connection, target['id'])
        if item['id'] == task['id']:
            _participants(connection, fresh, [u for u in ids(data.get('owner')) if u in members], [u for u in ids(data.get('assist')) if u in members])
        else:
            tu = table('project_task_users')
            connection.execute(tu.delete().where(tu.c.task_id == item['id'], tu.c.userid.not_in(list(members))))
        vu = table('project_task_visibility_users')
        connection.execute(vu.delete().where(vu.c.task_id == item['id'], vu.c.userid.not_in(list(members))))
        handoff_record(connection, context, fresh, before, 'move')
        log(connection, context, p['id'], '移动{任务}', task=item, record={'change': [p['name'], target['name']]})
    sync_task_dialog(connection, by_id(connection, 'project_tasks', task['id']))
    for item in affected:
        fresh = by_id(connection, 'project_tasks', item['id'])
        observe_task(connection, context, fresh, 'update', previous_by_id[item['id']])
        results.append(task_data(connection, fresh, context.actor.userid))
    if old_root['id'] != task['id']:
        for item in [old_root] + descendants(connection, old_root['id']):
            observe_task(connection, context, by_id(connection, 'project_tasks', item['id']), 'update', old_family_users)
    return results


def copy_task(connection, context, source, target, column, flow, data, settings, parent_id=0):
    content = table('project_task_contents')
    latest = connection.execute(select(content.c.content).where(content.c.task_id == source['id']).order_by(content.c.id.desc()).limit(1)).scalar()
    params = {k: source[k] for k in ('name', 'p_level', 'p_name', 'p_color', 'visibility')}
    params.update(project_id=target['id'], column_id=column['id'], parent_id=parent_id,
        owner=data.get('owner', []), assist=data.get('assist', []), content=latest or '',
        times=[source['start_at'], source['end_at']])
    if source['visibility'] == 3:
        visible = table('project_task_visibility_users')
        viewers = connection.execute(select(visible.c.userid).where(visible.c.task_id == root_task(connection, source)['id'])).scalars()
        params['visibility_appointor'] = [u for u in viewers if u in member_map(connection, target['id'])]
    clone = create_task(connection, context, params, settings)
    for name in ('project_task_tags', 'project_task_files'):
        t = table(name)
        for old in rows(connection, name, t.c.task_id == source['id']):
            values = {k: v for k, v in old.items() if k not in ('id', 'created_at', 'updated_at')}
            values.update(project_id=target['id'], task_id=clone['id'])
            db.insert_row(connection, name, **values)
    t = table('project_tasks')
    for child in rows(connection, 'project_tasks', t.c.parent_id == source['id'], t.c.deleted_at.is_(None)):
        tu = table('project_task_users')
        child_users = rows(connection, 'project_task_users', tu.c.task_id == child['id'])
        target_users = member_map(connection, target['id'])
        child_data = {'owner': [u['userid'] for u in child_users if u['owner'] == 1 and u['userid'] in target_users],
            'assist': [u['userid'] for u in child_users if u['owner'] == 0 and u['userid'] in target_users], 'completed': False}
        f = table('project_flow_items')
        starting = rows(connection, 'project_flow_items', f.c.project_id == target['id'], f.c.status == 'start', order=[f.c.sort, f.c.id])
        copy_task(connection, context, child, target, column, starting[0] if starting else None, child_data, settings, clone['id'])
    values = {'flow_item_id': flow['id'] if flow else 0, 'flow_item_name': '|'.join([flow['status'], flow['name'], flow['color'] or '']) if flow else '',
        'complete_at': utcnow() if (flow and flow['status'] == 'end') or (not flow and str(data.get('completed', '')).lower() in ('1', 'true')) else None}
    values.update(color=source['color'], loop=source['loop'], loop_at=source['loop_at'])
    update(connection, 'project_tasks', clone['id'], values)
    fresh = by_id(connection, 'project_tasks', clone['id'])
    observe_task(connection, context, fresh, 'update')
    return fresh


def task_copy(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), write=True)
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_move', task)
    target, column, flow = _target(connection, context, task, data)
    clone = copy_task(connection, context, task, target, column, flow, data, settings)
    log(connection, context, target['id'], '复制{任务}', task=clone, record={'change': [task['id'], clone['id']]})
    return task_data(connection, clone, context.actor.userid)


def task_resetfromlog(connection, context, data, settings):
    entry = by_id(connection, 'project_logs', data.get('id'))
    if not entry or not entry['task_id']:
        raise ApiError('记录不存在')
    task = require_task(connection, context, entry['task_id'], write=True)
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_status', task)
    saved = decode(entry['record'], {}).get('flow')
    if not isinstance(saved, dict):
        raise ApiError('该记录不支持重置')
    flow = by_id(connection, 'project_flow_items', saved.get('flow_item_id'))
    if not flow or flow['project_id'] != p['id']:
        raise ApiError('流程不存在或已被删除')
    # Restoration still validates the current action permissions and owner membership.
    return mutate_task(connection, context, task, {k: v for k, v in saved.items() if k in ('flow_item_id', 'complete_at', 'owner', 'assist')}, settings)


def create_relation(connection, context, source, target, dialog_id=0, msg_id=0):
    if source['id'] == target['id']:
        return
    t = table('project_task_relations')
    for left, right, direction in ((source, target, 'mention'), (target, source, 'mentioned_by')):
        statement = insert(t).values(task_id=left['id'], related_task_id=right['id'], direction=direction,
            dialog_id=dialog_id or None, msg_id=msg_id or None, userid=actor(connection, context).userid,
            created_at=utcnow(), updated_at=utcnow())
        connection.execute(statement.on_duplicate_key_update(dialog_id=dialog_id or None, msg_id=msg_id or None, updated_at=utcnow()))
        log(connection, context, left['project_id'], '关联任务', task=left, record={'change': ['#' + str(right['id']) + ' ' + right['name']]})
        recipients = task_allowed_users(connection, left)
        if recipients:
            enqueue_push(connection, userids=recipients, message={'type': 'projectTask', 'action': 'relation',
                'data': {'id': left['id'], 'project_id': left['project_id']}}, scope={'task_id': left['id']})


def task_related_delete(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), write=True)
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_update', task)
    target_id = integer(data.get('related_task_id'))
    t = table('project_task_relations')
    condition = or_(and_(t.c.task_id == task['id'], t.c.related_task_id == target_id), and_(t.c.task_id == target_id, t.c.related_task_id == task['id']))
    if not connection.execute(t.delete().where(condition)).rowcount:
        raise ApiError('关联不存在')
    for item in (task, by_id(connection, 'project_tasks', target_id)):
        if item:
            log(connection, context, item['project_id'], '解除关联任务', task=item)
            observe_task(connection, context, item, 'relation')
    return []


def sync_message_relations(connection, message):
    uid = integer(message.get('userid'))
    users = table('users')
    user = row(connection, 'users', users.c.userid == uid)
    if not user:
        return
    context = RequestContext('message-project-observer', actor=Actor(uid, user, {}, {}))
    payload = decode(message.get('msg'), {})
    tasks = table('project_tasks')
    sources = rows(connection, 'project_tasks', tasks.c.dialog_id == message['dialog_id'], tasks.c.deleted_at.is_(None))
    if message.get('deleted_at') or message.get('revoke'):
        relations = table('project_task_relations')
        connection.execute(relations.delete().where(relations.c.msg_id == message['id']))
        return
    if message.get('type') == 'text':
        targets = ids(re.findall(r'<span\s+class=[\"\']mention task[\"\']\s+data-id=[\"\'](\d+)[\"\']', payload.get('text', ''), re.I))
        for source in sources:
            for target_id in targets:
                target = by_id(connection, 'project_tasks', target_id)
                if can_view_task(connection, uid, target):
                    create_relation(connection, context, source, target, message['dialog_id'], message['id'])


ROUTES = {
    'project/task/add': task_add, 'project/task/addsub': task_addsub, 'project/task/update': task_update,
    'project/task/upgrade': task_upgrade, 'project/task/dialog': task_dialog,
    'project/task/archived': task_archived, 'project/task/remove': task_remove,
    'project/task/resetfromlog': task_resetfromlog, 'project/task/move': task_move,
    'project/task/copy': task_copy, 'project/task/related_delete': task_related_delete,
}
