"""Favorites/recent browsing enforce current object ACL, not stale saved ownership."""
import html
import re
from sqlalchemy import select, delete, func
from .. import db
from ..context import utcnow
from ..errors import ApiError, success
from .account_common import *
from . import files
from .dialog_core import get_message, get_dialog

FAVORITE_TYPES=('task','project','file','message')


def target(c,ctx,kind,ident):
    if kind in ('task','project','task_file'):
        from .projects import require_task, require_project
        if kind=='task': return require_task(c,ctx,ident)
        if kind=='project': return require_project(c,ctx,ident)
        value=row(c,'project_task_files',id=ident)
        if not value: raise ApiError('文件不存在')
        require_task(c,ctx,value['task_id']); return value
    if kind=='file': return files.require(c,ctx,ident)
    if kind in ('message','message_file'): return get_message(c,ident,ctx.actor.userid)
    raise ApiError('无效的收藏类型')


def task_brief(task):
    parts=(task.get('flow_item_name') or '').split('|')
    result={k:task.get(k) for k in ('id','name','project_id','column_id','parent_id','flow_item_id','complete_at')}
    result.update(flow_item_name=parts[1] if len(parts)>1 else task.get('flow_item_name'),flow_item_status=parts[0],flow_item_color=parts[2] if len(parts)>2 else '')
    return result


def preview_message(value):
    from ..errors import translate
    kind=value.get('type'); msg=decoded(value.get('msg'))
    cut=lambda text:str(text or '')[:50]+('...' if len(str(text or ''))>50 else '')
    label=lambda text:'['+translate(text)+']'
    if kind in ('text','vote','word-chain'):
        text=str(msg.get('text') or '')
        if msg.get('type')=='md':
            text=re.sub(r'</?tool-use[^>]*>','',text); text=re.sub(r':::\s*reasoning[\s\S]*?:::','',text)
            if re.search(r':::\s*reasoning\s+',text): return translate('思考中...')
            title=re.search(r'^#{1,2}\s+(.+)',text,re.M)
            if title: text=title[1].strip()
            else:
                import markdown
                text=markdown.markdown(text)
        text=re.sub(r'<img\s+class="emoticon"[^>]*?alt="(\S+)"[^>]*?>',r'[\1]',text)
        text=re.sub(r'<img\s+class="emoticon"[^>]*?>',label('动画表情'),text)
        text=re.sub(r'<img\s+class="browse"[^>]*?>',label('图片'),text)
        text=html.unescape(re.sub('<[^>]*>','',text.replace('</p><p>','</p> <p>')))
        return (label('投票')+' ' if kind=='vote' else label('接龙')+' ' if kind=='word-chain' else '')+cut(re.sub(r'\s+',' ',text))
    if kind=='longtext': return cut(msg.get('desc')) or label('长文本')
    if kind=='record': return label('语音')
    if kind=='location': return label('位置')+' '+cut(msg.get('title'))
    if kind=='meeting': return label('会议')+' '+cut(msg.get('name'))
    if kind=='file': return label('图片') if msg.get('type')=='img' else label('视频') if msg.get('ext')=='mp4' else label('文件')+' '+cut(msg.get('name'))
    if kind in ('tag','top','todo'):
        labels={'tag':('标注','取消标注'),'top':('置顶','取消置顶'),'todo':('设待办','取消待办')}; action=labels[kind][int(msg.get('action')=='remove')]
        if kind=='todo' and msg.get('action')=='done': action='完成'
        return label(action)+' '+preview_message(msg.get('data') or {})
    if kind=='notice': return cut(msg.get('notice') if msg.get('source')=='api' else translate(msg.get('notice') or ''))
    if kind=='template':
        authored=lambda text:text if msg.get('source')=='api' else translate(text or '')
        if msg.get('title_raw'): return msg['title_raw']
        if msg.get('type')=='task_list' and len(msg.get('list',[]))==1: return authored(msg.get('title'))+': '+cut(msg['list'][0].get('name'))
        if msg.get('title'): return authored(msg['title'])
        if msg.get('type')=='content' and isinstance(msg.get('content'),str): return cut(authored(msg['content']))
        return translate('未知的消息')
    if kind=='merge-forward': return label('聊天记录')
    if kind=='preview': return msg.get('preview','')
    return label('未知的消息')


