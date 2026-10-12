"""Integration regression checks; implementation batch deliberately does not run these.

python -m backend.tests_integration
DOOTASK_INTEGRATION_TOKEN=<isolated synthetic administrator token>
python -m backend.tests_integration --database
The database option requires the fresh isolated schema, never an existing user DB.
"""
import argparse
import asyncio
from datetime import timedelta
import json
import os
import threading
import time
from uuid import uuid4
from sqlalchemy import select, func
from starlette.requests import Request
from . import auth, db, realtime
from .avatar import avatar_background
from .config import Settings
from .context import RequestContext, utcnow
from .errors import ApiError
from .main import ALLOWLIST, DOMAIN_ROUTES, read_inputs, route_aliases, token_input
from .domains import projects, files, reports
from .domains.filedav import authenticate_dav


def pure_checks():
    assert 'dialog/msg__send' in route_aliases('dialog/msg/send')
    assert 'assistant/match-elements' in DOMAIN_ROUTES
    assert 'projecttaskhandoff/assign' in DOMAIN_ROUTES
    assert not any(path.endswith('/middleware') for path in DOMAIN_ROUTES)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/file/content/office',
                       'query_string': b'dootask-token=identity', 'headers': []})
    assert token_input(request, {'token': 'office-jwt'}) == 'identity'
    assert avatar_background('Alice') == avatar_background('Alice')
    async def multipart():
        payload = b'--bound\r\nContent-Disposition: form-data; name="blob"; filename="chunk.bin"\r\nContent-Type: application/octet-stream\r\n\r\n' + b'x' * (5 * 1024**2) + b'\r\n--bound--\r\n'
        async def receive():
            return {'type': 'http.request', 'body': payload, 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/upload/chunk',
                           'query_string': b'', 'headers': [(b'content-type', b'multipart/form-data; boundary=bound')]}, receive)
        data = await read_inputs(request)
        try:
            assert data['blob'].size == 5 * 1024**2
            assert data['blob'].file.read(1) == b'x'
        finally:
            await request.state.upload_form.close()
    asyncio.run(multipart())
    async def legacy_json():
        async def receive():
            return {'type': 'http.request', 'body': b'{"name":"real task"}', 'more_body': False}
        scope = {'type': 'http', 'method': 'POST', 'path': '/api/project/task/add',
                 'query_string': b'', 'headers': [(b'content-type', b'OPTIONS, application/json')]}
        assert await read_inputs(Request(scope, receive)) == {'name': 'real task'}
        scope['headers'] = [(b'content-type', b'application/json, text/plain')]
        try:
            await read_inputs(Request(scope, receive))
        except ApiError:
            pass
        else:
            raise AssertionError('Ambiguous media types must be rejected')
    asyncio.run(legacy_json())


def scheduler_checks():
    from concurrent.futures import ThreadPoolExecutor
    from . import jobs
    from .domains import accounts, dialog, assistant, search
    settings = Settings.from_env()
    if settings.db_database != 'dootask_fastapi' or settings.db_port != 13306:
        raise RuntimeError('Only the dedicated isolated database is accepted')
    engine = db.configure(settings)
    try:
        for module in (accounts, projects, dialog, assistant, files, reports, search):
            module.register_jobs()
        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in pool.map(lambda _: jobs.tick(), range(40)):
                assert result is None
        with db.transaction() as connection:
            table = db.tables['backend_outbox']
            for kind in jobs.SCHEDULES:
                assert connection.execute(select(table.c.id).where(table.c.kind == kind).limit(1)).first()
        print('Concurrent real scheduler transactions passed')
    finally:
        engine.dispose()


