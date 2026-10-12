"""Users/contacts domain. Every public route returns its source envelope."""
import hashlib
import json
import secrets
from datetime import datetime, timedelta
from sqlalchemy import select, delete, func, or_, and_
from .. import auth, db
from ..context import utcnow
from ..errors import ApiError, success
from ..jobs import enqueue, emit
from .account_common import *
from .account_integrations import send_verification, verify_email, ldap_update


def managed_departments(connection, userid):
    t, o = db.tables['user_departments'], db.tables['user_department_owners']
    return rows(connection, select(t.c.id,t.c.name,t.c.parent_id,t.c.owner_userid).where(or_(t.c.owner_userid == userid,t.c.id.in_(select(o.c.department_id).where(o.c.userid == userid)))).order_by(t.c.id))


def department_users(connection, department_ids, recursive=True):
    scope = set(ids(department_ids))
    if recursive:
        departments = rows(connection, select(db.tables['user_departments']))
        while True:
            added = {x['id'] for x in departments if x['parent_id'] in scope} - scope
            if not added:
                break
            scope.update(added)
    if not scope:
        return []
    t = db.tables['users']
    return list(connection.execute(select(t.c.userid).where(or_(*(func.find_in_set(str(x),t.c.department)>0 for x in scope)))).scalars())


def info_managed(c, ctx, data, settings):
    a = actor(c,ctx)
    return success(managed_departments(c,a.userid) if db.setting(c,'system','department_owner_project_view','open') == 'open' else [])