def task_browse(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_task_browses']; result=[]
    for value in rows(c,select(t).where(t.c.userid==a.userid).order_by(t.c.browsed_at.desc()).limit(max(1,min(50,integer(data.get('limit'),20))))):
        try: task=target(c,ctx,'task',value['task_id'])
        except ApiError: continue
        if task.get('archived_at'): continue
        result.append(dict(task_brief(task),browsed_at=value['browsed_at']))
    return success(result)


def task_browse_save(c,ctx,data,settings):
    a=actor(c,ctx); taskid=integer(data.get('task_id'))
    if taskid<=0: raise ApiError('参数错误')
    target(c,ctx,'task',taskid); now=utcnow()
    upsert(c,'user_task_browses',{'userid':a.userid,'task_id':taskid},{'browsed_at':now})
    upsert(c,'user_recent_items',{'userid':a.userid,'target_type':'task','target_id':taskid},{'source_type':'project','source_id':0,'browsed_at':now})
    return success(message='记录成功')


def task_browse_clean(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_task_browses']; count=max(0,integer(data.get('keep_count'),100))
    keep=list(c.execute(select(t.c.id).where(t.c.userid==a.userid).order_by(t.c.browsed_at.desc()).limit(count)).scalars())
    deleted=c.execute(delete(t).where(t.c.userid==a.userid,t.c.id.not_in(keep))).rowcount
    return success({'deleted_count':deleted},'清理完成')


def recent_browse(c,ctx,data,settings):
    a=actor(c,ctx); t=db.tables['user_recent_items']; query=select(t).where(t.c.userid==a.userid)
    if text(data,'type'): query=query.where(t.c.target_type==text(data,'type'))
    page=paginate(c,query.order_by(t.c.browsed_at.desc()),data,100,20,'page_size'); result=[]
    for value in page['data']:
        kind=value['target_type']
        try: item=target(c,ctx,kind,value['target_id'])
        except ApiError: continue
        base={'record_id':value['id'],'source_type':value['source_type'],'source_id':value['source_id'],'browsed_at':value['browsed_at'] or value['updated_at'],'type':kind,'id':item['id']}
        if kind=='task':
            if item.get('archived_at'): continue
            project=row(c,'projects',id=item['project_id']); base.update(task_brief(item),project_name=project['name'] if project else ''); base.pop('parent_id',None)
        elif kind=='file': base.update(name=item['name'],ext=item['ext'],size=int(item['size'] or 0),file_type=item['type'],folder_id=int(item['pid'] or 0))
        elif kind=='task_file':
            project=row(c,'projects',id=item['project_id']); task=row(c,'project_tasks',id=item['task_id'])
            base.update({k:item[k] for k in ('name','ext','size','task_id','project_id')},task_name=task['name'] if task else '',project_name=project['name'] if project else '')
        elif kind=='message_file':
            if item['type']!='file': continue
            msg=decoded(item['msg']); dialog=get_dialog(c,item['dialog_id'],a.userid); base.update(name=msg.get('name',''),ext=msg.get('ext',''),size=integer(msg.get('size')),dialog_id=item['dialog_id'],dialog_name=dialog['name'])
        result.append(base)
    return success({'list':result,'page':page['current_page'],'page_size':page['per_page'],'total':page['total']})


def recent_delete(c,ctx,data,settings):
    a=actor(c,ctx); ident=integer(data.get('id'))
    if ident<=0: raise ApiError('参数错误')
    t=db.tables['user_recent_items']; deleted=c.execute(delete(t).where(t.c.userid==a.userid,t.c.id==ident)).rowcount
    if not deleted: raise ApiError('记录不存在')
    return success(message='删除成功')


def favorites(c,ctx,data,settings):
    a=actor(c,ctx); kind=text(data,'type'); t=db.tables['user_favorites']
    if kind and kind not in FAVORITE_TYPES: raise ApiError('无效的收藏类型')
    query=select(t).where(t.c.userid==a.userid)
    if kind: query=query.where(t.c.favoritable_type==kind)
    page=paginate(c,query.order_by(t.c.created_at.desc()),data); output={k:[] for k in ('tasks','projects','files','messages')}
    for value in page.pop('data'):
        kind=value['favoritable_type']
        try: item=target(c,ctx,kind,value['favoritable_id'])
        except ApiError: continue
        result={'id':item['id'],'favorited_at':value['created_at'],'remark':value['remark']}
        if kind=='task':
            project=row(c,'projects',id=item['project_id']); result.update(task_brief(item),project_name=project['name'] if project else '')
            result.pop('parent_id',None); result.pop('column_id',None)
        elif kind=='project': result.update({k:item[k] for k in ('name','desc','archived_at')})
        elif kind=='file':
            value_view=files.view(c,ctx,settings,item); result.update({k:value_view.get(k) for k in ('name','ext','size','pid','image_url','image_width','image_height')})
        else: result.update(name=preview_message(item) or '['+item['type'].capitalize()+']',dialog_id=item['dialog_id'],userid=item['userid'],type=item['type'])
        output[kind+'s'].append(result)
    return success({'data':output,**{k:page[k] for k in ('total','current_page','per_page','last_page')}})


def favorite_parameters(c,ctx,data):
    a=actor(c,ctx); kind=text(data,'type'); ident=integer(data.get('id'))
    if not kind or ident<=0: raise ApiError('参数错误')
    if kind not in FAVORITE_TYPES: raise ApiError('无效的收藏类型')
    return a,kind,ident


def favorite_toggle(c,ctx,data,settings):
    a,kind,ident=favorite_parameters(c,ctx,data); target(c,ctx,kind,ident); t=db.tables['user_favorites']; where={'userid':a.userid,'favoritable_type':kind,'favoritable_id':ident}; value=row(c,'user_favorites',**where)
    if value: c.execute(delete(t).where(t.c.id==value['id']))
    else: db.insert_row(c,'user_favorites',**where)
    return success({'favorited':not bool(value),'action':'removed' if value else 'added','remark':''},'取消收藏成功' if value else '收藏成功')


def favorite_remark(c,ctx,data,settings):
    a,kind,ident=favorite_parameters(c,ctx,data); remark=text(data,'remark')
    if not remark: raise ApiError('请输入修改备注')
    if len(remark)>255: raise ApiError('备注最多支持255个字符')
    where={'userid':a.userid,'favoritable_type':kind,'favoritable_id':ident}
    if not row(c,'user_favorites',**where): raise ApiError('收藏记录不存在')
    update(c,'user_favorites',where,{'remark':remark}); return success({'remark':remark},'修改备注成功')


def favorites_clean(c,ctx,data,settings):
    a=actor(c,ctx); kind=text(data,'type'); t=db.tables['user_favorites']; query=delete(t).where(t.c.userid==a.userid)
    if kind:
        if kind not in FAVORITE_TYPES: raise ApiError('无效的收藏类型')
        query=query.where(t.c.favoritable_type==kind)
    return success({'deleted_count':c.execute(query).rowcount},'清理'+kind+'收藏成功' if kind else '清理全部收藏成功')


def favorite_check(c,ctx,data,settings):
    a,kind,ident=favorite_parameters(c,ctx,data)
    return success({'favorited':bool(row(c,'user_favorites',userid=a.userid,favoritable_type=kind,favoritable_id=ident))})


ROUTES={'users/task/browse':task_browse,'users/task/browse_save':task_browse_save,'users/task/browse_clean':task_browse_clean,'users/recent/browse':recent_browse,'users/recent/delete':recent_delete,'users/favorites':favorites,'users/favorite/toggle':favorite_toggle,'users/favorite/remark':favorite_remark,'users/favorites/clean':favorites_clean,'users/favorite/check':favorite_check}
