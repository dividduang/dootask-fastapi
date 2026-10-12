"""Project persistence, authorization and transactional observer effects."""
import json
from datetime import datetime, timezone
from sqlalchemy import select, func, and_, or_
from sqlalchemy.dialects.mysql import insert
from .. import db
from ..auth import authenticate
from ..context import utcnow
from ..errors import ApiError, json_default
from ..jobs import enqueue, enqueue_push

DEFAULT_PERMISSIONS = {
    'task_list_add': [1, 2], 'task_list_update': [1, 2], 'task_list_remove': [1],
    'task_list_sort': [1, 2], 'task_add': [1, 2], 'task_update': [1, 3, 4],
    'task_time': [1, 3, 4], 'task_status': [1, 3], 'task_remove': [1, 3],
    'task_archived': [1, 3], 'task_move': [1, 3],
}


def table(name):
    return db.tables[name]


def integer(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def decode(value, default=None):
    if isinstance(value, (dict, list)):
        result = value
    else:
        try:
            result = json.loads(value) if value else (default if default is not None else [])
        except (ValueError, TypeError):
            result = default if default is not None else []
    if isinstance(default, dict) and not isinstance(result, dict):
        raise ApiError('对象参数格式错误')
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=json_default)


def ids(value):
    if isinstance(value, str):
        value = decode(value, value.split(','))
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return list(dict.fromkeys(integer(v) for v in value if integer(v) > 0))


def date(value):
    if value in (None, '', False, 0):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = float(value)
            if len(str(int(stamp))) >= 13:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result
    except (ValueError, OverflowError, OSError):
        raise ApiError('时间格式错误')


def time_range(value):
    if isinstance(value, str) and value.lstrip().startswith(('[', '{')):
        value = decode(value)
    if isinstance(value, dict):
        base = time_range(value.get('timerange'))
        return {'updated': date(value.get('updated_at') or value.get('at_after') or value.get('updated')) or base['updated'],
                'deleted': date(value.get('deleted_at') or value.get('deleted')) or base['deleted']}
    if isinstance(value, str):
        value = value.replace('|', ',')
        if ':' not in value:
            value = value.replace('-', ',')
        value = value.split(',')
    value = value if isinstance(value, (list, tuple)) else []
    return {'updated': date(value[0]) if value else None, 'deleted': date(value[1]) if len(value) > 1 else None}


def actor(connection, context):
    return context.actor or authenticate(connection, context)


def rows(connection, name, *conditions, order=None):
    query = select(table(name)).where(*conditions)
    if order is not None:
        query = query.order_by(*order)
    return [dict(r) for r in connection.execute(query).mappings()]


def row(connection, name, *conditions, lock=False):
    query = select(table(name)).where(*conditions).limit(1)
    if lock:
        query = query.with_for_update()
    value = connection.execute(query).mappings().first()
    return dict(value) if value else None


def by_id(connection, name, identifier, lock=False):
    return row(connection, name, table(name).c.id == integer(identifier), lock=lock)


def update(connection, name, identifier, values):
    values = dict(values)
    if 'updated_at' in table(name).c:
        values['updated_at'] = utcnow()
    connection.execute(table(name).update().where(table(name).c.id == identifier).values(**values))


def count(connection, name, *conditions):
    return connection.execute(select(func.count()).select_from(table(name)).where(*conditions)).scalar_one()


