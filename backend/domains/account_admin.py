"""Organization/admin mutations, sharing the caller's transaction."""
import json
import secrets
from datetime import datetime
from sqlalchemy import select, delete, func, or_
from .. import auth, db
from ..context import utcnow
from ..errors import ApiError, success, json_default
from ..jobs import enqueue, enqueue_push
from ..security import password_hash, password_verify
from .account_common import *
from .account_integrations import ldap_update, verify_email
from . import accounts
from .dialog_core import create_group, sync_members, member_ids, get_dialog


def checked_departments(c,value):
    values=ids(value) if isinstance(value,list) else []
    if len(values)>10: raise ApiError('最多只可加入10个部门')
    t=db.tables['user_departments']
    found=list(c.execute(select(t.c.id).where(t.c.id.in_(values))).scalars())
    if len(found)!=len(values): raise ApiError('修改部门不存在')
    return values


def set_departments(c,ctx,user,wanted):
    old=set(ids(user['department'])); wanted=set(wanted)
    result=accounts.mutate_user(c,ctx,user,{'department':packed(sorted(wanted))})
    for depid in old|wanted:
        dep=row(c,'user_departments',id=depid)
        if not dep or not dep['dialog_id']: continue
        get_dialog(c,dep['dialog_id'],lock=True)
        current=set(member_ids(c,dep['dialog_id']))
        current.add(user['userid']) if depid in wanted else current.discard(user['userid'])
        sync_members(c,dep['dialog_id'],current)
        if depid in wanted and depid not in old:
            from .dialog_core import send_message
            send_message(c,dialog_id=dep['dialog_id'],sender_id=0,msg_type='notice',
                msg={'notice':(user['nickname'] or user['email'].split('@')[0])+' 已加入群组'},context=ctx)
            enqueue(c,'dialog.member_hook',{'dialog_id':dep['dialog_id'],'userid':0,'userids':[user['userid']],'event':'member_join'})
    return result


def department_list(c,ctx,data,settings):
    actor(c,ctx,True); t=db.tables['user_departments']; result=rows(c,select(t).order_by(t.c.id)); o=db.tables['user_department_owners']
    owners=rows(c,select(o))
    for value in result: value['deputy_userids']=[x['userid'] for x in owners if x['department_id']==value['id']]
    return success(result)


def save_department(c,ctx,data):
    t=db.tables['user_departments']; depid=integer(data.get('id')); old=row(c,'user_departments',id=depid) if depid else None
    if depid and not old: raise ApiError('部门不存在或已被删除')
    name=text(data,'name'); parent=integer(data.get('parent_id')); owner=integer(data.get('owner_userid'))
    if not 2<=len(name)<=20: raise ApiError('部门名称长度限制2-20个字')
    if any(x in name for x in '~!@#$%^&*()+-_=.:?<>,'): raise ApiError('部门名称不能包含特殊符号')
    if not old and c.execute(select(func.count()).select_from(t)).scalar_one()>200: raise ApiError('最多只能创建200个部门')
    if parent:
        ancestor=row(c,'user_departments',id=parent); seen={depid} if depid else set(); depth=1
        if not ancestor: raise ApiError('上级部门不存在或已被删除')
        while ancestor:
            if ancestor['id'] in seen: raise ApiError('不能选择自己的子部门作为上级部门')
            seen.add(ancestor['id']); depth+=1
            ancestor=row(c,'user_departments',id=ancestor['parent_id']) if ancestor['parent_id'] else None
        if depth>3: raise ApiError('部门层级最多只能创建3级')
        if c.execute(select(func.count()).select_from(t).where(t.c.parent_id==parent,t.c.id!=depid)).scalar_one()>=20: raise ApiError('每个部门最多只能创建20个子部门')
    new_user=row(c,'users',userid=owner)
    if not new_user: raise ApiError('请选择正确的部门负责人')
    if c.execute(select(func.count()).select_from(t).where(t.c.owner_userid==owner,t.c.id!=depid)).scalar_one()>=10: raise ApiError('每个用户最多只能负责10个部门')
    dialogid=old['dialog_id'] if old else 0
    using=not dialogid and text(data,'dialog_group')=='use'
    if using:
        dialogid=integer(data.get('dialog_useid')); dialog=get_dialog(c,dialogid)
        if dialog['type']!='group' or dialog['group_type']!='user': raise ApiError('选择现有聊天群不存在')
    if not dialogid:
        dialogid=create_group(c,owner_id=owner,userids=[owner],name=name,group_type='department')
    else:
        members=set(member_ids(c,dialogid)); members.add(owner)
        if old and old['owner_userid']!=owner: members.discard(old['owner_userid'])
        sync_members(c,dialogid,members,owner_id=owner)
    update(c,'web_socket_dialogs',{'id':dialogid},{'name':name,'owner_id':owner,'group_type':'department'})
    if using:
        members=db.tables['web_socket_dialog_users']; c.execute(members.update().where(members.c.dialog_id==dialogid,members.c.userid!=owner).values(role=0))
    values={'name':name,'parent_id':parent,'owner_userid':owner,'dialog_id':dialogid}
    if old: update(c,'user_departments',{'id':depid},values)
    else: depid=db.insert_row(c,'user_departments',**values)
    o=db.tables['user_department_owners']; c.execute(delete(o).where(o.c.department_id==depid,o.c.userid==owner))
    if old and old['owner_userid']!=owner:
        previous=row(c,'users',userid=old['owner_userid'])
        if previous: accounts.mutate_user(c,ctx,previous,{'department':packed(x for x in ids(previous['department']) if x!=depid)})
    accounts.mutate_user(c,ctx,new_user,{'department':packed(ids(new_user['department'])+[depid])})
    recipients=member_ids(c,dialogid)
    if recipients: enqueue_push(c,userids=recipients,message={'type':'dialog','mode':'groupUpdate','data':{'id':dialogid,'name':name,'owner_id':owner,'group_type':'department','deputy_ids':list(c.execute(select(o.c.userid).where(o.c.department_id==depid)).scalars())}},scope={'dialog_id':dialogid})
    return depid


