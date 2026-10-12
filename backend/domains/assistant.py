import asyncio
import base64
import hashlib
import io
import json
import math
import re
from datetime import timedelta
from uuid import uuid4
from urllib.parse import urlencode, quote
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError, URLError
import redis
from PIL import Image
from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from .. import auth as identity, db, jobs
from ..context import RequestContext, utcnow
from ..errors import ApiError
from ..storage import atomic_write, confined_path
from . import apps
from .dialog_core import decode, encode, get_dialog


def actor(connection, context, settings):
    apps.require_installed(settings, "ai")
    return identity.authenticate(connection, context)


def request_json(url, payload, *, bearer, form=False, timeout=30):
    body = urlencode(payload).encode() if form else encode(payload).encode()
    request = URLRequest(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded" if form else "application/json", "Authorization": "Bearer " + bearer})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read(8192).decode("utf-8", errors="replace")
        raise ApiError(f"AI 接口错误 [{exc.code}]: {detail}") from exc
    except (URLError, ValueError) as exc:
        raise ApiError(f"AI 接口请求失败: {exc}") from exc


def auth(connection, context, data, settings):
    user = actor(connection, context, settings)
    config = db.setting(connection, "aibotSetting") or {}
    model_type, model_name = str(data.get("model_type", "")).strip(), str(data.get("model_name", "")).strip()
    if not model_type or not model_name:
        raise ApiError("参数错误")
    key = config.get(model_type + "_key", "")
    if model_type == "ollama" and not key:
        key = uuid4().hex[:6]
    if not key:
        raise ApiError("模型未启用")
    history = decode(data.get("context", []), [])
    if not isinstance(history, list):
        raise ApiError("context 参数格式错误")
    history = [item for item in history if isinstance(item, list) and len(item) >= 2 and item[0] and item[1]]
    fd = str(context.fd or data.get("fd", ""))
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        if client.hget(f"dootask:connection:{fd}", "userid") != str(user.userid):
            fd = "0"
        payload = {"api_key": key, "model_type": "qwen" if model_type == "qianwen" else model_type,
            "model_name": re.sub(r"(?:[\s_-]+(?:think|thinking|reasoning)|\s*\(\s*(?:think|thinking|reasoning)\s*\))$", "", model_name),
            "context": encode(history), "locale": "zh" if "zh" in str(data.get("locale", context.language)).lower() else "en",
            "rag_enabled": "1", "context_key": str(data.get("session_id", ""))[:100], "doo_enabled": "1", "fd": int(fd)}
        for field in ("base_url", "agency"):
            if config.get(model_type + "_" + field):
                payload[field] = config[model_type + "_" + field]
        result = request_json(settings.ai_url.rstrip("/") + "/invoke/auth", payload, bearer=context.token, form=True)
        if result.get("code") != 200:
            raise ApiError(result.get("error") or "AI 接口返回异常", data=result)
        stream_key = result.get("data", {}).get("stream_key")
        if not stream_key:
            raise ApiError("AI 接口返回数据异常")
        # Bind opaque upstream key to authenticated account; stream URL is a bearer capability.
        client.set("dootask:ai-stream:" + hashlib.sha256(stream_key.encode()).hexdigest(),
                   encode({"userid": user.userid, "token": context.token}), ex=3600)
        return {"stream_key": stream_key}
    finally:
        client.close()


def models(connection, context, data, settings):
    apps.require_installed(settings, "ai")
    return {k: v for k, v in (db.setting(connection, "aibotSetting") or {}).items() if k.endswith(("_model", "_models"))}


def embeddings(connection, settings, texts):
    apps.require_installed(settings, "ai")
    config = db.setting(connection, "aiIntegration") or {}
    if not settings.app_key:
        raise ApiError("APP_KEY未配置")
    result = request_json(config.get("embeddings_url", "http://ai:5001/embeddings"),
        {"input": [str(text)[:30000] for text in texts]}, bearer=settings.app_key, timeout=120 if len(texts) > 1 else 30)
    if result.get("code", 200) != 200:
        raise ApiError(f"Embedding 接口错误 [{result.get('code')}]: {result.get('error', 'unknown')}", data=result)
    vectors = {int(item["index"]): item["embedding"] for item in result.get("data", [])}
    if any(i not in vectors or not vectors[i] for i in range(len(texts))):
        raise ApiError("Embedding 向量为空")
    return [vectors[i] for i in range(len(texts))]


def cosine(a, b):
    if len(a) != len(b):
        raise ApiError("Embedding 向量维度不匹配")
    denominator = math.sqrt(sum(v * v for v in a) * sum(v * v for v in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


def match_elements(connection, context, data, settings):
    actor(connection, context, settings)
    query, elements = str(data.get("query", "")).strip(), data.get("elements", [])
    if not query or not isinstance(elements, list) or not elements:
        raise ApiError("参数不能为空")
    elements = [el for el in elements if isinstance(el, dict) and el.get("name")]
    vectors = embeddings(connection, settings, [query] + [el["name"] for el in elements])
    matches = [{"element": el, "similarity": cosine(vectors[0], vector)} for el, vector in zip(elements, vectors[1:])]
    return {"matches": sorted(matches, key=lambda item: item["similarity"], reverse=True)[:max(0, min(50, int(data.get("top_k", 10))))]}


def log__search(connection, context, data, settings):
    user = actor(connection, context, settings)
    query = str(data.get("query", "")).strip()[:500]
    if not query:
        raise ApiError("参数错误")
    dialog_id = max(0, int(data.get("dialog_id", 0)))
    if dialog_id:
        get_dialog(connection, dialog_id, user.userid)
    count = max(0, int(data.get("result_count", 0)))
    db.insert_row(connection, "ai_assistant_search_logs", userid=user.userid, dialog_id=dialog_id,
        context_key=str(data.get("context_key", ""))[:191], source=data.get("source") if data.get("source") in ("chat", "invoke") else "",
        query=query, locale=data.get("locale") if data.get("locale") in ("zh", "en") else "",
        source_ids=encode(data.get("source_ids", [])[:10]), top_score=max(0, min(1, float(data.get("top_score", 0)))),
        result_count=count, duration_ms=max(0, int(data.get("duration_ms", 0))), empty=int(not count))
    return []


def feedback__save(connection, context, data, settings):
    user = actor(connection, context, settings)
    session_id, local_id = str(data.get("session_id", ""))[:100], int(data.get("local_id", 0))
    feedback = str(data.get("feedback", ""))
    if not session_id or local_id <= 0:
        raise ApiError("参数错误")
    if feedback not in ("", "like", "dislike"):
        raise ApiError("反馈类型错误")
    table = db.tables["ai_assistant_feedbacks"]
    unique = dict(userid=user.userid, session_key=str(data.get("session_key", "default"))[:100], session_id=session_id, local_id=local_id)
    if not feedback:
        connection.execute(table.delete().where(*(table.c[key] == value for key, value in unique.items())))
    else:
        answer = str(data.get("answer", "")).strip()[:2000]
        values = dict(feedback=feedback, prompt=str(data.get("prompt", "")).strip()[:1000], answer=answer,
            answer_digest=hashlib.md5(answer.encode()).hexdigest(), source_ids=encode(data.get("source_ids", [])[:10]),
            model=str(data.get("model", ""))[:100], updated_at=utcnow())
        statement = insert(table).values(**unique, **values, created_at=utcnow())
        connection.execute(statement.on_duplicate_key_update(**values))
    return {"feedback": feedback}


def operation__dispatch(connection, context, data, settings):
    user = actor(connection, context, settings)
    fd, action = str(context.fd or data.get("fd", "")), str(data.get("action", "")).strip()
    if not fd or not action:
        raise ApiError("参数错误")
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        if client.hget(f"dootask:connection:{fd}", "userid") != str(user.userid):
            raise ApiError("会话不存在或无权限")
    finally:
        client.close()
    request_id = uuid4().hex
    db.insert_row(connection, "backend_operations", request_id=request_id, userid=user.userid, fd=fd,
        expires_at=utcnow() + timedelta(seconds=60))
    jobs.enqueue_push(connection, fds=[fd], message={"type": "operation", "data": {
        "requestId": request_id, "action": action, "payload": data.get("payload", [])}})
    return {"requestId": request_id}


def operation__result(connection, context, data, settings):
    user = actor(connection, context, settings)
    request_id = str(data.get("request_id", ""))
    if not request_id:
        raise ApiError("参数错误")
    table = db.tables["backend_operations"]
    row = connection.execute(select(table).where(table.c.request_id == request_id).with_for_update()).mappings().first()
    if row and row["userid"] != user.userid:
        raise ApiError("无权限")
    if not row or not row["result"]:
        return {"status": "pending"}
    connection.execute(table.delete().where(table.c.request_id == request_id))
    result = decode(row["result"])
    return {"status": "ready", "success": result["success"], "result": result.get("result"), "error": result.get("error")}


def session__list(connection, context, data, settings):
    user = actor(connection, context, settings)
    table = db.tables["ai_assistant_sessions"]
    rows = connection.execute(select(table).where(table.c.userid == user.userid,
        table.c.session_key == str(data.get("session_key", "default"))).order_by(table.c.updated_at.desc())).mappings()
    return [{"id": r["session_id"], "title": r["title"], "responses": decode(r["data"], []),
        "images": {key: context.base_url.rstrip("/") + "/" + path.lstrip("/") for key, path in decode(r["images"]).items()},
        "sceneKey": r["scene_key"], "createdAt": int(r["created_at"].timestamp() * 1000) if r["created_at"] else 0,
        "updatedAt": int(r["updated_at"].timestamp() * 1000) if r["updated_at"] else 0} for r in rows]


def save_image(connection, context, settings, data_url):
    match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)", str(data_url))
    if not match:
        raise ApiError("图片格式错误")
    try:
        raw = base64.b64decode(match[2], validate=True)
        if len(raw) > 20 * 1024 * 1024:
            raise ApiError("图片太大")
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
    except (ValueError, OSError) as exc:
        raise ApiError("图片格式错误") from exc
    from .upload import generic_upload
    return generic_upload(connection, context, io.BytesIO(raw), uuid4().hex + "." + match[1], {}, settings, image=True)["path"]


def session__save(connection, context, data, settings):
    user = actor(connection, context, settings)
    session_id = str(data.get("session_id", ""))[:100]
    if not session_id:
        raise ApiError("session_id 不能为空")
    table = db.tables["ai_assistant_sessions"]
    unique = dict(userid=user.userid, session_key=str(data.get("session_key", "default"))[:100], session_id=session_id)
    existing = connection.execute(select(table).where(*(table.c[key] == value for key, value in unique.items())).with_for_update()).mappings().first()
    images = decode(existing["images"]) if existing else {}
    added = {}
    for item in data.get("new_images", []):
        if item.get("imageId") and item.get("dataUrl"):
            added[str(item["imageId"])] = save_image(connection, context, settings, item["dataUrl"])
    images.update(added)
    values = dict(scene_key=str(data.get("scene_key", ""))[:200], title=str(data.get("title", ""))[:255],
        data=encode(data.get("data", [])), images=encode(images), updated_at=utcnow())
    statement = insert(table).values(**unique, **values, created_at=utcnow())
    connection.execute(statement.on_duplicate_key_update(**values))
    return {"image_urls": {key: context.base_url.rstrip("/") + "/" + path.lstrip("/") for key, path in added.items()}}


def session__delete(connection, context, data, settings):
    user = actor(connection, context, settings)
    table = db.tables["ai_assistant_sessions"]
    predicate = [table.c.userid == user.userid, table.c.session_key == str(data.get("session_key", "default"))]
    if not apps.on(data.get("clear_all", False)):
        if not data.get("session_id"):
            raise ApiError("session_id 不能为空")
        predicate.append(table.c.session_id == str(data["session_id"]))
    for row in connection.execute(select(table).where(*predicate).with_for_update()).mappings():
        jobs.enqueue(connection, "assistant.delete_images", {"userid": user.userid, "paths": list(decode(row["images"]).values())})
    connection.execute(table.delete().where(*predicate))
    return []


def _delete_images(connection, payload, settings):
    from urllib.parse import urlsplit, parse_qs
    downloads = db.tables["backend_file_downloads"]
    for path in payload["paths"]:
        parsed = urlsplit(path)
        key = parse_qs(parsed.query).get("key", [""])[0]
        if parsed.path != "/api/file/bytes" or parsed.netloc or not key:
            raise ApiError("图片路径无效")
        connection.execute(downloads.delete().where(downloads.c.id == key, downloads.c.userid == payload["userid"]))


def install_sse(app, settings):
    async def stream(stream_key: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,512}", stream_key):
            raise ApiError("无效的流凭据")
        def open_stream():
            client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
            try:
                raw = client.get("dootask:ai-stream:" + hashlib.sha256(stream_key.encode()).hexdigest())
            finally:
                client.close()
            if not raw:
                raise ApiError("流凭据已过期", -1)
            grant = decode(raw)
            context = RequestContext(request_id=uuid4().hex, token=grant["token"])
            with db.transaction() as connection:
                user = actor(connection, context, settings)
                if user.userid != grant["userid"]:
                    raise ApiError("无权限")
            headers = {"Accept": "text/event-stream", "Authorization": "Bearer " + context.token}
            if request.headers.get("last-event-id"):
                headers["Last-Event-ID"] = request.headers["last-event-id"]
            response = urlopen(URLRequest(settings.ai_url.rstrip("/") + "/invoke/stream/" + quote(stream_key, safe=""), headers=headers), timeout=60)
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                response.close()
                raise ApiError("AI 流响应格式错误")
            return response
        upstream = await asyncio.to_thread(open_stream)
        async def body():
            try:
                while not await request.is_disconnected():
                    chunk = await asyncio.to_thread(upstream.readline, 65536)
                    if not chunk:
                        break
                    # Preserve real append/replace/done frames, IDs and heartbeats verbatim.
                    yield chunk
            finally:
                upstream.close()
        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    app.add_api_route("/ai/invoke/stream/{stream_key}", stream, methods=["GET"])


def register_jobs():
    jobs.register("assistant.delete_images", _delete_images)


ROUTES = {"assistant/auth": auth, "assistant/models": models, "assistant/match_elements": match_elements,
    "assistant/log__search": log__search, "assistant/feedback__save": feedback__save,
    "assistant/operation__dispatch": operation__dispatch, "assistant/operation__result": operation__result,
    "assistant/session__list": session__list, "assistant/session__save": session__save, "assistant/session__delete": session__delete}
RESPONSE_MODE = "data"
