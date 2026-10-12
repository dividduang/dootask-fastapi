"""System configuration handlers; values retain the source JSON object/array shape."""
import base64
import hmac
import ipaddress
import json
import os
import re
import secrets
from urllib.parse import urlparse
from urllib.request import urlopen
from sqlalchemy import select, func
from .. import db
from ..errors import ApiError, success
from .account_common import *
from .account_integrations import send_mail, ldap_connection
from .apps import require_installed, installed_config

HANDOFF={'project_task_handoff':['close','open'],'project_task_handoff_role':['owner','close','managers'],'project_task_handoff_candidates':['department','project'],'project_task_handoff_adjust':['department','all'],'project_task_handoff_note':['optional','required']}
SYSTEM_FIELDS='reg reg_identity reg_invite temp_account_alias login_code password_policy project_invite project_add_permission project_add_userids chat_information anon_message convert_video compress_video e2e_message msg_rev_limit msg_edit_limit auto_archived archived_day task_visible task_default_time task_user_limit all_group_mute all_group_autoin user_private_chat_mute user_group_chat_mute system_alias login_logo system_welcome image_compress image_quality image_save_local file_upload_limit unclaimed_task_reminder unclaimed_task_reminder_time task_ai_auto_analyze department_owner_project_view project_task_handoff project_task_handoff_role project_task_handoff_candidates project_task_handoff_adjust project_task_handoff_note todo_set_permission'.split()


def check_version(ctx,data,minimum):
    value=ctx.version or text(data,'version') or '0.0.1'
    parse=lambda s:tuple(integer(x) for x in re.findall(r'\d+',s)[:3])
    if parse(value)<parse(minimum): raise ApiError(f'当前客户端版本 ({value}) 过低，最低版本要求 ({minimum})。')


def editable():
    if os.environ.get('SYSTEM_SETTING')=='disabled': raise ApiError('当前环境禁止修改')


def trim_tree(value):
    if isinstance(value,dict): return {k:trim_tree(v) for k,v in value.items()}
    if isinstance(value,list): return [trim_tree(v) for v in value]
    return value.strip() if isinstance(value,str) else value


def setting(c,ctx,data,settings):
    kind=text(data,'type'); config=db.setting(c,'system') or {}
    if kind=='save':
        editable(); check_version(ctx,data,'0.41.11'); actor(c,ctx,True)
        for k,options in HANDOFF.items():
            if k in data and data[k] not in options: raise ApiError('流转设置选项无效')
        values=trim_tree({k:v for k,v in data.items() if k in SYSTEM_FIELDS})
        try: values['archived_day']=float(values.get('archived_day') or 0)
        except (ValueError,TypeError): values['archived_day']=0
        if values.get('auto_archived')=='open':
            if values['archived_day']<=0: raise ApiError('自动归档时间不可小于1天！')
            if values['archived_day']>100: raise ApiError('自动归档时间不可大于100天！')
        if values.get('system_alias')==settings.system_alias: values['system_alias']=''
        if values.get('system_welcome')=='欢迎您，{username}': values['system_welcome']=''
        if 'login_logo' in values:
            value=values['login_logo']; value=value[0].get('path','') if isinstance(value,list) and value else value
            values['login_logo']=str(value or '').removeprefix(ctx.base_url)
        config=stored_setting(c,'system',dict(config,**values))
    if kind in ('all','save'):
        actor(c,ctx,True); config['reg_invite']=config.get('reg_invite') or secrets.token_urlsafe(8)
    else:
        config.pop('reg_invite',None); config.pop('project_add_userids',None)
    defaults={'system_alias':settings.system_alias,'reg':'open','reg_identity':'normal','temp_account_alias':'','login_code':'auto','password_policy':'simple','project_invite':'open','chat_information':'optional','anon_message':'open','convert_video':'close','compress_video':'close','e2e_message':'close','msg_rev_limit':'','msg_edit_limit':'','auto_archived':'close','archived_day':7,'task_visible':'close','all_group_mute':'open','todo_set_permission':'open','all_group_autoin':'yes','user_private_chat_mute':'open','user_group_chat_mute':'open','file_upload_limit':'','unclaimed_task_reminder':'close','unclaimed_task_reminder_time':'','task_ai_auto_analyze':'open','department_owner_project_view':'open'}
    for k,v in defaults.items(): config[k]=config.get(k) or v
    for k,options in HANDOFF.items():
        if config.get(k) not in options: config[k]=options[0]
    config.update(login_logo=url(ctx,config.get('login_logo')),app_ai_hidden=os.environ.get('APP_AI_HIDDEN','false').lower() in ('1','true','on','yes'),server_timezone='UTC',server_version=settings.version)
    config['deployment']={'mode':'independent','commercial_authorization_required':False,'vendor_authorized':False}
    return success(config,'保存成功' if kind=='save' else 'success')