def info_departments(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_departments']
    result=rows(c,select(t.c.id,t.c.owner_userid,t.c.parent_id,t.c.name).where(t.c.id.in_(ids(a.user['department']))).limit(10))
    result.sort(key=lambda x:x['owner_userid'] != a.userid)
    return success(result)


def departments(c,ctx,data,settings):
    actor(c,ctx); t=db.tables['user_departments']; u=db.tables['users']
    deps={x['id']:x for x in rows(c,select(t.c.id,t.c.name,t.c.parent_id,t.c.owner_userid).order_by(t.c.id))}
    members={key:set() for key in deps}; total=missing=0
    for user in rows(c,select(u.c.userid,u.c.department).where(u.c.disable_at.is_(None),u.c.bot==0)):
        total+=1; joined=set(ids(user['department']))&deps.keys()
        if not joined: missing+=1
        for depid in joined: members[depid].add(user['userid'])
    children={}
    for dep in deps.values():
        parent=dep['parent_id'] if dep['parent_id'] in deps and dep['parent_id']!=dep['id'] else 0
        children.setdefault(parent,[]).append(dep['id'])
    output=[]; visited=set()
    def walk(depid,level):
        visited.add(depid); index=len(output); output.append(dict(deps[depid],level=level,member_count=len(members[depid]),total_count=0))
        combined=set(members[depid])
        for child in children.get(depid,[]):
            if child not in visited: combined.update(walk(child,level+1))
        output[index]['total_count']=len(combined)
        return combined
    for root in children.get(0,[]):
        if root not in visited: walk(root,0)
    for depid in deps:
        if depid not in visited: walk(depid,0)
    return success({'total':total,'no_department_count':missing,'departments':output})


def changed(c, ctx, before, after, fields, deleted=False):
    if not before.get('bot') or deleted:
        enqueue(c,'accounts.changed',{'userid':before['userid'],'before':safe_user(c,before,ctx),
                'after':safe_user(c,after,ctx) if after else None,'fields':list(fields),'operator':ctx.actor.userid if ctx.actor else 0,'deleted':deleted})


def revoke_user(c, userid):
    auth.lock_user(c,userid)
    t=db.tables['user_devices']
    auth.revoke_devices(c,list(c.execute(select(t.c.id).where(t.c.userid==userid,t.c.deleted_at.is_(None))).scalars()))
    sessions=db.tables['auth_sessions']
    c.execute(sessions.update().where(sessions.c.userid==userid,sessions.c.purpose=='service').values(revoked_at=utcnow()))


def mutate_user(c,ctx,user,values):
    user=auth.lock_user(c,user['userid'])
    fields=[k for k,v in values.items() if user.get(k)!=v]
    if not fields:
        return user
    update(c,'users',{'userid':user['userid']},values)
    result=dict(user,**values)
    if set(fields)&{'identity','email','password','disable_at'}:
        revoke_user(c,user['userid'])
    changed(c,ctx,user,result,fields)
    return result


def profile_values(c,user,data):
    values={}
    for key,label in [('nickname','昵称'),('profession','职位/职称')]:
        if key in data:
            values[key]=named(text(data,key),label)
    if 'nickname' in values:
        from pypinyin import lazy_pinyin
        values['pinyin']=''.join(lazy_pinyin(values['nickname']))
        values['az']=(values['pinyin'][:1] or '#').upper()
    if 'tel' in data:
        value=text(data,'tel')
        if not 6 <= len(value.encode()) <= 20:
            raise ApiError('联系电话长度错误')
        t=db.tables['users']
        if c.execute(select(t.c.userid).where(t.c.tel==value,t.c.userid!=user['userid'])).first():
            raise ApiError('联系电话已存在')
        values['tel']=value
    for key,label,maximum in [('address','地址',100),('introduction','个人简介',500)]:
        if key in data:
            value=text(data,key)
            if len(value)>maximum:
                raise ApiError(f'{label}最多只能设置{maximum}个字')
            values[key]=value or None
    if 'birthday' in data:
        try:
            values['birthday']=datetime.fromisoformat(text(data,'birthday')).date() if text(data,'birthday') else None
        except ValueError as exc:
            raise ApiError('生日格式错误') from exc
    if 'lang' in data:
        value=text(data,'lang')
        if value not in ('zh','zh-CHT','en','ja','ko','de','fr','id','ru'):
            raise ApiError('语言错误')
        values['lang']=value
    if 'userimg' in data:
        value=data['userimg']; value=value[0].get('path','') if isinstance(value,list) and value else value
        values['userimg']='' if 'avatar/' in str(value) else str(value or '')
    return values


def editdata(c,ctx,data,settings):
    a=actor(c,ctx); account_policy(a.user,1)
    values=profile_values(c,a.user,data)
    user=mutate_user(c,ctx,a.user,values)
    ldap_update(c,user['email'],{dst:values[src] for src,dst in [('nickname','displayName'),('profession','employeeType'),('tel','mobile')] if src in values})
    return success(auth.user_data(c,user,ctx.token,ctx),'修改成功')


def login_qrcode(c,ctx,data,settings):
    import redis
    code=text(data,'code')
    if len(code)<32:
        raise ApiError('参数错误')
    client=redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_timeout=5)
    key='dootask:login:qrcode:'+hashlib.sha256(code.encode()).hexdigest()
    try:
        if text(data,'type')=='login':
            a=actor(c,ctx)
            if a.session['purpose']=='service': raise ApiError('服务身份不能确认登录')
            client.set(key,str(a.userid),ex=30)
            return success(message='扫码成功')
        userid=integer(client.getdel(key))
    finally:
        client.close()
    t=db.tables['users']
    found=c.execute(select(t).where(t.c.userid==userid).with_for_update()).mappings().first()
    if not found:
        raise ApiError('No identity')
    user=dict(found)
    if auth.is_disabled(user):
        raise ApiError('帐号已停用...')
    cred=row(c,'auth_credentials',userid=userid)
    if not cred:
        raise ApiError('身份已失效,请重新登录',-1)
    values={'login_num':(user['login_num'] or 0)+1,'last_ip':ctx.ip,'line_ip':ctx.ip,'last_at':utcnow(),'line_at':utcnow()}
    update(c,'users',{'userid':userid},values); user.update(values)
    token=auth.issue_token(c,user,cred,ctx)
    return success(auth.user_data(c,user,token,ctx))


