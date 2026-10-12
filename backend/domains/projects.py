"""Project, membership and column endpoints. Every callable returns raw envelope data."""
import base64
import secrets
from sqlalchemy import select, or_
from .. import db
from ..auth import require_admin
from ..context import utcnow
from ..errors import ApiError
from ..jobs import enqueue, enqueue_push
from .project_core import (actor, table, row, rows, by_id, update as update_row, count, page,
    ids, integer, decode, encode, date, time_range, member_map, department_projects, require_project,
    require_task, can_view_task, task_allowed_users, require_action, permission_record,
    deleted_record, deleted_ids, log, push_project, observe_task, task_data)
from .dialog_core import create_group, sync_members


def project_data(connection, project, userid, *, statistics=True, members=False, columns=False):
    result = dict(project)
    mapping = member_map(connection, project['id'])
    t = table('project_users')
    member = row(connection, 'project_users', t.c.project_id == project['id'], t.c.userid == userid)
    result.update(owner=mapping.get(userid, 0), top_at=member['top_at'] if member else None,
        sort=member['sort'] if member else None, owner_userid=next((u for u, r in mapping.items() if r == 1), 0),
        deputy_userids=[u for u, r in mapping.items() if r == 2], department_readonly=userid not in mapping)
    if statistics:
        tasks, users = table('project_tasks'), table('project_task_users')
        active = [tasks.c.project_id == project['id'], tasks.c.deleted_at.is_(None), tasks.c.archived_at.is_(None)]
        own = select(users.c.task_id).where(users.c.userid == userid, users.c.owner == 1)
        for prefix, conditions in (('task', active), ('task_my', active + [tasks.c.id.in_(own)])):
            total = count(connection, 'project_tasks', *conditions)
            complete = count(connection, 'project_tasks', *conditions, tasks.c.complete_at.is_not(None))
            result.update({prefix + '_num': total, prefix + '_complete': complete, prefix + '_percent': int(complete * 100 / total) if total else 0})
    if members:
        result['project_user'] = rows(connection, 'project_users', t.c.project_id == project['id'])
        result['userid_list'] = list(mapping)
    if columns:
        c = table('project_columns')
        result['project_column'] = rows(connection, 'project_columns', c.c.project_id == project['id'], c.c.deleted_at.is_(None), order=[c.c.sort, c.c.id])
    return result


def lists(connection, context, data, settings):
    user = actor(connection, context)
    p, pu = table('projects'), table('project_users')
    query = select(p).where(p.c.deleted_at.is_(None))
    if data.get('all'):
        require_admin(user)
    else:
        own = list(connection.execute(select(pu.c.project_id).where(pu.c.userid == user.userid)).scalars())
        selected = data.get('department_owner_ids', data.get('department_ids'))
        visible = department_projects(connection, user.userid, selected) if selected else []
        query = query.where(p.c.id.in_(set(own + visible)))
    if data.get('type') in ('personal', 'team'):
        query = query.where(p.c.personal == int(data['type'] == 'personal'))
    archived = data.get('archived', 'no')
    if archived != 'all':
        query = query.where(p.c.archived_at.is_not(None) if archived == 'yes' else p.c.archived_at.is_(None))
    from sqlalchemy import func
    total_all = connection.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    keys = decode(data.get('keys'), {})
    if keys.get('name'):
        query = query.where(p.c.name.like('%' + str(keys['name']) + '%'))
    timerange = time_range(data.get('timerange'))
    if isinstance(timerange, dict) and timerange.get('updated'):
        query = query.where(p.c.updated_at > date(timerange['updated']))
    query = query.outerjoin(pu, (pu.c.project_id == p.c.id) & (pu.c.userid == user.userid)).order_by(pu.c.top_at.desc(), pu.c.sort, p.c.id.desc())
    result = page(connection, query, data)
    result['data'] = [project_data(connection, p, user.userid, statistics=data.get('getstatistics', 'yes') == 'yes',
        members=data.get('getuserid') == 'yes', columns=data.get('getcolumn') == 'yes') for p in result['data']]
    result['total_all'] = total_all
    if result['current_page'] == 1:
        result['deleted_id'] = deleted_ids(connection, 'project', user.userid, timerange.get('deleted') if isinstance(timerange, dict) else None)
    return result


