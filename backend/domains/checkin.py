"""Public check-in shared-secret protocol and real attendance persistence."""
import hmac
import json
import re
import shlex
import urllib.request
from datetime import datetime, timedelta
from sqlalchemy import select
from starlette.responses import PlainTextResponse
from .. import db, jobs
from ..context import utcnow
from ..errors import ApiError
from .project_core import table, row, rows, update, integer, decode, encode


def checkin_window(setting, now):
    clocks = decode(setting.get('time'), ['09:00', '18:00'])
    if not isinstance(clocks, list) or len(clocks) != 2:
        raise ApiError('签到时间设置错误')
    try:
        start_clock, end_clock = (datetime.strptime(v, '%H:%M').time() for v in clocks)
    except (ValueError, TypeError) as exc:
        raise ApiError('签到时间设置错误') from exc
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = datetime.combine(now.date(), start_clock)
    end = datetime.combine(now.date(), end_clock)
    advance = timedelta(minutes=integer(setting.get('advance')) or 120)
    delay = timedelta(minutes=integer(setting.get('delay')) or 120)
    earliest = max(start - advance, day)
    if earliest <= now <= end + delay:
        return now.date().isoformat(), 'up' if now < end else 'down'
    if now < earliest and now <= end - timedelta(days=1) + delay:
        return (now.date() - timedelta(days=1)).isoformat(), 'down'
    return None, None


def record_checkin(connection, context, mac, report_time, already_tip=False):
    setting = db.setting(connection, 'checkinSetting', default={})
    if setting.get('open') != 'open':
        raise ApiError('function off')
    now = utcnow()
    target_date, check_type = checkin_window(setting, now)
    records, macs, users = table('user_checkin_records'), table('user_checkin_macs'), table('users')
    for value in str(mac).split(','):
        value = value.strip().upper()
        uid, remark, address = 0, '', value
        if re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', value):
            binding = row(connection, 'user_checkin_macs', macs.c.mac == value)
            if binding:
                uid, remark = binding['userid'], binding['remark']
        else:
            match = re.fullmatch(r'(MANUAL|LOCAT|FACE|CHECKIN)-(\d+)', value)
            if match:
                kind = {'MANUAL': 'manual', 'LOCAT': 'locat', 'FACE': 'face', 'CHECKIN': 'face'}[match[1]]
                uid = int(match[2])
                remark = setting.get(kind + '_remark') or {'manual': 'Manual', 'locat': 'Location', 'face': 'Machine'}[kind]
                address = '00:00:00:00:00:00'
        if not uid:
            continue
        # Serialize per account: report batches and concurrent devices cannot lose times.
        user = row(connection, 'users', users.c.userid == uid, users.c.bot == 0, users.c.disable_at.is_(None), lock=True)
        if not user:
            continue
        if not target_date:
            continue
        existing = row(connection, 'user_checkin_records', records.c.userid == uid, records.c.mac == address, records.c.date == target_date, lock=True)
        values = {'times': encode(decode(existing['times']) + [now.strftime('%H:%M:%S')]) if existing else encode([now.strftime('%H:%M:%S')]),
                  'report_time': integer(report_time)}
        if existing:
            update(connection, 'user_checkin_records', existing['id'], values)
        else:
            db.insert_row(connection, 'user_checkin_records', userid=uid, mac=address, date=target_date, **values)
        jobs.enqueue(connection, 'checkin.notify', {'userid': uid, 'date': target_date, 'type': check_type,
            'time': now.strftime('%H:%M'), 'remark': remark, 'already_tip': bool(already_tip)},
            dedupe_key=None if already_tip else f'checkin:{target_date}:{check_type}:{uid}')


def checkin_install(connection, context, data, settings):
    setting = db.setting(connection, 'checkinSetting', default={})
    if setting.get('open') != 'open':
        return PlainTextResponse('#!/bin/sh\necho "function off"\n')
    key = str(setting.get('key') or '')
    if not key or not hmac.compare_digest(str(data.get('key', '')).strip(), key):
        return PlainTextResponse('#!/bin/sh\necho "key error"\n')
    report_url = context.base_url.rstrip('/') + '/api/public/checkin/report'
    # Shell-quote the JSON fragment and URL; settings must not become executable shell text.
    fragment = json.dumps({'key': key}, ensure_ascii=False, separators=(',', ':'))[:-1]
    script = '\n'.join([
        '#!/bin/sh', 'echo "installing..."', "cat > /etc/init.d/dootask-checkin-report <<'DOOTASK_REPORT'",
        '#!/bin/sh', "mac=$(awk 'NR!=1&&$3==\"0x2\" {print $4}' /proc/net/arp | tr '\\n' ',')",
        'prefix=' + shlex.quote(fragment),
        'tmp="${prefix},\\"mac\\":\\"${mac}\\",\\"time\\":\\"$(date +%s)\\"}"',
        'curl -4 -X POST ' + shlex.quote(report_url) + ' -H "Content-Type: application/json" -d "$tmp"',
        'DOOTASK_REPORT', 'chmod +x /etc/init.d/dootask-checkin-report',
        "(crontab -l 2>/dev/null | sed '/\\/etc\\/init.d\\/dootask-checkin-report/d'; echo '* * * * * sh /etc/init.d/dootask-checkin-report') | crontab -",
        '/etc/init.d/cron enable', '/etc/init.d/cron restart', 'echo "installed"', '',
    ])
    return PlainTextResponse(script, media_type='text/x-shellscript')