def basic(c,ctx,data,settings):
    from .account_meetings import share_info
    if not share_info(ctx.headers.get('sharekey',''),settings):
        actor(c,ctx)
    values=decoded(data.get('userid'),[]) if isinstance(data.get('userid'),str) else data.get('userid')
    values=values if isinstance(values,list) and values else [data.get('userid')]
    if len(values)>50:
        raise ApiError('一次最多只能获取50条数据')
    result=[]
    for value in values:
        user=row(c,'users',userid=integer(value))
        if user:
            result.append(safe_user(c,user,ctx,True))
        else:
            old=row(c,'user_deletes',userid=integer(value))
            if old:
                cache=decoded(old['cache']); cache={k:v for k,v in cache.items() if k in ('userid','email','nickname','profession','department','userimg','bot','az','pinyin','line_at','disable_at','department_name')}
                cache['delete_at']=old['created_at']; result.append(cache)
    return success(result)


def search(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['users']; keys=data.get('keys') or {}; keys=keys if isinstance(keys,dict) else {}
    query=select(t); key=text(keys,'key')
    if key:
        query=query.where(t.c.email.like('%'+key+'%') if '@' in key else or_(t.c.nickname.like('%'+key+'%'),t.c.pinyin.like('%'+key+'%'),t.c.profession.like('%'+key+'%'),t.c.userid==integer(key,-1)))
    for field in ('disable','bot'):
        mode=integer(keys.get(field))
        if mode in (0,1):
            query=query.where((t.c.disable_at.is_(None) if mode==0 else t.c.disable_at.is_not(None)) if field=='disable' else t.c.bot==mode)
    if integer(data.get('updated_time'))>0:
        query=query.where(t.c.updated_at>=datetime.fromtimestamp(integer(data['updated_time'])))
    for key,table,column,negative in [('project_id','project_users','project_id',False),('no_project_id','project_users','project_id',True),('dialog_id','web_socket_dialog_users','dialog_id',False)]:
        if integer(keys.get(key))>0:
            other=db.tables[table]; condition=t.c.userid.in_(select(other.c.userid).where(other.c[column]==integer(keys[key])))
            query=query.where(~condition if negative else condition)
    if keys.get('departments') not in (None,'',[]):
        requested=keys['departments'] if isinstance(keys['departments'],list) else str(keys['departments']).split(',')
        conditions=[]
        for value in requested:
            if str(value).strip().isdigit():
                conditions.append(or_(t.c.department.is_(None),t.c.department=='') if integer(value)==0 else func.find_in_set(str(integer(value)),t.c.department)>0)
        query=query.where(or_(*conditions) if conditions else t.c.userid<0)
    sorts=data.get('sorts') or {}; direction=sorts.get('az') if isinstance(sorts,dict) else None
    if direction in ('asc','desc'):
        query=query.order_by(t.c.az.asc() if direction=='asc' else t.c.az.desc())
    else:
        if integer(keys.get('disable'))==2: query=query.order_by(t.c.disable_at)
        if integer(keys.get('bot'))==2: query=query.order_by(t.c.bot)
    query=query.order_by(t.c.userid)
    result=paginate(c,query,data,100,10) if 'page' in data else rows(c,query.limit(max(1,min(100,integer(data.get('take'),10)))))
    items=result['data'] if isinstance(result,dict) else result
    for index,user in enumerate(items):
        value=safe_user(c,user,ctx,True); value['created_at']=user['created_at']; value['identity']=auth.split_values(user['identity']); value['tags']=[]
        deps=managed_departments(c,user['userid'])
        if deps: value['tags'].append(deps[0]['name']+'负责人')
        if 'admin' in a.identities:
            if 'admin' in value['identity']: value['tags'].append('系统管理员')
            if 'temp' in value['identity']: value['tags'].append(db.setting(c,'system','temp_account_alias','临时帐号') or '临时帐号')
            if user['userid']>3 and user['created_at'] and user['created_at']>utcnow()-timedelta(days=30): value['tags'].append('新帐号')
        if integer(data.get('state'))==1:
            value['online']=online_status(user,settings)
        if integer(data.get('with_department')):
            dep=db.tables['user_departments']; value['department_info']=rows(c,select(dep.c.id,dep.c.name,dep.c.parent_id,dep.c.owner_userid).where(dep.c.id.in_(ids(user['department']))))
        items[index]=value
    return success(result)


def online_status(user,settings):
    import redis
    client=redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_timeout=5)
    try:
        online=bool(user.get('bot')) or any(client.exists('dootask:connection:'+fd) for fd in client.smembers(f"dootask:user:{user['userid']}"))
        return online
    finally:
        client.close()