def config_handler(c,ctx,data,settings,name,fields,defaults,*,email=False):
    a=actor(c,ctx,not email); config=db.setting(c,name) or {}; kind=text(data,'type')
    if kind=='save':
        editable(); from ..auth import require_admin
        require_admin(a); values=trim_tree({k:v for k,v in data.items() if k in fields})
        if name=='meetingSetting' and values.get('open')=='open' and (not values.get('appid') or not values.get('app_certificate')): raise ApiError('请填写基本配置')
        if name=='emailSetting':
            raw=values.get('msg_unread_time_ranges',[]); ranges=[]
            for item in raw if isinstance(raw,list) else []:
                item=item.split(',') if isinstance(item,str) else item
                if isinstance(item,list) and len(item)==2 and all(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?',str(x)) for x in item): ranges.append(item)
            values['msg_unread_time_ranges']=ranges
        config=stored_setting(c,name,dict(config,**values))
    for k,v in defaults.items():
        if not config.get(k): config[k]=v
    if email:
        for k in ('msg_unread_user_minute','msg_unread_group_minute'): config[k]=integer(config.get(k),-1)
        if 'admin' not in a.identities and kind!='save': config={'reg_verify':config['reg_verify']}
    if name=='meetingSetting' and os.environ.get('SYSTEM_SETTING')=='disabled':
        for key in ('appid','app_certificate','api_key','api_secret'):
            value=str(config.get(key,'')); config[key]=value[:4]+'*'*max(0,len(value)-8)+value[-4:]
    return success(config,'保存成功' if kind=='save' else 'success')


def setting_email(c,ctx,data,settings):
    return config_handler(c,ctx,data,settings,'emailSetting','smtp_server port account password reg_verify notice_msg msg_unread_user_minute msg_unread_group_minute msg_unread_time_ranges ignore_addr'.split(),{'smtp_server':'','port':'','account':'','password':'','reg_verify':'close','notice_msg':'close','msg_unread_time_ranges':[[]],'ignore_addr':''},email=True)


def setting_meeting(c,ctx,data,settings): return config_handler(c,ctx,data,settings,'meetingSetting','open appid app_certificate api_key api_secret'.split(),{'open':'close'})
def setting_apppush(c,ctx,data,settings): return config_handler(c,ctx,data,settings,'appPushSetting','push ios_key ios_secret android_key android_secret'.split(),{'push':'close'})
def setting_file(c,ctx,data,settings): return config_handler(c,ctx,data,settings,'fileSetting',['permission_pack_type','permission_pack_userids'],{})


def setting_thirdaccess(c,ctx,data,settings):
    if text(data,'type')=='testldap':
        actor(c,ctx,True); client=ldap_connection(trim_tree(data)); client.unbind(); return success(message='验证通过')
    result=config_handler(c,ctx,data,settings,'thirdAccessSetting','ldap_open ldap_host ldap_port ldap_password ldap_user_dn ldap_base_dn ldap_login_attr ldap_sync_local'.split(),{'ldap_open':'close','ldap_port':389,'ldap_login_attr':'cn','ldap_sync_local':'close'})
    result['data']['ldap_port']=integer(result['data'].get('ldap_port')) or 389
    return result


def setting_aibot(c,ctx,data,settings):
    actor(c,ctx,True); require_installed(settings,'ai'); config=db.setting(c,'aibotSetting') or {}
    if text(data,'type')=='save':
        editable(); check_version(ctx,data,'0.41.11'); config=stored_setting(c,'aibotSetting',trim_tree({k:data.get(k,v) for k,v in config.items()}))
    if text(data,'filter'): config={k:v for k,v in config.items() if k.startswith(text(data,'filter'))}
    if os.environ.get('SYSTEM_SETTING')=='disabled':
        for k,v in config.items():
            if v and k!='dooai_key' and k.endswith(('_key','_secret')):
                v=str(v); config[k]=v[:4]+'*'*max(0,len(v)-8)+v[-4:]
    return success(config,'保存成功' if text(data,'type')=='save' else 'success')


def historical_ai(c,ctx,data,settings):
    check_version(ctx,data,'1.4.35')
    # The source function deliberately has no return after the version gate.
    return None


def setting_checkin(c,ctx,data,settings):
    actor(c,ctx,True); config=db.setting(c,'checkinSetting') or {}; kind=text(data,'type')
    if kind=='save':
        editable(); allowed='open time advance delay remindin remindexceed edit face_upload face_remark face_retip locat_remark locat_map_type locat_bd_lbs_key locat_bd_lbs_point locat_amap_key locat_amap_point locat_tencent_key locat_tencent_point manual_remark modes key'.split()
        values=trim_tree({k:v for k,v in data.items() if k in allowed})
        if values.get('open')=='close': values.update(key=secrets.token_hex(16),face_key=secrets.token_hex(16))
        else:
            from .account_meetings import bot_get
            bot_get(c,'check-in')
            modes=values.get('modes',[])
            if 'locat' in modes:
                options={'baidu':('locat_bd_lbs_key','locat_bd_lbs_point','请填写百度地图AK'),'amap':('locat_amap_key','locat_amap_point','请填写高德地图Key'),'tencent':('locat_tencent_key','locat_tencent_point','请填写腾讯地图Key')}
                option=options.get(values.get('locat_map_type'))
                if not option: raise ApiError('请选择地图类型')
                key,point,message=option
                if not values.get(key): raise ApiError(message)
                point_value=values.get(point)
                if not isinstance(point_value,dict): raise ApiError('请选择允许签到位置')
                point_value['radius']=integer(point_value.get('radius'))
                if not all(point_value.get(x) for x in ('lng','lat','radius')): raise ApiError('请选择有效的签到位置')
            if 'face' in modes: require_installed(settings,'face')
        values['modes']=[x for x in values.get('modes',[]) if x in ('auto','manual','locat','face')]
        if values.get('open')=='open':
            times=decoded(values.get('time'),[]) if isinstance(values.get('time'),str) else values.get('time',[])
            if len(times)>=2:
                minutes=lambda s:integer(s[:2])*60+integer(s[3:5]); duration=(minutes(times[1])-minutes(times[0]))%1440 or 1440; maximum=1440-duration
                if (integer(values.get('advance')) or 120)+(integer(values.get('delay')) or 120)>=maximum: raise ApiError(f'提前和延后时间设置存在重叠，最大提前+延后时间不能超过 {maximum-1} 分钟')
        config=stored_setting(c,'checkinSetting',dict(config,**values))
    if not config.get('key') or not config.get('face_key'):
        config['key']=config.get('key') or secrets.token_hex(16); config['face_key']=config.get('face_key') or secrets.token_hex(16); stored_setting(c,'checkinSetting',config)
    defaults={'open':'close','face_upload':'close','face_remark':'考勤机','face_retip':'open','locat_remark':'定位签到','locat_map_type':'baidu','manual_remark':'手动签到','advance':120,'delay':120,'remindin':5,'remindexceed':10,'edit':'close','modes':[]}
    for k,v in defaults.items(): config[k]=config.get(k) or v
    for key in ('locat_bd_lbs_point','locat_amap_point','locat_tencent_point'):
        if not isinstance(config.get(key),dict): config[key]={'radius':500}
    config['time']=decoded(config.get('time'),['09:00','18:00']) if isinstance(config.get('time'),str) else config.get('time') or ['09:00','18:00']
    command="curl -sSL '"+url(ctx,'api/public/checkin/install?key='+config['key'])+"' | sh"
    config['cmd']=base64.b64encode(command.encode()).decode() if tuple(integer(v) for v in (ctx.version or '0.0.1').split('.')[:3]) >= (0,34,67) else command
    return success(config,'保存成功' if kind=='save' else 'success')


def normalize_priority(value):
    result=[]; default=None
    for item in value if isinstance(value,list) else []:
        if not isinstance(item,dict): continue
        name=text(item,'name'); color=text(item,'color'); priority=integer(item.get('priority'))
        if not name or not color or priority<=0: continue
        marked=bool(item.get('is_default') or item.get('default'))
        if default is None and marked: default=len(result)
        result.append({'name':name,'color':color,'priority':priority,'days':integer(item.get('days')),'is_default':0})
    if result: result[default or 0]['is_default']=1
    return result


def priority(c,ctx,data,settings):
    saving=text(data,'type')=='save'
    if saving:
        actor(c,ctx,True)
        if not isinstance(data.get('list'),list) or not data['list']: raise ApiError('参数错误')
        result=normalize_priority(data['list'])
        if not result: raise ApiError('参数为空')
        stored_setting(c,'priority',result)
    else: result=normalize_priority(db.setting(c,'priority'))
    return success(result,'保存成功' if saving else 'success')


def column_template(c,ctx,data,settings):
    saving=text(data,'type')=='save'
    if saving:
        actor(c,ctx,True); value=data.get('list')
        if not isinstance(value,list) or not value: raise ApiError('参数错误')
        result=[{'name':x['name'],'columns':list(dict.fromkeys(y for y in str(x['columns']).split(',') if y))} for x in value if isinstance(x,dict) and x.get('name') and x.get('columns')]
        if not result: raise ApiError('参数为空')
        stored_setting(c,'columnTemplate',result)
    else: result=db.setting(c,'columnTemplate')
    return success(result,'保存成功' if saving else 'success')


def visible(value):
    values=[str(x).strip() for x in (value if isinstance(value,list) else str(value).split(',')) if str(x).strip()]
    return ['all'] if 'all' in values else values or ['admin']


def normalize_apps(value):
    result=[]
    for app in value if isinstance(value,list) else []:
        if not isinstance(app,dict) or not text(app,'id'): continue
        menus=app.get('menu_items') if isinstance(app.get('menu_items'),list) else [app.get('menu')]; normalized=[]
        for menu in menus:
            if not isinstance(menu,dict) or not text(menu,'url'): continue
            item={'location':text(menu,'location','application'),'label':text(menu,'label') or app.get('name') or app['id'],'icon':trim_tree(menu.get('icon','')),'url':text(menu,'url'),'type':text(menu,'type','iframe').lower(),'key':text(menu,'key')}
            for key,default in [('keep_alive',True),('disable_scope_css',False),('auto_dark_theme',True),('transparent',False),('badge_clear_on_open',False)]: item[key]=bool(menu.get(key,default))
            for key in ('background','capsule'):
                if menu.get(key): item[key]=trim_tree(menu[key])
            normalized.append(item)
        if normalized: result.append({'id':text(app,'id'),'name':trim_tree(app.get('name','')),'version':trim_tree(app.get('version')) or 'custom','menu_items':normalized,'visible_to':visible(app.get('visible_to','admin'))})
    return result


def microapp_menu(c,ctx,data,settings):
    a=actor(c,ctx); saving=text(data,'type')=='save'
    if saving:
        from ..auth import require_admin
        require_admin(a); result=stored_setting(c,'microapp_menu',normalize_apps(data.get('list')))
    else: result=db.setting(c,'microapp_menu') or []
    allowed=lambda v:'all' in v or ('admin' in v and 'admin' in a.identities) or str(a.userid) in v
    output=[]
    for app in result:
        app=dict(app)
        if not saving and not allowed(visible(app.get('visible_to','admin'))): continue
        app['menu_items']=[dict(x) for x in app.get('menu_items',[]) if 'visible_to' not in x or allowed(visible(x['visible_to']))]
        for item in app['menu_items']: item.pop('visible_to',None)
        if not app['menu_items']: continue
        if 'admin' not in a.identities: app.pop('visible_to',None)
        output.append(app)
    return success(output,'保存成功' if saving else 'success')


def demo(c,ctx,data,settings):
    account,password=os.environ.get('DEMO_ACCOUNT'),os.environ.get('DEMO_PASSWORD')
    if not account or not password: raise ApiError('No demo account')
    return success({'account':account,'password':password})


def get_ip(c,ctx,data,settings): return ctx.ip


def get_cnip(c,ctx,data,settings):
    path=__import__('pathlib').Path(__file__).with_name('accounts_cn_ips.txt')
    if not path.is_file(): return -1
    try: address=ipaddress.ip_address(text(data,'ip') or ctx.ip)
    except ValueError: return 0
    with path.open(encoding='utf-8') as stream:
        return int(any(address in ipaddress.ip_network(line.strip(),strict=False) for line in stream if line.strip()))


def get_info(c,ctx,data,settings):
    if not text(data,'key') or not hmac.compare_digest(text(data,'key'),settings.app_key): return []
    # Deliberate security cutover: never echo Authorization/token/encrypt/raw headers.
    headers={k:v for k,v in ctx.headers.items() if k not in ('authorization','token','cookie','encrypt','key')}
    return success({'ip':ctx.ip,'ip-iscn':get_cnip(c,ctx,{},settings),'header':headers,'token':'','url':ctx.base_url+'api/system/get/info'})


def email_check(c,ctx,data,settings):
    actor(c,ctx,True); recipient=text(data,'to')
    ignored=str(db.setting(c,'emailSetting','ignore_addr','')).replace('\n',',').split(',')
    if any(value.strip() and value.strip().lower() in recipient.lower() for value in ignored): raise ApiError('收件人地址错误或已被忽略')
    send_mail(data,recipient,'Mail sending test','<p>收到此电子邮件意味着您的邮箱配置正确。</p>',db.setting(c,'system','system_alias','Task') or 'Task')
    return success(message='成功发送')


def version(c,ctx,data,settings):
    result={'device_count':0,'version':settings.version,'publish':[]}
    if ctx.token:
        try:
            a=actor(c,ctx); t=db.tables['user_devices']; result['device_count']=c.execute(select(func.count()).select_from(t).where(t.c.userid==a.userid,t.c.deleted_at.is_(None))).scalar_one()
        except ApiError: pass
    package=json.loads((settings.frontend_root/'package.json').read_text(encoding='utf-8'))
    for index,item in enumerate(package.get('app',[])):
        urls=item.get('urls') or item.get('url') or []; urls=urls if isinstance(urls,list) else [urls]
        if isinstance(item.get('publish'),list) and (index==0 or any(urlparse(ctx.base_url).hostname==urlparse(u).hostname for u in urls)): result['publish']=item['publish']
    return success(result) if 'version' in ctx.headers else result


def get_updatelog(c,ctx,data,settings):
    path=__import__('pathlib').Path(__file__).with_name('accounts_upstream_changelog.txt'); result=[]
    if path.is_file():
        sections=re.split(r'## \[(.*?)\]',path.read_text(encoding='utf-8'))
        result=[(sections[i],sections[i+1]) for i in range(1,len(sections)-1,2)][:min(100,max(10,integer(data.get('take'),50)))]
    return success({'logVersion':result[0][0] if result else '', 'updateLog':'\n'.join('## '+title+body for title,body in result)})


def update_check(c,ctx,data,settings):
    actor(c,ctx,True)
    return success({'supported':False,'reason':'disabled','branch':'','current_version':settings.version,'latest_version':'','behind':0,'has_update':False,'has_local_changes':False,'has_db_changes':False,'changelog':'','checked_at':int(utcnow().timestamp())})


def update_start(c,ctx,data,settings):
    a=actor(c,ctx,True)
    from ..security import password_verify
    if not password_verify(text(data,'password'),a.credential['password_hash']): raise ApiError('密码错误')
    raise ApiError('当前 FastAPI 部署不支持在线自更新；禁止使用旧 PHP 更新器覆盖新后端')


def update_status(c,ctx,data,settings):
    actor(c,ctx,True)
    return success({'status':'idle','log':'','offset':0})


def prefetch(c,ctx,data,settings):
    ua=ctx.user_agent.lower(); main='maintaskwindow' in ua; app='kuaifan_eeui' in ua; result=[]
    if main or app:
        root=settings.frontend_root/'public'/'js'/'build'
        if root.is_dir(): result.extend('js/build/'+path.name for path in root.rglob('*') if path.is_file() and path.stat().st_size>50*1024)
    if main:
        path=settings.frontend_root/'.prefetch'
        if path.is_file(): result.extend(path.read_text(encoding='utf-8').splitlines())
        office_path=''
        if installed_config(settings,'office'):
            try:
                with urlopen(os.environ.get('OFFICE_URL','http://office').rstrip('/')+'/web-apps/apps/api/documents/api.js',timeout=15) as response: content=response.read().decode()
                match=re.search(r"const\s+ver\s*=\s*'/*([^']+)'",content)
                if match: office_path=match[1]
            except OSError: office_path=''
        result=[x.replace('office/{path}/','/office/'+office_path+'/') for x in result if office_path or not x.startswith('office/{path}/')]
        if installed_config(settings,'okr'):
            try:
                with urlopen(os.environ.get('NGINX_INTERNAL_URL','http://nginx').rstrip('/')+'/apps/okr/',timeout=15) as response: content=response.read().decode()
                result.extend(re.findall(r'<script[^>]*src=["\']([^"\']+)["\'][^>]*>',content,re.I))
                result.extend(re.findall(r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>',content,re.I))
            except OSError as exc: raise ApiError('OKR预加载服务请求失败') from exc
    return [url(ctx,x.strip().replace('{version}',settings.version)) for x in result if x.strip()]


ROUTES={'system/setting':setting,'system/setting/email':setting_email,'system/setting/meeting':setting_meeting,'system/setting/ai':historical_ai,'system/setting/aibot':setting_aibot,'system/setting/aibot_models':historical_ai,'system/setting/aibot_defmodels':historical_ai,'system/setting/checkin':setting_checkin,'system/setting/apppush':setting_apppush,'system/setting/thirdaccess':setting_thirdaccess,'system/setting/file':setting_file,'system/demo':demo,'system/priority':priority,'system/microapp_menu':microapp_menu,'system/column/template':column_template,'system/get/info':get_info,'system/get/ip':get_ip,'system/get/cnip':get_cnip,'system/get/updatelog':get_updatelog,'system/email/check':email_check,'system/version':version,'system/update/check':update_check,'system/update/start':update_start,'system/update/status':update_status,'system/prefetch':prefetch}

from .account_system_files import ROUTES as FILE_ROUTES
ROUTES.update(FILE_ROUTES)
RESPONSE_MODE = 'envelope_or_response'
RAW_ROUTES = {'system/get/info', 'system/get/ip', 'system/get/cnip', 'system/version', 'system/prefetch', 'system/setting/ai', 'system/setting/aibot_models', 'system/setting/aibot_defmodels'}
BINARY_ROUTES = {'system/checkin/down'}