def page(connection, query, data, maximum=100, default=50):
    size = min(maximum, max(1, integer(data.get('pagesize', data.get('page_size')), default)))
    number = max(1, integer(data.get('page'), 1))
    total = connection.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    result = [dict(r) for r in connection.execute(query.limit(size).offset((number - 1) * size)).mappings()]
    return {'current_page': number, 'data': result, 'first_page_url': None,
            'from': (number - 1) * size + 1 if result else None, 'last_page': max(1, (total + size - 1) // size),
            'last_page_url': None, 'next_page_url': None, 'path': '', 'per_page': size,
            'prev_page_url': None, 'to': (number - 1) * size + len(result) if result else None, 'total': total}


def member_map(connection, project_id):
    return {r['userid']: r['owner'] for r in rows(connection, 'project_users', table('project_users').c.project_id == project_id)}


def department_projects(connection, userid, selected='all'):
    from .accounts import managed_departments, department_users
    if db.setting(connection, 'system', 'department_owner_project_view', 'open') != 'open':
        return []
    managed = managed_departments(connection, userid)
    managed_ids = [integer(item['id']) if isinstance(item, dict) else integer(item) for item in managed]
    selected_ids = managed_ids if selected in (None, '', 'all') else [v for v in ids(selected) if v in managed_ids]
    users = department_users(connection, selected_ids, recursive=True)
    userids = [integer(u['userid']) if isinstance(u, dict) else integer(u) for u in users]
    p, pu = table('projects'), table('project_users')
    return list(connection.execute(select(p.c.id).join(pu, pu.c.project_id == p.c.id).where(
        pu.c.userid.in_(userids), p.c.deleted_at.is_(None), p.c.department_owner_view != 'close').distinct()).scalars())


def can_view_project(connection, userid, project):
    return bool(project and not project.get('deleted_at') and (userid in member_map(connection, project['id'])
                or project['id'] in department_projects(connection, userid)))


def require_project(connection, context, project_id, *, write=False, owner=None, archived=True):
    userid = actor(connection, context).userid
    project = by_id(connection, 'projects', project_id, lock=write)
    if not project or project['deleted_at']:
        raise ApiError('项目不存在或不在成员列表内', -4001, {'project_id': integer(project_id)})
    members = member_map(connection, project['id'])
    readonly = userid not in members
    if readonly and (write or owner is not None or project['id'] not in department_projects(connection, userid)):
        raise ApiError('项目不存在或不在成员列表内', -4001, {'project_id': project['id']})
    if archived is True and project['archived_at']:
        raise ApiError('项目已归档', -4001, {'project_id': project['id']})
    if archived is False and not project['archived_at']:
        raise ApiError('项目未归档')
    role = members.get(userid, 0)
    if (owner is True and not role) or (owner == 'primary' and role != 1):
        raise ApiError('仅限项目负责人操作')
    if owner is False and role:
        raise ApiError('禁止项目负责人操作')
    project.update(owner=role, owner_userid=next((u for u, r in members.items() if r == 1), 0),
                   deputy_userids=[u for u, r in members.items() if r == 2], department_readonly=readonly)
    return project


def root_task(connection, task):
    seen = set()
    while task['parent_id']:
        if task['id'] in seen:
            raise ApiError('任务父子关系异常')
        seen.add(task['id'])
        parent = by_id(connection, 'project_tasks', task['parent_id'])
        if not parent or parent['project_id'] != task['project_id']:
            raise ApiError('父任务不存在', -4004, {'task_id': task['id']})
        task = parent
    return task


def task_allowed_users(connection, task):
    root = root_task(connection, task)
    members = member_map(connection, task['project_id'])
    pu = table('project_task_users')
    tasks = table('project_tasks')
    family, frontier = {root['id']}, [root['id']]
    while frontier:
        child_ids = set(connection.execute(select(tasks.c.id).where(tasks.c.parent_id.in_(frontier))).scalars())
        frontier = list(child_ids - family)
        family.update(frontier)
    participant = set(connection.execute(select(pu.c.userid).where(pu.c.task_id.in_(family))).scalars())
    if root['visibility'] == 1:
        return sorted(set(members) | participant)
    allowed = participant | {u for u, role in members.items() if role in (1, 2)}
    if root['visibility'] == 3:
        visible = table('project_task_visibility_users')
        allowed.update(connection.execute(select(visible.c.userid).where(visible.c.task_id.in_([task['id'], root['id']]), visible.c.userid.in_(list(members)))).scalars())
    return sorted(allowed)


def can_view_task(connection, userid, task):
    if not task or task.get('deleted_at'):
        return False
    if userid in task_allowed_users(connection, task):
        return True
    return task['visibility'] == 1 and task['project_id'] in department_projects(connection, userid)


def require_task(connection, context, task_id, *, write=False, archived=True, deleted=False):
    userid = actor(connection, context).userid
    task = by_id(connection, 'project_tasks', task_id, lock=write)
    if not task or (deleted is False and task['deleted_at']) or (deleted is True and not task['deleted_at']):
        raise ApiError('任务不存在', -4002, {'task_id': integer(task_id)})
    allowed = userid in task_allowed_users(connection, task)
    if not allowed and not (not write and task['visibility'] == 1 and task['project_id'] in department_projects(connection, userid)):
        raise ApiError('无任务权限', -4002, {'task_id': task['id'], 'force': 1})
    if write:
        require_project(connection, context, task['project_id'], write=True, archived=None)
    if archived is True and task['archived_at']:
        raise ApiError('任务已归档', data={'task_id': task['id']})
    if archived is False and not task['archived_at']:
        raise ApiError('任务未归档')
    return task


def permission_record(connection, project_id):
    t = table('project_permissions')
    result = row(connection, 'project_permissions', t.c.project_id == project_id)
    if not result:
        identifier = db.insert_row(connection, 'project_permissions', project_id=project_id, permissions=encode(DEFAULT_PERMISSIONS))
        result = by_id(connection, 'project_permissions', identifier)
    result['permissions'] = decode(result['permissions'], {})
    return result


def require_action(connection, context, project, action, task=None):
    userid = actor(connection, context).userid
    if project.get('department_readonly'):
        raise ApiError('部门负责人视角仅可查看')
    permitted = permission_record(connection, project['id'])['permissions'].get(action)
    if permitted is None:
        raise ApiError('项目权限设置不存在')
    members = member_map(connection, project['id'])
    roles = set()
    if members.get(userid) in (1, 2):
        roles.add(1)
    if userid in members:
        roles.add(2)
    if task:
        root = root_task(connection, task)
        t = table('project_task_users')
        task_roles = rows(connection, 'project_task_users', t.c.userid == userid, t.c.task_id.in_([task['id'], root['id']]))
        if any(r['owner'] == 1 for r in task_roles):
            roles.add(3)
        if any(r['owner'] == 0 and r['task_id'] == root['id'] for r in task_roles):
            roles.add(4)
    if not roles.intersection(permitted):
        raise ApiError('没有执行此操作的权限')


def deleted_record(connection, kind, identifier, userids, forget=False):
    t = table('deleteds')
    for userid in set(userids):
        condition = and_(t.c.type == kind, t.c.did == identifier, t.c.userid == userid)
        if forget:
            connection.execute(t.delete().where(condition))
        else:
            statement = insert(t).values(type=kind, did=identifier, userid=userid, created_at=utcnow())
            connection.execute(statement.on_duplicate_key_update(created_at=utcnow()))


def deleted_ids(connection, kind, userid, since=None):
    t = table('deleteds')
    query = select(t.c.did).where(t.c.type == kind, t.c.userid == userid)
    if since:
        query = query.where(t.c.created_at > date(since))
    return list(connection.execute(query).scalars())


def log(connection, context, project_id, detail, *, task=None, column_id=0, record=None, task_only=0):
    return db.insert_row(connection, 'project_logs', project_id=project_id, column_id=task['column_id'] if task else column_id,
        task_id=task['id'] if task else 0, task_only=task_only, userid=actor(connection, context).userid,
        detail=detail.replace('{任务}', '子任务' if task and task['parent_id'] else '任务'), record=encode(record or {}))


def push_project(connection, context, project_id, action, payload=None, recipients=None):
    project = by_id(connection, 'projects', project_id)
    members = member_map(connection, project_id)
    for userid in (recipients if recipients is not None else members):
        data = dict(payload or {})
        if action == 'delete':
            data = {'id': project_id}
        else:
            data.update(id=project_id, name=project['name'], desc=project['desc'])
            if 'owner' in data:
                data['owner'] = members.get(userid, 0)
            data.pop('top_at', None)
        enqueue_push(connection, userids=[userid], ignore_fds=[context.fd],
            message={'type': 'project', 'action': action, 'data': data},
            scope=None if action == 'delete' else {'project_id': project_id})


def task_snapshot(connection, task):
    t = table('project_task_users')
    return {**{key: task.get(key) for key in ('flow_item_id', 'flow_item_name', 'complete_at', 'archived_at')},
            'owners': sorted(connection.execute(select(t.c.userid).where(t.c.task_id == task['id'], t.c.owner == 1)).scalars())}


def handoff_record(connection, context, task, before, source='update', note=''):
    after = task_snapshot(connection, task)
    if before != after:
        db.insert_row(connection, 'project_task_handoffs', task_id=task['id'], userid=actor(connection, context).userid,
                      source=source, record=encode({'before': before, 'after': after, 'note': note}))


def observe_task(connection, context, task, action, previous_users=()):
    allowed = task_allowed_users(connection, task)
    revoked = set(previous_users) - set(allowed)
    if task['deleted_at'] or task['archived_at']:
        revoked.update(allowed)
    if revoked:
        deleted_record(connection, 'projectTask', task['id'], revoked)
        enqueue_push(connection, userids=revoked, ignore_fds=[context.fd],
            message={'type': 'projectTask', 'action': 'delete' if action not in ('archived',) else action,
                     'data': {'id': task['id'], 'project_id': task['project_id'], 'parent_id': task['parent_id'],
                              **({'archived_at': task['archived_at'], 'archived_userid': task['archived_userid']} if action == 'archived' else {})}})
    if not task['deleted_at'] and not task['archived_at']:
        deleted_record(connection, 'projectTask', task['id'], allowed, forget=True)
        for userid in allowed:
            payload = task_data(connection, task, userid)
            payload['assist'] = int(payload['owner'] == 0)
            payload['owner'] = int(bool(payload['owner']))
            enqueue_push(connection, userids=[userid], ignore_fds=[context.fd],
                message={'type': 'projectTask', 'action': action, 'data': payload}, scope={'task_id': task['id']})
    enqueue(connection, 'search.sync', {'type': 'task', 'id': task['id']})


def task_data(connection, task, userid):
    result = dict(task)
    tu, tt, tasks = table('project_task_users'), table('project_task_tags'), table('project_tasks')
    result['task_user'] = rows(connection, 'project_task_users', tu.c.task_id == task['id'], order=[tu.c.owner.desc(), tu.c.id.desc()])
    result['task_tag'] = rows(connection, 'project_task_tags', tt.c.task_id == task['id'], order=[tt.c.id])
    result['owner'] = next((u['owner'] for u in result['task_user'] if u['userid'] == userid), None)
    result['assist'] = int(any(u['userid'] == userid for u in result['task_user']))
    children = rows(connection, 'project_tasks', tasks.c.parent_id == task['id'], tasks.c.deleted_at.is_(None))
    result['sub_num'] = len(children)
    result['sub_complete'] = sum(bool(t['complete_at']) for t in children)
    result['percent'] = 100 if task['complete_at'] else int(result['sub_complete'] * 100 / len(children)) if children else 0
    result['file_num'] = count(connection, 'project_task_files', table('project_task_files').c.task_id == task['id'])
    result['msg_num'] = count(connection, 'web_socket_dialog_msgs', table('web_socket_dialog_msgs').c.dialog_id == task['dialog_id']) if task['dialog_id'] else 0
    result['today'] = bool(task['end_at'] and task['end_at'].date() == utcnow().date())
    result['overdue'] = bool(task['end_at'] and task['end_at'] < utcnow())
    project = by_id(connection, 'projects', task['project_id'])
    column = by_id(connection, 'project_columns', task['column_id'])
    result.update(project_name=project['name'] if project else '', column_name=column['name'] if column else '',
                  project_deleted=int(not project or bool(project['deleted_at'])), project_archived=int(bool(project and project['archived_at'])),
                  department_readonly=userid not in member_map(connection, task['project_id']))
    visible = table('project_task_visibility_users')
    result['visibility_appointor'] = [0] if task['visibility'] == 1 else list(connection.execute(select(visible.c.userid).where(visible.c.task_id == root_task(connection, task)['id'])).scalars())
    return result
