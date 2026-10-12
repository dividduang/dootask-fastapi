"""Source AI task suggestion workflow, with durable failures and recipient ACL filtering."""
import re
from datetime import timedelta
from sqlalchemy import select, or_
from sqlalchemy.dialects.mysql import insert
from .. import db, jobs
from ..context import utcnow
from .project_core import table, row, rows, by_id, update, count, ids, decode, encode, member_map, task_allowed_users, can_view_task

EVENT_TYPES = ('description', 'subtasks', 'assignee', 'similar')
TITLES = {
    'zh': ['建议补充任务描述', '建议拆分子任务', '推荐负责人', '发现相似任务', '以下任务与当前任务内容相似，可能是重复任务或可作为参考：'],
    'zh-CHT': ['建議補充任務描述', '建議拆分子任務', '推薦負責人', '發現相似任務', '以下任務與當前任務內容相似，可能是重複任務或可作為參考：'],
    'en': ['Suggested Task Description', 'Suggested Subtasks', 'Recommended Assignee', 'Similar Tasks Found', 'The following tasks are similar and may be duplicates or references:'],
    'ko': ['작업 설명 추가 제안', '하위 작업 분할 제안', '추천 담당자', '유사한 작업 발견', '다음 작업은 현재 작업과 유사하며 중복되거나 참고할 수 있습니다:'],
    'ja': ['タスク説明の追加を提案', 'サブタスクの分割を提案', '推奨担当者', '類似タスクを発見', '以下のタスクは現在のタスクと類似しており、重複している可能性があります：'],
    'de': ['Vorgeschlagene Aufgabenbeschreibung', 'Vorgeschlagene Unteraufgaben', 'Empfohlener Verantwortlicher', 'Ähnliche Aufgaben gefunden', 'Die folgenden Aufgaben sind ähnlich und könnten Duplikate oder Referenzen sein:'],
    'fr': ['Description de tâche suggérée', 'Sous-tâches suggérées', 'Responsable recommandé', 'Tâches similaires trouvées', 'Les tâches suivantes sont similaires et peuvent être des doublons ou des références:'],
    'id': ['Saran Deskripsi Tugas', 'Saran Pembagian Subtugas', 'Penanggung Jawab yang Direkomendasikan', 'Tugas Serupa Ditemukan', 'Tugas berikut mirip dengan tugas saat ini dan mungkin duplikat atau referensi:'],
    'ru': ['Предлагаемое описание задачи', 'Предлагаемые подзадачи', 'Рекомендуемый ответственный', 'Найдены похожие задачи', 'Следующие задачи похожи на текущую и могут быть дубликатами или справочными:'],
}


def scan(connection, payload, settings):
    from .apps import installed_config
    if not installed_config(settings, 'ai') or db.setting(connection, 'system', 'task_ai_auto_analyze', 'open') == 'close':
        return
    now = utcnow()
    t, e, p = table('project_tasks'), table('project_task_ai_events'), table('projects')
    query = select(t).join(p, p.c.id == t.c.project_id).where(t.c.parent_id == 0, t.c.deleted_at.is_(None),
        t.c.archived_at.is_(None), p.c.deleted_at.is_(None), p.c.ai_auto_analyze != 'close',
        t.c.created_at.between(now - timedelta(days=1), now - timedelta(seconds=10)),
        t.c.id.not_in(select(e.c.task_id))).order_by(t.c.created_at).limit(5)
    tasks = [dict(v) for v in connection.execute(query).mappings()]
    if len(tasks) < 5:
        query = select(t).where(t.c.deleted_at.is_(None), t.c.id.in_(select(e.c.task_id).where(e.c.status == 'failed', e.c.retry_count < 3))).limit(5-len(tasks))
        tasks.extend(dict(v) for v in connection.execute(query).mappings())
    for task in tasks:
        for kind in EVENT_TYPES:
            statement = insert(e).values(task_id=task['id'], event_type=kind, status='pending', retry_count=0, created_at=now, updated_at=now)
            connection.execute(statement.on_duplicate_key_update(id=e.c.id))
        jobs.enqueue(connection, 'projects.ai_analyze', {'task_id': task['id']}, dedupe_key=f'ai-task:{task["id"]}:{int(now.timestamp())//60}')


def clean(value, maximum=500):
    return str(value or '').replace('```', '').replace('---', '').replace('===', '').strip()[:maximum]


def parse_subtasks(text):
    return [line for line in (re.sub(r'^\d+[.\)、]\s*', '', v.strip()) for v in text.splitlines()) if line and len(line) <= 100][:5]


def parse_assignees(text, members):
    mapping, result, added = {m['userid']: m for m in members}, [], set()
    for line in text.splitlines():
        line = re.sub(r'^\d+[.\)、]\s*', '', line.strip())
        match = re.fullmatch(r'(\d+)\|(.+)', line)
        if match:
            uid = int(match[1])
            if uid in mapping and uid not in added:
                result.append({'userid': uid, 'nickname': mapping[uid]['nickname'], 'reason': match[2].strip()})
                added.add(uid)
    return result[:2]


