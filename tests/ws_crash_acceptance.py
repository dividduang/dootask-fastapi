"""Real send-before-commit crash recovery across owned ASGI processes.
Uses a Python trace breakpoint, never a mocked handler or transport. Only the
additional loopback 18081 process is terminated; 18080/18443 remain untouched.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
for name in ('runtime.env','backend.env'):
    for line in (ROOT/'.runtime'/name).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1);os.environ[key]=value
OUT=ROOT/'docs/evidence/qa-backend'
ARM=ROOT/'.runtime/ws-crash-arm'
MARKER=ROOT/'.runtime/ws-crash-breakpoint.json'

if '--server' in sys.argv:
    target=int(os.environ['QA_CRASH_USERID'])
    def breakpoint(frame, event, arg):
        if event == 'call' and frame.f_globals.get('__name__') == 'backend.realtime' and frame.f_code.co_name == '_transport_sent' and frame.f_locals.get('userid') == target and ARM.exists():
            MARKER.write_text(json.dumps({'userid':target,'fd':frame.f_locals['fd'],'msg_id':frame.f_locals['msg_id'],'pid':os.getpid(),'location':'_transport_sent entry after successful ASGI send; no DB sent commit yet'}),encoding='utf-8')
            threading.Event().wait()
        return None
    threading.settrace(breakpoint)
    import uvicorn
    uvicorn.run('backend.main:create_app',factory=True,host='127.0.0.1',port=18081,access_log=False,proxy_headers=False)
    raise SystemExit()

from tests import protocol_acceptance as p
from websockets.sync.client import connect
p.ARGS=SimpleNamespace(base='http://127.0.0.1:18080',https='https://127.0.0.1:18443')
p.CURRENT='WS-SEND-COMMIT-PROCESS-CRASH'
process=None
sockets=[]
try:
    assert not ARM.exists() and not MARKER.exists(), 'Refuse stale fault markers'
    try:
        urlopen('http://127.0.0.1:18081/health',timeout=1)
    except Exception:
        pass
    else:
        raise AssertionError('Port18081 already in use')
    sender=p.register('crash-sender');receiver=p.register('crash-receiver')
    dialog=p.api('dialog/open/user',sender['token'],{'userid':receiver['userid']})
    env=os.environ.copy();env['QA_CRASH_USERID']=str(receiver['userid'])
    command=[sys.executable,'-m','tests.ws_crash_acceptance','--server']
    with (OUT/'ws-crash-process.log').open('wb') as logfile:
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=logfile,stderr=subprocess.STDOUT)
    p.event('owned_process_started',pid=process.pid,command=command)
    deadline=time.monotonic()+30
    while True:
        try:
            urlopen('http://127.0.0.1:18081/health',timeout=1);break
        except Exception:
            assert process.poll() is None and time.monotonic()<deadline
            time.sleep(.2)
    socket=connect('ws://127.0.0.1:18081/ws?action=web&token='+quote(receiver['token']),open_timeout=15)
    sockets.append(socket)
    p.ws_frames(socket,'before-crash',2)
    ARM.write_text('armed',encoding='utf-8')
    message=p.api('dialog/msg/sendtext',sender['token'],{'dialog_id':dialog['id'],'text':'real-crash-window-'+p.RUN,'text_type':'md'})
    frames=p.ws_frames(socket,'before-crash',15,lambda f:f.get('data',{}).get('id')==message['id'])
    delivered=next(f for f in frames if f.get('data',{}).get('id')==message['id'])
    deadline=time.monotonic()+10
    while not MARKER.exists():
        assert time.monotonic()<deadline;time.sleep(.05)
    marker=json.loads(MARKER.read_text(encoding='utf-8'))
    assert marker['msg_id']==delivered['msgId']
    rows=p.sql('SELECT msg_id,transport_sent_at,acknowledged_at FROM backend_deliveries WHERE msg_id=%s',(marker['msg_id'],))
    assert rows and rows[0]['transport_sent_at'] is None and rows[0]['acknowledged_at'] is None
    p.event('breakpoint_observed',**marker)
    killed=subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True,check=True)
    process.wait(timeout=15)
    p.event('owned_process_terminated',pid=process.pid,exit_code=process.returncode,stdout=killed.stdout.decode(errors='replace'))
    recovered=connect('ws://127.0.0.1:18080/ws?action=web&token='+quote(receiver['token']),open_timeout=15)
    sockets.append(recovered)
    p.ws_frames(recovered,'other-worker-open',5,lambda f:f.get('type')=='open')
    frames=p.ws_frames(recovered,'other-worker-recovery',120,lambda f:f.get('data',{}).get('id')==message['id'])
    duplicate=next(f for f in frames if f.get('data',{}).get('id')==message['id'])
    assert duplicate['msgId']==marker['msg_id']
    rows=p.wait_db('SELECT msg_id,transport_sent_at,acknowledged_at FROM backend_deliveries WHERE msg_id=%s',(marker['msg_id'],),lambda rows:bool(rows and rows[0]['transport_sent_at']),timeout=10)
    assert rows[0]['acknowledged_at'] is None
    p.event('result',status='PASS',same_message_redelivered=True,false_ack=False,lease_recovery_bound_seconds=120)
finally:
    for socket in sockets:
        socket.close()
    if process is not None and process.poll() is None:
        subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True)
    ARM.unlink(missing_ok=True);MARKER.unlink(missing_ok=True)
    (OUT/'ws-crash-recovery.json').write_text(json.dumps(p.clean(p.EVENTS),ensure_ascii=False,indent=2),encoding='utf-8')
print('Real cross-process send-before-commit crash recovery passed')