def tags_meta(c,userid,viewer):
    t,r,u=db.tables['user_tags'],db.tables['user_tag_recognitions'],db.tables['users']
    result=rows(c,select(t,u.c.nickname.label('created_by_name'),func.count(r.c.id).label('recognition_total')).select_from(t.outerjoin(u,u.c.userid==t.c.created_by).outerjoin(r,r.c.tag_id==t.c.id)).where(t.c.user_id==userid).group_by(*t.c,u.c.nickname).order_by(func.count(r.c.id).desc(),t.c.id))
    recognized=set(c.execute(select(r.c.tag_id).where(r.c.user_id==viewer.userid)).scalars())
    for value in result:
        manageable='admin' in viewer.identities or viewer.userid in (value['user_id'],value['created_by'])
        value.update(recognized=value['id'] in recognized,can_edit=manageable,can_delete=manageable)
        for key in ('updated_by','created_at','updated_at'): value.pop(key,None)
    return {'list':result,'top':result[:10],'total':len(result)}


def tags_lists(c,ctx,data,settings):
    a=actor(c,ctx); userid=integer(data.get('userid')) or a.userid
    if not row(c,'users',userid=userid): raise ApiError('会员不存在')
    return success(tags_meta(c,userid,a))


def tags_add(c,ctx,data,settings):
    a=actor(c,ctx); userid=integer(data.get('userid')) or a.userid; name=text(data,'name')
    if not row(c,'users',userid=userid): raise ApiError('会员不存在')
    if not name: raise ApiError('请输入个性标签')
    if len(name)>20: raise ApiError('标签名称最多只能设置20个字')
    if row(c,'user_tags',user_id=userid,name=name): raise ApiError('标签已存在')
    t=db.tables['user_tags']
    if c.execute(select(func.count()).select_from(t).where(t.c.user_id==userid)).scalar_one()>=100: raise ApiError('每位会员最多添加100个标签')
    db.insert_row(c,'user_tags',user_id=userid,name=name,created_by=a.userid,updated_by=a.userid)
    enqueue(c,'accounts.search',{'userid':userid})
    return success(tags_meta(c,userid,a),'添加成功')


def tag_mutation(c,ctx,data,settings,action):
    a=actor(c,ctx); tag=row(c,'user_tags',id=integer(data.get('tag_id')))
    if not tag: raise ApiError('标签不存在')
    if action!='recognize' and 'admin' not in a.identities and a.userid not in (tag['user_id'],tag['created_by']): raise ApiError('无权操作该标签')
    if action=='update':
        name=text(data,'name')
        if not name: raise ApiError('请输入个性标签')
        if len(name)>20: raise ApiError('标签名称最多只能设置20个字')
        duplicate=row(c,'user_tags',user_id=tag['user_id'],name=name)
        if duplicate and duplicate['id']!=tag['id']: raise ApiError('标签已存在')
        update(c,'user_tags',{'id':tag['id']},{'name':name,'updated_by':a.userid}); message='保存成功'
    elif action=='delete':
        r=db.tables['user_tag_recognitions']; c.execute(delete(r).where(r.c.tag_id==tag['id']))
        t=db.tables['user_tags']; c.execute(delete(t).where(t.c.id==tag['id'])); message='删除成功'
    else:
        r=db.tables['user_tag_recognitions']; old=row(c,'user_tag_recognitions',tag_id=tag['id'],user_id=a.userid)
        if old:
            c.execute(delete(r).where(r.c.id==old['id'])); message='已取消认可'
        else:
            db.insert_row(c,'user_tag_recognitions',tag_id=tag['id'],user_id=a.userid); message='认可成功'
    enqueue(c,'accounts.search',{'userid':tag['user_id']})
    return success(tags_meta(c,tag['user_id'],a),message)