def members_info(connection, task):
    t, tu, u = table('project_tasks'), table('project_task_users'), table('users')
    existing = set(connection.execute(select(tu.c.userid).where(tu.c.task_id == task['id'])).scalars())
    candidates = set(member_map(connection, task['project_id'])) - existing
    result = rows(connection, 'users', u.c.userid.in_(candidates), u.c.disable_at.is_(None), u.c.bot == 0)
    for user in result:
        related = select(tu.c.task_id).where(tu.c.userid == user['userid'])
        user['in_progress_count'] = count(connection, 'project_tasks', t.c.id.in_(related), t.c.complete_at.is_(None), t.c.archived_at.is_(None), t.c.deleted_at.is_(None))
        user['completed_count'] = count(connection, 'project_tasks', t.c.id.in_(related), t.c.complete_at >= utcnow()-timedelta(days=30), t.c.deleted_at.is_(None))
    return result


def prompt(kind, task, project, column, content, members, language):
    base = f'所属项目：{clean(project["name"], 100)}\n所属栏目：{clean((column or {}).get("name", "未知栏目"), 50)}\n任务标题：{clean(task["name"], 100)}\n'
    language_rule = f'输出语言：与任务标题的语言保持一致，如无法确定则使用{language}。\n'
    if kind == 'description':
        return ('你是一名任务规划助手，擅长根据任务标题推断并补充任务描述。\n' + base +
            '你的任务：根据标题、项目和栏目信息，推断任务意图并生成实用的任务描述。\n生成原则：\n'
            '1. 基于标题关键词和上下文进行合理推断，内容要具体、可执行\n'
            '2. 使用 Markdown 格式，根据任务性质灵活组织结构（可包含目标、要求、验收标准等）\n'
            '3. 简单任务保持简洁，复杂任务可适当展开，避免空泛的套话\n' + language_rule +
            '输出要求：仅返回 Markdown 格式的描述内容；禁止输出额外说明、引导语或与任务无关的内容')
    base += '任务描述：' + clean(content) + '\n'
    if kind == 'subtasks':
        return ('你是一名任务拆解助手，擅长将复杂任务分解为可执行的子任务。\n' + base +
            '你的任务：分析任务内容，拆解出关键的执行步骤作为子任务。\n拆解原则：\n'
            '1. 每个子任务聚焦单一可执行动作，避免含糊或重复\n'
            '2. 根据任务复杂度灵活决定数量（通常 2-5 个），简单任务少拆，复杂任务多拆\n'
            '3. 子任务之间保持合理的执行顺序或逻辑关系\n4. 子任务名称简洁明了，控制在 8-30 个字符内\n' + language_rule +
            '输出格式：1. [子任务名称]，每个子任务占一行。仅返回子任务列表，禁止输出额外说明或引导语')
    people = '\n'.join(f'- {clean(m["nickname"],20)}（ID:{m["userid"]}），职位：{clean(m.get("profession"),50)}，进行中：{m["in_progress_count"]}个，近期完成：{m["completed_count"]}个' for m in members)
    return ('你是一名任务分配助手，根据任务内容和成员情况推荐合适的负责人。\n' + base + '可选成员：\n' + people +
        '\n推荐原则：分析任务内容，匹配成员职位或专业方向；优先推荐进行中任务较少的成员，平衡工作负载；近期完成任务多说明执行力强，可作为参考。\n' +
        language_rule + '输出格式：1. [userid]|[推荐理由]，每个推荐占一行。推荐1-2名最合适的负责人，按优先级排序；推荐理由需具体说明为何此人适合该任务，不超过20字；仅返回推荐列表，禁止输出额外说明。')


def markdown(task_id, suggestions, msg_id=0, language='zh'):
    titles = dict(zip(EVENT_TYPES + ('similar_hint',), TITLES.get(language, TITLES['zh'])))
    parts = []
    for result in suggestions:
        kind, content = result['type'], result['content']
        section = '### ' + titles[kind] + '\n\n'
        action = f':::ai-action{{type="{kind}" task="{task_id}" msg="{msg_id}"'
        if kind == 'description':
            section += content + '\n\n' + action + '}:::'
        elif kind == 'subtasks':
            section += '\n'.join(f'{i+1}. {name}' for i, name in enumerate(content)) + '\n' + action + '}:::'
        elif kind == 'assignee':
            section += '\n'.join(f'- **[{v["nickname"]}](dootask://contact/{v["userid"]})** - {v["reason"]} ' + action + f' userid="{v["userid"]}"' + '}:::' for v in content)
        elif kind == 'similar':
            section += titles['similar_hint'] + '\n\n' + '\n'.join(f'{i+1}. **[#{v["task_id"]}](dootask://task/{v["task_id"]})** {v["name"]} ' + action + f' related="{v["task_id"]}"' + '}:::' for i, v in enumerate(content))
        parts.append(section)
    return '\n\n---\n\n'.join(parts)


