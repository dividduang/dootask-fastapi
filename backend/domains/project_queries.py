"""Read-side task filtering, incremental tombstones, history and task attachments."""
from datetime import timedelta
from sqlalchemy import select, or_, and_, func
from starlette.responses import FileResponse, RedirectResponse
from .. import db
from ..auth import require_admin
from ..context import utcnow
from ..errors import ApiError
from .project_core import (table, actor, row, rows, by_id, update, count, page, ids, integer, decode,
    date, time_range, require_project, require_task, require_action, member_map, department_projects,
    can_view_task, task_data, deleted_ids, observe_task, log)


def _time_range(value):
    now = utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if isinstance(value, str) and ',' in value:
        value = value.split(',')
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return date(value[0]).replace(hour=0, minute=0, second=0), date(value[1]).replace(hour=23, minute=59, second=59)
    if value == 'today':
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    if value == 'week':
        start -= timedelta(days=start.weekday())
        return start, start + timedelta(days=7) - timedelta(microseconds=1)
    if value == 'month':
        start = start.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, end - timedelta(microseconds=1)
    if value == 'year':
        return start.replace(month=1, day=1), start.replace(year=start.year+1, month=1, day=1) - timedelta(microseconds=1)
    return None


def visible_task_query(connection, context, data):
    uid = actor(connection, context).userid
    t, tu, pu, tt = (table(v) for v in ('project_tasks', 'project_task_users', 'project_users', 'project_task_tags'))
    pid, parent_id = integer(data.get('project_id')), integer(data.get('parent_id'))
    query = select(t)
    archived, deleted = data.get('archived', 'no'), data.get('deleted', 'no')
    if deleted != 'all':
        query = query.where(t.c.deleted_at.is_not(None) if deleted == 'yes' else t.c.deleted_at.is_(None))
    if parent_id > 0:
        require_task(connection, context, parent_id, archived=None, deleted=None)
        query = query.where(t.c.parent_id == parent_id)
        archived = 'all'
    elif parent_id == -1:
        query = query.where(t.c.parent_id == 0)
    if pid:
        require_project(connection, context, pid)
        query = query.where(t.c.project_id == pid)
    elif parent_id <= 0 and data.get('scope') == 'all_project':
        own = list(connection.execute(select(pu.c.project_id).where(pu.c.userid == uid)).scalars())
        managed = department_projects(connection, uid, data.get('department_owner_ids', data.get('department_ids', 'all')))
        query = query.where(t.c.project_id.in_(set(own + managed)))
    if not pid and parent_id <= 0 and data.get('scope') != 'all_project':
        member_query = select(tu.c.task_id).where(tu.c.userid == uid)
        if data.get('owner') not in (None, ''):
            member_query = member_query.where(tu.c.owner == integer(data['owner']))
        query = query.where(t.c.id.in_(member_query))
    elif data.get('owner') not in (None, ''):
        query = query.where(t.c.id.in_(select(tu.c.task_id).where(tu.c.userid == uid, tu.c.owner == integer(data['owner']))))
    if archived != 'all':
        query = query.where(t.c.archived_at.is_not(None) if archived == 'yes' else t.c.archived_at.is_(None))
    keys = decode(data.get('keys'), {})
    for keyword in (data.get('name'), keys.get('name')):
        if keyword:
            like = '%' + str(keyword) + '%'
            query = query.where(or_(t.c.name.like(like), t.c.desc.like(like), t.c.id == integer(keyword, -1)))
    if keys.get('tag'):
        query = query.where(t.c.id.in_(select(tt.c.task_id).where(tt.c.name == str(keys['tag']))))
    status = str(keys.get('status', ''))
    if status == 'completed':
        query = query.where(t.c.complete_at.is_not(None))
    elif status == 'uncompleted':
        query = query.where(t.c.complete_at.is_(None))
    elif status.startswith('flow-'):
        value = status[5:]
        query = query.where(t.c.flow_item_id == int(value) if value.isdigit() else t.c.flow_item_name.like('%' + value + '%'))
    period = _time_range(data.get('time'))
    if period:
        query = query.where(t.c.start_at <= period[1], t.c.end_at >= period[0])
    timerange = time_range(data.get('timerange'))
    if isinstance(timerange, dict) and timerange.get('updated'):
        query = query.where(t.c.updated_at > date(timerange['updated']))
    # Apply ACL before count/pagination. The root helper preserves arbitrary-depth inheritance.
    candidates = [dict(v) for v in connection.execute(query).mappings()]
    visible = []
    for task in candidates:
        probe = {**task, 'deleted_at': None}
        if can_view_task(connection, uid, probe):
            visible.append(task['id'])
    query = query.where(t.c.id.in_(visible))
    sorts = decode(data.get('sorts'), {})
    for key, direction in sorts.items():
        if key in ('complete_at', 'archived_at', 'end_at', 'deleted_at') and direction in ('asc', 'desc'):
            query = query.order_by(t.c[key].asc() if direction == 'asc' else t.c[key].desc())
    return query.order_by(t.c.id.desc())