def department_add(c,ctx,data,settings):
    actor(c,ctx,True); save_department(c,ctx,data); return success(message='保存成功' if integer(data.get('id')) else '新建成功')


def deputy(c,ctx,data,adding):
    actor(c,ctx,True); dep=row(c,'user_departments',id=integer(data.get('id'))); userid=integer(data.get('userid'))
    if not dep: raise ApiError('部门不存在或已被删除')
    t=db.tables['user_department_owners']; user=row(c,'users',userid=userid)
    if adding:
        if not user: raise ApiError('该用户不存在')
        if dep['owner_userid']==userid: raise ApiError('不能将部门负责人任命为部门管理员')
        upsert(c,'user_department_owners',{'department_id':dep['id'],'userid':userid},{})
        set_departments(c,ctx,user,ids(user['department'])+[dep['id']])
        if dep['dialog_id']:
            update(c,'web_socket_dialog_users',{'dialog_id':dep['dialog_id'],'userid':userid},{'role':2,'important':1})
    else:
        count=c.execute(delete(t).where(t.c.department_id==dep['id'],t.c.userid==userid)).rowcount
        if count and user and dep['owner_userid']!=userid: set_departments(c,ctx,user,[x for x in ids(user['department']) if x!=dep['id']])
    if dep['dialog_id']:
        recipient=member_ids(c,dep['dialog_id'])
        if recipient: enqueue_push(c,userids=recipient,message={'type':'dialog','mode':'groupUpdate','data':{'id':dep['dialog_id'],'deputy_ids':list(c.execute(select(t.c.userid).where(t.c.department_id==dep['id'])).scalars())}},scope={'dialog_id':dep['dialog_id']})
    return success(message='任命成功' if adding else '罢免成功')


def department_adddeputy(c,ctx,data,settings): return deputy(c,ctx,data,True)
def department_deldeputy(c,ctx,data,settings): return deputy(c,ctx,data,False)


def department_del(c,ctx,data,settings):
    actor(c,ctx,True); depid=integer(data.get('id')); dep=row(c,'user_departments',id=depid)
    if not dep: raise ApiError('部门不存在或已被删除')
    if row(c,'user_departments',parent_id=depid): raise ApiError('含有子部门无法删除')
    for userid in accounts.department_users(c,[depid],False):
        user=row(c,'users',userid=userid); accounts.mutate_user(c,ctx,user,{'department':packed(x for x in ids(user['department']) if x!=depid)})
    if dep['dialog_id']:
        sync_members(c,dep['dialog_id'],[]); update(c,'web_socket_dialogs',{'id':dep['dialog_id']},{'deleted_at':utcnow()})
    t=db.tables['user_department_owners']; c.execute(delete(t).where(t.c.department_id==depid))
    t=db.tables['user_departments']; c.execute(delete(t).where(t.c.id==depid))
    return success(message='删除成功')


