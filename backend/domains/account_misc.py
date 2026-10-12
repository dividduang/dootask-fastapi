"""Remaining account reporting/sharing and device alias handlers."""
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_, or_, literal_column
from .. import db
from ..context import utcnow
from ..errors import ApiError, success
from .account_common import *
from . import files
from .dialog_core import dialog_data, ensure_user_dialog


def umeng_alias(c,ctx,data,settings):
    if integer(data.get('isDebug')): raise ApiError('调试模式下不允许使用')
    alias=text(data,'alias')
    if not alias: raise ApiError('别名不能为空')
    if not 2<=len(alias)<=20: raise ApiError('别名的长度在2-20个字符')
    a=actor(c,ctx); t=db.tables['umeng_alias']
    if text(data,'action')=='remove':
        c.execute(t.delete().where(t.c.alias==alias,t.c.userid==a.userid)); return success(message='删除成功')
    if ctx.platform not in ('ios','android'): raise ApiError('设备类型错误')
    where={'userid':a.userid,'alias':alias,'platform':ctx.platform}; old=row(c,'umeng_alias',**where); device=row(c,'user_devices',id=a.session['device_id'])
    version=text(data,'appVersionName')+' ('+str(data['appVersion'])+')' if data.get('appVersion') else ''
    upsert(c,'umeng_alias',where,{'ua':text(data,'userAgent'),'device':text(data,'deviceModel'),'device_hash':device['hash'],'version':version,'is_notified':int(str(data.get('isNotified')).lower()=='true') if str(data.get('isNotified')).lower() in ('true','false') else integer(data.get('isNotified'))})
    return success(message='别名已存在' if old else '添加成功')


def share_list(c,ctx,data,settings):
    a=actor(c,ctx); kind=text(data,'type','file'); key=text(data,'key'); pid=integer(data.get('pid'),-1); uploadid=integer(data.get('upload_file_id'),-1)
    if uploadid!=-1:
        upload=data.get('files') or data.get('file')
        if not hasattr(upload,'file'): raise ApiError('请选择文件')
        return success(files.upload_path(c,ctx,settings,upload.file,upload.filename,dict(data,pid=max(0,pid))))
    result=[]
    if kind=='file' and pid!=-1:
        listing=files.lists(c,ctx,{'pid':pid,'scope':'all'},settings)
        for item in listing:
            if item['id']!=pid and item['type']=='folder': result.append({'type':'children','url':url(ctx,'api/users/share/list?pid='+str(item['id'])),'icon':url(ctx,'images/file/light/folder-share.png' if item['share']==1 else 'images/file/light/folder.png'),'extend':{'upload_file_id':item['id']},'name':item['name']})
        return success(result)
    if kind=='file': result.append({'type':'children','url':url(ctx,'api/users/share/list?pid=0'),'icon':url(ctx,'images/file/light/folder.png'),'extend':{'upload_file_id':0},'name':'文件','sort':253370764800})
    dialogs,members=db.tables['web_socket_dialogs'],db.tables['web_socket_dialog_users']; own=select(members.c.dialog_id).where(members.c.userid==a.userid)
    query=select(dialogs.c.id).where(dialogs.c.id.in_(own),dialogs.c.deleted_at.is_(None))
    if key: query=query.where(dialogs.c.name.like('%'+key+'%'))
    dialogids=list(c.execute(query.order_by(dialogs.c.updated_at.desc()).limit(50)).scalars())
    if key and len(dialogids)<50:
        users=db.tables['users']
        candidates=c.execute(select(users.c.userid).where(or_(users.c.email.like('%'+key+'%'),users.c.nickname.like('%'+key+'%'),users.c.pinyin.like('%'+key+'%'),users.c.profession.like('%'+key+'%')),users.c.disable_at.is_(None)).order_by(users.c.userid).limit(50-len(dialogids))).scalars()
        for userid in candidates:
            dialogid=ensure_user_dialog(c,a.userid,userid)
            if dialogid not in dialogids: dialogids.append(dialogid)
    for dialogid in dialogids:
        value=dialog_data(c,dialogid,a.userid); image=value.get('avatar') or value.get('userimg')
        if not image:
            group={'department':'department','project':'project','task':'task'}.get(value.get('group_type'),'people'); image='images/avatar/default_group_'+group+'.png'
        last=value.get('last_at'); timestamp=int(last.timestamp()) if isinstance(last,datetime) else 0
        result.append({'type':'item','name':value.get('name',''),'icon':url(ctx,image),'url':url(ctx,'api/dialog/msg/'+('sendfiles' if kind=='file' else 'sendtext')),'sort':timestamp,'extend':{'dialog_ids':dialogid,'text_type':'text','reply_id':0,'silence':'no'}})
    result.sort(key=lambda x:x['sort'],reverse=True)
    return success(result)


