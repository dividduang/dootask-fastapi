"""Project workflow, template, tag, handoff and AI suggestion endpoints."""
import hashlib
import hmac
from sqlalchemy import select, or_
from .. import db
from ..context import utcnow
from ..errors import ApiError
from ..jobs import enqueue_push
from .project_core import (actor, table, row, rows, by_id, update, count, page, ids, integer, decode, encode,
    require_project, require_task, require_action, member_map, department_projects, task_snapshot,
    task_data, task_allowed_users, observe_task, handoff_record, log)


def flow_data(connection, flow):
    f = table('project_flow_items')
    result = dict(flow)
    result['project_flow_item'] = rows(connection, 'project_flow_items', f.c.flow_id == flow['id'], order=[f.c.sort, f.c.id])
    for item in result['project_flow_item']:
        item['turns'], item['userids'] = decode(item['turns']), decode(item['userids'])
    return result


def save_flow(connection, context, project_id, items):
    if not isinstance(items, list) or len(items) > 10:
        raise ApiError('流程状态最多不能超过10个')
    if not any(i.get('status') == 'start' for i in items):
        raise ApiError('至少需要1个开始状态')
    if not any(i.get('status') == 'end' for i in items):
        raise ApiError('至少需要1个结束状态')
    members = member_map(connection, project_id)
    flow = row(connection, 'project_flows', table('project_flows').c.project_id == project_id)
    if not flow:
        fid = db.insert_row(connection, 'project_flows', project_id=project_id, name='Default')
        flow = by_id(connection, 'project_flows', fid)
    f = table('project_flow_items')
    existing = {v['id']: v for v in rows(connection, 'project_flow_items', f.c.flow_id == flow['id'])}
    mapping, saved = {}, []
    for index, item in enumerate(items):
        old_id = integer(item.get('id'))
        name = str(item.get('name', '')).strip().replace('|', '·')
        if not name or len(name) > 50:
            raise ApiError('状态名称填写错误')
        users = ids(item.get('userids'))
        mode = item.get('usertype', 'add')
        if mode not in ('add', 'replace', 'merge'):
            raise ApiError('流转模式填写错误')
        if (mode in ('replace', 'merge') or integer(item.get('userlimit'))) and not users:
            raise ApiError('设置流转模式或限制负责人时必须填写状态负责人')
        if set(users) - set(members):
            raise ApiError('状态负责人不在项目成员内')
        column_id = integer(item.get('columnid'))
        if column_id:
            column = by_id(connection, 'project_columns', column_id)
            if not column or column['project_id'] != project_id or column['deleted_at']:
                raise ApiError('列表不存在')
        values = {'project_id': project_id, 'flow_id': flow['id'], 'name': name, 'status': str(item.get('status', ''))[:20],
            'color': str(item.get('color', ''))[:20], 'sort': integer(item.get('sort'), index), 'turns': '[]',
            'userids': encode(users), 'usertype': mode, 'userlimit': int(bool(integer(item.get('userlimit')))), 'columnid': column_id}
        if old_id in existing:
            identifier = old_id
            update(connection, 'project_flow_items', identifier, values)
        else:
            identifier = db.insert_row(connection, 'project_flow_items', **values)
        mapping[old_id] = identifier
        saved.append((identifier, item))
        t = table('project_tasks')
        for task in rows(connection, 'project_tasks', t.c.flow_item_id == identifier, t.c.deleted_at.is_(None)):
            update(connection, 'project_tasks', task['id'], {'flow_item_name': '|'.join([values['status'], name, values['color']])})
            handoff_record(connection, context, by_id(connection, 'project_tasks', task['id']), task_snapshot(connection, task))
            observe_task(connection, context, by_id(connection, 'project_tasks', task['id']), 'update')
    keep = [identifier for identifier, _ in saved]
    removed = set(existing) - set(keep)
    connection.execute(f.delete().where(f.c.id.in_(removed)))
    tasks = table('project_tasks')
    for task in rows(connection, 'project_tasks', tasks.c.flow_item_id.in_(removed)):
        update(connection, 'project_tasks', task['id'], {'flow_item_id': 0, 'flow_item_name': ''})
        handoff_record(connection, context, by_id(connection, 'project_tasks', task['id']), task_snapshot(connection, task))
        observe_task(connection, context, by_id(connection, 'project_tasks', task['id']), 'update')
    for identifier, item in saved:
        turns = item.get('turns', [])
        if not isinstance(turns, list):
            raise ApiError('流转状态参数错误')
        normalized = sorted(({mapping.get(integer(v), integer(v)) for v in turns} | {identifier}) & set(keep))
        update(connection, 'project_flow_items', identifier, {'turns': encode(normalized)})
    return flow_data(connection, flow)


