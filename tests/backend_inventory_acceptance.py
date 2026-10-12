"""Real local app inventory transport checks; load private runtime env first."""
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from backend.config import Settings

root = Path(__file__).resolve().parents[1]
settings = Settings.from_env()
account = json.loads((root / '.runtime/qa-accounts.json').read_text(encoding='utf-8'))['admin']
url = 'http://127.0.0.1:18080/appstore/api/v1/internal/installed'
try:
    urlopen(url, timeout=10)
except HTTPError as error:
    assert error.code == 401
    assert json.load(error)['ret'] == -1
else:
    raise AssertionError('Anonymous registry access must be rejected')
with urlopen(Request(url, headers={'Token':account['token']}), timeout=10) as response:
    data = json.load(response)
    assert response.status == 200 and data['code'] == 200
assert isinstance(data['data'], list)
assert len({item['id'] for item in data['data']}) == len(data['data'])
for item in data['data']:
    assert set(item) == {'id','name','version','menu_items'}
    assert isinstance(item['menu_items'], list)
if not (settings.appstore_root / 'config').exists():
    assert data['data'] == [], 'Absent installation registry must report no installed packages'
result = {'authenticated_status':200,'anonymous_status':401,'registry_exists':(settings.appstore_root/'config').exists(),
          'installed_count':len(data['data']),'result':'PASS','scope':'Actual local installation inventory, not external installer/hooks'}
(root/'docs/evidence/qa-backend/local-app-inventory.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print('Real authenticated local app inventory checks passed')
