"""Replay real local business state machines; no backend imports or SQL writes.
Run only in the backend owner's stable isolated-service window.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import time
import traceback
import protocol_acceptance as p

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.runtime/business-accounts.json'
ACCOUNTS = []
ADMIN = ''


def check(value, assertion):
    p.event('assertion', assertion=assertion, passed=bool(value))
    assert value, assertion


def api(path, account=None, data=None, ok=True):
    token = account if isinstance(account, str) else (account or {}).get('token', '')
    result = p.api(path, token, data, ok=ok, headers={'version': '1.9.0'})
    if isinstance(result, dict) and result.get('reg_invite'):
        p.SECRETS.append(result['reg_invite'])
    p.EVENTS[-1]['role'] = 'administrator' if token == ADMIN else str((account or {}).get('label', 'anonymous')) if not isinstance(account, str) else 'session'
    return result


def register(label):
    password = 'Biz-' + secrets.token_urlsafe(12) + '9!'
    p.SECRETS.append(password)
    email = 'bq' + secrets.token_hex(7) + '@example.test'
    value = api('users/login', data={'type': 'reg', 'email': email, 'password': password})
    value.update(label=label, password=password, email=email)
    ACCOUNTS.append(value)
    STATE.write_text(json.dumps(ACCOUNTS), encoding='utf-8')
    check(bool(value.get('token') and value.get('userid')), 'real registration initializes account and session')
    return value


def login(a, captcha=False):
    fields = {'email': a['email'], 'password': a['password']}
    if captcha:
        challenge = api('users/login/codejson')
        p.SECRETS.append(challenge['key'])
        image = ROOT / '.runtime/business-captcha.png'
        image.write_bytes(base64.b64decode(challenge['img'].split(',', 1)[1]))
        answer = ROOT / '.runtime/business-captcha-answer'
        if answer.exists():
            answer.unlink()
        print(f'Read {image}; write displayed CAPTCHA into {answer}', flush=True)
        deadline = time.monotonic() + 180
        while not answer.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        check(answer.exists(), 'visible CAPTCHA answer supplied within challenge expiry')
        code = answer.read_text(encoding='utf-8-sig').strip()
        answer.unlink()
        p.SECRETS.append(code)
        fields.update(code_key=challenge['key'], code=code)
    value = api('users/login', data=fields)
    a['token'] = value['token']
    STATE.write_text(json.dumps(ACCOUNTS), encoding='utf-8')
    return a


def ids(value):
    return {v['id'] for v in value}


def project(a, b=None, flow=False):
    result = api('project/add', a, {'name': '业务验收' + secrets.token_hex(4), 'columns': '待办,进行,结束', 'flow': 'open' if flow else 'close'})
    if b:
        api('project/user', a, {'project_id': result['id'], 'userid': [a['userid'], b['userid']]})
    return result


def task(a, project_id, name='业务任务', **fields):
    return api('project/task/add', a, {'project_id': project_id, 'name': name + secrets.token_hex(3), 'owner': [a['userid']], **fields})


def reports():
    a, b, c = register('report-author'), register('report-recipient'), register('report-outsider')
    body = '<p>本地报告 &amp; 状态转换</p>'
    fields = {'title': '业务日报', 'type': 'daily', 'content': body, 'receive': [b['userid']], 'offset': 0}
    before = api('report/unread', b)['total']
    r = api('report/store', a, fields); rid = r['id']
    check(r['title'] == fields['title'], 'report creation preserves title')
    check(rid in ids(api('report/my', a)['data']), 'author list persists report')
    check(rid in ids(api('report/receive', b)['data']), 'receiver list contains created report')
    check(api('report/unread', b)['total'] == before + 1, 'new report increments recipient unread')
    api('report/detail', c, {'id': rid}, ok=False)
    check(api('report/unread', b)['total'] == before + 1, 'outsider failure does not mark recipient read')
    detail = api('report/detail', b, {'id': rid})
    check(detail['content'] == body, 'receiver sees exact rich report content')
    check(api('report/unread', b)['total'] == before, 'detail marks recipient read persistently')
    api('report/mark', b, {'id': [rid], 'action': 'unread'})
    check(api('report/unread', b)['total'] == before + 1, 'mark unread restores unread count')
    api('report/read', b, {'ids': str(rid)})
    check(api('report/unread', b)['total'] == before, 'batch read clears unread count')
    api('report/store', b, dict(fields, id=rid, title='unauthorized'), ok=False)
    check(api('report/detail', a, {'id': rid})['title'] == fields['title'], 'recipient edit denial leaves author title unchanged')
    count = api('report/my', a)['total']
    api('report/store', a, fields, ok=False)
    check(api('report/my', a)['total'] == count, 'duplicate report rejected without extra report')
    messages = api('report/share', a, {'id': rid, 'userids': [c['userid']], 'leave_message': '共享实测'})
    check(len(messages) == 1 and 'single/report/detail/' in messages[0]['msg']['text'], 'share creates real report-link message')
    code = re.search(r'single/report/detail/([^"<>]+)', messages[0]['msg']['text']).group(1)
    p.SECRETS.append(code)
    check(api('report/detail', c, {'code': code})['id'] == rid, 'shared recipient can open bearer report code')
    api('report/store', a, dict(fields, id=rid, title='已修订', receive=[]))
    api('report/detail', b, {'id': rid}, ok=False)
    check(rid not in ids(api('report/receive', b)['data']), 'removed receiver cannot list report after refresh')
    check(api('report/detail', a, {'id': rid})['title'] == '已修订', 'author revision persists after receiver revocation')
    p.sql('SELECT id,userid,title,type FROM reports WHERE id=%s', (rid,))
    check(not p.sql('SELECT rid,userid,`read` FROM report_receives WHERE rid=%s', (rid,)), 'receiver removal persisted in database')


def files():
    a, b = register('file-owner'), register('file-shared-reader')
    folder = api('file/add', a, {'name': '业务目录', 'type': 'folder'})['id']
    target = api('file/add', a, {'name': '移动目录', 'type': 'folder'})['id']
    fid = api('file/add', a, {'name': '业务文档', 'type': 'document', 'pid': folder})['id']
    first, second = '<p>第一版中文</p>', '<p>第二版内容</p>'
    old = api('file/content/save', a, {'id': fid, 'content': json.dumps({'type': 'text', 'content': first})})
    api('file/content/save', a, {'id': fid, 'content': json.dumps({'type': 'text', 'content': second})})
    check(api('file/content', a, {'id': fid})['content']['content'] == second, 'newest file content survives readback')
    check(len(api('file/content/history', a, {'id': fid})['data']) == 2, 'two real content versions persisted')
    api('file/share/update', a, {'id': folder, 'permission': 0, 'userids': [b['userid']]})
    check(api('file/content', b, {'id': fid})['content']['content'] == second, 'folder reader inherits content access')
    api('file/content/save', b, {'id': fid, 'content': json.dumps({'content': 'denied'})}, ok=False)
    api('file/content/restore', b, {'id': fid, 'history_id': old['id']}, ok=False)
    check(api('file/content', a, {'id': fid})['content']['content'] == second, 'read-only save and restore do not mutate bytes')
    api('file/share/update', a, {'id': folder, 'permission': 1, 'userids': [b['userid']]})
    api('file/content/restore', b, {'id': fid, 'history_id': old['id']})
    check(api('file/content', a, {'id': fid})['content']['content'] == first, 'writer history restore visible to owner')
    check(len(api('file/content/history', a, {'id': fid})['data']) == 3, 'restore appends history instead of erasing versions')
    api('file/move', a, {'ids': [fid], 'pid': target})
    api('file/content', b, {'id': fid}, ok=False)
    check(fid in ids(api('file/lists', a, {'pid': target})), 'moved file appears in target directory')
    check(fid not in ids(api('file/lists', a, {'pid': folder})), 'moved file absent in source directory')
    api('file/share/update', a, {'id': target, 'permission': 0, 'userids': [b['userid']]})
    check(api('file/content', b, {'id': fid})['content']['content'] == first, 'new parent sharing grants restored content')
    api('file/share/update', a, {'id': target, 'permission': -1, 'userids': [b['userid']]})
    api('file/one', b, {'id': fid}, ok=False)
    api('file/move', a, {'ids': [target], 'pid': fid}, ok=False)
    check(api('file/one', a, {'id': fid})['pid'] == target, 'invalid move leaves directory ownership and location unchanged')
    p.sql('SELECT id,pid,userid,pshare,share,name FROM files WHERE id IN (%s,%s,%s)', (folder,target,fid))


def organization():
    a, b, c = register('department-owner'), register('department-member'), register('role-subject')
    name = '验收部门' + secrets.token_hex(3)
    api('users/department/add', b, {'name': name, 'owner_userid': a['userid']}, ok=False)
    check(not any(v['name'] == name for v in api('users/department/list', ADMIN)), 'ordinary user cannot create department')
    api('users/department/add', ADMIN, {'name': name, 'owner_userid': a['userid']})
    dep = next(v for v in api('users/department/list', ADMIN) if v['name'] == name); did = dep['id']
    api('users/operation', ADMIN, {'userid': b['userid'], 'type': 'department', 'department': [did]})
    check(did in ids(api('users/info/managed_departments', a)), 'owner sees managed department')
    check(did in ids(api('users/info/departments', b)), 'assigned member sees department')
    check(did not in ids(api('users/info/managed_departments', b)), 'ordinary member lacks department-management role')
    api('users/department/adddeputy', ADMIN, {'id': did, 'userid': b['userid']})
    check(did in ids(api('users/info/managed_departments', b)), 'deputy appointment updates managed-department view')
    api('users/department/deldeputy', ADMIN, {'id': did, 'userid': b['userid']})
    check(did not in ids(api('users/info/managed_departments', b)), 'deputy dismissal revokes managed view')
    api('users/operation', ADMIN, {'userid': b['userid'], 'type': 'department', 'department': [did]})
    pr = project(b); t = task(b, pr['id'], '团队统计任务')
    stats = api('dashboard/team/stats', a, {'department_owner_ids': str(did), 'refresh': 1})
    check(stats['member_count'] == 2 and stats['blocks']['uncompleted'] == 1, 'dashboard counts exact synthetic department members and task')
    listing = api('dashboard/team/tasks', a, {'department_owner_ids': str(did), 'type': 'uncompleted'})
    check(t['id'] in ids(listing['data']) and listing['data'][0]['department_readonly'], 'department owner sees nonmember task read-only')
    api('project/task/update', a, {'task_id': t['id'], 'name': 'denied'}, ok=False)
    check(api('project/task/one', b, {'task_id': t['id']})['name'] == t['name'], 'department-view write denial has no mutation')
    api('dashboard/team/stats', c, {'department_owner_ids': str(did)}, ok=False)
    api('project/task/update', b, {'task_id': t['id'], 'complete_at': datetime.now(timezone.utc).isoformat()})
    stats = api('dashboard/team/stats', a, {'department_owner_ids': str(did), 'refresh': 1})
    check(stats['blocks']['uncompleted'] == 0 and stats['blocks']['week_completed'] == 1, 'dashboard reflects completed task transition')
    api('users/operation', b, {'userid': c['userid'], 'type': 'setadmin'}, ok=False)
    check('admin' not in api('users/info', c)['identity'], 'ordinary user cannot promote another account')
    api('users/operation', ADMIN, {'userid': c['userid'], 'type': 'setadmin'})
    api('users/info', c, ok=False); login(c)
    check('admin' in api('users/info', c)['identity'], 'promotion revokes old session and relogin sees administrator role')
    check(did in ids(api('users/department/list', c)), 'promoted admin can read administrative department list')
    api('users/operation', ADMIN, {'userid': c['userid'], 'type': 'clearadmin'})
    api('users/info', c, ok=False); login(c)
    api('users/department/list', c, ok=False)
    check('admin' not in api('users/info', c)['identity'], 'demotion revokes administrative role')
    api('users/operation', ADMIN, {'userid': c['userid'], 'type': 'setdisable', 'disable_time': datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
    api('users/info', c, ok=False)
    api('users/login', data={'email': c['email'], 'password': c['password']}, ok=False)
    api('users/operation', ADMIN, {'userid': c['userid'], 'type': 'cleardisable'})
    login(c, captcha=True); check(api('users/info', c)['userid'] == c['userid'], 'reenabled account authenticates normally')
    p.sql('SELECT id,name,owner_userid,dialog_id FROM user_departments WHERE id=%s', (did,))


def navigation_delta():
    a, b = register('navigation-owner'), register('navigation-member')
    pr = project(a, b); pid = pr['id']
    t = task(a, pid, '导航游标任务'); tid = t['id']
    baseline = api('project/lists', b, {'getstatistics':'yes'})
    check(pid in ids(baseline['data']), 'full project list contains joined project')
    time.sleep(1.1)
    cursor = int(time.time())
    wire = f'{cursor},0'
    delta = api('project/lists', b, {'timerange':wire,'getstatistics':'yes'})
    check(not delta['data'] and delta['total']==0 and delta['total_all']==baseline['total_all'], 'original seconds cursor omits unchanged projects but retains total_all')
    delta = api('project/task/lists', b, {'project_id':pid,'timerange':wire})
    check(not delta['data'] and not delta['deleted_id'], 'original seconds cursor returns empty unchanged task delta without deletion')
    check(tid in ids(api('project/task/lists', b, {'project_id':pid})['data']), 'cursor-independent full task refresh remains available')
    api('project/task/remove', a, {'task_id':tid})
    delta = api('project/task/lists', b, {'project_id':pid,'timerange':wire})
    check(tid in delta['deleted_id'] and tid not in ids(delta['data']), 'task soft deletion emits cache tombstone without live row')
    api('project/task/remove', a, {'task_id':tid,'type':'recovery'})
    check(tid in ids(api('project/task/lists', b, {'project_id':pid,'timerange':wire})['data']), 'restored task reappears in original-wire incremental response')
    api('users/task/browse_save', b, {'task_id':tid})
    recent=api('users/recent/browse', b)['list']
    record=next(v['record_id'] for v in recent if v['id']==tid)
    api('users/recent/delete', b, {'id':record})
    check(tid not in ids(api('users/recent/browse', b)['list']), 'explicit recent removal survives refresh')
    api('users/favorite/toggle', b, {'type':'task','id':tid})
    api('users/favorite/remark', b, {'type':'task','id':tid,'remark':'业务导航备注'})
    favorite=next(v for v in api('users/favorites', b)['data']['tasks'] if v['id']==tid)
    check(favorite['remark']=='业务导航备注', 'favorite remark persists for consumer')
    api('users/favorite/toggle', b, {'type':'task','id':tid})
    check(not api('users/favorite/check', b, {'type':'task','id':tid})['favorited'], 'favorite toggle removal confirmed by check endpoint')
    fid=api('file/add', a, {'name':'检索文件'+secrets.token_hex(4),'type':'document'})
    api('file/share/update', a, {'id':fid['id'],'userids':[b['userid']],'permission':0})
    check(fid['id'] in ids(api('search/file', b, {'key':fid['name'],'search_type':'text'})), 'file fallback search returns authorized shared document')
    api('file/share/update', a, {'id':fid['id'],'userids':[b['userid']],'permission':-1})
    check(not api('search/file', b, {'key':fid['name'],'search_type':'text'}), 'file search removes document immediately after share revocation')


def organization_resume():
    ACCOUNTS.extend(json.loads(STATE.read_text(encoding='utf-8')))
    p.remember(ACCOUNTS)
    c = next(v for v in reversed(ACCOUNTS) if v['label']=='role-subject')
    login(c, captcha=True)
    check(api('users/info', c)['userid']==c['userid'], 'reenabled account authenticates after visible CAPTCHA')
    api('users/department/list', c, ok=False)
    row = p.sql('SELECT userid,identity,disable_at FROM users WHERE userid=%s', (c['userid'],))[0]
    check(row['disable_at'] is None and 'admin' not in row['identity'] and 'disable' not in row['identity'], 'reenabled ordinary role persists without administrator or disabled flags')


def project_states():
    a, b = register('project-owner'), register('project-member')
    pr = project(a,b,True); pid = pr['id']
    cols = api('project/column/lists', a, {'project_id':pid})['data']
    api('project/sort', a, {'project_id':pid,'only_column':1,'sort':json.dumps([{'id':v['id']} for v in reversed(cols)])})
    check([v['id'] for v in api('project/column/lists',b,{'project_id':pid})['data']] == [v['id'] for v in reversed(cols)], 'column sorting observed by second user')
    api('project/permission/update',a,{'project_id':pid,'task_update':[1]})
    before = api('project/permission',a,{'project_id':pid})
    api('project/permission/update',b,{'project_id':pid,'task_update':[2]},ok=False)
    check(api('project/permission',a,{'project_id':pid}) == before,'member cannot elevate project permissions')
    t = task(a,pid,'业务流程'); tid=t['id']
    sub = api('project/task/addsub',a,{'task_id':tid,'name':'业务子任务'})
    flows = api('project/flow/list',a,{'project_id':pid})[0]['project_flow_item']
    end = next(f['id'] for f in flows if f['status']=='end')
    progress = next(f['id'] for f in flows if f['status']=='progress')
    api('project/task/update',b,{'task_id':tid,'name':'unauthorized'},ok=False)
    check(api('project/task/one',a,{'task_id':tid})['name']==t['name'],'denied task rename leaves title unchanged')
    api('project/task/update',a,{'task_id':tid,'flow_item_id':end},ok=False)
    check(not api('project/task/one',a,{'task_id':tid})['complete_at'],'unfinished child blocks parent completion transaction')
    api('project/task/update',a,{'task_id':tid,'flow_item_id':progress})
    check(api('project/task/one',b,{'task_id':tid})['flow_item_id']==progress,'second user observes workflow transition')
    api('project/task/update',a,{'task_id':sub['id'],'flow_item_id':end})
    api('project/task/update',a,{'task_id':tid,'flow_item_id':end})
    check(api('project/task/subdata',b,{'task_id':tid})['sub_complete']==1,'child completion count observed')
    api('project/task/archived',a,{'task_id':tid})
    check(tid not in ids(api('project/task/lists',b,{'project_id':pid})['data']),'archived task absent active list')
    check(tid in ids(api('project/task/lists',b,{'project_id':pid,'archived':'yes'})['data']),'archived task present archive list')
    api('project/archived',a,{'project_id':pid})
    api('project/archived',a,{'project_id':pid,'type':'recovery'})
    check(api('project/task/one',a,{'task_id':tid,'archived':'all'})['archived_at'],'independently archived task remains archived after project recovery')
    api('project/task/archived',a,{'task_id':tid,'type':'recovery'})
    check(tid in ids(api('project/task/lists',b,{'project_id':pid})['data']),'task restore returns active list')
    fresh = api('project/task/one',a,{'task_id':tid})
    cursor = fresh['updated_at']
    delta = api('project/task/lists',b,{'project_id':pid,'timerange':{'updated':cursor}})
    check(tid not in ids(delta['data']),'unchanged task omitted from incremental response, not a full snapshot')
    check(tid in ids(api('project/task/lists',b,{'project_id':pid})['data']),'full refresh still includes task despite empty incremental response')
    time.sleep(1.1)
    api('project/task/update',a,{'task_id':tid,'name':'增量更新任务'})
    delta = api('project/task/lists',b,{'project_id':pid,'timerange':{'updated':cursor}})
    check(tid in ids(delta['data']) and next(v for v in delta['data'] if v['id']==tid)['name']=='增量更新任务','incremental update includes changed task and correct title')
    api('users/task/browse_save',b,{'task_id':tid})
    api('users/favorite/toggle',b,{'type':'task','id':tid})
    check(tid in ids(api('users/task/browse',b)),'task navigation history contains visited task')
    check(tid in ids(api('users/recent/browse',b)['list']),'recent navigation includes visited task')
    check(tid in ids(api('users/favorites',b)['data']['tasks']),'favorite list contains authorized task')
    api('project/user',a,{'project_id':pid,'userid':[a['userid']]})
    api('project/task/one',b,{'task_id':tid},ok=False)
    check(tid not in ids(api('users/task/browse',b)),'revoked task removed from navigation history response')
    check(tid not in ids(api('users/recent/browse',b)['list']),'revoked task removed from recent response')
    check(tid not in ids(api('users/favorites',b)['data']['tasks']),'revoked task removed from favorite response')
    tomb = api('project/task/lists',b,{'scope':'all_project'})
    check(tid in tomb['deleted_id'],'revoked member receives task tombstone for cached navigation')
    api('users/favorite/toggle',b,{'type':'task','id':tid},ok=False)
    check(api('project/task/one',a,{'task_id':tid})['name']=='增量更新任务','owner retains task after member removal')
    p.sql('SELECT id,project_id,parent_id,name,complete_at,archived_at FROM project_tasks WHERE id IN (%s,%s)',(tid,sub['id']))


def handoff_relations():
    a,b = register('handoff-owner'),register('handoff-member')
    config = api('system/setting',ADMIN,{'type':'all'})
    saved = {k:config[k] for k in ('project_task_handoff','archived_day')}
    try:
        api('system/setting',ADMIN,dict(saved,type='save',project_task_handoff='open'))
        pr=project(a,b); t=task(a,pr['id'],'交接任务'); other=task(a,pr['id'],'被关联任务')
        policy=api('projecttaskhandoff/options',a,{'task_id':t['id']})
        check(b['userid'] in policy['candidates'],'project member is real assignable handoff candidate')
        api('projecttaskhandoff/assign',a,{'task_id':t['id'],'owners':[b['userid']],'version':policy['version'],'note':'正式指派'})
        changed=api('projecttaskhandoff/options',b,{'task_id':t['id']})
        check(changed['owners']==[b['userid']] and changed['version']!=policy['version'],'handoff updates owners and optimistic version')
        api('projecttaskhandoff/assign',a,{'task_id':t['id'],'owners':[a['userid']],'version':policy['version'],'note':'stale'},ok=False)
        check(api('projecttaskhandoff/options',b,{'task_id':t['id']})['owners']==[b['userid']],'stale assignment does not revert owners')
        history=api('projecttaskhandoff/lists',b,{'task_id':t['id']})['lists']
        check(any(v['source']=='assign' and v['record']['note']=='正式指派' for v in history),'handoff history preserves explicit assignment note')
        dialog=api('project/task/dialog',a,{'task_id':t['id']})
        did=dialog['dialog_id']
        api('dialog/msg/sendtext',a,{'dialog_id':did,'text':f'<p><span class="mention task" data-id="{other["id"]}">#{other["id"]}</span></p>','text_type':'html'})
        relations=api('project/task/related',b,{'task_id':t['id']})
        check(any(v['related_task_id']==other['id'] and v['mention'] for v in relations['list']),'real task mention creates visible relation')
        api('project/task/related_delete',a,{'task_id':t['id'],'related_task_id':other['id']})
        check(not api('project/task/related',b,{'task_id':t['id']})['list'],'relation removal visible after refresh')
    finally:
        api('system/setting',ADMIN,dict(saved,type='save'))


def complaints_search():
    a,b,c=register('complaint-owner'),register('complaint-member'),register('search-outsider')
    pr=project(a,b); t=task(a,pr['id'],'检索任务')
    reason='合成投诉'+secrets.token_hex(5)
    api('complaint/submit',b,{'dialog_id':pr['dialog_id'],'type':1,'reason':reason,'imgs':[]})
    report=next(v for v in api('complaint/lists',ADMIN,{'status':0})['data'] if v['reason']==reason)
    check(report['userid']==b['userid'],'complaint persists reporter identity and content')
    api('complaint/action',b,{'id':report['id'],'type':'handle'},ok=False)
    check(report['id'] in ids(api('complaint/lists',ADMIN,{'status':0})['data']),'unauthorized complaint action leaves status unchanged')
    api('complaint/action',ADMIN,{'id':report['id'],'type':'handle'})
    check(report['id'] in ids(api('complaint/lists',ADMIN,{'status':1})['data']),'admin handling changes persisted complaint status')
    api('complaint/submit',c,{'dialog_id':pr['dialog_id'],'type':1,'reason':reason+'denied','imgs':[]},ok=False)
    check(not any(v['reason']==reason+'denied' for v in api('complaint/lists',ADMIN)['data']),'nonmember complaint rejected without insert')
    check(t['id'] in ids(api('search/task',b,{'key':t['name'],'search_type':'text'})),'unconfigured search uses original MySQL task fallback')
    check(pr['id'] in ids(api('search/project',b,{'key':pr['name'],'search_type':'text'})),'project fallback search contains authorized project')
    check(not api('search/task',c,{'key':t['name'],'search_type':'text'}),'outsider search does not disclose private project task')
    api('project/user',a,{'project_id':pr['id'],'userid':[a['userid']]})
    check(not api('search/task',b,{'key':t['name'],'search_type':'text'}),'search ACL recomputed after membership revocation')
    api('complaint/action',ADMIN,{'id':report['id'],'type':'delete'})
    check(report['id'] not in ids(api('complaint/lists',ADMIN)['data']),'admin deletion removes complaint from refreshed list')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:18080')
    parser.add_argument('--only',default='reports,files,organization,project_states,handoff_relations,complaints_search,navigation_delta')
    parser.add_argument('--admin-token',default=str(ROOT/'.runtime/protocol-admin-token'))
    args=parser.parse_args(); p.ARGS=args; p.TLS=None
    p.OUT=ROOT/'docs/evidence/qa-business'; p.RUN=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    ADMIN=Path(args.admin_token).read_text(encoding='utf-8-sig').strip(); p.SECRETS.append(ADMIN)
    sources=list((ROOT/'backend/domains').glob('*.py'))+[ROOT/'backend/main.py',Path(__file__),ROOT/'tests/protocol_acceptance.py']
    original=ROOT.parent/'dootask/app/Http/Controllers/Api'
    sources += [original/(n+'Controller.php') for n in ('Project','ProjectTaskHandoff','Users','Report','File','Complaint','Dashboard','Search')]
    p.event('provenance',command=sys.argv,python=sys.version,source_hashes={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in sources},constraints='HTTP business writes only; SQL read-only isolated 13306; no browser, no external mocks')
    start=time.monotonic()
    for name in args.only.split(','):
        p.CURRENT=name; begin=time.monotonic()
        try:
            globals()[name]()
            p.RESULTS.append({'id':name,'status':'PASS','elapsed_seconds':time.monotonic()-begin})
        except Exception as exc:
            p.RESULTS.append({'id':name,'status':'FAIL','error':str(exc),'traceback':traceback.format_exc(),'elapsed_seconds':time.monotonic()-begin})
        p.flush()
    failed=any(v['status']=='FAIL' for v in p.RESULTS)
    p.event('execution_end',exit_code=int(failed),elapsed_seconds=time.monotonic()-start)
    p.flush()
    print(json.dumps(p.clean({'evidence':str(p.OUT/(p.RUN+'.json')),'results':p.RESULTS}),ensure_ascii=False))
    raise SystemExit(int(failed))
