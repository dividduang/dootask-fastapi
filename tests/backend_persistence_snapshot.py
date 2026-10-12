"""Read-only isolated persistence snapshot; label argument selects evidence filename."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
import pymysql

root = Path(__file__).resolve().parents[1]
assert os.environ['DB_DATABASE'] == 'dootask_fastapi' and int(os.environ['DB_PORT']) == 13306
core = json.loads((root / '.runtime/backend-core-state.json').read_text())
result = {'at': datetime.now(timezone.utc).isoformat(), 'database': 'dootask_fastapi', 'port': 13306}
with pymysql.connect(host=os.environ['DB_HOST'],port=13306,user=os.environ['DB_USERNAME'],password=os.environ['DB_PASSWORD'],database='dootask_fastapi',cursorclass=pymysql.cursors.DictCursor) as connection:
    with connection.cursor() as c:
        for name, ids, columns in [('projects',[core['project_id'],7],'id,name,userid'),('project_tasks',[core['task_id'],5],'id,project_id,name,dialog_id'),('files',[core['file_id'],6],'id,name,ext,size,hash')]:
            c.execute('SELECT '+columns+' FROM '+name+' WHERE id IN (%s,%s) ORDER BY id',ids)
            result[name] = c.fetchall()
        result['file_bytes'] = []
        for item in result['files']:
            c.execute('SELECT content FROM file_contents WHERE fid=%s ORDER BY id DESC LIMIT 1',(item['id'],))
            metadata = json.loads(c.fetchone()['content'])
            blob = root / '.runtime/storage' / metadata['url']
            result['file_bytes'].append({'id':item['id'],'sha256':hashlib.sha256(blob.read_bytes()).hexdigest(),'bytes':blob.stat().st_size})
        c.execute('SELECT kind,COUNT(*) n,SUM(completed_at IS NOT NULL) completed,MAX(attempts) attempts,MAX(last_error) last_error FROM backend_outbox GROUP BY kind ORDER BY kind')
        result['outbox'] = c.fetchall()
        c.execute("SELECT userid,email,bot FROM users WHERE email LIKE '%%@bot.system' ORDER BY userid")
        result['system_bots'] = c.fetchall()
(root / 'docs/evidence/qa-backend' / ('persistence-'+sys.argv[1]+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print('Read-only persistence snapshot captured:',sys.argv[1])