def checkin_report(connection, context, data, settings):
    setting = db.setting(connection, 'checkinSetting', default={})
    if setting.get('open') != 'open':
        return PlainTextResponse('function off')
    face = str(data.get('type', '')).strip() == 'face'
    modes = decode(setting.get('modes'), []) if not isinstance(setting.get('modes'), list) else setting['modes']
    if ('face' if face else 'auto') not in modes:
        return PlainTextResponse('mode off')
    expected = str(setting.get('face_key' if face else 'key') or '')
    if not expected or not hmac.compare_digest(str(data.get('key', '')).strip(), expected):
        return PlainTextResponse('key error')
    mac = str(data.get('mac', '')).strip()
    if len(mac) > 65535:
        return PlainTextResponse('mac error')
    # Router keys cannot impersonate manual/location/face attendance identities.
    pattern = r'(?:face|checkin)-\d+' if face else r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}'
    if any(not re.fullmatch(pattern, part.strip(), re.I) for part in mac.split(',') if part.strip()):
        return PlainTextResponse('mode error')
    record_checkin(connection, context, mac, integer(data.get('time')), face and setting.get('face_retip') == 'open')
    return PlainTextResponse('success')


def notify(connection, payload, settings):
    from .account_meetings import bot_get
    from .dialog_core import ensure_user_dialog, send_message
    bot = bot_get(connection, 'check-in')
    dialog_id = ensure_user_dialog(connection, bot['userid'], payload['userid'])
    messages = table('web_socket_dialog_msgs')
    marker = 'checkin:' + payload['date'] + ':' + payload['type'] + ':' + str(payload['userid'])
    exists = row(connection, 'web_socket_dialog_msgs', messages.c.dialog_id == dialog_id, messages.c.key == marker)
    label = '上班' if payload['type'] == 'up' else '下班'
    if exists and not payload.get('already_tip'):
        return
    title = ('今日已' + label + '打卡，无需重复打卡。') if exists else (label + '打卡成功，打卡时间: ' + payload['time'])
    if payload.get('remark'):
        title += ' (' + payload['remark'] + ')'
    if payload['date'] != utcnow().date().isoformat():
        title += ' (' + payload['date'] + ')'
    send_message(connection, dialog_id=dialog_id, sender_id=bot['userid'], msg_type='template',
        msg={'type': 'content', 'title': title, 'content': [{'content': title}]}, search_key=marker, silence=payload['type'] != 'up')


def is_holiday(day):
    # Source Extranet falls back to weekends when the official data service fails.
    url = 'https://api.apihubs.cn/holiday/get?field=date&month=' + day.strftime('%Y%m') + '&workday=2&size=31'
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.load(response)
        if payload.get('code') == 0 and payload.get('data', {}).get('list'):
            return day.strftime('%Y%m%d') in {str(v['date']) for v in payload['data']['list']}
    except (OSError, ValueError, KeyError):
        import logging
        logging.getLogger(__name__).warning('Holiday service unavailable; applying source weekend fallback', exc_info=True)
    return day.weekday() >= 5


def remind(connection, payload, settings):
    setting = db.setting(connection, 'checkinSetting', default={})
    if setting.get('open') != 'open':
        return
    now = utcnow()
    clocks = decode(setting.get('time'), ['09:00', '18:00'])
    start = datetime.combine(now.date(), datetime.strptime(clocks[0], '%H:%M').time())
    before = integer(setting.get('remindin')) or 5
    after = integer(setting.get('remindexceed')) or 10
    kind = 'in' if start - timedelta(minutes=before) <= now <= start else 'exceed' if start + timedelta(minutes=after) <= now <= start + timedelta(minutes=after+5) else None
    if not kind or is_holiday(now.date()):
        return
    u, r = table('users'), table('user_checkin_records')
    query = select(u.c.userid).where(u.c.bot == 0, u.c.disable_at.is_(None),
        u.c.userid.in_(select(r.c.userid).where(r.c.created_at > now - timedelta(days=3))),
        u.c.userid.not_in(select(r.c.userid).where(r.c.date == now.date().isoformat())))
    for uid in connection.execute(query).scalars():
        jobs.enqueue(connection, 'checkin.reminder_message', {'userid': uid, 'type': kind},
            dedupe_key=f'checkin-remind:{now.date()}:{kind}:{uid}')


def reminder_message(connection, payload, settings):
    from .account_meetings import bot_get
    from .dialog_core import ensure_user_dialog, send_message
    bot = bot_get(connection, 'check-in')
    title = '缺卡提醒' if payload['type'] == 'exceed' else '打卡提醒'
    content = '上班时间到了，你还没有打卡哦~' if payload['type'] == 'exceed' else '快到上班时间了，别忘了打卡哦~'
    did = ensure_user_dialog(connection, bot['userid'], payload['userid'])
    send_message(connection, dialog_id=did, sender_id=bot['userid'], msg_type='template', msg={'type': 'content',
        'title': title, 'content': [{'content': title, 'style': 'font-weight:bold'}, {'content': content, 'style': 'padding-top:4px;opacity:0.6'}]})


def register_jobs():
    jobs.register('checkin.notify', notify)
    jobs.register('checkin.remind', remind)
    jobs.register('checkin.reminder_message', reminder_message)
    jobs.schedule('checkin.remind', 60)


ROUTES = {'public/checkin/install': checkin_install, 'public/checkin/report': checkin_report}
RESPONSE_KINDS = {key: 'Response' for key in ROUTES}
PUBLIC_ROUTES = set(ROUTES)
