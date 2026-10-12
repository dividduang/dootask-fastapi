"""Real MariaDB identity checks. NOT_RUN during implementation.

Run on a newly initialized isolated database (workers stopped):
DOOTASK_TEST_INDEPENDENT=1 python -m backend.tests_independent
The concurrency check commits two genuinely registered accounts; never run on production.
"""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import select, func
from . import auth, db
from .config import Settings
from .context import RequestContext, utcnow
from .errors import ApiError
from .domains import accounts, account_admin, account_import, account_meetings


def rejected(call):
    try:
        call()
    except ApiError:
        return
    raise AssertionError('Expected real business rejection')


def context(label, token=''):
    return RequestContext(label, token=token, ip='127.0.0.1', base_url='http://127.0.0.1:18080/')


def main():
    from .domains import system
    assert 'system/license' not in system.ROUTES
    assert auth.is_disabled({'identity':',disable,','disable_at':None})
    assert auth.is_disabled({'identity':'','disable_at':utcnow()})
    assert not auth.is_disabled({'identity':'','disable_at':None})
    if os.environ.get('DOOTASK_TEST_INDEPENDENT') != '1':
        raise SystemExit('Set DOOTASK_TEST_INDEPENDENT=1 only for a fresh isolated MariaDB')
    settings=Settings.from_env()
    engine=db.configure(settings)
    try:
        with db.transaction() as c:
            assert c.execute(select(func.count()).select_from(db.tables['users'])).scalar_one()==0, 'fresh database required'
            assert db.setting(c,'system','reg','open')=='open'
            assert db.setting(c,'emailSetting','reg_verify')!='open', 'SMTP branch has separate live-provider acceptance'
            assert db.setting(c,'thirdAccessSetting','ldap_open')!='open'
        barrier=Barrier(2)
        password='Independent-Password7'
        suffix=secrets.token_hex(4)
        def register(index):
            email=f'{suffix}{index}@example.test'
            barrier.wait(timeout=15)
            with db.transaction() as c:
                result=auth.login(c,context('register'),email,password,settings=settings,registration=True)
                assert result['token']
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(register,i) for i in range(2)]
            users=[f.result(timeout=45) for f in futures]
        assert sum('admin' in u['identity'] for u in users)==1
        admin=next(u for u in users if 'admin' in u['identity'])
        normal=next(u for u in users if 'admin' not in u['identity'])
        with engine.connect() as c:
            transaction=c.begin()
            try:
                a=context('admin',admin['token'])
                n=context('normal',normal['token'])
                rejected(lambda: auth.login(c,context('bad'),normal['email'],'Wrong-password7',settings=settings))
                rejected(lambda: account_import.createuser(c,n,{'email':'new@example.test','nickname':'New user','password':password},settings))
                imported=account_import.createuser(c,a,{'email':'new@example.test','nickname':'New user','password':password},settings)['data']
                assert imported['userid'] and imported['changepass']==1
                robot=account_meetings.bot_edit(c,a,{'name':'Real bot'},settings)['data']
                assert robot['id'] and account_meetings.bot_get(c,'system-msg')['bot']==1
                assert account_meetings.bot_get(c,'system-msg')['userid']==account_meetings.bot_get(c,'system-msg')['userid']
                projects=db.tables['projects']
                assert c.execute(select(projects.c.id).where(projects.c.userid==imported['userid'],projects.c.personal==1)).first()
                devices=db.tables['user_devices']
                count=c.execute(select(func.count()).select_from(devices).where(devices.c.userid==normal['userid'])).scalar_one()
                service=[auth.issue_service_token(c,normal['userid'],context('bot-task')) for _ in range(205)]
                assert c.execute(select(func.count()).select_from(devices).where(devices.c.userid==normal['userid'])).scalar_one()==count
                auth.authenticate(c,context('still-interactive',normal['token']))
                service_actor=auth.authenticate(c,context('service',service[0]))
                assert service_actor.session['purpose']=='service'
                assert not auth.should_refresh(c,service_actor)
                assert 'token' not in auth.token_expire(c,context('no-refresh',service[0]),True)
                rejected(lambda: auth.change_password(c,context('no-upgrade',service[0]),password,'Other-Password7',settings=settings))
                auth.logout(c,context('logout',normal['token']))
                rejected(lambda: auth.authenticate(c,context('revoked-service',service[0])))
                relogin=auth.login(c,context('relogin'),normal['email'],password,settings=settings)
                account_admin.operation(c,a,{'userid':normal['userid'],'type':'setdisable','disable_time':'2026-10-12T00:00:00'},settings)
                rejected(lambda: auth.authenticate(c,context('disabled',relogin['token'])))
                rejected(lambda: auth.login(c,context('disabled-login'),normal['email'],password,settings=settings))
                from dataclasses import replace
                from pathlib import Path
                from tempfile import TemporaryDirectory
                from .domains import account_system_files
                from .domains.dialog_core import ensure_user_dialog
                rejected(lambda: account_system_files.export_job(c,{'userid':normal['userid']},settings))
                bot=account_meetings.bot_get(c,'system-msg')
                dialog=ensure_user_dialog(c,bot['userid'],admin['userid'])
                payload={'userid':admin['userid'],'userids':[admin['userid']],'_event_id':123456,
                    'date':['2026-10-12','2026-10-12'],'time':['09:00','18:00'],'delay':120,
                    'dialog_id':dialog,'bot_id':bot['userid'],'base_url':a.base_url,'language':'zh'}
                with TemporaryDirectory() as directory:
                    export_settings=replace(settings,storage_root=Path(directory))
                    account_system_files.export_job(c,payload,export_settings)
                    grants=db.tables['backend_file_downloads']
                    key=c.execute(select(grants.c.id).where(grants.c.userid==admin['userid'],grants.c.path=='temp/checkin/export/123456.zip')).scalar_one()
                    assert account_system_files.checkin_down(c,context('download'),{'key':key},export_settings).status_code==200
                    accounts.mutate_user(c,a,a.actor.user,{'identity':''})
                    rejected(lambda: account_system_files.export_job(c,payload,export_settings))
                    assert account_system_files.checkin_down(c,context('revoked-download'),{'key':key},export_settings).status_code==403
            finally:
                transaction.rollback()
        # Admin password reset versus the user's own change: both now lock users before credentials.
        gate=Barrier(2)
        def change(admin_reset):
            gate.wait(timeout=15)
            with db.transaction() as c:
                if admin_reset:
                    account_admin.operation(c,context('admin-reset',admin['token']),{'userid':normal['userid'],'password':'Reset-Password7'},settings)
                else:
                    try:
                        auth.change_password(c,context('self-change',normal['token']),password,'Self-Password7',settings=settings)
                    except ApiError:
                        pass  # A committed administrator reset legitimately revokes the old session.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(change,flag) for flag in (True,False)]
            for future in futures:
                future.result(timeout=45)
        with db.transaction() as c:
            assert auth.login(c,context('reset-login'),normal['email'],'Reset-Password7',settings=settings)['userid']==normal['userid']
        print('Independent MariaDB identity checks passed; two new local accounts retained in isolated database')
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