def one(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'))
    return project_data(connection, p, context.actor.userid, members=True)


def _project_name(data):
    name, desc = str(data.get('name', '')).strip(), str(data.get('desc', '')).strip()
    if not 2 <= len(name) <= 32:
        raise ApiError('项目名称长度应为2至32个字')
    if len(desc) > 255:
        raise ApiError('项目介绍最多只能设置255个字')
    return name, desc


def add(connection, context, data, settings):
    user = actor(connection, context)
    name, desc = _project_name(data)
    personal = integer(data.get('personal')) == 1
    if personal:
        if count(connection, 'projects', table('projects').c.userid == user.userid, table('projects').c.personal == 1):
            raise ApiError('个人项目已存在，无须重复创建')
    else:
        modes = db.setting(connection, 'system', 'project_add_permission', ['all'])
        if 'all' not in modes and 'admin' not in user.identities:
            from .accounts import managed_departments
            if not ('departmentOwner' in modes and managed_departments(connection, user.userid)) and not (
                'appoint' in modes and user.userid in ids(db.setting(connection, 'system', 'project_add_userids', []))):
                raise ApiError('当前仅指定人员可以创建项目')
    columns = [c.strip() for c in str(data.get('columns', '')).split(',') if c.strip()] or ['Default']
    if len(columns) > 30 or any(len(c) > 100 for c in columns):
        raise ApiError('项目列表最多不能超过30个，名称最多100个字')
    pid = db.insert_row(connection, 'projects', name=name, desc=desc, userid=user.userid, personal=int(personal))
    db.insert_row(connection, 'project_users', project_id=pid, userid=user.userid, owner=1)
    for index, name_column in enumerate(columns):
        db.insert_row(connection, 'project_columns', project_id=pid, name=name_column, sort=index)
    dialog_id = create_group(connection, owner_id=user.userid, userids=[user.userid], name=name, group_type='project', link_id=pid)
    update_row(connection, 'projects', pid, {'dialog_id': dialog_id, 'user_simple': f'1|{user.userid}'})
    if data.get('flow') == 'open':
        from .project_configuration import save_flow
        values = [('待处理', 'start'), ('进行中', 'progress'), ('待测试', 'test'), ('已完成', 'end'), ('已取消', 'end')]
        save_flow(connection, context, pid, [{'id': -10-i, 'name': n, 'status': s, 'turns': [-10-j for j in range(5)],
            'userids': [], 'usertype': 'add', 'sort': i, 'color': '#999999' if i == 4 else ''} for i, (n, s) in enumerate(values)])
    log(connection, context, pid, '创建项目')
    result = project_data(connection, by_id(connection, 'projects', pid), user.userid)
    push_project(connection, context, pid, 'add', result)
    enqueue(connection, 'search.sync', {'type': 'project', 'id': pid})
    return result


def update(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    name, desc = _project_name(data)
    values = {'name': name, 'desc': desc, 'archive_method': str(data.get('archive_method', 'system'))}
    if values['archive_method'] == 'custom':
        days = integer(data.get('archive_days'))
        if not 1 <= days <= 365:
            raise ApiError('自动归档天数设置错误，范围：1-365')
        values['archive_days'] = days
    for key in ('ai_auto_analyze', 'task_template_share', 'department_owner_view'):
        if data.get(key) in ('open', 'close'):
            values[key] = data[key]
    for key, value in values.items():
        if p.get(key) != value:
            log(connection, context, p['id'], '修改项目' + key, record={'change': [p.get(key), value]})
    update_row(connection, 'projects', p['id'], values)
    if p['dialog_id']:
        update_row(connection, 'web_socket_dialogs', p['dialog_id'], {'name': name})
    result = project_data(connection, by_id(connection, 'projects', p['id']), context.actor.userid)
    push_project(connection, context, p['id'], 'update', result)
    enqueue(connection, 'search.sync', {'type': 'project', 'id': p['id']})
    return result


def sync_project_dialog(connection, project_id):
    p = by_id(connection, 'projects', project_id)
    mapping = member_map(connection, project_id)
    owner_id = next((u for u, role in mapping.items() if role == 1), None)
    if owner_id is None:
        raise ApiError('项目负责人数据异常，请先修复项目负责人')
    if p['dialog_id']:
        sync_members(connection, p['dialog_id'], list(mapping), owner_id=owner_id)
        du = table('web_socket_dialog_users')
        for uid, role in mapping.items():
            connection.execute(du.update().where(du.c.dialog_id == p['dialog_id'], du.c.userid == uid).values(role=role, important=1))
    update_row(connection, 'projects', project_id, {'user_simple': str(len(mapping)) + '|' + ','.join(str(v) for v in list(mapping)[:6])})


def sync_project_members(connection, context, project_id, member_ids, deputy_ids=None):
    project = require_project(connection, context, project_id, write=True, owner=True)
    current = member_map(connection, project['id'])
    wanted = ids(member_ids)
    primaries = [u for u, role in current.items() if role == 1]
    if len(primaries) != 1:
        raise ApiError('项目负责人数据异常，请先修复项目负责人')
    if primaries[0] not in wanted:
        raise ApiError('项目成员列表必须包含项目负责人')
    if len(wanted) > 100:
        raise ApiError('项目人数最多100个')
    deputies = [u for u, role in current.items() if role == 2]
    if project['owner'] != 1 and set(deputies) - set(wanted):
        raise ApiError('项目管理员不能移除项目负责人或项目管理员')
    if project['owner'] == 1 and deputy_ids is not None:
        deputies = ids(deputy_ids)
        if set(deputies) - set(wanted) or primaries[0] in deputies:
            raise ApiError('项目管理员必须是非负责人成员')
    u = table('users')
    valid = set(connection.execute(select(u.c.userid).where(u.c.userid.in_(wanted), u.c.disable_at.is_(None))).scalars())
    if valid != set(wanted):
        raise ApiError('成员不存在或已停用')
    tasks = rows(connection, 'project_tasks', table('project_tasks').c.project_id == project_id, table('project_tasks').c.deleted_at.is_(None))
    before = {t['id']: task_allowed_users(connection, t) for t in tasks}
    pu = table('project_users')
    removed = set(current) - set(wanted)
    connection.execute(pu.delete().where(pu.c.project_id == project_id, pu.c.userid.in_(removed)))
    remove_task_memberships(connection, context, project_id, removed)
    for uid in wanted:
        role = 1 if uid == primaries[0] else 2 if uid in deputies else 0
        existing = row(connection, 'project_users', pu.c.project_id == project_id, pu.c.userid == uid)
        if existing:
            update_row(connection, 'project_users', existing['id'], {'owner': role})
        else:
            db.insert_row(connection, 'project_users', project_id=project_id, userid=uid, owner=role)
    deleted_record(connection, 'project', project_id, removed)
    deleted_record(connection, 'project', project_id, wanted, forget=True)
    sync_project_dialog(connection, project_id)
    log(connection, context, project_id, '修改项目成员', record={'userid': wanted})
    if removed:
        push_project(connection, context, project_id, 'delete', recipients=removed)
    push_project(connection, context, project_id, 'detail', project_data(connection, by_id(connection, 'projects', project_id), context.actor.userid, members=True))
    for task in tasks:
        observe_task(connection, context, task, 'update', before[task['id']])
    enqueue(connection, 'search.sync', {'type': 'project', 'id': project_id})
    return {'id': project_id}


def user(connection, context, data, settings):
    return sync_project_members(connection, context, integer(data.get('project_id')), data.get('userid'), data.get('deputy_userid'))


def transfer_project(connection, context, project_id, new_owner_id):
    p = require_project(connection, context, project_id, write=True, owner='primary')
    u = table('users')
    if not row(connection, 'users', u.c.userid == new_owner_id, u.c.disable_at.is_(None)):
        raise ApiError('成员不存在')
    pu = table('project_users')
    connection.execute(pu.update().where(pu.c.project_id == p['id'], pu.c.owner == 1).values(owner=0, updated_at=utcnow()))
    existing = row(connection, 'project_users', pu.c.project_id == p['id'], pu.c.userid == new_owner_id)
    if existing:
        update_row(connection, 'project_users', existing['id'], {'owner': 1})
    else:
        db.insert_row(connection, 'project_users', project_id=p['id'], userid=new_owner_id, owner=1)
    sync_project_dialog(connection, p['id'])
    log(connection, context, p['id'], '移交项目给', record={'userid': new_owner_id})
    push_project(connection, context, p['id'], 'detail', {'owner': 1, 'owner_userid': new_owner_id})
    for task in rows(connection, 'project_tasks', table('project_tasks').c.project_id == p['id'], table('project_tasks').c.deleted_at.is_(None)):
        observe_task(connection, context, task, 'update')
    return {'id': p['id']}


def transfer(connection, context, data, settings):
    return transfer_project(connection, context, integer(data.get('project_id')), integer(data.get('owner_userid')))


def _deputy(connection, context, data, role):
    p = require_project(connection, context, data.get('project_id'), write=True, owner='primary')
    pu = table('project_users')
    member = row(connection, 'project_users', pu.c.project_id == p['id'], pu.c.userid == integer(data.get('userid')))
    if not member:
        if role == 2:
            raise ApiError('该用户不是项目成员')
        return []
    if member['owner'] == 1:
        raise ApiError('不能将负责人任命为项目管理员')
    if member['owner'] != role:
        update_row(connection, 'project_users', member['id'], {'owner': role})
        sync_project_dialog(connection, p['id'])
        log(connection, context, p['id'], '任命项目管理员' if role else '罢免项目管理员', record={'userid': member['userid']})
        push_project(connection, context, p['id'], 'detail', {'deputy_userids': [u for u, r in member_map(connection, p['id']).items() if r == 2]})
        for task in rows(connection, 'project_tasks', table('project_tasks').c.project_id == p['id'], table('project_tasks').c.deleted_at.is_(None)):
            observe_task(connection, context, task, 'update', [member['userid']])
    return []


def adddeputy(connection, context, data, settings):
    return _deputy(connection, context, data, 2)


def deldeputy(connection, context, data, settings):
    return _deputy(connection, context, data, 0)


def invite(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    if db.setting(connection, 'system', 'project_invite') == 'close':
        raise ApiError('未开放此功能')
    t = table('project_invites')
    existing = row(connection, 'project_invites', t.c.project_id == p['id'])
    if not existing or data.get('refresh') == 'yes':
        code = base64.urlsafe_b64encode(f'{p["id"]},{context.actor.userid},{secrets.token_urlsafe(24)}'.encode()).decode()
        if existing:
            update_row(connection, 'project_invites', existing['id'], {'code': code})
        else:
            db.insert_row(connection, 'project_invites', project_id=p['id'], code=code)
    else:
        code = existing['code']
    return {'url': context.base_url.rstrip('/') + '/manage/project/invite/' + code, 'code': code}


def invite_info(connection, context, data, settings):
    uid = actor(connection, context).userid
    if db.setting(connection, 'system', 'project_invite') == 'close':
        raise ApiError('未开放此功能')
    invite_row = row(connection, 'project_invites', table('project_invites').c.code == str(data.get('code', '')))
    if not invite_row:
        raise ApiError('邀请code不存在')
    p = by_id(connection, 'projects', invite_row['project_id'])
    if not p or p['deleted_at'] or p['archived_at']:
        raise ApiError('项目不存在或已归档')
    return {**invite_row, 'project': p, 'already': uid in member_map(connection, p['id'])}


def invite_join(connection, context, data, settings):
    result = invite_info(connection, context, data, settings)
    if not result['already']:
        pid = result['project_id']
        by_id(connection, 'projects', pid, lock=True)
        if len(member_map(connection, pid)) >= 100:
            raise ApiError('项目人数最多100个')
        db.insert_row(connection, 'project_users', project_id=pid, userid=context.actor.userid)
        update_row(connection, 'project_invites', result['id'], {'num': result['num'] + 1})
        sync_project_dialog(connection, pid)
        deleted_record(connection, 'project', pid, [context.actor.userid], forget=True)
        log(connection, context, pid, '通过邀请链接加入项目')
        push_project(connection, context, pid, 'add', result['project'], recipients=[context.actor.userid])
        enqueue(connection, 'search.sync', {'type': 'project', 'id': pid})
        for task in rows(connection, 'project_tasks', table('project_tasks').c.project_id == pid, table('project_tasks').c.deleted_at.is_(None)):
            observe_task(connection, context, task, 'update')
    return invite_info(connection, context, data, settings)


def exit(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=False)
    uid = context.actor.userid
    pu = table('project_users')
    remove_task_memberships(connection, context, p['id'], [uid])
    connection.execute(pu.delete().where(pu.c.project_id == p['id'], pu.c.userid == uid))
    sync_project_dialog(connection, p['id'])
    deleted_record(connection, 'project', p['id'], [uid])
    push_project(connection, context, p['id'], 'delete', recipients=[uid])
    log(connection, context, p['id'], '退出项目')
    enqueue(connection, 'search.sync', {'type': 'project', 'id': p['id']})
    for task in rows(connection, 'project_tasks', table('project_tasks').c.project_id == p['id'], table('project_tasks').c.deleted_at.is_(None)):
        observe_task(connection, context, task, 'update', [uid])
    return {'id': p['id']}


def archived(connection, context, data, settings):
    recover = data.get('type') == 'recovery'
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True, archived=not recover)
    when = None if recover else utcnow()
    update_row(connection, 'projects', p['id'], {'archived_at': when, 'archived_userid': context.actor.userid})
    t = table('project_tasks')
    tasks = rows(connection, 'project_tasks', t.c.project_id == p['id'], t.c.deleted_at.is_(None), t.c.archived_follow == 1 if recover else t.c.archived_at.is_(None))
    from .project_core import handoff_record, task_snapshot
    for task in tasks:
        before = task_snapshot(connection, task)
        update_row(connection, 'project_tasks', task['id'], {'archived_at': when, 'archived_follow': 0 if recover else 1})
        task = by_id(connection, 'project_tasks', task['id'])
        handoff_record(connection, context, task, before, 'archive')
        observe_task(connection, context, task, 'recovery' if recover else 'archived')
    deleted_record(connection, 'project', p['id'], member_map(connection, p['id']), forget=recover)
    log(connection, context, p['id'], '项目取消归档' if recover else '项目归档')
    push_project(connection, context, p['id'], 'recovery' if recover else 'archived', by_id(connection, 'projects', p['id']))
    enqueue(connection, 'search.sync', {'type': 'project', 'id': p['id']})
    return {'id': p['id']}


def remove(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner='primary', archived=None)
    from .project_tasks import delete_task
    t = table('project_tasks')
    for task in rows(connection, 'project_tasks', t.c.project_id == p['id'], t.c.parent_id == 0, t.c.deleted_at.is_(None), t.c.archived_at.is_(None)):
        delete_task(connection, context, task)
    c = table('project_columns')
    connection.execute(c.update().where(c.c.project_id == p['id'], c.c.deleted_at.is_(None)).values(deleted_at=utcnow(), updated_at=utcnow()))
    if p['dialog_id']:
        update_row(connection, 'web_socket_dialogs', p['dialog_id'], {'deleted_at': utcnow()})
    update_row(connection, 'projects', p['id'], {'deleted_at': utcnow()})
    deleted_record(connection, 'project', p['id'], member_map(connection, p['id']))
    push_project(connection, context, p['id'], 'delete')
    log(connection, context, p['id'], '删除项目')
    enqueue(connection, 'search.sync', {'type': 'project', 'id': p['id']})
    return {'id': p['id']}


def column_lists(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'))
    c = table('project_columns')
    return page(connection, select(c).where(c.c.project_id == p['id'], c.c.deleted_at.is_(None)).order_by(c.c.sort, c.c.id), data, 200, 100)


def push_column(connection, context, column, action):
    recipients = list(member_map(connection, column['project_id']))
    if recipients:
        enqueue_push(connection, userids=recipients, ignore_fds=[context.fd], message={
            'type': 'projectColumn', 'action': action, 'data': column}, scope={'project_id': column['project_id']})


def create_column(connection, context, project, name):
    name = str(name).strip()
    if not name or len(name) > 100:
        raise ApiError('列表名称不能为空且最多100个字')
    require_action(connection, context, project, 'task_list_add')
    c = table('project_columns')
    existing = rows(connection, 'project_columns', c.c.project_id == project['id'], c.c.deleted_at.is_(None))
    if len(existing) >= 50:
        raise ApiError('项目列表最多不能超过50个')
    identifier = db.insert_row(connection, 'project_columns', project_id=project['id'], name=name, sort=max([r['sort'] for r in existing] or [0]) + 1)
    log(connection, context, project['id'], '创建列表：' + name, column_id=identifier)
    column = by_id(connection, 'project_columns', identifier)
    column['project_task'] = []
    push_column(connection, context, column, 'add')
    return column


def column_add(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True)
    return create_column(connection, context, p, data.get('name', ''))


def column_one(connection, context, data, settings):
    column = by_id(connection, 'project_columns', data.get('column_id'))
    deleted = data.get('deleted', 'no')
    if not column or (deleted == 'no' and column['deleted_at']) or (deleted == 'yes' and not column['deleted_at']):
        raise ApiError('列表不存在')
    require_project(connection, context, column['project_id'])
    return column


def column_update(connection, context, data, settings):
    c = column_one(connection, context, data, settings)
    p = require_project(connection, context, c['project_id'], write=True)
    require_action(connection, context, p, 'task_list_update')
    values = {k: str(data[k]) for k in ('name', 'color') if k in data}
    if 'name' in values and not 1 <= len(values['name'].strip()) <= 100:
        raise ApiError('列表名称不能为空且最多100个字')
    if len(values.get('color', '')) > 20:
        raise ApiError('颜色设置错误')
    update_row(connection, 'project_columns', c['id'], values)
    log(connection, context, p['id'], '修改列表', column_id=c['id'], record={'change': [c, values]})
    result = by_id(connection, 'project_columns', c['id'])
    push_column(connection, context, result, 'update')
    return result


def column_remove(connection, context, data, settings):
    c = column_one(connection, context, data, settings)
    p = require_project(connection, context, c['project_id'], write=True)
    require_action(connection, context, p, 'task_list_remove')
    from .project_tasks import delete_task
    t = table('project_tasks')
    for task in rows(connection, 'project_tasks', t.c.column_id == c['id'], t.c.deleted_at.is_(None), t.c.archived_at.is_(None), t.c.parent_id == 0):
        delete_task(connection, context, task)
    update_row(connection, 'project_columns', c['id'], {'deleted_at': utcnow()})
    log(connection, context, p['id'], '删除列表：' + c['name'], column_id=c['id'])
    push_column(connection, context, {'id': c['id'], 'project_id': p['id']}, 'delete')
    return {'id': c['id']}


def sort(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True)
    items = decode(data.get('sort'), [])
    if not isinstance(items, list):
        raise ApiError('参数错误')
    columns = table('project_columns')
    valid = {r['id'] for r in rows(connection, 'project_columns', columns.c.project_id == p['id'], columns.c.deleted_at.is_(None))}
    if integer(data.get('only_column')):
        require_action(connection, context, p, 'task_list_sort')
        for index, item in enumerate(items):
            cid = integer(item.get('id'))
            if cid not in valid:
                raise ApiError('列表不存在')
            update_row(connection, 'project_columns', cid, {'sort': index})
    else:
        from .project_tasks import descendants
        for item in items:
            cid = integer(item.get('id'))
            if cid not in valid:
                raise ApiError('列表不存在')
            for index, tid in enumerate(ids(item.get('task'))):
                task = require_task(connection, context, tid, write=True)
                if task['project_id'] != p['id']:
                    raise ApiError('任务不在项目内')
                if task['column_id'] != cid:
                    require_action(connection, context, p, 'task_move', task)
                if not task['complete_at']:
                    update_row(connection, 'project_tasks', tid, {'column_id': cid, 'sort': index})
                    for child in descendants(connection, tid):
                        update_row(connection, 'project_tasks', child['id'], {'column_id': cid})
                    observe_task(connection, context, by_id(connection, 'project_tasks', tid), 'update')
    log(connection, context, p['id'], '调整列表排序' if integer(data.get('only_column')) else '调整任务排序')
    push_project(connection, context, p['id'], 'sort')
    return []


def user_sort(connection, context, data, settings):
    uid = actor(connection, context).userid
    pu = table('project_users')
    for index, pid in enumerate(ids(data.get('list'))):
        connection.execute(pu.update().where(pu.c.userid == uid, pu.c.project_id == pid).values(sort=index))
    return []


def top(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, archived=None)
    pu = table('project_users')
    member = row(connection, 'project_users', pu.c.project_id == p['id'], pu.c.userid == context.actor.userid)
    when = None if member['top_at'] else utcnow()
    update_row(connection, 'project_users', member['id'], {'top_at': when})
    update_row(connection, 'projects', p['id'], {})
    return {'id': p['id'], 'top_at': when}


def permission(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, archived=None)
    return permission_record(connection, p['id'])


def permission_update(connection, context, data, settings):
    # The source only checked membership, allowing members to grant themselves rights.
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    record = permission_record(connection, p['id'])
    for key in record['permissions']:
        if key in data:
            values = ids(data[key])
            if set(values) - {1, 2, 3, 4} or not values:
                raise ApiError('项目权限设置错误')
            record['permissions'][key] = values
    update_row(connection, 'project_permissions', record['id'], {'permissions': encode(record['permissions'])})
    log(connection, context, p['id'], '修改项目权限')
    return permission_record(connection, p['id'])


def remove_task_memberships(connection, context, project_id, userids):
    from .project_core import task_snapshot, handoff_record
    from .project_tasks import sync_task_dialog
    tu = table('project_task_users')
    task_ids = list(connection.execute(select(tu.c.task_id).where(tu.c.project_id == project_id, tu.c.userid.in_(list(userids))).distinct()).scalars())
    for tid in task_ids:
        task = by_id(connection, 'project_tasks', tid, lock=True)
        before = task_snapshot(connection, task) if task else None
        previous = task_allowed_users(connection, task) if task else []
        connection.execute(tu.delete().where(tu.c.task_id == tid, tu.c.userid.in_(list(userids))))
        if task:
            handoff_record(connection, context, task, before, 'member_exit')
            sync_task_dialog(connection, task)
            observe_task(connection, context, task, 'update', previous)


def transfer_user_memberships(connection, context, original_userid, new_userid):
    from .project_core import task_snapshot, handoff_record
    from .project_tasks import sync_task_dialog
    require_admin(actor(connection, context))
    users = table('users')
    target = row(connection, 'users', users.c.userid == new_userid, users.c.disable_at.is_(None))
    if not target:
        raise ApiError('接收成员不存在或已停用')
    original = row(connection, 'users', users.c.userid == original_userid)
    pu = table('project_users')
    memberships = rows(connection, 'project_users', pu.c.userid == original_userid)
    project_ids = []
    for membership in memberships:
        pid = membership['project_id']
        project = by_id(connection, 'projects', pid, lock=True)
        existing = row(connection, 'project_users', pu.c.project_id == pid, pu.c.userid == new_userid)
        if existing:
            if membership['owner'] == 1:
                update_row(connection, 'project_users', existing['id'], {'owner': 1})
            connection.execute(pu.delete().where(pu.c.id == membership['id']))
        else:
            update_row(connection, 'project_users', membership['id'], {'userid': new_userid, 'owner': 1 if membership['owner'] == 1 else 0})
        if project and not project['deleted_at']:
            if project['personal']:
                update_row(connection, 'projects', pid, {'name': ('【' + str((original or {}).get('nickname') or original_userid) + '】' + project['name'])[:255]})
            log(connection, context, pid, '移交项目身份', record={'change': [{'type': 'user', 'data': original_userid}, {'type': 'user', 'data': new_userid}]})
            sync_project_dialog(connection, pid)
            deleted_record(connection, 'project', pid, [original_userid])
            deleted_record(connection, 'project', pid, [new_userid], forget=True)
            push_project(connection, context, pid, 'delete', recipients=[original_userid])
            push_project(connection, context, pid, 'detail', {'owner': 1})
            project_ids.append(pid)
            enqueue(connection, 'search.sync', {'type': 'project', 'id': pid})
    f = table('project_flow_items')
    for flow in rows(connection, 'project_flow_items', f.c.project_id.in_(project_ids)):
        owners = ids(decode(flow['userids']))
        if original_userid in owners:
            update_row(connection, 'project_flow_items', flow['id'], {'userids': encode(list(dict.fromkeys([u for u in owners if u != original_userid] + [new_userid])))})
    tu = table('project_task_users')
    for membership in rows(connection, 'project_task_users', tu.c.userid == original_userid):
        task = by_id(connection, 'project_tasks', membership['task_id'], lock=True)
        before = task_snapshot(connection, task) if task else None
        previous = task_allowed_users(connection, task) if task else []
        existing = row(connection, 'project_task_users', tu.c.task_id == membership['task_id'], tu.c.userid == new_userid)
        if existing:
            update_row(connection, 'project_task_users', existing['id'], {'owner': max(existing['owner'], membership['owner'])})
            connection.execute(tu.delete().where(tu.c.id == membership['id']))
        else:
            update_row(connection, 'project_task_users', membership['id'], {'userid': new_userid})
        if task:
            handoff_record(connection, context, task, before, 'transfer')
            log(connection, context, task['project_id'], '移交{任务}身份', task=task, task_only=1,
                record={'change': [{'type': 'user', 'data': original_userid}, {'type': 'user', 'data': new_userid}]})
            sync_task_dialog(connection, task)
            observe_task(connection, context, task, 'update', previous)
    for pid in project_ids:
        for task in rows(connection, 'project_tasks', table('project_tasks').c.project_id == pid, table('project_tasks').c.deleted_at.is_(None)):
            observe_task(connection, context, task, 'update', [original_userid])
    return project_ids


ROUTES = {
    'project/lists': lists, 'project/one': one, 'project/add': add, 'project/update': update,
    'project/user': user, 'project/invite': invite, 'project/invite/info': invite_info,
    'project/invite/join': invite_join, 'project/transfer': transfer, 'project/adddeputy': adddeputy,
    'project/deldeputy': deldeputy, 'project/sort': sort, 'project/user/sort': user_sort,
    'project/exit': exit, 'project/archived': archived, 'project/remove': remove,
    'project/column/lists': column_lists, 'project/column/add': column_add,
    'project/column/update': column_update, 'project/column/remove': column_remove,
    'project/column/one': column_one, 'project/top': top, 'project/permission': permission,
    'project/permission/update': permission_update,
}

# Explicit installation list; no filesystem discovery or attribute-based route exposure.
from . import project_tasks, project_queries, project_configuration, project_jobs, project_ai
from . import dashboard, complaints, checkin
from .project_core import can_view_project

ROUTES.update(project_tasks.ROUTES)
ROUTES.update(project_queries.ROUTES)
ROUTES.update(project_configuration.ROUTES)
ROUTES.update(project_jobs.ROUTES)
ROUTES.update(dashboard.ROUTES)
ROUTES.update(complaints.ROUTES)
ROUTES.update(checkin.ROUTES)
RESPONSE_KINDS = {key: 'data' for key in ROUTES}
RESPONSE_KINDS.update(project_queries.RESPONSE_KINDS)
RESPONSE_KINDS.update(project_jobs.RESPONSE_KINDS)
RESPONSE_KINDS.update(checkin.RESPONSE_KINDS)
RESPONSE_KINDS.update({'project/task/ai_generate': 'Response', 'project/ai/generate': 'Response'})
PUBLIC_ROUTES = checkin.PUBLIC_ROUTES | project_jobs.PUBLIC_ROUTES | {'project/task/ai_generate', 'project/ai/generate'}
POST_ONLY_ROUTES = {'projecttaskhandoff/assign'}
HANDLER_SIGNATURE = '(connection, context, data, settings)'
TRANSACTION_CONTRACT = 'Use the caller SQLAlchemy Connection transaction; data responses require success(data); Response instances are returned unchanged.'


def register_jobs():
    project_jobs.register_jobs()
    project_ai.register_jobs()
    complaints.register_jobs()
    checkin.register_jobs()