def analyze(connection, payload, settings):
    from .apps import installed_config
    from .messaging_ai import invoke
    from .project_jobs import service_context
    from .project_tasks import task_dialog, descendants
    from .dialog_core import send_message
    task = by_id(connection, 'project_tasks', payload['task_id'], lock=True)
    if not task or task['deleted_at'] or task['archived_at']:
        return
    project = by_id(connection, 'projects', task['project_id'])
    if not project or project['deleted_at'] or project['ai_auto_analyze'] == 'close':
        return
    c, e = table('project_task_contents'), table('project_task_ai_events')
    content = connection.execute(select(c.c.content).where(c.c.task_id == task['id']).order_by(c.c.id.desc()).limit(1)).scalar() or ''
    user = row(connection, 'users', table('users').c.userid == task['userid']) or {}
    language = user.get('lang') or 'zh'
    names = {'zh': '简体中文', 'zh-CHT': '繁體中文', 'en': 'English', 'ja': '日本語', 'ko': '한국어', 'de': 'Deutsch', 'fr': 'Français', 'id': 'Bahasa Indonesia', 'ru': 'Русский'}
    column = by_id(connection, 'project_columns', task['column_id'])
    events = rows(connection, 'project_task_ai_events', e.c.task_id == task['id'], e.c.status.in_(['pending', 'failed']), e.c.retry_count < 3)
    suggestions, completed_ids = [], []
    for event in events:
        kind = event['event_type']
        tu, t = table('project_task_users'), table('project_tasks')
        eligible = (len(content.strip()) < 20 if kind == 'description' else not count(connection, 'project_tasks', t.c.parent_id == task['id'], t.c.deleted_at.is_(None)) and len(task['name']) > 5 if kind == 'subtasks' else not count(connection, 'project_task_users', tu.c.task_id == task['id'], tu.c.owner == 1) if kind == 'assignee' else bool(installed_config(settings, 'search')))
        if not eligible:
            update(connection, 'project_task_ai_events', event['id'], {'status': 'skipped', 'error': '不满足执行条件', 'executed_at': utcnow()})
            continue
        update(connection, 'project_task_ai_events', event['id'], {'status': 'processing'})
        try:
            if kind == 'similar':
                from .search import task_vector_search, embedding
                context = service_context(connection, task)
                vector = embedding(task['name'])
                matches = task_vector_search(connection, vector, context.actor.userid, 200, project_id=task['project_id'], exclude_id=task['id'])
                excluded = {v['id'] for v in descendants(connection, task['id'])}
                recipients = set(task_allowed_users(connection, task))
                values = []
                for match in matches:
                    similarity = match.get('similarity', 1 - float(match.get('distance', 1)))
                    if match['task_id'] in excluded or similarity < .5:
                        continue
                    related = by_id(connection, 'project_tasks', match['task_id'])
                    if not recipients.issubset(set(task_allowed_users(connection, related))):
                        continue
                    values.append({'task_id': match['task_id'], 'name': related['name'], 'similarity': round(similarity, 2)})
                    if len(values) == 5:
                        break
                result = {'type': kind, 'lang': language, 'content': values} if values else None
            else:
                members = members_info(connection, task) if kind == 'assignee' else []
                if kind == 'assignee' and not members:
                    result = None
                else:
                    response = invoke(connection, settings, [{'role': 'system', 'content': '你是 DooTask 任务管理系统的 AI 助手，帮助用户管理任务。'},
                        {'role': 'user', 'content': prompt(kind, task, project, column, content, members, names.get(language, '简体中文'))}])
                    value = parse_subtasks(response) if kind == 'subtasks' else parse_assignees(response, members) if kind == 'assignee' else response
                    result = {'type': kind, 'content': value} if value else None
            update(connection, 'project_task_ai_events', event['id'], {'status': 'completed' if result else 'skipped',
                'result': encode(result) if result else None, 'error': None if result else '未生成有效建议', 'executed_at': utcnow()})
            if result:
                suggestions.append(result)
                completed_ids.append(event['id'])
        except Exception as error:
            update(connection, 'project_task_ai_events', event['id'], {'status': 'failed', 'error': str(error),
                'retry_count': event['retry_count'] + 1, 'executed_at': utcnow()})
    if suggestions:
        context = service_context(connection, task)
        dialog = task_dialog(connection, context, {'task_id': task['id']}, settings)
        message = send_message(connection, dialog_id=dialog['dialog_id'], sender_id=-1, msg_type='text',
            msg={'text': markdown(task['id'], suggestions, language=language), 'type': 'md'}, silence=True, trusted_sender=True)
        send_message(connection, dialog_id=dialog['dialog_id'], sender_id=-1, msg_type='text', update_id=message['id'],
            msg={'text': markdown(task['id'], suggestions, message['id'], language), 'type': 'md'}, trusted_sender=True)
        connection.execute(e.update().where(e.c.id.in_(completed_ids)).values(msg_id=message['id']))


def register_jobs():
    jobs.register('projects.ai_scan', scan)
    jobs.register('projects.ai_analyze', analyze)
    jobs.schedule('projects.ai_scan', 60)
