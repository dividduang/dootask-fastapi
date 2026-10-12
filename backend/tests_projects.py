"""Runnable project regression checks against independently managed local accounts.

Pure checks: python -m backend.tests_projects
MariaDB boundary checks: set DOOTASK_PROJECT_TEST_TOKENS to a JSON array of three
valid local test-user tokens (first must be an administrator), then add --integration.
The database checks roll back all project, task, handoff and outbox rows.
"""
import json
import os
import sys
from datetime import datetime
from sqlalchemy import select, func
from . import db
from .auth import authenticate, require_admin
from .config import Settings
from .context import RequestContext
from .errors import ApiError
from .domains import projects
from .domains.project_core import can_view_task, task_allowed_users
from .domains.project_tasks import normalize_times, times, next_loop
from .domains.project_ai import parse_subtasks, parse_assignees, markdown
from .domains.checkin import checkin_window


def rejected(callback, ret=None):
    try:
        callback()
    except ApiError as error:
        if ret is not None:
            assert error.ret == ret, (error.ret, ret)
        return
    raise AssertionError('Expected ApiError')


def pure_checks():
    assert len(projects.ROUTES) == 82
    assert projects.RESPONSE_KINDS['project/task/filedown'] == 'Response'
    assert projects.RESPONSE_KINDS['project/task/one'] == 'data'
    assert 'projecttaskhandoff/assign' in projects.POST_ONLY_ROUTES
    assert not any('invoke' in key or 'middleware' in key for key in projects.ROUTES)
    rejected(lambda: normalize_times({'start_at': '2026-10-12 09:00:00'}))
    rejected(lambda: times(['2026-10-12 18:00:00', '2026-10-12 09:00:00']))
    assert times(['2026-10-12 09:00:00', '2026-10-12 18:00:00'])[0].hour == 9
    assert next_loop('weekdays', datetime(2026, 10, 9, 9), datetime(2026, 10, 9)).date().isoformat() == '2026-10-12'
    assert next_loop('month', datetime(2026, 1, 31), datetime(2026, 1, 31)).date().isoformat() == '2026-03-03'
    assert parse_subtasks('1. one\n2、 two\n3) three') == ['one', 'two', 'three']
    assert parse_assignees('1. 4|good\n2. 999|outside\n3. 4|repeat', [{'userid': 4, 'nickname': 'A'}]) == [{'userid': 4, 'nickname': 'A', 'reason': 'good'}]
    text = markdown(5, [{'type': 'subtasks', 'content': ['one']}], 8, 'en')
    assert ':::ai-action{type="subtasks" task="5" msg="8"}:::' in text
    setting = {'time': ['09:00', '23:00'], 'advance': 120, 'delay': 120}
    assert checkin_window(setting, datetime(2026, 10, 13, 0, 30)) == ('2026-10-12', 'down')
    assert checkin_window(setting, datetime(2026, 10, 13, 5, 0)) == (None, None)
    assert checkin_window(setting, datetime(2026, 10, 13, 8, 0)) == ('2026-10-13', 'up')