def flow_list(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'))
    return [flow_data(connection, f) for f in rows(connection, 'project_flows', table('project_flows').c.project_id == p['id'])]


def flow_save(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    return save_flow(connection, context, p['id'], decode(data.get('flows')))


def flow_delete(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    for name in ('project_flow_items', 'project_flows'):
        t = table(name)
        connection.execute(t.delete().where(t.c.project_id == p['id']))
    t = table('project_tasks')
    for task in rows(connection, 'project_tasks', t.c.project_id == p['id'], t.c.flow_item_id != 0):
        update(connection, 'project_tasks', task['id'], {'flow_item_id': 0, 'flow_item_name': ''})
        handoff_record(connection, context, by_id(connection, 'project_tasks', task['id']), task_snapshot(connection, task))
        observe_task(connection, context, by_id(connection, 'project_tasks', task['id']), 'update')
    return []


def task_flow(connection, context, data, settings):
    task = require_task(connection, context, data.get('task_id'), archived=None, deleted=None)
    pid = integer(data.get('project_id')) or task['project_id']
    require_project(connection, context, pid, archived=None)
    f = table('project_flow_items')
    options = rows(connection, 'project_flow_items', f.c.project_id == pid, order=[f.c.sort, f.c.id])
    current = task['flow_item_id'] if any(v['id'] == task['flow_item_id'] for v in options) else 0
    if not current:
        current = next((v['id'] for v in options if v['status'] == ('end' if task['complete_at'] else 'start')), 0)
    turns = [{**{k: v[k] for k in ('id', 'name', 'status', 'color')}, 'turns': decode(v['turns'])} for v in options]
    return {'task_id': task['id'], 'flow_item_id': current, 'turns': turns}


def template_list(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), archived=None)
    t = table('project_task_templates')
    return rows(connection, 'project_task_templates', t.c.project_id == p['id'], order=[t.c.sort, t.c.id.desc()])


def _template_query(connection, context, data):
    uid = actor(connection, context).userid
    t, p, pu = table('project_task_templates'), table('projects'), table('project_users')
    query = select(t, p.c.name.label('project_name')).join(p, p.c.id == t.c.project_id).where(p.c.deleted_at.is_(None),
        t.c.project_id.in_(select(pu.c.project_id).where(pu.c.userid == uid)))
    current = by_id(connection, 'projects', data.get('current_project_id'))
    if current and current['task_template_share'] == 'close':
        query = query.where(t.c.project_id == current['id'])
    return query


def template_visible(connection, context, data, settings):
    t = table('project_task_templates')
    query = _template_query(connection, context, data).order_by((t.c.project_id == integer(data.get('current_project_id'))).desc(), t.c.sort, t.c.id)
    return [dict(v) for v in connection.execute(query).mappings()]


def template_search(connection, context, data, settings):
    t, u = table('project_task_templates'), table('users')
    query = _template_query(connection, context, data).add_columns(u.c.nickname.label('user_name')).outerjoin(u, u.c.userid == t.c.userid)
    keyword = str(data.get('keyword', '')).strip()
    if keyword:
        query = query.where(or_(t.c.name.like('%' + keyword + '%'), t.c.title.like('%' + keyword + '%'), t.c.content.like('%' + keyword + '%')))
    query = query.order_by(t.c.use_count.desc(), t.c.last_used_at.desc(), t.c.created_at.desc())
    result = page(connection, query, data, 50, 20)
    return {'total': result['total'], 'page': result['current_page'], 'page_size': result['per_page'], 'items': result['data']}


def template_save(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    name, title, content = (str(data.get(k, '')).strip() for k in ('name', 'title', 'content'))
    if not name or len(name) > 100:
        raise ApiError('请输入模板名称，最多100个字')
    if not title and not content:
        raise ApiError('请输入任务标题或内容')
    if len(title) > 255:
        raise ApiError('任务标题过长')
    values = {'project_id': p['id'], 'name': name, 'title': title, 'content': content, 'userid': context.actor.userid}
    t = table('project_task_templates')
    identifier = integer(data.get('id'))
    if identifier:
        existing = by_id(connection, 'project_task_templates', identifier)
        if not existing or existing['project_id'] != p['id']:
            raise ApiError('模板不存在或已被删除')
        update(connection, 'project_task_templates', identifier, values)
    else:
        templates = rows(connection, 'project_task_templates', t.c.project_id == p['id'])
        if len(templates) >= 50:
            raise ApiError('每个项目最多添加50个模板')
        identifier = db.insert_row(connection, 'project_task_templates', **values, sort=max([v['sort'] for v in templates] or [-1]) + 1)
    return by_id(connection, 'project_task_templates', identifier)


def _sort_configuration(connection, context, data, name, label):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    t = table(name)
    existing = rows(connection, name, t.c.project_id == p['id'], order=[t.c.sort, t.c.id.desc()])
    available = {v['id'] for v in existing}
    wanted = [v for v in ids(data.get('list')) if v in available]
    wanted += [v['id'] for v in existing if v['id'] not in wanted]
    for index, identifier in enumerate(wanted):
        update(connection, name, identifier, {'sort': index})
    log(connection, context, p['id'], label)
    return []


def template_sort(connection, context, data, settings):
    return _sort_configuration(connection, context, data, 'project_task_templates', '调整模板排序')


def template_delete(connection, context, data, settings):
    template = by_id(connection, 'project_task_templates', data.get('id'))
    if not template:
        raise ApiError('模板不存在或已被删除')
    require_project(connection, context, template['project_id'], write=True, owner=True)
    connection.execute(table('project_task_templates').delete().where(table('project_task_templates').c.id == template['id']))
    return []


def template_default(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True, owner=True)
    template = by_id(connection, 'project_task_templates', data.get('id'))
    if not template or template['project_id'] != p['id']:
        raise ApiError('模板不存在或已被删除')
    t = table('project_task_templates')
    if not template['is_default']:
        connection.execute(t.update().where(t.c.project_id == p['id']).values(is_default=0))
    update(connection, 'project_task_templates', template['id'], {'is_default': int(not template['is_default'])})
    return []


def tag_list(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), archived=None)
    t = table('project_tags')
    return rows(connection, 'project_tags', t.c.project_id == p['id'], order=[t.c.sort, t.c.id.desc()])


def tag_save(connection, context, data, settings):
    p = require_project(connection, context, data.get('project_id'), write=True)
    name, desc, color = (str(data.get(k, '')).strip() for k in ('name', 'desc', 'color'))
    if not name or len(name) > 50 or len(desc) > 255 or not color or len(color) > 10:
        raise ApiError('标签名称、描述或颜色填写错误')
    identifier = integer(data.get('id'))
    t = table('project_tags')
    if row(connection, 'project_tags', t.c.project_id == p['id'], t.c.name == name, t.c.id != identifier):
        raise ApiError('标签已存在')
    if identifier:
        tag = by_id(connection, 'project_tags', identifier)
        if not tag or tag['project_id'] != p['id']:
            raise ApiError('标签不存在或已被删除')
        if not p['owner'] and tag['userid'] != context.actor.userid:
            raise ApiError('没有权限修改标签')
        tt = table('project_task_tags')
        touched = rows(connection, 'project_task_tags', tt.c.project_id == p['id'], tt.c.name == tag['name'])
        connection.execute(tt.update().where(tt.c.project_id == p['id'], tt.c.name == tag['name']).values(name=name, color=color, updated_at=utcnow()))
        for task_id in {v['task_id'] for v in touched}:
            update(connection, 'project_tasks', task_id, {})
            observe_task(connection, context, by_id(connection, 'project_tasks', task_id), 'update')
        update(connection, 'project_tags', identifier, {'name': name, 'desc': desc, 'color': color})
    else:
        existing = rows(connection, 'project_tags', t.c.project_id == p['id'])
        if len(existing) >= 100:
            raise ApiError('每个项目最多添加100个标签')
        identifier = db.insert_row(connection, 'project_tags', project_id=p['id'], name=name, desc=desc, color=color,
            userid=context.actor.userid, sort=max([v['sort'] for v in existing] or [-1]) + 1)
    log(connection, context, p['id'], '修改标签' if data.get('id') else '添加标签', record={'change': {'type': 'tag', 'name': name, 'color': color}})
    return by_id(connection, 'project_tags', identifier)


def tag_sort(connection, context, data, settings):
    return _sort_configuration(connection, context, data, 'project_tags', '调整标签排序')


def tag_delete(connection, context, data, settings):
    tag = by_id(connection, 'project_tags', data.get('id'))
    if not tag:
        raise ApiError('标签不存在或已被删除')
    p = require_project(connection, context, tag['project_id'], write=True)
    if not p['owner'] and tag['userid'] != context.actor.userid:
        raise ApiError('没有权限删除标签')
    tt = table('project_task_tags')
    touched = rows(connection, 'project_task_tags', tt.c.project_id == p['id'], tt.c.name == tag['name'])
    connection.execute(tt.delete().where(tt.c.project_id == p['id'], tt.c.name == tag['name']))
    connection.execute(table('project_tags').delete().where(table('project_tags').c.id == tag['id']))
    for tid in {v['task_id'] for v in touched}:
        update(connection, 'project_tasks', tid, {})
        observe_task(connection, context, by_id(connection, 'project_tasks', tid), 'update')
    log(connection, context, p['id'], '删除标签', record={'change': {'type': 'tag', 'name': tag['name'], 'color': tag['color']}})
    return []


def _handoff_settings(connection):
    setting = db.setting(connection, 'system', default={})
    options = {'project_task_handoff': ('close', 'open'), 'project_task_handoff_role': ('owner', 'close', 'managers'),
        'project_task_handoff_candidates': ('department', 'project'), 'project_task_handoff_adjust': ('department', 'all'),
        'project_task_handoff_note': ('optional', 'required')}
    return {key: setting[key] if setting.get(key) in allowed else allowed[0] for key, allowed in options.items()}


def _handoff_task(connection, context, data):
    if _handoff_settings(connection)['project_task_handoff'] != 'open':
        raise ApiError('任务流转未开启')
    return require_task(connection, context, data.get('task_id'), archived=None)


def handoff_policy(connection, context, task, data):
    setting = _handoff_settings(connection)
    p = by_id(connection, 'projects', task['project_id'], lock=True)
    if not p or p['deleted_at'] or p['archived_at'] or task['archived_at']:
        raise ApiError('当前任务不可指派')
    owners = task_snapshot(connection, task)['owners']
    members = list(member_map(connection, p['id']))
    uid = actor(connection, context).userid
    protected, candidates = [], members
    if uid in members:
        project = require_project(connection, context, p['id'], write=True)
        if owners:
            require_action(connection, context, project, 'task_update', task)
    else:
        if setting['project_task_handoff_role'] == 'close' or task['visibility'] != 1 or p['id'] not in department_projects(connection, uid):
            raise ApiError('无指派权限')
        from .accounts import managed_departments, department_users
        departments = managed_departments(connection, uid)
        if setting['project_task_handoff_role'] == 'owner':
            departments = [d for d in departments if isinstance(d, dict) and d.get('owner_userid') == uid]
        roots = [integer(d['id']) if isinstance(d, dict) else integer(d) for d in departments]
        selected = data.get('department_owner_ids', data.get('department_ids'))
        if selected not in (None, '', 'all'):
            roots = [v for v in roots if v in ids(selected)]
        managed = department_users(connection, roots, recursive=True)
        managed_ids = [integer(u['userid']) if isinstance(u, dict) else integer(u) for u in managed]
        if not set(managed_ids) & set(members):
            raise ApiError('无指派权限')
        if setting['project_task_handoff_candidates'] == 'department':
            candidates = [u for u in members if u in managed_ids]
        if setting['project_task_handoff_adjust'] == 'department':
            protected = [u for u in owners if u not in managed_ids]
    u = table('users')
    candidates = list(connection.execute(select(u.c.userid).where(u.c.userid.in_(candidates), u.c.disable_at.is_(None), u.c.bot == 0)).scalars())
    h = table('project_task_handoffs')
    last = connection.execute(select(h.c.id).where(h.c.task_id == task['id']).order_by(h.c.id.desc()).limit(1)).scalar()
    version = hashlib.sha256(encode([task['project_id'], owners, last]).encode()).hexdigest()
    return {'owners': owners, 'protected': protected, 'candidates': candidates,
        'note_required': setting['project_task_handoff_note'] == 'required', 'version': version}


def handoff_lists(connection, context, data, settings):
    task = _handoff_task(connection, context, data)
    t = table('project_task_handoffs')
    query = select(t).where(t.c.task_id == task['id'])
    if integer(data.get('before_id')):
        query = query.where(t.c.id < integer(data['before_id']))
    entries = [dict(v) for v in connection.execute(query.order_by(t.c.id.desc()).limit(21)).mappings()]
    for item in entries:
        item['record'] = decode(item['record'], {})
    try:
        handoff_policy(connection, context, task, data)
        can_assign = True
    except ApiError:
        can_assign = False
    return {'lists': entries[:20], 'has_more': len(entries) > 20, 'can_assign': can_assign}


def handoff_options(connection, context, data, settings):
    task = _handoff_task(connection, context, data)
    result = handoff_policy(connection, context, task, data)
    u = table('users')
    result['users'] = [dict(v) for v in connection.execute(select(u.c.userid, u.c.nickname, u.c.userimg)
        .where(u.c.userid.in_(set(result['candidates'] + result['owners']))).order_by(u.c.nickname)).mappings()]
    return result


def handoff_assign(connection, context, data, settings):
    task = _handoff_task(connection, context, data)
    task = by_id(connection, 'project_tasks', task['id'], lock=True)
    policy = handoff_policy(connection, context, task, data)
    raw_owners = data.get('owners', [])
    if not isinstance(raw_owners, list) or len(raw_owners) > 10 or any(isinstance(v, bool) or not str(v).isdigit() or integer(v) <= 0 for v in raw_owners):
        raise ApiError('指派参数无效')
    if not isinstance(data.get('version'), str) or not isinstance(data.get('note', ''), str) or len(data.get('note', '')) > 1000:
        raise ApiError('指派参数无效')
    owners = sorted(ids(raw_owners))
    note = data.get('note', '').strip()
    if not hmac.compare_digest(policy['version'], str(data.get('version', ''))):
        raise ApiError('负责人已发生变化，请刷新后重试')
    if len(owners) > 10:
        raise ApiError('任务负责人最多不能超过10个')
    if set(policy['protected']) - set(owners):
        raise ApiError('不能移除管理范围外的负责人')
    if (set(owners) - set(policy['owners'])) - set(policy['candidates']):
        raise ApiError('所选负责人不在可指派范围内')
    if owners == policy['owners']:
        raise ApiError('负责人未发生变化')
    if policy['note_required'] and not note:
        raise ApiError('请填写指派留言')
    from .project_tasks import _participants, descendants
    from .project_core import handoff_record, root_task
    before = task_snapshot(connection, task)
    family_root = root_task(connection, task)
    family = [family_root] + descendants(connection, family_root['id'])
    previous = {item['id']: task_allowed_users(connection, item) for item in family}
    tu = table('project_task_users')
    assists = list(connection.execute(select(tu.c.userid).where(tu.c.task_id == task['id'], tu.c.owner == 0)).scalars())
    _participants(connection, task, owners, assists)
    update(connection, 'project_tasks', task['id'], {})
    fresh = by_id(connection, 'project_tasks', task['id'])
    handoff_record(connection, context, fresh, before, 'assign', note)
    log(connection, context, task['project_id'], '修改{任务}负责人', task=task, record={'userid': owners, 'note': note})
    for item in family:
        observe_task(connection, context, by_id(connection, 'project_tasks', item['id']), 'update', previous[item['id']])
    from ..jobs import enqueue
    enqueue(connection, 'projects.notify', {'task_id': task['id'], 'userids': list(set(owners) - set(policy['owners'])), 'type': 0})
    result = task_data(connection, fresh, context.actor.userid)
    result['update_marking'] = {'is_update_project': True}
    return result


def _ai_status(connection, context, data, settings, status):
    task = require_task(connection, context, data.get('task_id'), write=True)
    project = require_project(connection, context, task['project_id'], write=True)
    require_action(connection, context, project, 'task_update', task)
    event_type = str(data.get('type', ''))
    if event_type not in ('description', 'subtasks', 'assignee', 'similar'):
        raise ApiError('无效的建议类型')
    e = table('project_task_ai_events')
    event = row(connection, 'project_task_ai_events', e.c.task_id == task['id'], e.c.event_type == event_type, e.c.msg_id == integer(data.get('msg_id')), lock=True)
    if not event:
        raise ApiError('建议不存在')
    result = decode(event['result'], {})
    if status == 'applied' and not result:
        raise ApiError('建议内容为空')
    update(connection, 'project_task_ai_events', event['id'], {'status': status})
    if status == 'applied' and event_type == 'similar' and integer(data.get('related')):
        from .project_tasks import create_relation
        target = require_task(connection, context, data['related'], archived=None)
        create_relation(connection, context, task, target, task['dialog_id'], event['msg_id'])
    from .dialog_core import get_message, send_message
    message = get_message(connection, event['msg_id'])
    if message['dialog_id'] != task['dialog_id']:
        raise ApiError('建议消息不属于任务')
    import re
    payload = decode(message['msg'], {})
    content = payload.get('text', '')
    if not content:
        raise ApiError('消息内容为空')
    qualifier = ''
    if event_type == 'assignee' and integer(data.get('userid')):
        qualifier = '[^}]*userid="' + str(integer(data['userid'])) + '"'
    elif event_type == 'similar' and integer(data.get('related')):
        qualifier = '[^}]*related="' + str(integer(data['related'])) + '"'
    pattern = r'(:::ai-action\{type="' + re.escape(event_type) + '"' + qualifier + r'[^}]*)\}:::'
    payload = {'text': re.sub(pattern, lambda m: re.sub(r' status="[^"]*"', '', m[1]) + ' status="' + status + '"}:::', content), 'type': 'md'}
    updated_message = send_message(connection, dialog_id=task['dialog_id'], sender_id=message['userid'], msg_type=message['type'],
        msg=payload, context=context, update_id=message['id'])
    log(connection, context, task['project_id'], 'AI建议：' + ('采纳' if status == 'applied' else '忽略') + event_type + '建议', task=task)
    return {'type': event_type, 'task_id': task['id'], 'result': result, 'msg': updated_message} if status == 'applied' else {'msg': updated_message}


def ai_apply(connection, context, data, settings):
    return _ai_status(connection, context, data, settings, 'applied')


def ai_dismiss(connection, context, data, settings):
    return _ai_status(connection, context, data, settings, 'dismissed')


def obsolete_ai_generate(connection, context, data, settings):
    from starlette.responses import Response
    version = tuple(integer(part) for part in str(context.version).split('.')[:3])
    if version < (1, 4, 35):
        raise ApiError('当前客户端版本过低，最低版本要求 (1.4.35)。')
    # Source obsolete methods only check the version, then return an empty HTTP body.
    return Response(content=b'', media_type='text/html')


ROUTES = {
    'project/flow/list': flow_list, 'project/flow/save': flow_save, 'project/flow/delete': flow_delete,
    'project/task/flow': task_flow, 'project/task/template_list': template_list,
    'project/task/template_visible': template_visible, 'project/task/template_search': template_search,
    'project/task/template_save': template_save, 'project/task/template_sort': template_sort,
    'project/task/template_delete': template_delete, 'project/task/template_default': template_default,
    'project/tag/save': tag_save, 'project/tag/sort': tag_sort, 'project/tag/delete': tag_delete, 'project/tag/list': tag_list,
    'project/task/ai_apply': ai_apply, 'project/task/ai_dismiss': ai_dismiss,
    'project/task/ai_generate': obsolete_ai_generate, 'project/ai/generate': obsolete_ai_generate,
    'projecttaskhandoff/lists': handoff_lists, 'projecttaskhandoff/options': handoff_options,
    'projecttaskhandoff/assign': handoff_assign,
}