def task_lists(connection, context, data, settings):
    result = page(connection, visible_task_query(connection, context, data), data, 200, 100)
    result['data'] = [task_data(connection, task, context.actor.userid) for task in result['data']]
    if result['current_page'] == 1:
        timerange = time_range(data.get('timerange'))
        result['deleted_id'] = deleted_ids(connection, 'projectTask', context.actor.userid, timerange.get('deleted') if isinstance(timerange, dict) else None)
    return result


def task_one(connection, context, data, settings):
    archived = {'all': None, 'yes': False, 'no': True}.get(data.get('archived', 'no'), True)
    task = require_task(connection, context, data.get('task_id'), archived=archived)
    return task_data(connection, task, context.actor.userid)


def task_subdata(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'))
    value = task_data(connection, task, context.actor.userid)
    return {k: value[k] for k in ('id', 'sub_num', 'sub_complete', 'percent')}


def task_easylists(connection, context, data, settings):
    actor(connection, context)
    tu, t = table('project_task_users'), table('project_tasks')
    userids = ids(data.get('userid'))
    query = visible_task_query(connection, context, {'scope': 'all_project', 'archived': 'all', 'keys': {'status': 'uncompleted'}})
    query = query.where(t.c.id.in_(select(tu.c.task_id).where(tu.c.userid.in_(userids), tu.c.owner == 1)), t.c.id != integer(data.get('taskid')))
    period = time_range(data.get('timerange'))
    if period['updated'] and period['deleted']:
        query = query.where(t.c.start_at <= period['deleted'].replace(hour=23, minute=59, second=59), t.c.end_at >= period['updated'].replace(hour=0, minute=0, second=0))
    result = page(connection, query, data, 200, 100)
    result['data'] = [{k: task_data(connection, task, context.actor.userid)[k] for k in ('project_name', 'project_id', 'id', 'name', 'start_at', 'end_at', 'task_user')} for task in result['data']]
    return result


def _works(connection, context, target):
    user = actor(connection, context)
    if target <= 0:
        raise ApiError('成员不存在')
    if target == user.userid or 'admin' in user.identities:
        return None
    from .accounts import managed_departments, department_users
    managed = managed_departments(connection, user.userid)
    roots = [integer(v['id']) if isinstance(v, dict) else integer(v) for v in managed]
    people = department_users(connection, roots, recursive=True)
    if target not in [integer(v['userid']) if isinstance(v, dict) else integer(v) for v in people]:
        raise ApiError('没有查看权限')
    if db.setting(connection, 'system', 'department_owner_project_view', 'open') != 'open':
        raise ApiError('没有查看权限')
    return department_projects(connection, user.userid)


def user_projects(connection, context, data, settings):
    target = integer(data.get('userid'))
    readonly = _works(connection, context, target)
    p, pu = table('projects'), table('project_users')
    query = select(p).join(pu, pu.c.project_id == p.c.id).where(pu.c.userid == target, p.c.deleted_at.is_(None))
    if readonly is not None:
        query = query.where(p.c.id.in_(readonly))
    archived = data.get('archived', 'no')
    if archived != 'all':
        query = query.where(p.c.archived_at.is_not(None) if archived == 'yes' else p.c.archived_at.is_(None))
    keyword = decode(data.get('keys'), {}).get('name')
    if keyword:
        query = query.where(p.c.name.like('%' + str(keyword) + '%'))
    result = page(connection, query.order_by(pu.c.top_at.desc(), pu.c.sort, p.c.id.desc()), data)
    from .projects import project_data
    result['data'] = [{**project_data(connection, item, target), 'department_readonly': readonly is not None} for item in result['data']]
    return result


def _user_tasks_query(connection, context, data, target, readonly):
    t, tu = table('project_tasks'), table('project_task_users')
    query = select(t).where(t.c.id.in_(select(tu.c.task_id).where(tu.c.userid == target,
        *([] if data.get('owner') in (None, '') else [tu.c.owner == integer(data['owner'])]))),
        t.c.archived_at.is_(None), t.c.deleted_at.is_(None))
    if readonly is not None:
        query = query.where(t.c.project_id.in_(readonly), t.c.visibility == 1)
    if integer(data.get('project_id')):
        query = query.where(t.c.project_id == integer(data['project_id']))
    keys = decode(data.get('keys'), {})
    if keys.get('name'):
        query = query.where(or_(t.c.name.like('%' + str(keys['name']) + '%'), t.c.desc.like('%' + str(keys['name']) + '%')))
    if keys.get('status') in ('completed', 'uncompleted'):
        query = query.where(t.c.complete_at.is_not(None) if keys['status'] == 'completed' else t.c.complete_at.is_(None))
    return query.order_by(t.c.id.desc())


def user_tasks(connection, context, data, settings):
    target = integer(data.get('userid'))
    readonly = _works(connection, context, target)
    query = _user_tasks_query(connection, context, data, target, readonly)
    result = page(connection, query, data)
    result['data'] = [{**task_data(connection, item, target), 'department_readonly': readonly is not None} for item in result['data']]
    return result


def user_counts(connection, context, data, settings):
    target = integer(data.get('userid'))
    readonly = _works(connection, context, target)
    projects = user_projects(connection, context, {**data, 'archived': 'no'}, settings)['total']
    query = _user_tasks_query(connection, context, {k: v for k, v in data.items() if k != 'keys'}, target, readonly)
    t = table('project_tasks')
    todo = connection.execute(select(func.count()).select_from(query.where(t.c.complete_at.is_(None)).subquery())).scalar_one()
    done = connection.execute(select(func.count()).select_from(query.where(t.c.complete_at.is_not(None)).subquery())).scalar_one()
    return {'project': projects, 'todo': todo, 'done': done}


def task_related(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), archived=None)
    t = table('project_task_relations')
    relations = rows(connection, 'project_task_relations', t.c.task_id == task['id'], order=[t.c.updated_at.desc()])[:100]
    result = {}
    for relation in relations:
        other = by_id(connection, 'project_tasks', relation['related_task_id'])
        if not can_view_task(connection, context.actor.userid, other):
            continue
        value = task_data(connection, other, context.actor.userid)
        parts = str(other['flow_item_name'] or '').split('|')
        value.update(flow_item_status=parts[0] if len(parts) > 1 else '', flow_item_name=parts[1] if len(parts) > 1 else parts[0],
            flow_item_color=parts[2] if len(parts) > 2 else '')
        entry = result.setdefault(other['id'], {'task_id': task['id'], 'related_task_id': other['id'],
            'mention': False, 'mentioned_by': False, 'latest_msg_id': relation['msg_id'], 'latest_at': relation['updated_at'],
            'task': {k: value[k] for k in ('id', 'name', 'project_id', 'project_name', 'column_id', 'column_name',
                'complete_at', 'archived_at', 'flow_item_name', 'flow_item_status', 'flow_item_color')}})
        if relation['direction'] in ('mention', 'mentioned_by'):
            entry[relation['direction']] = True
    return {'task_id': task['id'], 'list': list(result.values())}


