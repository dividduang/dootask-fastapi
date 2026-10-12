import base64
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from . import auth, crypto, db, security
from .config import Settings
from .context import RequestContext, current_context
from .errors import ApiError, failure, success, json_default, load_translations
from .host import install_host
from .avatar import install_avatar
from . import realtime
from .dav import install_dav
from .domains import accounts, system, projects, dialog, assistant, apps
from .domains import files, filedav, upload, reports, search, index

log = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Exact handler map; no getattr/reflection or arbitrary inherited method exposure.
ALLOWLIST = {
    "users/login": "login", "users/logout": "logout", "users/info": "info",
    "users/editpass": "editpass", "users/key/client": "key_client",
    "users/token/expire": "token_expire", "users/reg/needinvite": "needinvite",
    "users/login/needcode": "needcode", "users/login/codejson": "codejson",
    "users/login/codeimg": "codeimg",
}

def route_aliases(path):
    canonical = path.lower().replace("__", "/")
    aliases = {canonical, path.lower()}
    resource, _, action = canonical.partition("/")
    if "/" in action:
        aliases.add(resource + "/" + action.replace("/", "__", 1))
    if canonical == "assistant/match_elements":
        aliases.add("assistant/match-elements")
    return aliases


DOMAIN_ROUTES = {}
for _module, _mode in ((accounts, "envelope"), (system, "envelope"),
                       (projects, "data"), (dialog, "data"), (assistant, "data"),
                       (apps, "data"), (files, "data"), (filedav, "data"),
                       (upload, "data"), (reports, "data"), (search, "data")):
    for _path, _handler in _module.ROUTES.items():
        for _alias in route_aliases(_path):
            if _alias in DOMAIN_ROUTES:
                raise RuntimeError(f"Duplicate explicit API route: {_alias}")
            DOMAIN_ROUTES[_alias] = (_handler, "raw" if _path in files.RAW_ROUTES else _mode, _path)
for _path, _handler in files.EXTRA_ROUTES.items():
    DOMAIN_ROUTES[_path] = (_handler, "raw", _path)
for _path, _handler in tuple(ALLOWLIST.items()):
    for _alias in route_aliases(_path):
        if _alias in DOMAIN_ROUTES:
            raise RuntimeError(f"Domain overrides shared identity route: {_alias}")
        ALLOWLIST[_alias] = _handler


def field(data, key, default=""):
    value = data.get(key, default)
    if not isinstance(value, (str, int, float, bool)) and value is not None:
        raise ApiError("参数格式错误")
    return str(value).strip() if value is not None else default


def input_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key.endswith("[]"):
            name = key[:-2]
            if name not in result:
                result[name] = []
            if not isinstance(result[name], list):
                raise ApiError("参数格式错误")
            result[name].append(value)
        else:
            result[key] = value
    return result


async def read_inputs(request):
    data = input_pairs(request.query_params.multi_items())
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media == "multipart/form-data":
        # Starlette spools files to disk; never copy a multi-GB upload into RAM.
        total = 0
        async def receive():
            nonlocal total
            message = await request.receive()
            total += len(message.get("body", b""))
            if total > 10 * 1024**3 + MAX_BODY:
                raise ApiError("请求内容过大")
            return message
        bounded = Request(request.scope, receive)
        try:
            form = await bounded.form(max_files=10, max_fields=1000, max_part_size=MAX_BODY)
        except Exception:
            await bounded.close()
            raise
        request.state.upload_form = form
        data.update(input_pairs(form.multi_items()))
        return data
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise ApiError("请求内容过大")
        raw.extend(chunk)
    if not raw:
        return data
    version = request.headers.get("version", "")
    old_form = bool(re.fullmatch(r"\d+\.\d+\.\d+", version) and tuple(map(int, version.split("."))) < (0, 25, 48))
    try:
        if old_form or media == "application/x-www-form-urlencoded":
            body = input_pairs(parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=1000))
        elif media == "application/json":
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ApiError("请求内容必须是对象")
        else:
            raise ApiError("不支持的请求格式")
    except (ValueError, UnicodeError) as exc:
        raise ApiError("请求内容格式错误") from exc
    data.update(body)
    return data