def tags_update(c,ctx,data,settings): return tag_mutation(c,ctx,data,settings,'update')
def tags_delete(c,ctx,data,settings): return tag_mutation(c,ctx,data,settings,'delete')
def tags_recognize(c,ctx,data,settings): return tag_mutation(c,ctx,data,settings,'recognize')


def extra(c,ctx,data,settings):
    a=actor(c,ctx); userid=integer(data.get('userid')) or a.userid; user=row(c,'users',userid=userid)
    if not user:
        old=row(c,'user_deletes',userid=userid)
        if not old: raise ApiError('会员不存在')
        user=decoded(old['cache'])
    meta=tags_meta(c,userid,a)
    target_user=row(c,'users',userid=userid)
    visible=bool(target_user and not target_user['bot'] and (a.userid==userid or 'admin' in a.identities))
    if not visible and target_user and not target_user['bot'] and db.setting(c,'system','department_owner_project_view','open')=='open':
        managed=department_users(c,[x['id'] for x in managed_departments(c,a.userid)])
        p,pu=db.tables['projects'],db.tables['project_users']
        visible=userid in managed and c.execute(select(p.c.id).join(pu,pu.c.project_id==p.c.id).where(pu.c.userid==userid,p.c.deleted_at.is_(None),or_(p.c.department_owner_view!='close',p.c.department_owner_view.is_(None))).limit(1)).first() is not None
    return success({'userid':userid,**{k:user.get(k) for k in ('birthday','address','introduction')},'personal_tags':meta['top'],'personal_tags_total':meta['total'],'works_visible':visible})


def email_verification(c,ctx,data,settings):
    code=text(data,'code')
    if not code: raise ApiError('验证码不能为空')
    verification=db.tables['user_email_verifications']
    value=c.execute(select(verification).where(verification.c.code==code,verification.c.type==1)).mappings().first()
    if not value: raise ApiError('无效连接,请重新注册')
    user=auth.lock_user(c,value['userid'])
    if auth.is_disabled(user) or user['email']!=value['email']: raise ApiError('无效连接,请重新注册')
    verify_email(c,user['email'],code,1,consume=False)
    value=c.execute(select(verification).where(verification.c.code==code,verification.c.type==1).with_for_update()).mappings().one()
    update(c,'user_email_verifications',{'id':value['id']},{'status':1})
    update(c,'users',{'userid':value['userid']},{'email_verity':1})
    return success(message='绑定邮箱成功')


def email_send(c,ctx,data,settings):
    a=actor(c,ctx); email=text(data,'email'); kind=integer(data.get('type'),2)
    if not email: raise ApiError('请输入新邮箱地址')
    if not email_valid(email): raise ApiError('邮箱地址错误')
    if email==a.user['email'] and kind==2: raise ApiError('不能与旧邮箱一致')
    if email!=a.user['email'] and kind==3: raise ApiError('与当前登录邮箱不一致')
    existing=row(c,'users',email=email)
    if existing and existing['userid']!=a.userid: raise ApiError('邮箱地址已存在')
    send_verification(c,ctx,a.user,kind,email)
    return success(message='发送成功')


def email_edit(c,ctx,data,settings):
    a=actor(c,ctx); account_policy(a.user)
    if a.session['purpose']=='service': raise ApiError('服务身份不能修改邮箱')
    if 'ldap' in a.identities: raise ApiError('LDAP 用户禁止修改邮箱')
    email=text(data,'newEmail')
    if not email: raise ApiError('请输入新邮箱地址')
    if not email_valid(email): raise ApiError('邮箱地址错误')
    existing=row(c,'users',email=email)
    if existing and existing['userid']!=a.userid: raise ApiError('邮箱地址已存在')
    if db.setting(c,'emailSetting','reg_verify')=='open': verify_email(c,email,text(data,'code'),2)
    user=mutate_user(c,ctx,a.user,{'email':email})
    token=auth.issue_token(c,user,a.credential,ctx)
    return success(auth.user_data(c,user,token,ctx),'修改成功')


