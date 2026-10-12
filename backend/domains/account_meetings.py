"""Bots and meeting access tokens, ported from the checked-in Agora 006 encoder."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import zlib
from sqlalchemy import select, func
from .. import auth, db
from ..errors import ApiError, success
from ..context import utcnow
from .account_common import *
from .dialog_core import ensure_user_dialog, send_message

SYSTEM_BOTS={'system-msg':'系统消息','task-alert':'任务提醒','todo-alert':'待办提醒','check-in':'签到打卡','anon-msg':'匿名消息','approval-alert':'审批','ai-openai':'ChatGPT','ai-claude':'Claude','ai-deepseek':'DeepSeek','ai-gemini':'Gemini','ai-grok':'Grok','ai-ollama':'Ollama','ai-zhipu':'智谱清言','ai-qianwen':'通义千问','ai-wenxin':'文心一言','ai-dooai':'Doo AI','bot-manager':'机器人管理','meeting-alert':'会议通知','okr-alert':'OKR提醒'}


def bot_get(c,key):
    if key not in SYSTEM_BOTS:
        raise ApiError('系统机器人不存在')
    user=row(c,'users',email=key+'@bot.system')
    if not user:
        from ..context import RequestContext
        auth.creation_lock(c)
        user=row(c,'users',email=key+'@bot.system')
        if not user:
            user=auth.create_account(c,RequestContext(request_id='system-bot:'+key),key+'@bot.system',secrets.token_urlsafe(24),
                nickname=SYSTEM_BOTS[key],bot=True,email_verity=True)
    if not user['bot']:
        raise ApiError('机器人身份冲突')
    if auth.is_disabled(user):
        raise ApiError('机器人已停用')
    return user


def webhook_events(value,fallback=True):
    value=decoded(value,[]) if isinstance(value,str) else value
    if not isinstance(value,list): value=[value]
    result=[x for x in value if x in ('message','dialog_open','member_join','member_leave')]
    return result or (['message'] if fallback else [])


def bot_info_data(c,a,botid):
    user=row(c,'users',userid=botid,bot=1)
    if not user: raise ApiError('机器人不存在')
    bot=row(c,'user_bots',userid=a.userid,bot_id=botid)
    system=SYSTEM_BOTS.get(user['email'].split('@')[0],'')
    if not bot:
        if not system: raise ApiError('不是你的机器人')
        auth.require_admin(a)
    return user,bot,system


def formatted_bot(c,ctx,user,bot,system):
    safe=safe_user(c,user,ctx)
    return {'id':user['userid'],'name':safe['nickname'],'avatar':safe['userimg'],'clear_day':bot['clear_day'] if bot else 0,'webhook_url':bot['webhook_url'] if bot else '', 'webhook_events':webhook_events(bot['webhook_events'] if bot else None),'system_name':system}


def bot_list(c,ctx,data,settings):
    a=actor(c,ctx); b=db.tables['user_bots']; result=[]
    for bot in rows(c,select(b).where(b.c.userid==a.userid).order_by(b.c.id.desc())):
        user=row(c,'users',userid=bot['bot_id'])
        if user: result.append(formatted_bot(c,ctx,user,bot,SYSTEM_BOTS.get(user['nickname'],'')))
    return success({'list':result})


def bot_info(c,ctx,data,settings):
    a=actor(c,ctx); user,bot,system=bot_info_data(c,a,integer(data.get('id')))
    return success(formatted_bot(c,ctx,user,bot,system))


def bot_edit(c,ctx,data,settings):
    from .accounts import mutate_user
    a=actor(c,ctx); botid=integer(data.get('id'))
    if not botid:
        b=db.tables['user_bots']
        if c.execute(select(func.count()).select_from(b).where(b.c.userid==a.userid)).scalar_one()>=50: raise ApiError('超过最大创建数量。')
        if not 2<=len(text(data,'name').encode())<=20: raise ApiError('机器人名称由2-20个字符组成。')
        user=auth.create_account(c,ctx,secrets.token_hex(12)+'@bot.system',secrets.token_urlsafe(24),
            nickname=text(data,'name'),bot=True,email_verity=True)
        botid=user['userid']
        db.insert_row(c,'user_bots',userid=a.userid,bot_id=botid)
    user,bot,system=bot_info_data(c,a,botid); changes={}
    if 'name' in data: changes['nickname']=text(data,'name')
    if 'avatar' in data:
        value=data['avatar']; value=value[0].get('path','') if isinstance(value,list) and value else value
        changes['userimg']='' if 'avatar/' in str(value) else str(value or '')
    if changes: user=mutate_user(c,ctx,user,changes)
    if bot:
        changes={}
        if 'clear_day' in data: changes['clear_day']=min(999,max(1,integer(data['clear_day'])))
        if 'webhook_url' in data: changes['webhook_url']=text(data,'webhook_url')
        if 'webhook_events' in data: changes['webhook_events']=json.dumps(webhook_events(data['webhook_events'],False))
        if changes: update(c,'user_bots',{'id':bot['id']},changes); bot.update(changes)
    return success(formatted_bot(c,ctx,user,bot,system),'修改成功')


def bot_delete(c,ctx,data,settings):
    from .account_admin import delete_user
    a=actor(c,ctx); remark=text(data,'remark')
    if not remark: raise ApiError('请输入删除备注')
    if len(remark)>255: raise ApiError('删除备注长度限制255个字')
    user,bot,system=bot_info_data(c,a,integer(data.get('id')))
    if not bot: raise ApiError('系统机器人不能删除')
    delete_user(c,ctx,user,remark); return success(message='删除成功')


def search_ai(c,ctx,data,settings):
    actor(c,ctx); key='ai-'+text(data,'type')
    if key not in SYSTEM_BOTS: raise ApiError('AI机器人不存在')
    return success(safe_user(c,bot_get(c,key),ctx))


def cache(settings):
    import redis
    return redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_timeout=5)


def share_info(code,settings):
    if not code: return None
    client=cache(settings)
    try: return decoded(client.get('dootask:meeting:share:'+code),None) or None
    finally: client.close()


def agora_token(appid,certificate,channel,uid,*,salt=None,timestamp=None):
    salt=secrets.randbelow(100001) if salt is None else salt
    timestamp=int(time.time())+168*3600 if timestamp is None else timestamp
    # AgoraTokenGenerator::buildToken does not call initPrivilege: preserve empty privilege map.
    message=struct.pack('<IIH',salt,timestamp,0)
    uid=str(uid) if uid else ''
    signature=hmac.digest(certificate.encode(),appid.encode()+channel.encode()+uid.encode()+message,'sha256')
    content=struct.pack('<H',len(signature))+signature+struct.pack('<IIH',zlib.crc32(channel.encode()),zlib.crc32(uid.encode()),len(message))+message
    return '006'+appid+base64.b64encode(content).decode()


def invitations(c,ctx,meeting,userids):
    result=[]
    for userid in ids(userids):
        if not row(c,'users',userid=userid): continue
        bot=bot_get(c,'meeting-alert'); dialog=ensure_user_dialog(c,bot['userid'],userid)
        # Source attributes the meeting invitation to the initiating user even in bot DM.
        result.append(send_message(c,dialog_id=dialog,sender_id=ctx.actor.userid,msg_type='meeting',msg=dict(meeting),context=ctx,trusted_sender=True))
    return result


def meeting_open(c,ctx,data,settings):
    kind=text(data,'type'); code=text(data,'sharekey'); meetingid=text(data,'meetingid').replace(' ',''); a=None
    shared=share_info(code,settings) if code and kind=='join' else None
    if code and kind=='join':
        if not shared or shared['meetingid']!=meetingid: raise ApiError('分享链接已过期')
    else: a=actor(c,ctx)
    config=db.setting(c,'meetingSetting')
    if config.get('open')!='open': raise ApiError('会议功能未开启，请联系管理员开启')
    if not config.get('appid') or not config.get('app_certificate'): raise ApiError('会议功能配置错误，请联系管理员')
    if kind=='join':
        meeting=row(c,'meetings',meetingid=meetingid)
        if not meeting: raise ApiError('频道ID不存在')
        if meeting['end_at']: raise ApiError('会议已结束')
        update(c,'meetings',{'id':meeting['id']},{})
    elif kind=='create':
        meetingid=''.join(secrets.choice('0123456789') for _ in range(11))
        channel='DooTask:'+hashlib.md5((meetingid+settings.app_key).encode(),usedforsecurity=False).hexdigest()[16:]
        mid=db.insert_row(c,'meetings',meetingid=meetingid,name=text(data,'name') or f"{a.user['nickname']} 发起的会议",channel=channel,userid=a.userid)
        meeting=row(c,'meetings',id=mid)
    else: raise ApiError('参数错误')
    uid=int(str(secrets.randbelow(100000)).zfill(5).rjust(6,'9')+str(a.userid)) if a else int(str(secrets.randbelow(10000)).zfill(4).rjust(9,'8'))
    result=dict(meeting,appid=config['appid'],uid=uid,token=agora_token(config['appid'],config['app_certificate'],meeting['channel'],uid),msgs=invitations(c,ctx,meeting,data.get('userids',[])) if kind=='create' else [])
    safe=safe_user(c,a.user,ctx) if a else {}
    result['nickname']=text(data,'username') if code else safe['nickname']
    result['userimg']=(text(data,'userimg') or url(ctx,'avatar/'+result['nickname']+'.png')) if code else safe['userimg']
    client=cache(settings)
    try: client.set('dootask:meeting:tourist:'+str(uid),json.dumps({k:result[k] for k in ('uid','userimg','nickname')},ensure_ascii=False),ex=21600)
    finally: client.close()
    return success(result)


def meeting_link(c,ctx,data,settings):
    meetingid=text(data,'meetingid'); shared=share_info(text(data,'sharekey'),settings)
    if not shared: actor(c,ctx)
    elif shared['meetingid']!=meetingid: raise ApiError('分享链接已过期')
    meeting=row(c,'meetings',meetingid=meetingid)
    if not meeting: raise ApiError('频道ID不存在')
    code=secrets.token_urlsafe(32); client=cache(settings)
    try: client.set('dootask:meeting:share:'+code,json.dumps({k:meeting[k] for k in ('id','meetingid','channel')}),ex=21600)
    finally: client.close()
    return success(url(ctx,'meeting/'+meetingid+'/'+code))


def meeting_tourist(c,ctx,data,settings):
    client=cache(settings)
    try: value=decoded(client.get('dootask:meeting:tourist:'+text(data,'tourist_id')))
    finally: client.close()
    if not value: raise ApiError('Id不存在')
    return success(value)


def meeting_invitation(c,ctx,data,settings):
    actor(c,ctx); meeting=row(c,'meetings',meetingid=text(data,'meetingid'))
    if not meeting: raise ApiError('频道ID不存在')
    return success(dict(meeting,msgs=invitations(c,ctx,meeting,data.get('userids',[]))),'发送邀请成功')


ROUTES={'users/bot/list':bot_list,'users/bot/info':bot_info,'users/bot/edit':bot_edit,'users/bot/delete':bot_delete,'users/search/ai':search_ai,'users/meeting/open':meeting_open,'users/meeting/link':meeting_link,'users/meeting/tourist':meeting_tourist,'users/meeting/invitation':meeting_invitation}
