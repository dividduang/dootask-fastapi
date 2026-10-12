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
for _path, _handler in tuple(ALLOWLIST.items()):
    if _path.count("/") == 2:
        _resource, _method, _action = _path.split("/")
        ALLOWLIST[f"{_resource}/{_method}__{_action}"] = _handler


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
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise ApiError("请求内容过大")
        raw.extend(chunk)
    if not raw:
        return data
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    version = request.headers.get("version", "")
    old_form = bool(re.fullmatch(r"\d+\.\d+\.\d+", version) and tuple(map(int, version.split("."))) < (0, 25, 48))
    try:
        if old_form or media == "application/x-www-form-urlencoded":
            body = input_pairs(parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=1000))
        elif media == "application/json":
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ApiError("请求内容必须是对象")
        elif media == "multipart/form-data":
            async def receive():
                return {"type": "http.request", "body": bytes(raw), "more_body": False}
            bounded = Request(request.scope, receive)
            body = input_pairs((await bounded.form(max_files=10, max_fields=1000, max_part_size=MAX_BODY)).multi_items())
        else:
            raise ApiError("不支持的请求格式")
    except (ValueError, UnicodeError) as exc:
        raise ApiError("请求内容格式错误") from exc
    data.update(body)
    return data


def token_input(request, data):
    return (request.headers.get("dootask-token") or field(data, "dootask-token")
            or request.headers.get("token") or field(data, "token"))


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
                return success(auth.login(connection, ctx, email, field(data, "password"), registration=registration,
                                          invite=field(data, "invite")), "注册成功" if registration else "登录成功")
            if handler == "info":
                return success(auth.info(connection, ctx), "success")
            if handler == "editpass":
                return success(auth.change_password(connection, ctx, field(data, "oldpass"), field(data, "newpass")), "修改成功")
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
            yield
        finally:
            await run_in_threadpool(engine.dispose)

    app = FastAPI(title="DooTask FastAPI shared backend", version=settings.version,
                  lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.api_route("/api/{path:path}", methods=METHODS, include_in_schema=False)
    async def api(request: Request, path: str):
        handler = ALLOWLIST.get(path.lower())
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
            current_context.reset(context_token)

    install_avatar(app, settings)
    install_host(app, settings)
    return app
