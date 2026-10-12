"""Source account bot commands and periodic meeting/attendance consumers."""
import base64
import json
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen
from sqlalchemy import select
from .. import auth, db, jobs
from ..context import RequestContext, utcnow
from ..errors import ApiError, json_default
from .account_common import *
from .account_meetings import bot_get, cache, bot_edit, bot_delete
from .dialog_core import get_dialog, get_message, send_message, ensure_user_dialog, dialog_data, push_dialog

log=logging.getLogger(__name__)




def command_context(c,payload):
    user=auth.lock_user(c,payload['userid'])
    if auth.is_disabled(user): raise ApiError('帐号已停用...')
    get_dialog(c,payload['dialog_id'],user['userid'])
    bot=row(c,'users',userid=payload['bot_id'],bot=1)
    if not bot: raise ApiError('机器人不存在')
    get_dialog(c,payload['dialog_id'],bot['userid'])
    return RequestContext(request_id='account-bot:'+str(payload['_event_id']),base_url=payload.get('base_url',''),actor=auth.Actor(user['userid'],user,row(c,'auth_credentials',userid=user['userid']) or {},{})),bot


def quick_checkin(c,ctx,command,extra,settings):
    from .checkin import record_checkin
    client=cache(settings)
    try:
        if not client.set('dootask:checkin-command:'+str(ctx.actor.userid),'1',ex=1,nx=True): return '操作频繁！'
    finally: client.close()
    if command not in ('manual-checkin','locat-checkin'): return None
    config=db.setting(c,'checkinSetting'); mode=command.split('-')[0]
    if config.get('open')!='open': return '暂未开启签到功能。'
    if mode not in config.get('modes',[]): return '暂未开放手动签到。' if mode=='manual' else '暂未开放定位签到。'
    if mode=='locat':
        if not extra: return '当前客户端版本低（所需版本≥v0.39.75）。'
        if extra.get('type') not in ('baidu','amap','tencent'): return '错误的定位签到。'
        # Source 336-340 validates provider only, not distance. Preserve this branch;
        # it is not evidence of geofence enforcement.
    record_checkin(c,ctx,mode+'-'+str(ctx.actor.userid),int(utcnow().timestamp()),already_tip=True)
    return None


def bot_checkin(c,payload,settings):
    ctx,bot=command_context(c,payload)
    if bot['email']!='check-in@bot.system': raise ApiError('签到机器人不存在')
    content=quick_checkin(c,ctx,payload['command'],payload.get('location',{}),settings)
    if content: send_message(c,dialog_id=payload['dialog_id'],sender_id=bot['userid'],msg_type='template',msg={'type':'content','content':content},context=ctx)