def device_list(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_devices']
    result=rows(c,select(t).where(t.c.userid==a.userid,t.c.deleted_at.is_(None)).order_by(t.c.id.desc()).limit(200))
    for value in result:
        value['detail']=decoded(value['detail']); value['is_current']=int(value['id']==a.session['device_id']); value.pop('hash',None)
    return success({'list':result})


def device_logout(c,ctx,data,settings):
    a=actor(c,ctx); value=row(c,'user_devices',id=integer(data.get('id')),userid=a.userid)
    if not value: raise ApiError('设备不存在或已被删除')
    auth.revoke_devices(c,[value['id']]); return success()


def device_edit(c,ctx,data,settings):
    a=actor(c,ctx); changes={k:text(data,k) for k in ('device_name','app_brand','app_model','app_os') if k in data}
    if not changes: raise ApiError('参数错误')
    value=row(c,'user_devices',id=a.session['device_id'],userid=a.userid)
    if not value: raise ApiError('设备不存在或已被删除')
    update(c,'user_devices',{'id':value['id']},{'detail':json.dumps(dict(decoded(value['detail']),**changes),ensure_ascii=False)})
    return success(message='保存成功')


def socket_status(c,ctx,data,settings):
    import redis
    a=actor(c,ctx); fd=text(data,'fd') or ctx.fd
    client=redis.Redis.from_url(settings.redis_url,decode_responses=True,socket_timeout=5)
    try: value=client.hgetall('dootask:connection:'+fd)
    finally: client.close()
    if not value or integer(value.get('userid'))!=a.userid: raise ApiError('error')
    return success({'id':fd,'fd':fd,'userid':a.userid,'updated_at':a.user['line_at']})


def normalize_sorts(value):
    return {k:list(dict.fromkeys(x.strip() for x in (value.get(k,[]) if isinstance(value,dict) and isinstance(value.get(k,[]),list) else []) if isinstance(x,str) and x.strip())) for k in ('base','admin')}


def appsort(c,ctx,data,settings):
    a=actor(c,ctx); found=row(c,'user_app_sorts',userid=a.userid)
    return success({'sorts':normalize_sorts(decoded(found['sorts']) if found else {})})


def appsort_save(c,ctx,data,settings):
    a=actor(c,ctx); value=normalize_sorts(data.get('sorts'))
    upsert(c,'user_app_sorts',{'userid':a.userid},{'sorts':json.dumps(value,ensure_ascii=False)})
    return success({'sorts':value},'保存成功')


ROUTES = {
    'users/login/qrcode':login_qrcode,'users/info/managed_departments':info_managed,'users/info/departments':info_departments,
    'contacts/departments':departments,'users/editdata':editdata,'users/search':search,'users/basic':basic,'users/extra':extra,
    'users/tags/lists':tags_lists,'users/tags/add':tags_add,'users/tags/update':tags_update,'users/tags/delete':tags_delete,'users/tags/recognize':tags_recognize,
    'users/email/verification':email_verification,'users/email/send':email_send,'users/email/edit':email_edit,
    'users/device/list':device_list,'users/device/logout':device_logout,'users/device/edit':device_edit,'users/socket/status':socket_status,
    'users/appsort':appsort,'users/appsort/save':appsort_save,
}

# Explicit module ownership; no module scanning or string-to-handler reflection.
from .account_admin import ROUTES as ADMIN_ROUTES
from .account_import import ROUTES as IMPORT_ROUTES
from .account_meetings import ROUTES as MEETING_ROUTES
from .account_checkin import ROUTES as CHECKIN_ROUTES
from .account_history import ROUTES as HISTORY_ROUTES
from .account_misc import ROUTES as MISC_ROUTES
ROUTES.update(ADMIN_ROUTES)
ROUTES.update(IMPORT_ROUTES)
ROUTES.update(MEETING_ROUTES)
ROUTES.update(CHECKIN_ROUTES)
ROUTES.update(HISTORY_ROUTES)
ROUTES.update(MISC_ROUTES)
RESPONSE_MODE = 'envelope_or_response'


def register_jobs():
    from . import account_jobs, account_bot_jobs, account_system_files
    account_jobs.register_jobs()
    account_jobs.register_housekeeping()
    account_bot_jobs.register_jobs()
    account_system_files.register_jobs()