def integration_checks():
    tokens = json.loads(os.environ['DOOTASK_PROJECT_TEST_TOKENS'])
    assert isinstance(tokens, list) and len(tokens) == 3 and all(isinstance(v, str) and v for v in tokens)
    settings = Settings.from_env()
    db.configure(settings)
    identifiers = {}

    class RollbackExpected(Exception):
        pass

    try:
        with db.transaction() as connection:
            contexts = [RequestContext('project-regression-' + str(i), token=token, version='1.9.66') for i, token in enumerate(tokens)]
            actors = [authenticate(connection, context) for context in contexts]
            require_admin(actors[0])
            assert len({actor.userid for actor in actors}) == 3
            owner, assignee, outsider = contexts
            p = projects.add(connection, owner, {'name': '隔离项目回归', 'columns': '第一列,第二列'}, settings)
            identifiers['project'] = p['id']
            projects.user(connection, owner, {'project_id': p['id'], 'userid': [a.userid for a in actors]}, settings)
            columns = projects.column_lists(connection, owner, {'project_id': p['id']}, settings)['data']
            task = projects.ROUTES['project/task/add'](connection, owner, {'project_id': p['id'], 'column_id': columns[0]['id'],
                'name': '私密授权回归', 'owner': [actors[1].userid], 'visibility': 2}, settings)
            identifiers['task'] = task['id']
            assert can_view_task(connection, actors[0].userid, task)
            assert can_view_task(connection, actors[1].userid, task)
            assert not can_view_task(connection, actors[2].userid, task)
            rejected(lambda: projects.ROUTES['project/task/content'](connection, outsider, {'task_id': task['id']}, settings), -4002)
            rejected(lambda: projects.ROUTES['project/permission/update'](connection, outsider,
                {'project_id': p['id'], 'task_remove': [2]}, settings))
            rejected(lambda: projects.ROUTES['project/task/update'](connection, outsider,
                {'task_id': task['id'], 'name': '越权更新'}, settings), -4002)
            projects.ROUTES['project/task/update'](connection, assignee, {'task_id': task['id'], 'visibility': 1}, settings)
            fresh = projects.ROUTES['project/task/one'](connection, outsider, {'task_id': task['id']}, settings)
            assert fresh['visibility'] == 1
            projects.ROUTES['project/task/update'](connection, assignee, {'task_id': task['id'], 'visibility': 2}, settings)
            deleted = db.tables['deleteds']
            assert connection.execute(select(deleted.c.id).where(deleted.c.type == 'projectTask', deleted.c.did == task['id'], deleted.c.userid == actors[2].userid)).first()
            child = projects.ROUTES['project/task/addsub'](connection, assignee, {'task_id': task['id'], 'name': '未完成子任务'}, settings)
            rejected(lambda: projects.ROUTES['project/task/update'](connection, assignee,
                {'task_id': task['id'], 'complete_at': '2026-10-12 12:00:00'}, settings), -4004)
            projects.ROUTES['project/task/update'](connection, assignee, {'task_id': child['id'], 'complete_at': '2026-10-12 12:00:00'}, settings)
            projects.ROUTES['project/task/update'](connection, assignee, {'task_id': task['id'], 'complete_at': '2026-10-12 12:00:00'}, settings)
            projects.ROUTES['project/task/archived'](connection, assignee, {'task_id': task['id']}, settings)
            projects.ROUTES['project/task/archived'](connection, assignee, {'task_id': task['id'], 'type': 'recovery'}, settings)
            destination = projects.add(connection, owner, {'name': '跨项目移动回归'}, settings)
            projects.user(connection, owner, {'project_id': destination['id'], 'userid': [a.userid for a in actors]}, settings)
            destination_column = projects.column_lists(connection, owner, {'project_id': destination['id']}, settings)['data'][0]
            moved = projects.ROUTES['project/task/move'](connection, assignee, {'task_id': task['id'],
                'project_id': destination['id'], 'column_id': destination_column['id'], 'owner': [actors[1].userid]}, settings)
            assert {v['id'] for v in moved} == {task['id'], child['id']}
            assert all(v['project_id'] == destination['id'] for v in moved)
            projects.user(connection, owner, {'project_id': destination['id'], 'userid': [actors[0].userid, actors[2].userid]}, settings)
            assert not can_view_task(connection, actors[1].userid, projects.require_task(connection, owner, task['id']))
            outbox = db.tables['backend_outbox']
            pending = [dict(v) for v in connection.execute(select(outbox).where(outbox.c.completed_at.is_(None))).mappings()]
            assert any(v['kind'] == 'search.sync' and str(task['id']) in v['payload'] for v in pending)
            assert any(v['kind'] == 'realtime.push' and 'projectTask' in v['payload'] for v in pending)
            handoffs = db.tables['project_task_handoffs']
            assert connection.execute(select(func.count()).select_from(handoffs).where(handoffs.c.task_id == task['id'])).scalar_one() >= 4
            raise RollbackExpected()
    except RollbackExpected:
        pass
    with db.transaction() as connection:
        p, t = db.tables['projects'], db.tables['project_tasks']
        assert not connection.execute(select(p.c.id).where(p.c.id == identifiers['project'])).first()
        assert not connection.execute(select(t.c.id).where(t.c.id == identifiers['task'])).first()


if __name__ == '__main__':
    pure_checks()
    if '--integration' in sys.argv:
        integration_checks()
    print('Project checks passed' + (' including rolled-back MariaDB boundaries' if '--integration' in sys.argv else ' (pure checks only)'))