def token_input(request, data):
    return (request.headers.get("dootask-token") or request.query_params.get("dootask-token")
            or field(data, "dootask-token") or request.headers.get("token") or field(data, "token"))


def handle(handler, data, ctx, settings, captcha_cookie):
    # Expensive work has persistent IP/account quotas, not per-process dictionaries.
    if handler in ("login", "editpass", "key_client", "codeimg", "codejson"):
        security.consume_limit(handler + ":ip:" + ctx.ip, 30 if handler != "key_client" else 10, 60)
    if handler in ("codeimg", "codejson"):
        key, image = security.create_captcha(settings.csrf_secret)
        if handler == "codeimg":
            response = Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})
            response.set_cookie("dootask_captcha", key, max_age=300, httponly=True, samesite="lax", secure=ctx.base_url.startswith("https:"))
            return response
        return success({"sensitive": False, "key": key, "img": "data:image/png;base64," + base64.b64encode(image).decode()}, "success")
    email = field(data, "email")
    registration = field(data, "type") == "reg"
    if handler == "login":
        security.consume_limit("login:email:" + email.casefold(), 20, 300)
        with db.transaction() as connection:
            needs = not registration and security.needs_captcha(connection, email)
        if needs:
            code = field(data, "code")
            if not code:
                raise ApiError("请输入验证码", data={"code": "need"})
            security.verify_captcha(field(data, "code_key") or captcha_cookie, code, settings.csrf_secret)
    try:
        with db.transaction() as connection:
            if handler == "login":
                result = auth.login(connection, ctx, email, field(data, "password"), registration=registration,
                                    invite=field(data, "invite"), settings=settings)
                if isinstance(result, auth.EmailVerificationRequired):
                    return failure(ApiError(result.message, data=result.data))
                return success(result, "注册成功" if registration else "登录成功")
            if handler == "info":
                return success(auth.info(connection, ctx), "success")
            if handler == "editpass":
                return success(auth.change_password(connection, ctx, field(data, "oldpass"), field(data, "newpass"), settings=settings), "修改成功")
            if handler == "logout":
                auth.logout(connection, ctx)
                return success(message="退出成功")
            if handler == "token_expire":
                return success(auth.token_expire(connection, ctx, field(data, "refresh") not in ("", "0", "false")), "success")
            if handler == "key_client":
                actor = auth.authenticate(connection, ctx) if ctx.token else None
                return success(crypto.key_exchange(connection, field(data, "client_id"), actor.userid if actor else 0,
                                                   actor.user["email"] if actor else "", settings.pgp_storage_key), "success")
            if handler == "needinvite":
                mode = db.setting(connection, "system", "reg", "open")
                return success({"enabled": mode != "close", "need": mode == "invite"}, "success")
            if handler == "needcode":
                needed = security.needs_captcha(connection, email)
                return {"ret": 1 if needed else 0, "msg": "need" if needed else "no", "data": []}
    except ApiError as exc:
        if handler == "login" and not registration and exc.message in ("帐号或密码错误", "帐号已停用..."):
            security.mark_login_failure(email)
            with db.transaction() as connection:
                needed = security.needs_captcha(connection, email)
            exc.data = {"code": "need" if needed else "no"}
        raise
    except IntegrityError as exc:
        if handler == "login" and registration:
            raise ApiError("邮箱地址已存在") from exc
        raise
    raise RuntimeError("Allowlisted handler lacks implementation")


