"""Department-management dashboard, matching DashboardTeam's owner-only aggregates."""
from datetime import timedelta
from sqlalchemy import select, func, or_, and_, case
from .. import db
from ..context import utcnow
from ..errors import ApiError
from .project_core import actor, table, rows, ids, integer, decode, page


def team_context(connection, context, data):
    from .accounts import managed_departments, department_users
    uid = actor(connection, context).userid
    if db.setting(connection, 'system', 'department_owner_project_view', 'open') != 'open':
        raise ApiError('未开启部门负责人视角功能')
    managed = managed_departments(connection, uid)
    managed_ids = [integer(v['id']) if isinstance(v, dict) else integer(v) for v in managed]
    selected = data.get('department_owner_ids')
    chosen = managed_ids if selected in (None, '', 'all') or selected == [] else [v for v in ids(selected) if v in managed_ids]
    if not chosen:
        raise ApiError('没有可查看的部门数据')
    people = department_users(connection, chosen, recursive=True)
    userids = [integer(v['userid']) if isinstance(v, dict) else integer(v) for v in people]
    u = table('users')
    members = [dict(v) for v in connection.execute(select(u.c.userid, u.c.nickname, u.c.userimg)
        .where(u.c.userid.in_(userids), u.c.disable_at.is_(None), u.c.bot == 0).order_by(u.c.userid)).mappings()]
    userids = [v['userid'] for v in members]
    p, pu = table('projects'), table('project_users')
    projects = list(connection.execute(select(p.c.id).join(pu, pu.c.project_id == p.c.id).where(
        pu.c.userid.in_(userids), p.c.archived_at.is_(None), p.c.deleted_at.is_(None),
        or_(p.c.department_owner_view != 'close', p.c.department_owner_view.is_(None))).distinct()).scalars())
    own = set(connection.execute(select(pu.c.project_id).where(pu.c.userid == uid, pu.c.project_id.in_(projects))).scalars())
    return {'members': members, 'member_ids': userids, 'project_ids': projects, 'own': own}


def base_query(scope):
    t, tu = table('project_tasks'), table('project_task_users')
    owners = select(tu.c.task_id).where(tu.c.owner == 1)
    managed = select(tu.c.task_id).where(tu.c.owner == 1, tu.c.userid.in_(scope['member_ids']))
    return select(t).where(t.c.project_id.in_(scope['project_ids']), t.c.parent_id == 0, t.c.visibility == 1,
        t.c.archived_at.is_(None), t.c.deleted_at.is_(None), or_(t.c.id.in_(managed), t.c.id.not_in(owners)))


def priority_list(connection):
    values = db.setting(connection, 'priority', default=[])
    return [v for v in values if isinstance(v, dict) and integer(v.get('priority')) > 0]


def team_stats(connection, context, data, settings):
    scope = team_context(connection, context, data)
    now = utcnow()
    week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    soon = (now + timedelta(days=3)).replace(hour=23, minute=59, second=59)
    t = table('project_tasks')
    tasks = [dict(v) for v in connection.execute(base_query(scope)).mappings()]
    pending = [v for v in tasks if not v['complete_at']]
    task_ids = [v['id'] for v in tasks]
    tu = table('project_task_users')
    owners = rows(connection, 'project_task_users', tu.c.task_id.in_(task_ids), tu.c.owner == 1)
    owned_ids = {v['task_id'] for v in owners}
    blocks = {'uncompleted': len(pending), 'overdue': sum(bool(v['end_at'] and v['end_at'] < now) for v in pending),
        'due_soon': sum(bool(v['end_at'] and now <= v['end_at'] <= soon) for v in pending),
        'week_completed': sum(bool(v['complete_at'] and week <= v['complete_at'] < week + timedelta(days=7)) for v in tasks),
        'last_week_completed': sum(bool(v['complete_at'] and week - timedelta(days=7) <= v['complete_at'] < week) for v in tasks),
        'no_owner': sum(v['id'] not in owned_ids for v in pending)}
    f = table('project_flow_items')
    flows = {v['id']: v['status'] for v in rows(connection, 'project_flow_items', f.c.project_id.in_(scope['project_ids']))}
    pending_map = {v['id']: v for v in pending}
    member_tasks = {uid: set() for uid in scope['member_ids']}
    for owner in owners:
        if owner['userid'] in member_tasks and owner['task_id'] in pending_map:
            member_tasks[owner['userid']].add(owner['task_id'])
    members = []
    for member in scope['members']:
        assigned = [pending_map[tid] for tid in member_tasks[member['userid']]]
        segments = {'progress': 0, 'start': 0, 'test': 0}
        for task in assigned:
            status = flows.get(task['flow_item_id'])
            segments[status if status in ('progress', 'test') else 'start'] += 1
        members.append({**member, 'total': len(assigned), 'overdue': sum(bool(v['end_at'] and v['end_at'] < now) for v in assigned), 'segments': segments})
    members.sort(key=lambda v: (-v['overdue'], -v['total'], v['userid']))
    blocks['overdue_owner_count'] = sum(v['overdue'] > 0 for v in members)
    priorities = priority_list(connection)
    levels = [integer(p['priority']) for p in priorities]
    priority = [{'level': integer(p['priority']), 'name': p['name'], 'color': p['color'],
        'num': sum(v['p_level'] == integer(p['priority']) for v in pending)} for p in priorities]
    unset = sum(v['p_level'] not in levels for v in pending)
    if unset:
        priority.append({'level': -1, 'name': '', 'color': '#c5c8ce', 'num': unset})
    return {'generated_at': now, 'member_count': len(members), 'blocks': blocks, 'priority': priority, 'members': members, 'high_levels': levels[:2]}