def annual_report(c,ctx,data,settings):
    a=actor(c,ctx); year=2023; start=datetime(year,1,1); end=datetime(year+1,1,1)
    t,tu,p,col=db.tables['project_tasks'],db.tables['project_task_users'],db.tables['projects'],db.tables['project_columns']
    taskquery=select(t).join(tu,tu.c.task_id==t.c.id).where(tu.c.userid==a.userid,tu.c.owner==1,t.c.created_at>=start,t.c.created_at<end)
    count=lambda query:c.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
    duration=func.timestampdiff(literal_column('MINUTE'),t.c.start_at,t.c.complete_at)
    durations=select(t.c.id,t.c.flow_item_name,t.c.name.label('task_name'),p.c.name.label('project_name'),col.c.name.label('project_column_name'),t.c.start_at,t.c.end_at,t.c.complete_at,t.c.created_at,duration.label('duration')).select_from(t.join(tu,tu.c.task_id==t.c.id).outerjoin(p,p.c.id==t.c.project_id).outerjoin(col,col.c.id==t.c.column_id)).where(tu.c.userid==a.userid,tu.c.owner==1,t.c.created_at>=start,t.c.created_at<end,t.c.start_at.is_not(None),t.c.complete_at.is_not(None))
    first=lambda query:next(iter(rows(c,query.limit(1))),None)
    m,d,du,u=db.tables['web_socket_dialog_msgs'],db.tables['web_socket_dialogs'],db.tables['web_socket_dialog_users'],db.tables['users']
    counts=select(m.c.dialog_id,func.count().label('chat_num')).where(m.c.userid==a.userid,m.c.created_at>=start,m.c.created_at<end).group_by(m.c.dialog_id).subquery()
    longest=first(select(d.c.id,d.c.name.label('dialog_name'),d.c.type.label('dialog_type'),d.c.group_type.label('dialog_group_type'),counts.c.chat_num,u.c.userid,u.c.email.label('user_email'),u.c.nickname.label('user_nickname'),func.coalesce(d.c.avatar,u.c.userimg).label('avatar')).select_from(d.outerjoin(counts,counts.c.dialog_id==d.c.id).outerjoin(du,and_(du.c.dialog_id==d.c.id,du.c.userid!=a.userid,d.c.type=='user')).outerjoin(u,u.c.userid==du.c.userid)).where(or_(d.c.type!='user',u.c.bot==0)).order_by(counts.c.chat_num.desc()))
    if longest:
        if longest['avatar']: longest['avatar']=url(ctx,longest['avatar'])
        elif longest['dialog_type']=='user': longest['avatar']=url(ctx,'avatar/'+str(longest['user_nickname'] or longest['user_email'] or '')+'.png')
        else: longest['avatar']=url(ctx,'images/avatar/default_group_'+{'department':'department','project':'project','task':'task'}.get(longest['dialog_group_type'],'people')+'.png')
    records=db.tables['user_checkin_records']; latest=first(select(records).where(records.c.userid==a.userid,func.year(func.from_unixtime(records.c.report_time))==year).order_by(func.time_format(func.timestampadd(literal_column('HOUR'),18,func.from_unixtime(records.c.report_time)),'%H%i%s').desc()))
    ai=count(select(m.c.id).select_from(m.join(d,d.c.id==m.c.dialog_id).join(du,du.c.dialog_id==d.c.id).join(u,u.c.userid==du.c.userid)).where(u.c.email.like('%ai-%'),u.c.bot==1,m.c.userid==a.userid,m.c.created_at>=start,m.c.created_at<end))
    f=db.tables['files']; pu=db.tables['project_users']; projects=rows(c,select(p.c.id,p.c.name).select_from(p.join(pu,pu.c.project_id==p.c.id).join(tu,tu.c.project_id==p.c.id)).where(or_(and_(pu.c.userid==a.userid,pu.c.created_at>=start,pu.c.created_at<end),and_(tu.c.userid==a.userid,tu.c.created_at>=start,tu.c.created_at<end))).group_by(p.c.id,p.c.name).limit(100))
    safe=safe_user(c,a.user,ctx); hired=a.user['created_at']; sentinel=datetime(2300,1,1,0,0,1)
    completed=rows(c,select(func.month(t.c.complete_at).label('month'),func.count(t.c.id).label('num')).select_from(t.join(tu,tu.c.task_id==t.c.id)).where(tu.c.userid==a.userid,tu.c.owner==1,t.c.created_at>=start,t.c.created_at<end,t.c.complete_at>=start,t.c.complete_at<end).group_by(func.month(t.c.complete_at)))
    return success({'user':{'userid':a.userid,'email':safe['email'],'nickname':safe['nickname'],'avatar':safe['userimg']},'hire_date':hired.date(),'tenure_days':(utcnow().replace(hour=0,minute=0,second=0,microsecond=0)-hired).days,'latest_online_time':datetime.utcfromtimestamp(latest['report_time'] or 0) if latest else datetime(1970,1,1),'longest_chat_user':longest,'chat_al_num':ai,'file_created_num':count(select(f.c.id).where(f.c.created_id==a.userid,f.c.created_at>=start,f.c.created_at<end,f.c.deleted_at.is_(None))),'projects':projects,'tasks':{'total':count(taskquery),'completed':count(taskquery.where(t.c.complete_at.is_not(None))),'overtime':count(taskquery.where(func.coalesce(t.c.complete_at,sentinel)>func.coalesce(t.c.end_at,sentinel))),'longest_task':first(durations.order_by(duration.desc())),'fastest_task':first(durations.order_by(duration)),'month_completed_task':completed}})


ROUTES={'users/umeng/alias':umeng_alias,'users/share/list':share_list,'users/annual/report':annual_report}