def task_content(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), archived=None)
    t = table('project_task_contents')
    query = select(t).where(t.c.task_id == task['id'])
    if integer(data.get('history_id')):
        query = query.where(t.c.id == integer(data['history_id']))
    result = connection.execute(query.order_by(t.c.id.desc()).limit(1)).mappings().first()
    if not result:
        if integer(data.get('history_id')):
            raise ApiError('历史版本不存在')
        return {}
    result = dict(result)
    result.pop('updated_at', None)
    if integer(data.get('history_id')):
        result['name'] = task['name']
    return result


def task_content_history(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), archived=None)
    t = table('project_task_contents')
    return page(connection, select(t.c.id, t.c.task_id, t.c.desc, t.c.userid, t.c.created_at).where(t.c.task_id == task['id']).order_by(t.c.id.desc()), data, 100, 20)


def task_files(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), archived=None)
    t = table('project_task_files')
    return rows(connection, 'project_task_files', t.c.task_id == task['id'], order=[t.c.id.desc()])[:50]


def _file(connection, context, data, *, write=False):
    file = by_id(connection, 'project_task_files', data.get('file_id'))
    if not file:
        raise ApiError('文件不存在或已被删除')
    task = require_task(connection, context, file['task_id'], write=write, archived=True if write else None)
    return file, task