def department_sync(c,ctx,data,settings):
    actor(c,ctx,True); depid=integer(data.get('id')); dep=row(c,'user_departments',id=depid)
    if not dep: raise ApiError('部门不存在或已被删除')
    deps=rows(c,select(db.tables['user_departments'])); scope={depid}
    while True:
        added={x['id'] for x in deps if x['parent_id'] in scope}-scope
        if not added: break
        scope.update(added)
    scope.discard(depid); count=existing=0
    for userid in accounts.department_users(c,scope,False):
        user=row(c,'users',userid=userid)
        if user['disable_at']: continue
        if depid in ids(user['department']): existing+=1
        else:
            set_departments(c,ctx,user,ids(user['department'])+[depid]); count+=1
    return success({'synced_count':count,'already_in_dept_count':existing,'sub_department_ids':sorted(scope)},f'同步完成，共同步 {count} 个成员')


def delete_user(c,ctx,user,reason):
    user=auth.lock_user(c,user['userid'])
    safe=accounts.safe_user(c,user,ctx)
    db.insert_row(c,'user_deletes',operator=ctx.actor.userid,userid=user['userid'],email=user['email'],reason=reason,cache=json.dumps(safe,ensure_ascii=False,default=json_default))
    accounts.revoke_user(c,user['userid'])
    for name,key,value in [('web_socket_dialog_msg_reads','userid',user['userid']),('web_socket_dialog_msg_todos','userid',user['userid']),('user_email_verifications','email',user['email'])]:
        t=db.tables[name]; c.execute(delete(t).where(t.c[key]==value))
    accounts.changed(c,ctx,user,None,[],True)
    t=db.tables['users']; c.execute(delete(t).where(t.c.userid==user['userid']))


def delete_account(c,ctx,data,settings):
    a=actor(c,ctx); account_policy(a.user,1); email=text(data,'email')
    if not email: raise ApiError('请输入新邮箱地址')
    if not email_valid(email): raise ApiError('邮箱地址错误')
    if email!=a.user['email']: raise ApiError('与当前登录邮箱不一致')
    if db.setting(c,'emailSetting','reg_verify')=='open': verify_email(c,email,text(data,'code'),3,consume=text(data,'type')=='confirm')
    elif not password_verify(text(data,'password'),a.credential['password_hash']): raise ApiError('密码错误')
    result=accounts.safe_user(c,a.user,ctx)
    if text(data,'type')=='confirm': delete_user(c,ctx,a.user,text(data,'reason'))
    return success(result,'删除成功' if text(data,'type')=='confirm' else 'success')


def operation(c,ctx,data,settings):
    a=actor(c,ctx,True); userid=integer(data.get('userid')); user=auth.lock_user(c,userid)
    account_policy(user,1); kind=text(data,'type'); values={}; identities=auth.split_values(user['identity'])
    if kind in ('setadmin','clearadmin','settemp','cleartemp'):
        role='admin' if 'admin' in kind else 'temp'; identities=[x for x in identities if x!=role]
        if kind.startswith('set'): identities.append(role)
        values['identity']=packed(identities)
    elif kind in ('setverity','clearverity'): values['email_verity']=int(kind=='setverity')
    elif kind=='department': user=set_departments(c,ctx,user,checked_departments(c,data.get('department')))
    elif kind=='setdisable':
        if userid==a.userid: raise ApiError('不能操作自己离职')
        try: values['disable_at']=datetime.fromisoformat(text(data,'disable_time'))
        except ValueError as exc: raise ApiError('离职时间错误') from exc
        values['identity']=packed([x for x in identities if x!='disable']+['disable'])
        transfer=ids(data.get('transfer_userid'))
        if transfer:
            target=row(c,'users',userid=transfer[0])
            if not target: raise ApiError('请选择正确的交接人')
            if target['userid']==userid: raise ApiError('不能移交给自己')
            if auth.is_disabled(target): raise ApiError('交接人已离职，请选择另一个交接人')
            transferid=db.insert_row(c,'user_transfers',original_userid=userid,new_userid=target['userid'])
            enqueue(c,'accounts.transfer',{'transfer_id':transferid,'operator':a.userid,'base_url':ctx.base_url,'language':ctx.language})
    elif kind=='cleardisable': values.update(disable_at=None,identity=packed(x for x in identities if x!='disable'))
    elif kind=='delete':
        if userid==a.userid: raise ApiError('不能删除自己')
        if not text(data,'delete_reason'): raise ApiError('请填写删除原因')
        delete_user(c,ctx,user,text(data,'delete_reason')); return success(accounts.safe_user(c,user,ctx),'删除成功')
    elif kind in ('checkin_macs','checkin_face'):
        from .account_checkin import save_macs, save_face
        return save_macs(c,userid,data.get('checkin_macs',[])) if kind=='checkin_macs' else save_face(c,ctx,user,text(data,'checkin_face'),'管理员上传',settings)
    profile={k:v for k,v in data.items() if k in ('nickname','profession','userimg')}
    values.update(accounts.profile_values(c,user,profile))
    for field in ('email','tel'):
        if field in data:
            value=text(data,field); t=db.tables['users']
            if c.execute(select(t.c.userid).where(t.c[field]==value,t.c.userid!=userid)).first(): raise ApiError('邮箱地址已存在' if field=='email' else '联系电话已存在')
            if field=='email' and 'ldap' in identities: raise ApiError('LDAP 用户禁止修改邮箱')
            values[field]=value
    if 'password' in data:
        password=text(data,'password'); auth.validate_password(c,password)
        update(c,'auth_credentials',{'userid':userid},{'password_hash':password_hash(password),'generation':secrets.token_hex(32)})
        values.update(encrypt=secrets.token_hex(16),changepass=int(integer(data.get('changepass'),1)==1)); accounts.revoke_user(c,userid)
    user=accounts.mutate_user(c,ctx,user,values)
    ldap_changes={dst:values[src] for src,dst in [('nickname','displayName'),('profession','employeeType'),('tel','mobile')] if src in values}
    if 'password' in data: ldap_changes['userPassword']=text(data,'password')
    ldap_update(c,user['email'],ldap_changes)
    if kind=='cleardisable' and db.setting(c,'system','all_group_autoin','yes')=='yes':
        t=db.tables['web_socket_dialogs']; group=c.execute(select(t.c.id).where(t.c.group_type=='all',t.c.deleted_at.is_(None)).order_by(t.c.id.desc())).first()
        if group: sync_members(c,group.id,set(member_ids(c,group.id))|{userid})
    return success(accounts.safe_user(c,user,ctx),'修改成功')


