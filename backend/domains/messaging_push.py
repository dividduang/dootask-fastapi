"""Umeng customizedcast API; official signing Appendix I, docs/67966/detail/149296."""
from collections import defaultdict
from datetime import timedelta
import hashlib
import json
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from sqlalchemy import select, func
from .. import db
from ..context import utcnow
from ..errors import ApiError

URL = "https://msgapi.umeng.com/api/send"


def signature(url, body, secret):
    return hashlib.md5(b"POST" + url.encode() + body + secret.encode()).hexdigest()


def push_to_users(connection, userids, message, settings):
    config = db.setting(connection, "appPushSetting") or {}
    if config.get("push") != "open":
        return
    aliases, reads = db.tables["umeng_alias"], db.tables["web_socket_dialog_msg_reads"]
    title = str(message.get("title", "")).replace("\r", "").replace("\n", "")
    body_text = str(message.get("body", "")).replace("\r", "").replace("\n", "")
    for userid in sorted(set(userids)):
        rows = connection.execute(select(aliases).where(aliases.c.userid == userid,
            aliases.c.updated_at > utcnow() - timedelta(days=30)).order_by(aliases.c.updated_at.desc()).limit(5)).mappings().all()
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["platform"]].append(row["alias"])
        badge = connection.execute(select(func.count()).select_from(reads).where(reads.c.userid == userid, reads.c.silence == 0, reads.c.read_at.is_(None))).scalar_one()
        for platform, targets in grouped.items():
            if platform not in ("ios", "android"):
                continue
            key, secret = config.get(platform + "_key"), config.get(platform + "_secret")
            if not key or not secret:
                raise ApiError(f"{platform}友盟推送凭据未配置")
            payload = {"appkey": key, "timestamp": str(int(time.time())), "type": "customizedcast", "alias_type": "userid", "alias": ",".join(targets),
                "production_mode": "true", "description": message.get("description", "no description"),
                "policy": {"expire_time": (utcnow() + timedelta(seconds=int(message.get("seconds", 86400)))).strftime("%Y-%m-%d %H:%M:%S")}}
            extra = message.get("extra", {})
            if platform == "ios":
                payload["payload"] = {"aps": {"alert": {"title": title, "subtitle": str(message.get("subtitle", "")), "body": body_text}, "sound": "default", "badge": badge}, **extra}
            else:
                payload.update(payload={"display_type": "notification", "body": {"ticker": title, "title": title, "text": body_text,
                    "after_open": "go_app", "play_sound": True, "set_badge": min(99, badge)}, **extra}, mipush=True,
                    mi_activity="app.eeui.umeng.activity.MfrMessageActivity", category=1,
                    channel_properties={"main_activity": "com.dootask.task.WelcomeActivity", "oppo_channel_id": "dootask", "vivo_category": "IM",
                        "huawei_channel_importance": "NORMAL", "huawei_channel_category": "IM", "channel_fcm": 0},
                    local_properties={"importance": "IMPORTANCE_DEFAULT", "category": "CATEGORY_MESSAGE"})
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            request = Request(URL + "?sign=" + signature(URL, raw, secret), data=raw, headers={"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=15) as response:
                    result = json.loads(response.read())
            except HTTPError as exc:
                raise ApiError("友盟推送失败", data={"status": exc.code, "body": exc.read(8192).decode(errors="replace")}) from exc
            db.insert_row(connection, "umeng_logs", request=raw.decode(), response=json.dumps(result, ensure_ascii=False))
            if result.get("ret") != "SUCCESS":
                raise ApiError("友盟推送失败", data=result)