def team_tasks(connection, context, data, settings):
    scope = team_context(connection, context, data)
    t, tu, p, f = (table(v) for v in ('project_tasks', 'project_task_users', 'projects', 'project_flow_items'))
    now = utcnow()
    query = base_query(scope).where(t.c.complete_at.is_(None))
    member = integer(data.get('member_id'))
    levels = [integer(v['priority']) for v in priority_list(connection)]
    if member:
        if member not in scope['member_ids']:
            raise ApiError('没有查看该成员的权限')
        query = query.where(t.c.id.in_(select(tu.c.task_id).where(tu.c.userid == member, tu.c.owner == 1)))
    elif data.get('level') is not None:
        level = integer(data['level'])
        query = query.where(t.c.p_level.not_in(levels) if level == -1 else t.c.p_level == level)
    elif data.get('type') == 'overdue':
        query = query.where(t.c.end_at < now)
    elif data.get('type') == 'soon':
        query = query.where(t.c.end_at.between(now, (now + timedelta(days=3)).replace(hour=23, minute=59, second=59)))
    elif data.get('type') == 'hi':
        query = query.where(t.c.p_level.in_(levels[:2]))
    elif data.get('type') == 'noowner':
        query = query.where(t.c.id.not_in(select(tu.c.task_id).where(tu.c.owner == 1)))
    query = query.add_columns(p.c.name.label('project_name'), f.c.status.label('flow_item_status'), f.c.color.label('flow_item_color'))
    query = query.join(p, p.c.id == t.c.project_id).outerjoin(f, f.c.id == t.c.flow_item_id).order_by(t.c.end_at.is_(None), t.c.end_at, t.c.id.desc())
    result = page(connection, query, data, 50, 20)
    ids_page = [v['id'] for v in result['data']]
    u = table('users')
    owner_rows = [dict(v) for v in connection.execute(select(tu.c.task_id, tu.c.userid, u.c.nickname).outerjoin(u, u.c.userid == tu.c.userid)
        .where(tu.c.task_id.in_(ids_page), tu.c.owner == 1)).mappings()]
    for task in result['data']:
        owners = [{'userid': v['userid'], 'nickname': v['nickname'] or ''} for v in owner_rows if v['task_id'] == task['id']]
        owners.sort(key=lambda v: (v['userid'] != member, v['userid'] not in scope['member_ids'], v['userid']))
        parts = (task['flow_item_name'] or '').split('|')
        if len(parts) >= 2:
            task['flow_item_name'] = parts[1]
            task['flow_item_status'] = task['flow_item_status'] or parts[0]
            task['flow_item_color'] = task['flow_item_color'] or (parts[2] if len(parts) > 2 else '')
        task.update(owners=owners, owner=owners[0] if owners else None, department_readonly=task['project_id'] not in scope['own'])
    return result


ROUTES = {'dashboard/team/stats': team_stats, 'dashboard/team/tasks': team_tasks}
