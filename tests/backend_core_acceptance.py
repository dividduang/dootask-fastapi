"""Real HTTP/core DB acceptance; run from project root with private runtime env loaded.
python -m tests.backend_core_acceptance [--readback]
Only synthetic identities/resources; never clears or resets a database.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pymysql

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:18080'
OUT = ROOT / 'docs/evidence/qa-backend'
STATE = ROOT / '.runtime/backend-core-state.json'
accounts = json.loads((ROOT / '.runtime/qa-accounts.json').read_text())
records = []

def scrub(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if any(x in k.lower() for x in ('password', 'token', 'secret', 'key', 'code', 'url')) else scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value

def request(path, data=None, token='', *, binary=False, multipart=None):
    headers = {'version': '1.9.66'}
    if token:
        headers['dootask-token'] = token
    if multipart:
        body, headers['Content-Type'] = multipart
    else:
        body = json.dumps(data or {}).encode()
        headers['Content-Type'] = 'application/json'
    start = time.monotonic()
    try:
        response = urlopen(Request(BASE + '/api/' + path, data=body, headers=headers), timeout=30)
    except HTTPError as error:
        response = error
    raw = response.read()
    result = raw if binary else json.loads(raw)
    records.append({'at': datetime.now(timezone.utc).isoformat(), 'path': path, 'method': 'POST', 'authenticated': bool(token), 'input': scrub(data), 'status': response.status, 'duration_ms': round((time.monotonic()-start)*1000), 'response': {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)} if binary else scrub(result)})
    return result

def ok(path, data=None, token=''):
    result = request(path, data, token)
    assert result['ret'] == 1, (path, result)
    return result['data']

def denied(path, data=None, token=''):
    result = request(path, data, token)
    assert result['ret'] != 1, (path, result)

def db_rows(sql, values=()):
    with pymysql.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']), user=os.environ['DB_USERNAME'], password=os.environ['DB_PASSWORD'], database=os.environ['DB_DATABASE'], cursorclass=pymysql.cursors.DictCursor) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, values)
            return cursor.fetchall()

try:
    assert os.environ['DB_DATABASE'] == 'dootask_fastapi' and int(os.environ['DB_PORT']) == 13306
    admin = ok('users/login', {k: accounts['admin'][k] for k in ('email', 'password')})
    a = admin['token']
    if '--readback' in sys.argv:
        state = json.loads(STATE.read_text())
        assert ok('project/one', {'project_id':state['project_id']}, a)['id'] == state['project_id']
        assert ok('project/task/one', {'task_id':state['task_id']}, a)['name'] == state['task_name']
        raw = request('file/content', {'id':state['file_id'], 'down':'yes'}, a, binary=True)
        assert hashlib.sha256(raw).hexdigest() == state['file_sha256']
        assert ok('report/detail', {'id':state['report_id']}, a)['id'] == state['report_id']
        assert db_rows('SELECT id FROM web_socket_dialog_msgs WHERE id=%s', (state['message_id'],))
    else:
        stamp = str(int(time.time()))
        password = 'Qa7!' + secrets.token_urlsafe(15)
        email = 'b' + stamp + '@example.test'
        member = ok('users/login', {'type':'reg','email':email,'password':password})
        m = member['token']
        changed = ok('users/editpass', {'oldpass':password,'newpass':password+'x'}, m)
        denied('users/info', {}, m)
        m = changed['token']
        ok('users/logout', {}, m)
        denied('users/info', {}, m)
        member = ok('users/login', {'email':email,'password':password+'x'})
        m = member['token']
        outsider = ok('users/login', {k: accounts['outsider'][k] for k in ('email', 'password')})
        o = outsider['token']
        p = ok('project/add', {'name':'HTTP core '+stamp,'columns':'Backlog,Done'}, a)
        denied('project/one', {'project_id':p['id']}, o)
        denied('project/one', {'project_id':p['id']})
        ok('project/user', {'project_id':p['id'],'userid':[admin['userid'],member['userid']]}, a)
        col = ok('project/column/lists', {'project_id':p['id']}, a)['data'][0]
        task = ok('project/task/add', {'project_id':p['id'],'column_id':col['id'],'name':'Persistent HTTP task '+stamp,'owner':[member['userid']]}, a)
        denied('project/task/update', {'task_id':task['id'],'name':'Not allowed'}, o)
        dialog = ok('project/task/dialog', {'task_id':task['id']}, a)
        message = ok('dialog/msg/sendtext', {'dialog_id':dialog['dialog_id'],'text':'Real task comment '+stamp}, m)
        denied('dialog/msg/sendtext', {'dialog_id':dialog['dialog_id'],'text':'Rejected outsider'}, o)
        before = db_rows('SELECT COUNT(*) n FROM project_tasks WHERE project_id=%s', (p['id'],))[0]['n']
        denied('project/task/add', {'project_id':p['id'],'column_id':col['id'],'name':'rollback child','owner':[99999999]}, a)
        assert db_rows('SELECT COUNT(*) n FROM project_tasks WHERE project_id=%s', (p['id'],))[0]['n'] == before
        raw = ('UTF-8 actual file: 验收 '+stamp+'\n').encode()
        boundary = 'core-acceptance-boundary'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="core.txt"\r\nContent-Type: text/plain\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
        upload = request('file/content/upload', token=a, multipart=(body, 'multipart/form-data; boundary='+boundary))
        assert upload['ret'] == 1, upload
        file = upload['data'][-1]
        assert request('file/content', {'id':file['id'],'down':'yes'}, a, binary=True) == raw
        denied('file/one', {'id':file['id']}, o)
        report = ok('report/store', {'title':'HTTP report '+stamp,'content':'<p>Actual completed task</p>','type':'daily','receive':[member['userid']]}, a)
        assert ok('report/detail', {'id':report['id']}, m)['id'] == report['id']
        denied('report/detail', {'id':report['id']}, o)
        ok('system/setting', {}, a)
        denied('system/setting', {'type':'save','system_name':'Not allowed'}, m)
        denied('users/login', {'email':email,'password':'Wrong-Password79!'})
        state = {'project_id':p['id'],'task_id':task['id'],'task_name':task['name'],'file_id':file['id'],'file_sha256':hashlib.sha256(raw).hexdigest(),'report_id':report['id'],'message_id':message['id']}
        STATE.write_text(json.dumps(state), encoding='utf-8')
    records.append({'persistent_state':state,'database':db_rows('SELECT id,name FROM projects WHERE id=%s',(state['project_id'],)), 'result':'PASS'})
finally:
    name = 'core-readback.json' if '--readback' in sys.argv else 'core-http.json'
    (OUT/name).write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print('Core real HTTP '+('readback' if '--readback' in sys.argv else 'scenario')+' passed')