def dispatch(handler, data, ctx, settings, request):
    try:
        if ctx.pgp.get("encrypt_type") == "pgp":
            if request.method != "POST" or not field(data, "encrypted"):
                raise ApiError("加密请求必须使用POST并提供encrypted")
            with db.transaction() as connection:
                data.update(crypto.decrypt_request(connection, field(data, "encrypted"), ctx.pgp.get("encrypt_id", ""), settings.pgp_storage_key))
        ctx.token = token_input(request, data)
        if len(ctx.token) > 512:
            raise ApiError("身份已失效,请重新登录", -1)
        if isinstance(handler, tuple):
            function, mode, canonical = handler
            if canonical in projects.POST_ONLY_ROUTES and request.method != "POST":
                return Response(status_code=405, headers={"Allow": "POST"})
            with db.transaction() as connection:
                if canonical in projects.ROUTES and canonical not in projects.PUBLIC_ROUTES:
                    auth.authenticate(connection, ctx)
                payload = function(connection, ctx, data, settings)
            if payload is None:
                return Response(media_type="text/html")
            if mode == "data" and not isinstance(payload, Response):
                payload = success(payload)
        else:
            payload = handle(handler, data, ctx, settings, request.cookies.get("dootask_captcha", ""))
    except ApiError as exc:
        payload = failure(exc)
    if isinstance(payload, Response):
        return payload
    if ctx.pgp.get("client_type") == "pgp":
        payload = crypto.encrypt_response(payload, ctx.pgp["client_key"])
    return Response(json.dumps(payload, ensure_ascii=False, default=json_default), media_type="application/json",
                    headers={"Cache-Control": "no-store", "X-Request-ID": ctx.request_id})


def create_app(settings=None):
    settings = settings or Settings.from_env()
    load_translations(settings.frontend_root / "language/translate.json")

    @asynccontextmanager
    async def lifespan(app):
        engine = await run_in_threadpool(db.configure, settings)
        try:
            for registrar in (accounts.register_jobs, projects.register_jobs, dialog.register_jobs,
                              assistant.register_jobs, files.register_jobs, reports.register_jobs, search.register_jobs):
                registrar()
            async with realtime.job_lifecycle(settings):
                yield
        finally:
            await run_in_threadpool(engine.dispose)

    app = FastAPI(title="DooTask independent FastAPI backend", version=settings.version,
                  lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.api_route("/api/{path:path}", methods=METHODS, include_in_schema=False)
    async def api(request: Request, path: str):
        handler = ALLOWLIST.get(path.lower()) or DOMAIN_ROUTES.get(path.lower())
        if not handler:
            if request.headers.get("content-type") == "application/json":
                return JSONResponse(failure(ApiError("接口不存在")))
            return Response(status_code=404, headers={"X-Error-Message-Base64": base64.b64encode("接口不存在".encode()).decode()})
        ctx = RequestContext(request_id=secrets.token_hex(16), language=request.headers.get("language", settings.locale),
                             platform=request.headers.get("platform", "web"), version=request.headers.get("version", ""),
                             fd=request.headers.get("fd", ""), ip=request.client.host if request.client else "",
                             user_agent=request.headers.get("user-agent", "")[:1024], base_url=str(request.base_url),
                             headers=dict(request.headers))
        context_token = current_context.set(ctx)
        try:
            ctx.pgp = crypto.parse_header(request.headers.get("encrypt", ""))
            data = await read_inputs(request)
            return await run_in_threadpool(dispatch, handler, data, ctx, settings, request)
        except ApiError as exc:
            # Invalid client keys cannot themselves receive an encrypted response.
            return JSONResponse(failure(exc), headers={"Cache-Control": "no-store"})
        except SQLAlchemyError:
            log.exception("Database failure request_id=%s", ctx.request_id)
            return JSONResponse(failure(ApiError("数据服务暂不可用")), status_code=503)
        finally:
            if hasattr(request.state, "upload_form"):
                await request.state.upload_form.close()
            current_context.reset(context_token)

    install_avatar(app, settings)
    realtime.install_realtime(app, settings)
    assistant.install_sse(app, settings)
    install_dav(app, settings)
    index.install_index(app, settings)
    install_host(app, settings)
    return app
