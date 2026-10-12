"""Runnable consumer-boundary checks; deliberately not executed during implementation.
Run: python -m tests.test_accounts_contract
These are algorithm/shape checks, not DB/SMTP/LDAP acceptance evidence.
"""
import base64
import hashlib
import hmac
import struct
import zlib
from backend.domains import accounts, system
from backend.domains.account_checkin import time_sections
from backend.domains.account_history import preview_message
from backend.domains.account_import import parse_sheet
from backend.domains.account_meetings import agora_token
from backend.domains.account_common import ids
from backend.context import RequestContext
from backend.errors import ApiError


def main():
    assert accounts.normalize_sorts({'base':[' x ','x',42,''],'admin':[' a ']}) == {'base':['x'],'admin':['a']}
    assert ids(',1,2,1,not-id,') == [1,2]
    assert time_sections(['09:00:01','09:15:00','12:00:00','12:10:00']) == [['09:00','09:15'],['12:00','12:10']]
    assert time_sections(['09:00','12:00']) == [['09:00','12:00']]
    assert parse_sheet([['headers'],['a@b.com','测试人员','Password1','工程师'],['','','','ignored']]) == [{'line':2,'email':'a@b.com','nickname':'测试人员','password':'Password1','profession':'工程师'}]
    priorities=system.normalize_priority([{'name':'A','color':'red','priority':1},{'name':'B','color':'blue','priority':2,'default':True},{'name':'C','color':'black','priority':3,'default':True}])
    assert [x['is_default'] for x in priorities] == [0,1,0]
    assert system.normalize_apps([{'id':'test','menu':{'url':'/app'},'visible_to':'1,2'}])[0]['visible_to']==['1','2']
    assert preview_message({'type':'text','msg':{'text':'a'*51}})=='a'*50+'...'
    assert preview_message({'type':'template','msg':{'title_raw':'literal'}})=='literal'
    appid='a'*32; cert='b'*32; channel='DooTask:contract'; uid=42; expiry=1800000000
    token=agora_token(appid,cert,channel,uid,salt=17,timestamp=expiry)
    assert token.startswith('006'+appid)
    packed=base64.b64decode(token[35:]); size=struct.unpack('<H',packed[:2])[0]
    signature=packed[2:2+size]; crc_channel,crc_uid,msg_size=struct.unpack('<IIH',packed[2+size:12+size]); message=packed[12+size:]
    assert crc_channel==zlib.crc32(channel.encode()) and crc_uid==zlib.crc32(str(uid).encode())
    assert len(message)==msg_size and struct.unpack('<IIH',message)==(17,expiry,0)
    assert hmac.compare_digest(signature,hmac.digest(cert.encode(),appid.encode()+channel.encode()+str(uid).encode()+message,'sha256'))
    try:
        system.historical_ai(None,RequestContext(request_id='contract',version='1.4.34'),{},None)
    except ApiError:
        pass
    else:
        raise AssertionError('Historical AI route must reject old clients')
    assert system.historical_ai(None,RequestContext(request_id='contract',version='1.4.35'),{},None) is None
    assert len(accounts.ROUTES)==60 and len(system.ROUTES)==30
    assert 'users/login' not in accounts.ROUTES
    assert system.RAW_ROUTES >= {'system/version','system/get/ip','system/prefetch'}


if __name__=='__main__':
    main()
