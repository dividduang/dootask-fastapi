"""Attendance settings, record grouping and face-device HTTP integration."""
import base64
import calendar
import json
import os
import re
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from sqlalchemy import select, delete
from .. import db
from ..errors import ApiError, success
from ..storage import confined_path
from .account_common import *
from .apps import require_installed


def save_macs(c,userid,value):
    value=list(value.values()) if isinstance(value,dict) else value
    value=value if isinstance(value,list) else []; normalized={}
    for item in value:
        if not isinstance(item,dict): continue
        mac=text(item,'mac').upper()
        if re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}',mac): normalized[mac]=text(item,'remark')[:50]
    t=db.tables['user_checkin_macs']; kept=[]
    for mac,remark in normalized.items():
        if c.execute(select(t.c.id).where(t.c.mac==mac,t.c.userid!=userid)).first(): raise ApiError(mac+' 已被其他成员设置')
        found=row(c,'user_checkin_macs',userid=userid,mac=mac)
        if found:
            if remark: update(c,'user_checkin_macs',{'id':found['id']},{'remark':remark})
            kept.append(found['id'])
        else: kept.append(db.insert_row(c,'user_checkin_macs',userid=userid,mac=mac,remark=remark))
    c.execute(delete(t).where(t.c.userid==userid,t.c.id.not_in(kept)))
    return success(rows(c,select(t).where(t.c.id.in_(kept)).order_by(t.c.id)),'修改成功')


def face_request(settings,path,payload):
    require_installed(settings,'face')
    endpoint=os.environ.get('FACE_URL','http://face:7788').rstrip('/')+path
    try:
        with urlopen(Request(endpoint,json.dumps(payload).encode(),{'Content-Type':'application/json'},method='POST'),timeout=15) as response:
            result=json.load(response)
    except (OSError,ValueError) as exc:
        raise ApiError('人脸服务请求失败') from exc
    if not isinstance(result,dict) or result.get('ret')!=1: raise ApiError(result.get('msg','人脸服务响应错误') if isinstance(result,dict) else '人脸服务响应错误')
    return result


def save_face(c,ctx,user,faceimg,remark,settings):
    relative=faceimg.removeprefix(ctx.base_url).lstrip('/')
    payload={'name':user.get('nickname') or user['email'],'enrollid':user['userid'],'admin':0,'backupnum':50}
    if relative:
        from urllib.parse import urlparse, parse_qs
        parsed=urlparse(faceimg)
        key=parse_qs(parsed.query).get('key',[''])[0]
        download=row(c,'backend_file_downloads',id=key) if parsed.path.endswith('/api/file/bytes') else None
        if not download or download['userid']!=ctx.actor.userid or download['expires_at']<=utcnow():
            raise ApiError('图片无访问权限')
        payload['record']=base64.b64encode(confined_path(settings.storage_root,download['path']).read_bytes()).decode()
    face_request(settings,'/user',payload)
    if not relative: face_request(settings,'/user/delete',{'enrollid':user['userid'],'backupnum':50})
    upsert(c,'user_checkin_faces',{'userid':user['userid']},{'faceimg':relative,'status':1,'remark':remark})
    return success(message='设置成功')


def checkin_get(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_checkin_macs']; face=row(c,'user_checkin_faces',userid=a.userid)
    return success({'list':rows(c,select(t).where(t.c.userid==a.userid).order_by(t.c.id)),'faceimg':url(ctx,face['faceimg']) if face else ''})


def checkin_save(c,ctx,data,settings):
    a=actor(c,ctx); config=db.setting(c,'checkinSetting'); kind=text(data,'type')
    if config.get('open')!='open': raise ApiError('此功能未开启，请联系管理员开启')
    result={'list':data.get('list'),'faceimg':data.get('faceimg')}
    if kind=='face':
        if config.get('face_upload')!='open': raise ApiError('未开放修改权限，请联系管理员')
        save_face(c,ctx,a.user,text(data,'faceimg'),'用户上传',settings)
    elif kind=='mac':
        if config.get('edit')!='open': raise ApiError('未开放修改权限，请联系管理员')
        value=data.get('list')
        if not isinstance(value,list) or not value: raise ApiError('参数错误')
        valid={text(x,'mac').upper() for x in value if isinstance(x,dict) and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',text(x,'mac'))}
        if len(valid)>3: raise ApiError('最多只能添加3个MAC地址')
        result['list']=save_macs(c,a.userid,value)['data']
    else: raise ApiError('参数错误')
    return success(result,'修改成功')


def record_times(c,userid,start,end):
    t=db.tables['user_checkin_records']; result={}
    for value in rows(c,select(t).where(t.c.userid==userid,t.c.created_at.between(start,end)).order_by(t.c.id)):
        day=str(value['date']); result.setdefault(day,set()).update(str(x)[:5] for x in decoded(value['times'],[]) if x)
    return {k:sorted(v) for k,v in result.items()}


def time_sections(times,diff=3600):
    start=end=''; result=[]
    for value in times:
        value=value[:5]
        if not start: start=value; continue
        if not end: end=value; continue
        seconds=lambda s:(int(s[:2])*60+int(s[3:5]))*60
        if seconds(value)-seconds(end)>diff:
            result.append([start,end]); start=value; end=''
        else: end=value
    if start: result.append([start,end])
    return result


def checkin_list(c,ctx,data,settings):
    a=actor(c,ctx)
    try: month=datetime.strptime(text(data,'ym'),'%Y-%m')
    except ValueError as exc: raise ApiError('日期选择错误') from exc
    end=month.replace(day=calendar.monthrange(month.year,month.month)[1],hour=23,minute=59,second=59)
    back=max(0,min(3,integer(data.get('before')))); index=month.year*12+month.month-1-back
    start=datetime(index//12,index%12+1,1)
    return success([{'date':day,'section':time_sections(times)} for day,times in sorted(record_times(c,a.userid,start,end).items())])


ROUTES={'users/checkin/get':checkin_get,'users/checkin/save':checkin_save,'users/checkin/list':checkin_list}