def database_checks():
    settings = Settings.from_env()
    if settings.db_database != 'dootask_fastapi' or settings.db_port != 13306:
        raise RuntimeError('Only the dedicated isolated database is accepted')
    token = os.environ['DOOTASK_INTEGRATION_TOKEN']
    engine = db.configure(settings)
    projects.register_jobs(); files.register_jobs(); reports.register_jobs()
    with db.transaction() as connection:
        actor = auth.authenticate(connection, RequestContext(request_id=uuid4().hex, token=token))
        userid = actor.userid
    # Service tokens cannot consume 200 interactive-device slots, and revoke together.
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            actor = auth.authenticate(connection, RequestContext(request_id=uuid4().hex, token=token))
            devices = db.tables['user_devices']; sessions = db.tables['auth_sessions']
            before = connection.execute(select(func.count()).select_from(devices).where(devices.c.userid == userid)).scalar_one()
            short = [auth.issue_service_token(connection, userid, RequestContext(request_id=uuid4().hex)) for _ in range(201)]
            after = connection.execute(select(func.count()).select_from(devices).where(devices.c.userid == userid)).scalar_one()
            assert before == after
            derived = auth.authenticate(connection, RequestContext(request_id=uuid4().hex, token=short[-1]))
            assert derived.session['purpose'] == 'service' and derived.session['device_id'] is None
            auth.logout(connection, RequestContext(request_id=uuid4().hex, token=token))
            try:
                auth.authenticate(connection, RequestContext(request_id=uuid4().hex, token=short[-1]))
            except ApiError:
                pass
            else:
                raise AssertionError('Interactive logout did not revoke service token')
        finally:
            transaction.rollback()
    # Real delivery rows: revoke >=200 scopes, then reach the tombstone next poll.
    table = db.tables['backend_deliveries']; fd = 'qa-' + uuid4().hex
    with db.transaction() as connection:
        for i in range(201):
            db.insert_row(connection, 'backend_deliveries', event_id=i + 1, userid=userid, fd=fd,
                          msg_id=uuid4().hex, payload=json.dumps({'type': 'project', 'data': {'id': -1}}),
                          scope=json.dumps({'project_id': -1}) if i < 200 else 'null', ignore_fds='[]',
                          expires_at=utcnow() + timedelta(minutes=5))
    try:
        assert realtime._pending(userid, fd) == []
        pending = realtime._pending(userid, fd)
        assert len(pending) == 1
        msgid = pending[0]['msg_id']
        realtime._receipt(userid, fd, None)
        with db.transaction() as connection:
            assert connection.execute(select(table.c.acknowledged_at).where(table.c.msg_id == msgid)).scalar_one() is None
        realtime._transport_sent(userid, fd, msgid)
        assert realtime._pending(userid, fd) == []
        with db.transaction() as connection:
            assert connection.execute(select(table.c.acknowledged_at).where(table.c.msg_id == msgid)).scalar_one() is None
    finally:
        with db.transaction() as connection:
            connection.execute(table.delete().where(table.c.fd == fd))
    # ABBA regression: one transaction owns user; concurrent DAV must not take
    # credential before waiting on that user. Wrong password is intentional: no grant.
    credentials = db.tables['webdav_credentials']; users = db.tables['users']
    public = 'qa_' + uuid4().hex[:24]
    from .security import password_hash
    import base64
    with db.transaction() as connection:
        ident = db.insert_row(connection, 'webdav_credentials', userid=userid, public_id=public,
                              name='isolated lock-order regression', password_hash=password_hash('correct'), password_suffix='rect')
    errors = []; started = threading.Event()
    def dav_attempt():
        try:
            with db.transaction() as connection:
                started.set()
                authenticate_dav(connection, RequestContext(request_id=uuid4().hex, base_url='https://localhost/', ip='127.0.0.1'),
                                 'Basic ' + base64.b64encode((public + ':wrong').encode()).decode())
        except ApiError:
            pass
        except Exception as exc:
            errors.append(exc)
    try:
        with db.transaction() as connection:
            connection.execute(select(users.c.userid).where(users.c.userid == userid).with_for_update()).one()
            thread = threading.Thread(target=dav_attempt)
            thread.start(); started.wait(5); time.sleep(.25)
            connection.execute(select(credentials).where(credentials.c.id == ident).with_for_update()).one()
            connection.execute(credentials.update().where(credentials.c.id == ident).values(revoked_at=utcnow()))
        thread.join(15)
        assert not thread.is_alive() and not errors, errors
    finally:
        with db.transaction() as connection:
            connection.execute(credentials.delete().where(credentials.c.id == ident))
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', action='store_true')
    args = parser.parse_args()
    pure_checks()
    if args.database:
        database_checks()
