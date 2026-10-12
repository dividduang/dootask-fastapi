"""Account observer and offboarding consumers, registered explicitly at startup."""
from sqlalchemy import select, delete
from .. import auth, db, jobs
from ..context import RequestContext
from ..errors import ApiError
from .account_common import row, rows, update, ids
from .account_integrations import ldap_update
from .account_admin import save_department, deputy
from .dialog_core import sync_members, member_ids


def search_job(c,payload,settings):
    jobs.enqueue(c,'search.sync',{'type':'user','id':payload['userid']})


def created_job(c,payload,settings):
    from .apps import dispatch_user_hook
    user=row(c,'users',userid=payload['userid'])
    if not user or user['bot']:
        return
    dispatch_user_hook(c,user,'user_onboard','onboard',[],settings)


def changed_job(c,payload,settings):
    from .apps import dispatch_user_hook
    fields=set(payload['fields']); before=payload['before']; after=payload.get('after'); userid=payload['userid']
    for profile in (before,after):
        if profile is not None and isinstance(profile.get('identity'),list):
            profile['identity']=','.join(profile['identity'])
    deleted=payload.get('deleted',False)
    if deleted or fields&{'nickname','email','profession','introduction','disable_at'}:
        jobs.enqueue(c,'search.sync',{'type':'user','id':userid})
    if deleted or ('disable_at' in fields and after.get('disable_at')):
        badges=db.tables['app_badges']; c.execute(delete(badges).where(badges.c.userid==userid))
    if deleted:
        if not before.get('bot'): dispatch_user_hook(c,before,'user_offboard','delete',[],settings)
        ldap_update(c,before['email'],{},delete_user=True)
    elif 'disable_at' in fields:
        if before.get('disable_at') and not after.get('disable_at'): dispatch_user_hook(c,after,'user_onboard','restore',[],settings)
        elif not before.get('disable_at') and after.get('disable_at'): dispatch_user_hook(c,after,'user_offboard','offboarded',[],settings)
    elif 'identity' not in fields:
        monitored=fields&{'email','tel','nickname','profession','birthday','address','introduction','department'}
        if monitored: dispatch_user_hook(c,after,'user_update','profile_update' if payload['operator']==userid else 'admin_update',sorted(monitored),settings)


def transfer_job(c,payload,settings):
    from .projects import transfer_user_memberships
    from .files import transfer_user_files
    transfer=row(c,'user_transfers',id=payload['transfer_id'])
    if not transfer: raise ApiError('交接记录不存在')
    operator=row(c,'users',userid=payload['operator'])
    if not operator or 'admin' not in auth.split_values(operator['identity']) or auth.is_disabled(operator): raise ApiError('交接发起管理员已失去权限')
    ctx=RequestContext(request_id='account-transfer:'+str(payload['_event_id']),base_url=payload['base_url'],language=payload['language'],actor=auth.Actor(operator['userid'],operator,{},{}))
    old,new=transfer['original_userid'],transfer['new_userid']; t=db.tables['user_departments']
    for dep in rows(c,select(t).where(t.c.owner_userid==old)):
        save_department(c,ctx,dict(dep,owner_userid=new))
    o=db.tables['user_department_owners']
    for depid in list(c.execute(select(o.c.department_id).where(o.c.userid==old)).scalars()):
        dep=row(c,'user_departments',id=depid)
        if dep and dep['owner_userid']!=new: deputy(c,ctx,{'id':depid,'userid':new},True)
        deputy(c,ctx,{'id':depid,'userid':old},False)
    transfer_user_memberships(c,ctx,old,new)
    transfer_user_files(c,ctx,old,new)
    d,m=db.tables['web_socket_dialogs'],db.tables['web_socket_dialog_users']
    groups=rows(c,select(d).where(d.c.type=='group',d.c.group_type!='okr',d.c.deleted_at.is_(None),d.c.id.in_(select(m.c.dialog_id).where(m.c.userid==old))))
    for group in groups:
        wanted=set(member_ids(c,group['id']))-{old}
        owner=None
        if group['owner_id']==old: wanted.add(new); owner=new
        sync_members(c,group['id'],wanted,owner_id=owner)


def register_jobs():
    jobs.register('accounts.changed',changed_job)
    jobs.register('accounts.search',search_job)
    jobs.register('accounts.transfer',transfer_job)
    from .account_integrations import verification_job
    jobs.register('accounts.created',created_job)
    jobs.register('accounts.verify_email',verification_job)


def expire_job(c,payload,settings):
    from datetime import timedelta
    from ..context import utcnow
    t=db.tables['user_devices']
    device_ids=list(c.execute(select(t.c.id).where(t.c.expired_at<utcnow()-timedelta(hours=24),t.c.deleted_at.is_(None))).scalars())
    auth.revoke_devices(c,device_ids)
    logs=db.tables['umeng_logs']
    c.execute(delete(logs).where(logs.c.created_at<utcnow()-timedelta(hours=72)))


def register_housekeeping():
    jobs.register('accounts.expire',expire_job)
    jobs.schedule('accounts.expire',3600)
