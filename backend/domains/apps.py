import hashlib
import hmac
import json
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import yaml
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from .. import auth, db, jobs
from ..context import utcnow
from ..errors import ApiError
from .dialog_core import ids, encode


def on(value):
    return str(value).strip().lower() not in ("", "0", "false", "off", "no", "none")


def installed_config(settings, appid):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", appid):
        raise ApiError("参数错误")
    path = settings.appstore_root / "config" / appid / "config.yml"
    if not path.is_file():
        return None
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return config if isinstance(config, dict) and config.get("status") == "installed" else None


def require_installed(settings, appid):
    config = installed_config(settings, appid)
    if not config:
        raise ApiError(f"应用「{appid}」未安装")
    return config


def menu_config(connection, settings, appid, menu_key=""):
    config = installed_config(settings, appid)
    if config:
        version = str(config.get("install_version", ""))
        if version and not re.fullmatch(r"[\w.+-]+", version):
            raise ApiError("应用版本配置错误")
        package = settings.appstore_root / "apps" / appid
        paths = [package / version / "config.yml", package / "config.yml"] if version else [package / "config.yml"]
        package_config = {}
        for path in paths:
            if path.is_file():
                package_config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                break
        menus = package_config.get("menu_items") or [{"key": "", "visible_to": "all"}]
        default = package_config.get("visible_to", "all")
    else:
        custom = db.setting(connection, "microapp_menu") or []
        app = next((a for a in custom if isinstance(a, dict) and str(a.get("id")) == appid), None)
        if app is None:
            raise ApiError("应用未安装")
        menus = app.get("menu_items") or [{"key": "", "visible_to": "all"}]
        default = app.get("visible_to", "admin")
    menu = next((m for m in menus if str(m.get("key", "")) == menu_key), None) if menu_key else menus[0]
    if menu is None:
        raise ApiError("菜单不存在")
    return {**menu, "visible_to": menu.get("visible_to", default)}


def _visible(user, menu):
    visible = menu.get("visible_to", "all")
    if not isinstance(visible, list):
        visible = str(visible).split(",")
    visible = [str(value).strip() for value in visible if str(value).strip()] or ["admin"]
    return "all" in visible or str(user["userid"]) in [str(v) for v in visible] or "admin" in visible and "admin" in str(user.get("identity", "")).split(",")


def _push(connection, appid, key, users, count, dot):
    if users:
        jobs.emit(connection, users, "appBadge", {"appid": appid, "menu_key": key, "count": count, "dot": bool(dot)})


def badge__set(connection, context, data, settings):
    appid, secret = str(data.get("appid", "")).strip(), str(data.get("secret", ""))
    config = require_installed(settings, appid)
    expected = str(config.get("app_secret", ""))
    if not expected or not hmac.compare_digest(expected, secret):
        raise ApiError("密钥无效")
    menu = menu_config(connection, settings, appid, str(data.get("menu_key", "")))
    userids = ids(data.get("userid"))
    if not userids:
        raise ApiError("参数错误")
    users, badges = db.tables["users"], db.tables["app_badges"]
    recipients = [row["userid"] for row in connection.execute(select(users.c.userid, users.c.identity)
        .where(users.c.userid.in_(userids), users.c.disable_at.is_(None))).mappings() if _visible(row, menu)]
    key, count, dot = str(menu.get("key", "")), max(0, int(data.get("count", 0))), on(data.get("dot", False))
    if not count and not dot:
        connection.execute(badges.delete().where(badges.c.app_id == appid, badges.c.menu_key == key, badges.c.userid.in_(recipients)))
    else:
        for userid in recipients:
            stmt = insert(badges).values(app_id=appid, menu_key=key, userid=userid, count=count, dot=dot, updated_at=utcnow())
            connection.execute(stmt.on_duplicate_key_update(count=count, dot=dot, updated_at=utcnow()))
    _push(connection, appid, key, recipients, count, dot)
    return {"appid": appid, "menu_key": key, "count": count, "dot": dot, "affected": len(recipients)}


def badge__clear(connection, context, data, settings):
    actor = auth.authenticate(connection, context)
    appid = str(data.get("appid", "")).strip()
    menu = menu_config(connection, settings, appid, str(data.get("menu_key", "")))
    key, table = str(menu.get("key", "")), db.tables["app_badges"]
    connection.execute(table.delete().where(table.c.app_id == appid, table.c.menu_key == key, table.c.userid == actor.userid))
    _push(connection, appid, key, [actor.userid], 0, False)
    return {"appid": appid, "menu_key": key}


def badge__list(connection, context, data, settings):
    actor = auth.authenticate(connection, context)
    table, result = db.tables["app_badges"], {}
    for row in connection.execute(select(table).where(table.c.userid == actor.userid)).mappings():
        try:
            menu = menu_config(connection, settings, row["app_id"], row["menu_key"])
        except ApiError:
            continue
        if _visible(actor.user, menu):
            result.setdefault(row["app_id"], {})[row["menu_key"]] = {"count": row["count"], "dot": bool(row["dot"])}
    return result


def functions(connection, context, data, settings):
    auth.authenticate(connection, context)
    config = installed_config(settings, "system-functions")
    params = (config or {}).get("params", {})
    return {"installed": config is not None, "functions": {name: config is not None and on(params.get(name.upper(), True))
        for name in ("report", "calendar", "favorite", "recent", "checkin")}}


def dispatch_user_hook(connection, user, action, event_type, changed_fields, settings):
    if action not in ("user_onboard", "user_offboard", "user_update"):
        raise ValueError("Invalid appstore lifecycle hook")
    if not settings.app_key:
        raise ApiError("APP_KEY未配置，无法通知应用商店")
    departments = db.tables["user_departments"]
    rows = connection.execute(select(departments.c.id, departments.c.name).where(departments.c.id.in_(ids(user.get("department"))))).mappings()
    profile = {"id": str(user["userid"]), "email": str(user.get("email", "")), "name": str(user.get("nickname", "")),
        "role": "admin" if "admin" in str(user.get("identity", "")).split(",") else "normal",
        "departments": [{"id": str(r["id"]), "name": r["name"]} for r in rows]}
    profile.update({key: str(user.get(key) or "") for key in ("tel", "profession", "birthday", "address", "introduction")})
    request = Request(settings.appstore_url.rstrip("/") + "/api/v1/internal/hooks/" + action,
        data=encode({"user": profile, "event_type": event_type, "changed_fields": changed_fields}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + hashlib.md5(settings.app_key.encode()).hexdigest(), "Version": settings.version})
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read()
            return json.loads(body) if body else None
    except (HTTPError, URLError, ValueError) as exc:
        raise ApiError(f"应用商店通知失败: {exc}") from exc


ROUTES = {"apps/badge__set": badge__set, "apps/badge__clear": badge__clear,
          "apps/badge__list": badge__list, "apps/functions": functions}
RESPONSE_MODE = "data"