def bot_command(c,payload,settings):
    from .accounts import safe_user,revoke_user
    ctx,bot=command_context(c,payload); textvalue=str(payload.get('text','')).strip(); reply=None
    if get_dialog(c,payload['dialog_id'],ctx.actor.userid)['type']!='user':
        return  # Source processMessage stops after webhook delivery in groups.
    if payload.get('msg_id'):
        source=get_message(c,payload['msg_id'],ctx.actor.userid)
        if source['userid']!=ctx.actor.userid or source['dialog_id']!=payload['dialog_id']: raise ApiError('消息身份不匹配')
    if bot['email']=='check-in@bot.system':
        content=quick_checkin(c,ctx,textvalue,{},settings)
        if content: reply={'type':'content','content':content}
    elif bot['email']=='anon-msg@bot.system' and textvalue in ('help','privacy'):
        reply={'type':'content','title':'匿名消息使用说明' if textvalue=='help' else '匿名消息隐私说明','content':'使用说明：打开你想要发匿名消息的个人对话，点击输入框右边的加号，选择「匿名消息」即可输入你想要发送的匿名消息内容。' if textvalue=='help' else '匿名消息将通过「匿名消息（机器人）」发送给对方，不会记录你的身份信息。'}
    if textvalue.startswith('/'):
        manager=bot['email']=='bot-manager@bot.system'; owned=row(c,'user_bots',bot_id=bot['userid'],userid=ctx.actor.userid)
        parts=textvalue.split(' ')+['','','']; command=parts[0]; botid=integer(parts[1]) if manager else bot['userid']; parameter=parts[2] if manager else parts[1]
        titles={'/hello':'您好','/help':'帮助指令','/list':'我的机器人','/info':'机器人信息','/newbot':'新建机器人','/setname':'设置名称','/deletebot':'删除机器人','/token':'机器人Token','/revoke':'更新Token','/webhook':'设置Webhook','/clearday':'设置保留消息时间','/dialog':'对话列表','/api':'API接口文档'}
        if not manager and not owned: reply={'type':'content','content':'非常抱歉，我不是你的机器人，无法完成你的指令。'}
        elif not manager and command in ('/list','/newbot'): return
        else:
            reply={'type':command,'data':[],'title':titles.get(command,'不支持的指令')}
            try:
                if command=='/api': reply.update(email=bot['email'],version=settings.version)
                elif command=='/help': reply['manager']=manager
                elif command=='/newbot': reply['data']=bot_edit(c,ctx,{'name':parts[1]},settings)['data']
                elif command=='/list':
                    b=db.tables['user_bots']; values=[]
                    for item in rows(c,select(b).where(b.c.userid==ctx.actor.userid).order_by(b.c.id.desc()).limit(50)):
                        user=row(c,'users',userid=item['bot_id'],bot=1)
                        if user: values.append(dict(safe_user(c,user,ctx),**{k:item[k] for k in ('clear_day','clear_at','webhook_url','webhook_num')}))
                    if values: reply['data']=values
                    else: reply={'type':'content','content':'您没有创建机器人。'}
                elif command in ('/hello','/info','/setname','/deletebot','/token','/revoke','/clearday','/webhook','/dialog'):
                    user=auth.lock_user(c,botid); owned=row(c,'user_bots',userid=ctx.actor.userid,bot_id=botid)
                    if not user['bot'] or auth.is_disabled(user) or not owned: raise ApiError('机器人不存在。')
                    if command=='/setname':
                        if not 2<=len(parameter.encode())<=20: raise ApiError('机器人名称由2-20个字符组成。')
                        bot_edit(c,ctx,{'id':botid,'name':parameter},settings); user=row(c,'users',userid=botid)
                    if command=='/deletebot': bot_delete(c,ctx,{'id':botid,'remark':'delete bot'},settings)
                    result=safe_user(c,user,ctx)
                    if command in ('/token','/revoke'):
                        credential=row(c,'auth_credentials',userid=botid)
                        if not credential: raise ApiError('机器人凭据不存在')
                        if command=='/revoke':
                            generation=secrets.token_hex(32); update(c,'auth_credentials',{'userid':botid},{'generation':generation}); revoke_user(c,botid)
                        else: result['token']=auth.issue_token(c,user,credential,ctx)
                    elif command=='/clearday':
                        days=min(integer(parameter) or 30,999); values={'clear_day':days,'clear_at':utcnow()+timedelta(days=days)}; update(c,'user_bots',{'id':owned['id']},values); result.update(values)
                    elif command=='/webhook':
                        if len(parameter)>255: raise ApiError('webhook地址最长仅支持255个字符。')
                        update(c,'user_bots',{'id':owned['id']},{'webhook_url':parameter,'webhook_num':0}); result.update(webhook_url=parameter or '-',webhook_num=0)
                    elif command=='/dialog':
                        d,m=db.tables['web_socket_dialogs'],db.tables['web_socket_dialog_users']; values=list(c.execute(select(d.c.id).join(m,m.c.dialog_id==d.c.id).where(m.c.userid==botid,d.c.name.like('%'+parameter+'%'),d.c.deleted_at.is_(None)).order_by(m.c.top_at.desc(),m.c.last_at.desc()).limit(20)).scalars())
                        if not values: raise ApiError('没有搜索到相关会话。')
                        result['list']=[dialog_data(c,v,botid) for v in values]
                    reply['data']=result
            except ApiError as exc: reply={'type':'content','content':exc.message}
    if reply: send_message(c,dialog_id=payload['dialog_id'],sender_id=bot['userid'],msg_type='template',msg=reply,context=ctx)


def close_meetings(c,payload,settings):
    config=db.setting(c,'meetingSetting')
    if config.get('open')!='open': return
    if not all(config.get(k) for k in ('appid','api_key','api_secret')): raise ApiError('会议 REST API 凭据未配置，无法核实会议频道状态')
    t=db.tables['meetings']; authorization=base64.b64encode((config['api_key']+':'+config['api_secret']).encode()).decode()
    for meeting in rows(c,select(t).where(t.c.end_at.is_(None),t.c.updated_at<utcnow()-timedelta(minutes=10)).limit(100).with_for_update()):
        request=Request('https://api.sd-rtn.com/dev/v1/channel/user/'+quote(config['appid'],safe='')+'/'+quote(meeting['channel'],safe=''),headers={'Accept':'application/json','Authorization':'Basic '+authorization})
        with urlopen(request,timeout=30) as response: result=json.load(response)
        if not result.get('success'): raise ApiError('会议频道状态查询失败')
        if result.get('data',{}).get('channel_exist'):
            update(c,'meetings',{'id':meeting['id']},{}); continue
        meeting['end_at']=utcnow(); update(c,'meetings',{'id':meeting['id']},{'end_at':meeting['end_at']})
        m,links=db.tables['web_socket_dialog_msgs'],db.tables['meeting_msgs']
        for msg in rows(c,select(m).join(links,links.c.msg_id==m.c.id).where(links.c.meetingid==meeting['meetingid'],m.c.deleted_at.is_(None))):
            content=dict(decoded(msg['msg']),**meeting); update(c,'web_socket_dialog_msgs',{'id':msg['id']},{'msg':json.dumps(content,ensure_ascii=False,default=json_default)})
            push_dialog(c,msg['dialog_id'],'update',{'id':msg['id'],'msg':content})






def register_jobs():
    jobs.register('accounts.bot_checkin',bot_checkin)
    jobs.register('accounts.bot_command',bot_command)
    jobs.register('accounts.close_meetings',close_meetings)
    jobs.schedule('accounts.close_meetings',600)
