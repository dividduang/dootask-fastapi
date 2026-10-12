"""Real HTTP/WS/DAV protocol acceptance. No backend imports, mocks, or SQL writes.
Run after API_READY. Admin credential is read only from a private runtime file.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile
import xml.etree.ElementTree as ET
import pymysql
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[1]
SECRETS = []
EVENTS = []
RESULTS = []
RUN = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
OUT = ROOT / 'docs/evidence/qa-protocol'
CURRENT = ''


def clean(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    for secret in sorted(set(SECRETS), key=len, reverse=True):
        if secret:
            text = text.replace(secret, '[REDACTED]')
    import re
    text = re.sub(r'([?&](?:key|token|dootask-token)=)[^&\s"\\<>]+', r'\1[REDACTED]', text)
    return json.loads(text)


def remember(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in ('token', 'password', 'password_hash', 'encrypt', 'generation', 'secret', 'app_secret') and isinstance(item, str):
                SECRETS.append(item)
            remember(item)
    elif isinstance(value, list):
        for item in value:
            remember(item)


def event(kind, **values):
    EVENTS.append({'utc': datetime.now(timezone.utc).isoformat(), 'case': CURRENT, 'kind': kind, **clean(values)})


def flush():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (RUN + '.json')).write_text(json.dumps(clean({'run': RUN, 'command': sys.argv, 'results': RESULTS, 'events': EVENTS}), ensure_ascii=False, indent=2), encoding='utf-8')


def check(condition, message):
    assert condition, message


def request(method, path, *, token='', data=None, body=None, headers=None, tls=False, basic=None):
    url = path if path.startswith('http') else (ARGS.https if tls else ARGS.base) + path
    hdr = {'User-Agent': 'DooTask-Protocol-Acceptance/1', **(headers or {})}
    if token:
        hdr['dootask-token'] = token
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        hdr['Content-Type'] = 'application/json'
    if basic:
        hdr['Authorization'] = 'Basic ' + base64.b64encode((':'.join(basic)).encode()).decode()
    started = time.monotonic()
    try:
        response = urlopen(Request(url, data=body, headers=hdr, method=method), timeout=40, context=TLS if url.startswith('https:') else None)
    except HTTPError as exc:
        response = exc
    raw = response.read()
    status, rh = response.status, {key.title():value for key,value in response.headers.items()}
    try:
        parsed = json.loads(raw)
        remember(parsed)
    except (ValueError, UnicodeError):
        parsed = None
    description = parsed if parsed is not None else raw.decode('utf-8', errors='replace') if 'xml' in rh.get('Content-Type', '') or 'text/' in rh.get('Content-Type', '') else None
    event('http', method=method, url=url, request_json=data, request_headers={k:v for k,v in hdr.items() if k.lower() not in ('authorization','dootask-token')}, request_bytes=len(body or b''), request_sha256=hashlib.sha256(body or b'').hexdigest(), status=status, response_headers={k:v for k,v in rh.items() if k.lower() not in ('set-cookie',)}, response=description, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest(), elapsed_seconds=time.monotonic()-started)
    return status, raw, parsed, rh


def api(path, token='', data=None, *, ok=True, tls=False, headers=None):
    status, raw, result, _ = request('POST', '/api/' + path, token=token, data={} if data is None else data, tls=tls, headers=headers)
    check(status == 200 and isinstance(result, dict) and {'ret','msg','data'} <= result.keys(), f'{path}: invalid envelope/status {status} {result}')
    check((result['ret'] == 1) if ok else (result['ret'] != 1), f'{path}: unexpected result {result.get("ret")} {result.get("msg")}')
    return result['data'] if ok else result


def multipart(path, token, filename, content, fields=None, ok=True):
    boundary = 'protocol-' + secrets.token_hex(12)
    chunks = []
    for key, value in (fields or {}).items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    field = 'blob' if path == 'upload/chunk' else 'files'
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(), content, f'\r\n--{boundary}--\r\n'.encode()])
    status, _, result, _ = request('POST', '/api/' + path, token=token, body=b''.join(chunks), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    check(status == 200 and isinstance(result, dict), f'{path}: invalid multipart response {status}')
    check((result['ret'] == 1) if ok else (result['ret'] != 1), f'{path}: {result.get("msg")}')
    return result['data'] if ok else result


def sql(query, args=()):
    check(query.lstrip().upper().startswith('SELECT '), 'Read-only database evidence only')
    config = {}
    for file in (ROOT / '.runtime/runtime.env', ROOT / '.runtime/backend.env'):
        for line in file.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                config[key] = value
    check(config.get('DB_DATABASE','dootask_fastapi') == 'dootask_fastapi' and int(config.get('DB_PORT',13306)) == 13306, 'Refusing non-isolated database')
    with pymysql.connect(host=config.get('DB_HOST','127.0.0.1'), port=13306, user=config.get('DB_USERNAME','dootask_app'), password=config['DB_PASSWORD'], database='dootask_fastapi', charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute(query, args)
            rows = cursor.fetchall()
        connection.rollback()
    event('database_readonly', query=query, parameters=args, rows=rows)
    return rows


def wait_db(query, args, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        rows = sql(query, args)
        if predicate(rows):
            return rows
        check(time.monotonic() < deadline, 'Timed out waiting for durable job state')
        time.sleep(2)


def register(label):
    password = 'Protocol-' + secrets.token_urlsafe(12) + '9!'
    SECRETS.append(password)
    email = 'pq-' + secrets.token_hex(7) + '@example.test'
    account = api('users/login', data={'type':'reg','email':email,'password':password})
    check(account.get('token') and account.get('userid'), 'Registration must issue a real session')
    return account


def routes():
    ledger = json.loads((ROOT / 'docs/evidence/integration/endpoint-coverage.json').read_text(encoding='utf-8'))
    rows = ledger['handlers']
    live = [r for r in rows if r['status'] == 'INSTALLED_IMPLEMENTATION_NOT_VERIFIED']
    denied = [r for r in rows if r['status'] == 'REJECTED_EXPLICIT_ALLOWLIST']
    check(len(live) == 326 and len(denied) == 222, 'Frozen route counts differ')
    failures = []
    observed = []
    for row in live:
        path = row['canonical_url']
        status, _, result, _ = request('POST', '/' + path, data={})
        if status >= 500 or isinstance(result,dict) and result.get('msg') == '接口不存在':
            failures.append({'path':path,'http':status,'response':result})
        observed.append({'id':row['id'], 'status':'DISPATCH_OBSERVED', 'http':status, 'ret':result.get('ret') if isinstance(result,dict) else None})
        aliases = row.get('aliases', [])
        if aliases:
            astatus, _, aresult, _ = request('POST', '/api/' + aliases[0][4:].upper(), data={})
            if not (astatus == status and (not isinstance(result,dict) or isinstance(aresult,dict) and aresult.get('ret') == result.get('ret'))):
                failures.append({'path':aliases[0], 'reason':'alias differs'})
    for row in denied:
        status, _, result, _ = request('POST', '/' + row['canonical_url'], data={})
        check(status == 200 and result.get('ret') != 1 and result.get('msg') == '接口不存在', 'Inherited entry became reachable')
    for path in ('system/license','license/status','users/__class__'):
        api(path, ok=False)
    status, _, _, _ = request('GET','/api/users/__class__')
    check(status == 404, 'Unknown non-JSON API must be 404')
    event('dispatch_coverage', installed=326, rejected=222, observations=observed, caveat='Dispatch-only; not 326 business successes')
    check(not failures, f'Dispatch failures: {failures}')


def zip_first():
    owner, outsider = register('file'), register('file-outsider')
    token, other = owner['token'], outsider['token']
    small = ('Unicode 边界\n' * 37).encode()
    before = sql("SELECT userid,email,bot FROM users WHERE email='system-msg@bot.system'")
    file = multipart('file/content/upload', token, '协议小文件-α.txt', small)[-1]
    api('file/one', other, {'id':file['id']}, ok=False)
    denied = api('file/download/pack', other, {'ids':[file['id']]}, ok=False)
    check('url' not in denied.get('data',{}), 'Denied ZIP leaked capability')
    packed = api('file/download/pack', token, {'ids':[file['id']], 'name':'协议首打包'})
    key = packed['url'].split('key=')[1]
    SECRETS.append(key)
    wait_db('SELECT userid,ready,name FROM backend_file_downloads WHERE id=%s', (key,), lambda rows: bool(rows and rows[0]['ready']))
    status, raw, _, _ = request('GET', packed['url'])
    check(status == 200 and zipfile.is_zipfile(io.BytesIO(raw)), 'ZIP bytes unavailable')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        check(archive.namelist() == ['协议小文件-α.txt'], 'ZIP names changed')
        check(archive.read(archive.namelist()[0]) == small, 'ZIP payload hash differs')
    after = sql("SELECT userid,email,bot FROM users WHERE email='system-msg@bot.system'")
    check(bool(after), 'First ZIP did not create real system bot')
    event('zip_first_bot', absent_before=not before, present_after=bool(after), zip_sha256=hashlib.sha256(raw).hexdigest())
    check(not before, 'First-bot precondition violated: bot existed before scenario')


def uploads():
    owner, outsider = register('chunk'), register('chunk-outsider')
    token, other = owner['token'], outsider['token']
    payload = (bytes(range(256)) * ((10*1024*1024+731)//256+1))[:10*1024*1024+731]
    init = {'hash':hashlib.md5(payload).hexdigest(), 'size':len(payload), 'name':'大文件-雪-β.bin', 'scene':'file_cabinet','scene_params':{'pid':0}}
    session = api('upload/init', token, init)
    upload_id = session['upload_id']
    check(session['chunk_count'] == 3, 'Expected >10MiB three-chunk transfer')
    api('upload/merge', other, {'upload_id':upload_id}, ok=False)
    api('upload/cancel', other, {'upload_id':upload_id})
    check(sql('SELECT state FROM backend_uploads WHERE id=%s',(upload_id,))[0]['state']=='pending', 'Outsider cancel changed owner session')
    api('upload/merge', token, {'upload_id':upload_id}, ok=False)
    size = session['chunk_size']
    multipart('upload/chunk', other, '块.bin', payload[:size], {'upload_id':upload_id,'index':0}, ok=False)
    for index in (0,0,2,1):
        received = multipart('upload/chunk', token, '块.bin', payload[index*size:(index+1)*size], {'upload_id':upload_id,'index':index})
        check(len(received['received']) == len(set(received['received'])), 'Duplicate chunk counted twice')
    merged = api('upload/merge', token, {'upload_id':upload_id})
    check(api('upload/merge', token, {'upload_id':upload_id}) == merged, 'Merge replay changed result')
    large = merged[-1]
    status, content, _, _ = request('GET', '/api/file/content?id='+str(large['id'])+'&down=yes', token=token)
    check(status == 200 and content == payload, 'Large download hash mismatch')
    event('large_file_hash', bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(), id=large['id'])
    cancel_data = {**init,'hash':hashlib.md5(b'cancel').hexdigest(),'size':6,'name':'取消.bin'}
    cancelled = api('upload/init',token,cancel_data)['upload_id']
    api('upload/cancel', token, {'upload_id':cancelled})
    multipart('upload/chunk',token,'取消.bin',b'cancel',{'upload_id':cancelled,'index':0},ok=False)
    api('upload/merge',token,{'upload_id':cancelled},ok=False)
    api('upload/init',token,{**init,'name':'../escape.bin'},ok=False)
    sql('SELECT id,userid,size,name,state,received FROM backend_uploads WHERE id IN (%s,%s)',(upload_id,cancelled))


def dav():
    account = register('dav')
    token = account['token']
    api('file/dav/create',token,{'name':'HTTP rejected'},ok=False)
    credential = api('file/dav/create', token, {'name':'真实协议客户端','expire_days':1},tls=True)
    basic = (credential['public_id'], credential['password'])
    def send(method, path, body=None, headers=None, expected=200):
        result = request(method, path, body=body, headers=headers,tls=True,basic=basic)
        check(result[0] in ([expected] if isinstance(expected,int) else expected), f'DAV {method} expected {expected}, got {result[0]}')
        return result
    root='/dav/files/'+quote('协议DAV-'+secrets.token_hex(4))
    file=root+'/'+quote('雪-α.txt')
    data=('真实DAV字节\n'*40).encode()
    check(request('OPTIONS','/dav/')[0] == 403, 'DAV accepted non-TLS transport')
    send('PROPFIND','/dav/files/%2e%2e/secret',headers={'Depth':'0'},expected=400)
    status, _, _, headers=send('OPTIONS','/dav/')
    check(set(headers['Allow'].split(', ')) == set('OPTIONS PROPFIND GET HEAD PUT DELETE MKCOL MOVE COPY PROPPATCH LOCK UNLOCK'.split()),'DAV 12-method declaration differs')
    send('MKCOL',root,expected=201)
    send('PUT',file,data,expected=201)
    _, content, _, headers=send('GET',file)
    check(content==data,'DAV GET bytes mismatch')
    _, empty, _, hh=send('HEAD',file)
    check(not empty and int(hh['Content-Length'])==len(data),'DAV HEAD representation differs')
    _, ranged, _, _=send('GET',file,headers={'Range':'bytes=3-27'},expected=206)
    check(ranged==data[3:28],'DAV Range mismatch')
    send('PUT',file,b'forbidden',{'If-Match':'"wrong"'},expected=412)
    send('PUT',file,b'forbidden',{'If-None-Match':'*'},expected=412)
    find=b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
    for depth, count in [('0',1),('1',2)]:
        _, xml, _, _=send('PROPFIND',root,find,{'Depth':depth},expected=207)
        check(len(ET.fromstring(xml).findall('{DAV:}response'))==count,'DAV Depth count mismatch')
    send('PROPFIND',root,find,{'Depth':'infinity'},expected=403)
    send('PROPFIND',root,b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>',{'Depth':'0'},expected=400)
    lock=b'<d:lockinfo xmlns:d="DAV:"><d:lockscope><d:exclusive/></d:lockscope><d:locktype><d:write/></d:locktype><d:owner>protocol</d:owner></d:lockinfo>'
    _,_,_,lh=send('LOCK',file,lock,{'Depth':'0','Timeout':'Second-300'})
    lock_token=lh['Lock-Token']
    send('PUT',file,b'forbidden',expected=423)
    send('PUT',file,b'forbidden',{'If':'(<opaquelocktoken:invalid>)'},expected=412)
    send('PUT',file,data,{'If':'('+lock_token+')'},expected=204)
    send('LOCK',file,b'',{'If':'('+lock_token+')','Timeout':'Second-300'})
    send('UNLOCK',file,headers={'Lock-Token':'<opaquelocktoken:invalid>'},expected=409)
    send('UNLOCK',file,headers={'Lock-Token':lock_token},expected=204)
    patch=b'<d:propertyupdate xmlns:d="DAV:" xmlns:q="urn:protocol"><d:set><d:prop><q:label>unicode</q:label><q:complex lang="zh"><q:child>nested</q:child></q:complex></d:prop></d:set></d:propertyupdate>'
    _,xml,_,_=send('PROPPATCH',file,patch,expected=207)
    check('200 OK' in xml.decode(),'PROPPATCH property not accepted')
    _,xml,_,_=send('PROPFIND',file,find,{'Depth':'0'},expected=207)
    check(ET.fromstring(xml).find('.//{urn:protocol}label').text=='unicode','Custom DAV property lost')
    check(ET.fromstring(xml).find('.//{urn:protocol}complex/{urn:protocol}child').text=='nested','Complex DAV property lost')
    copied=root+'/'+quote('复制.txt')
    moved=root+'/'+quote('移动.txt')
    send('COPY',file,headers={'Destination':ARGS.https+copied,'Depth':'0'},expected=201)
    send('COPY',file,headers={'Destination':ARGS.https+copied,'Overwrite':'F'},expected=412)
    send('MOVE',copied,headers={'Destination':ARGS.https+moved},expected=201)
    check(send('GET',moved)[1]==data,'MOVE/COPY payload differs')
    _,xml,_,_=send('PROPFIND',moved,find,{'Depth':'0'},expected=207)
    check(ET.fromstring(xml).find('.//{urn:protocol}label').text=='unicode' and ET.fromstring(xml).find('.//{urn:protocol}complex/{urn:protocol}child').text=='nested','MOVE/COPY properties lost')
    send('GET',copied,expected=404)
    send('DELETE',moved,expected=204)
    send('DELETE',root,expected=204)
    # Concurrent authenticated HTTP revoke and DAV PUT exercise the lock-order boundary.
    with ThreadPoolExecutor(max_workers=2) as pool:
        put=pool.submit(request,'PUT','/dav/files/protocol-race-'+secrets.token_hex(4)+'.txt',body=data,tls=True,basic=basic)
        revoke=pool.submit(api,'file/dav/revoke',token,{'id':credential['id']})
        result=put.result(); revoke.result()
    check(result[0] in (201,401),'Concurrent revoke/PUT produced server failure')
    send('OPTIONS','/dav/',expected=401)
    sql('SELECT id,userid,revoked_at,last_used_at FROM webdav_credentials WHERE id=%s',(credential['id'],))
    event('dav_complete', methods=12, sha256=hashlib.sha256(data).hexdigest(), tls='Project-only trusted self-signed SAN certificate')


def ws_frames(socket, label, seconds, until=None):
    frames=[]
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            frame=json.loads(socket.recv(timeout=max(0.01,deadline-time.monotonic())))
        except TimeoutError:
            break
        event('ws_frame', client=label, direction='in',frame=frame)
        frames.append(frame)
        if until and until(frame):
            break
    return frames


def websocket():
    a,b=register('ws-owner'),register('ws-member')
    wsbase=ARGS.base.replace('http://','ws://').replace('https://','wss://')
    def opening(user,label):
        socket=connect(wsbase+'/ws?action=web&token='+quote(user['token']),open_timeout=15)
        frame=ws_frames(socket,label,15,lambda f:f.get('type')=='open')[0]
        check(frame['type']=='open' and frame['data']['ud']==user['userid'],'WS open/fd identity differs')
        return socket,frame['data']['fd']
    project=api('project/add',a['token'],{'name':'真实WS项目'+secrets.token_hex(3)})
    pid,did=project['id'],project['dialog_id']
    api('project/user',a['token'],{'project_id':pid,'userid':[a['userid'],b['userid']]})
    sa,afd=opening(a,'owner'); sb,bfd=opening(b,'member')
    try:
        message=api('dialog/msg/sendtext',a['token'],{'dialog_id':did,'text':'no-receipt-'+RUN,'text_type':'md'},headers={'fd':str(afd)})
        frames=ws_frames(sb,'member',30,lambda f:f.get('type')=='dialog' and f.get('mode')=='add' and f.get('data',{}).get('id')==message['id'])
        target=next(f for f in frames if f.get('mode')=='add' and f.get('data',{}).get('id')==message['id'])
        later=ws_frames(sb,'member',18)
        check(not any(f.get('msgId')==target['msgId'] for f in later),'Unreceipted message repeated after 15s')
        rows=sql('SELECT userid,fd,msg_id,transport_sent_at,acknowledged_at FROM backend_deliveries WHERE msg_id=%s',(target['msgId'],))
        check(rows[0]['transport_sent_at'] is not None and rows[0]['acknowledged_at'] is None,'Transport completion was falsely ACKed')
        sb.close()
        # Populate solely through 201 real business HTTP requests, never SQL injection.
        for index in range(201):
            api('dialog/msg/sendtext',a['token'],{'dialog_id':did,'text':f'revoked-private-{index}-{RUN}','text_type':'md'},headers={'fd':str(afd)})
        query="SELECT COUNT(*) AS n FROM backend_deliveries WHERE userid=%s AND transport_sent_at IS NULL AND discarded_at IS NULL AND JSON_UNQUOTE(JSON_EXTRACT(scope,'$.dialog_id'))=%s"
        wait_db(query,(b['userid'],str(did)),lambda rows:rows[0]['n']>=200)
        api('project/user',a['token'],{'project_id':pid,'userid':[a['userid']]})
        api('project/one',b['token'],{'project_id':pid},ok=False)
        api('dialog/one',b['token'],{'dialog_id':did},ok=False)
        direct=api('dialog/open/user',a['token'],{'userid':b['userid']})
        marker=api('dialog/msg/sendtext',a['token'],{'dialog_id':direct['id'],'text':'authorized-after-tombstone-'+RUN,'text_type':'md'})
        sb,bfd=opening(b,'member-reconnected')
        frames=ws_frames(sb,'member-reconnected',45)
        check(not any('revoked-private-' in json.dumps(f) for f in frames),'Revoked private content leaked')
        check(any(f.get('mode')=='groupExit' and f.get('data',{}).get('id')==did for f in frames),'200 denied entries blocked tombstone')
        check(any(f.get('mode')=='add' and f.get('data',{}).get('id')==marker['id'] for f in frames),'Denied head blocked other conversation')
        rows=sql("SELECT discard_reason,COUNT(*) AS n,SUM(acknowledged_at IS NOT NULL) AS acked,SUM(transport_sent_at IS NOT NULL) AS sent FROM backend_deliveries WHERE userid=%s AND JSON_UNQUOTE(JSON_EXTRACT(scope,'$.dialog_id'))=%s GROUP BY discard_reason",(b['userid'],str(did)))
        check(any(r['discard_reason']=='scope_revoked' and r['n']>=200 and not r['acked'] and not r['sent'] for r in rows),'Revoked queue retirement misclassified')
        api('users/logout',b['token'])
        try:
            ws_frames(sb,'member-logout',10)
            check(False,'Logout did not close WebSocket')
        except ConnectionClosed as exc:
            event('ws_closed',reason='logout',code=exc.code)
        api('users/info',b['token'],ok=False)
        admin_path=Path(ARGS.admin_token)
        check(admin_path.is_file(),'Missing private admin token for disable case')
        admin=admin_path.read_text(encoding='utf-8-sig').strip(); SECRETS.append(admin)
        api('users/operation',admin,{'userid':a['userid'],'type':'setdisable','disable_time':datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})
        try:
            ws_frames(sa,'owner-disabled',10)
            check(False,'Disable did not close WebSocket')
        except ConnectionClosed as exc:
            event('ws_closed',reason='disable',code=exc.code)
        api('users/info',a['token'],ok=False)
    finally:
        sa.close(); sb.close()


def external():
    for name,prerequisite in [('Office/nginx','Real Office JWT/config, nginx DNS and reverse proxy absent'),('AI SSE','Real AI/appstore plugin and provider configuration absent'),('Manticore','Real Manticore/embedding service and index absent')]:
        RESULTS.append({'id':name,'status':'NOT_RUN','blocker':prerequisite})
    status,_,result,_=request('GET','/ai/invoke/stream/protocol-invalid')
    event('external_probe', http=status, caveat='Invalid stream capability rejection is not external integration success')
    check(status in (401,403) or status == 200 and isinstance(result,dict) and result.get('ret') != 1, 'Invalid SSE capability must return a controlled authentication error, not a server exception')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:18080')
    parser.add_argument('--https',default='https://127.0.0.1:18443')
    parser.add_argument('--cert',default=str(ROOT/'.runtime/protocol-cert.pem'))
    parser.add_argument('--admin-token',default=str(ROOT/'.runtime/protocol-admin-token'))
    parser.add_argument('--only',default='zip_first,uploads,dav,websocket,routes,external')
    ARGS=parser.parse_args()
    TLS=ssl.create_default_context(cafile=ARGS.cert)
    started=time.monotonic()
    event('provenance', python=sys.version, source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),ROOT/'backend/main.py',ROOT/'backend/realtime.py',ROOT/'backend/dav.py',ROOT/'backend/auth.py',ROOT/'backend/crypto.py',ROOT/'backend/jobs.py',ROOT/'backend/domains/upload.py',ROOT/'backend/domains/files_jobs.py',ROOT/'backend/domains/files_support.py',ROOT/'backend/domains/filedav.py']})
    for name in ARGS.only.split(','):
        CURRENT=name
        begin=time.monotonic()
        try:
            globals()[name]()
            RESULTS.append({'id':name,'status':'PASS','elapsed_seconds':time.monotonic()-begin})
        except Exception as exc:
            RESULTS.append({'id':name,'status':'FAIL','error':str(exc),'traceback':traceback.format_exc(),'elapsed_seconds':time.monotonic()-begin})
        flush()
    failed=any(r['status']=='FAIL' for r in RESULTS)
    event('execution_end', exit_code=int(failed),elapsed_seconds=time.monotonic()-started)
    flush()
    print(json.dumps(clean({'evidence':str(OUT/(RUN+'.json')),'results':RESULTS}),ensure_ascii=False))
    raise SystemExit(int(failed))