def task_filedelete(connection, context, data, settings):
    file, task = _file(connection, context, data, write=True)
    p = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, p, 'task_remove', task)
    log(connection, context, p['id'], '删除附件：' + file['name'], task=task,
        record={k: file[k] for k in ('name', 'size', 'path', 'thumb')} | {'file_id': file['id']})
    connection.execute(table('project_task_files').delete().where(table('project_task_files').c.id == file['id']))
    from .project_core import task_allowed_users
    recipients = task_allowed_users(connection, task)
    if recipients:
        from ..jobs import enqueue_push
        enqueue_push(connection, userids=recipients, message={'type': 'projectTask', 'action': 'filedelete',
            'data': {'id': task['id'], 'project_id': task['project_id'], 'file_id': file['id']}}, scope={'task_id': task['id']})
    return []


def task_filedetail(connection, context, data, settings):
    file, task = _file(connection, context, data)
    if data.get('only_update_at') == 'yes':
        return {'id': file['id'], 'update_at': file['updated_at']}
    from .account_common import upsert
    upsert(connection, 'user_recent_items', {'userid': context.actor.userid, 'target_type': 'task_file', 'target_id': file['id']},
        {'source_type': 'project_task', 'source_id': task['id'], 'browsed_at': utcnow()})
    from .files_collaboration import format_file_data
    return format_file_data(connection, context, file, settings)


def task_filedown(connection, context, data, settings):
    file, task = _file(connection, context, data)
    from .files_collaboration import resolve_attachment_path
    if data.get('down') == 'preview':
        from .index import to_preview_url
        return RedirectResponse(to_preview_url(context, file), status_code=302)
    path = resolve_attachment_path(connection, context, file['path'], settings)
    if path is None:
        return RedirectResponse(file['path'])
    if not path.is_file():
        raise ApiError('文件不存在')
    update(connection, 'project_task_files', file['id'], {'download': integer(file['download']) + 1})
    return FileResponse(path, filename=file['name'])


def log_lists(connection, context, data, settings):
    l = table('project_logs')
    task_id = integer(data.get('task_id'))
    if task_id:
        task = require_task(connection, context, task_id, archived=None)
        query = select(l).where(l.c.task_id == task['id'])
    else:
        p = require_project(connection, context, data.get('project_id'))
        # Project logs must not disclose private task names/content to other project members.
        t = table('project_tasks')
        visible = [task['id'] for task in rows(connection, 'project_tasks', t.c.project_id == p['id'])
            if can_view_task(connection, context.actor.userid, {**task, 'deleted_at': None})]
        query = select(l).where(l.c.project_id == p['id'], l.c.task_only == 0, or_(l.c.task_id == 0, l.c.task_id.in_(visible)))
    result = page(connection, query.order_by(l.c.created_at.desc(), l.c.id.desc()), data, 100, 20)
    for item in result['data']:
        item['record'] = decode(item['record'], {})
        when = item['created_at'] or utcnow()
        item['time'] = {'ymd': when.strftime('%m-%d' if when.year == utcnow().year else '%Y-%m-%d'),
            'hi': when.strftime('%I:%M'), 'week': '周' + '一二三四五六日'[when.weekday()],
            'segment': '凌晨' if when.hour < 6 else '上午' if when.hour < 12 else '下午' if when.hour < 18 else '晚上'}
        if not task_id and item['task_id']:
            task = by_id(connection, 'project_tasks', item['task_id'])
            item['project_task'] = {k: task[k] for k in ('id', 'parent_id', 'name')} if task else None
    return result


ROUTES = {
    'project/task/lists': task_lists, 'project/task/easylists': task_easylists,
    'project/task/one': task_one, 'project/task/subdata': task_subdata, 'project/task/related': task_related,
    'project/task/content': task_content, 'project/task/content_history': task_content_history,
    'project/task/files': task_files, 'project/task/filedelete': task_filedelete,
    'project/task/filedetail': task_filedetail, 'project/task/filedown': task_filedown,
    'project/user/projects': user_projects, 'project/user/tasks': user_tasks, 'project/user/counts': user_counts,
    'project/log/lists': log_lists,
}
RESPONSE_KINDS = {'project/task/filedown': 'Response'}