def lists(c,ctx,data,settings):
    actor(c,ctx,True); t=db.tables['users']; query=select(t); keys=data.get('keys') or {}; keys=keys if isinstance(keys,dict) else {}
    key=text(keys,'key')
    if key:
        query=query.where(t.c.email.like('%'+key+'%') if '@' in key else or_(*(t.c[f].like('%'+key+'%') for f in ('email','tel','nickname','profession'))))
    else:
        for field in ('email','tel','nickname','profession'):
            if text(keys,field): query=query.where(t.c[field].like('%'+text(keys,field)+'%'))
    identity=text(keys,'identity')
    if identity: query=query.where(~t.c.identity.like('%,'+identity[2:]+',%') if identity.startswith('no') else t.c.identity.like('%,'+identity+',%'))
    if keys.get('disable')=='yes': query=query.where(t.c.disable_at.is_not(None)).order_by(t.c.disable_at.desc())
    elif keys.get('disable')!='all': query=query.where(t.c.disable_at.is_(None))
    if keys.get('bot')!='all': query=query.where(t.c.bot==int(keys.get('bot')=='yes'))
    if keys.get('email_verity') in ('yes','no'): query=query.where(t.c.email_verity==int(keys['email_verity']=='yes'))
    if 'department' in keys:
        depid=integer(keys['department']); dep=row(c,'user_departments',id=depid)
        query=query.where(or_(func.find_in_set(str(depid),t.c.department)>0,t.c.userid==(dep['owner_userid'] if dep else -1)) if depid else or_(t.c.department=='',t.c.department==',,',t.c.department.is_(None)))
        if depid:
            principal=(t.c.userid==(dep['owner_userid'] if dep else -1))
            query=query.add_columns(principal.label('is_principal')).order_by(principal.desc())
    if integer(data.get('get_checkin_data'))==1:
        for key,name,column in [('checkin_face','user_checkin_faces','faceimg'),('checkin_mac','user_checkin_macs','mac')]:
            if key in keys:
                x=db.tables[name]; query=query.where(t.c.userid.in_(select(x.c.userid).where(x.c[column].is_not(None) if key=='checkin_face' else x.c[column].like('%'+str(keys[key])+'%'))))
    result=paginate(c,query.order_by(t.c.userid.desc()),data,50,20)
    for i,user in enumerate(result['data']):
        value=accounts.safe_user(c,user,ctx); value['nickname_original']=user['nickname']
        if integer(data.get('get_checkin_data'))==1:
            f=row(c,'user_checkin_faces',userid=user['userid']); value['checkin_face']=url(ctx,f['faceimg']) if f else ''; m=db.tables['user_checkin_macs']; value['checkin_macs']=rows(c,select(m.c.id,m.c.mac,m.c.remark).where(m.c.userid==user['userid']).order_by(m.c.id))
        result['data'][i]=value
    return success(result)


ROUTES={'users/department/list':department_list,'users/department/add':department_add,'users/department/adddeputy':department_adddeputy,'users/department/deldeputy':department_deldeputy,'users/department/del':department_del,'users/department/sync':department_sync,'users/operation':operation,'users/lists':lists,'users/delete/account':delete_account}
