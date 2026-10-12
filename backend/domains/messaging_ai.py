"""Actual configured AI provider calls shared by dialog translation/audio/jobs."""
import json
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from uuid import uuid4
from .. import db
from ..errors import ApiError
from .apps import require_installed
from .dialog_core import encode

PRIORITY = ("openai", "claude", "deepseek", "gemini", "grok", "ollama", "zhipu", "qianwen", "wenxin", "dooai")


def provider(connection, settings, audio=False):
    require_installed(settings, "ai")
    config = db.setting(connection, "aibotSetting") or {}
    for vendor in ("openai",) if audio else PRIORITY:
        key = str(config.get(vendor + "_key", "")).strip()
        base = str(config.get(vendor + "_base_url", "")).strip()
        model = str(config.get(vendor + "_model", "")).strip()
        if vendor == "openai":
            if not key:
                continue
            base = base or "https://api.openai.com/v1"
            model = "gpt-4o-mini-transcribe" if audio else model or "gpt-5.1-mini"
        elif vendor == "ollama":
            if not base:
                continue
            key = key or uuid4().hex[:6]
        elif not key or not base:
            continue
        if model:
            return {"vendor": vendor, "key": key, "base": base.rstrip("/"), "model": model, "proxy": config.get(vendor + "_agency", "")}
    raise ApiError("请先在「AI 助手」设置中配置 OpenAI" if audio else "请先配置 AI 助手")


def _request(provider, path, body, content_type, timeout):
    req = Request(provider["base"] + path, data=body, headers={"Authorization": "Bearer " + provider["key"], "Content-Type": content_type})
    proxy = provider.get("proxy")
    if proxy and not str(proxy).startswith(("http://", "https://")):
        raise ApiError("AI SOCKS代理需部署支持该代理的HTTP出口")
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else build_opener()
    try:
        with opener.open(req, timeout=timeout) as response:
            result = json.loads(response.read())
    except HTTPError as exc:
        raise ApiError("AI 接口请求失败", data={"status": exc.code, "body": exc.read(8192).decode(errors="replace")}) from exc
    except (URLError, ValueError) as exc:
        raise ApiError(f"AI 接口请求失败: {exc}") from exc
    return result


def invoke(connection, settings, messages, timeout=60):
    config = provider(connection, settings)
    result = _request(config, "/chat/completions", encode({"model": config["model"], "messages": messages}).encode(), "application/json", timeout)
    choices = result.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if not content:
        raise ApiError("AI 接口返回数据格式错误", data=result)
    return str(content).strip()


def transcribe(connection, settings, path, prompt=""):
    config = provider(connection, settings, audio=True)
    boundary = "dootask-" + uuid4().hex
    fields = {"model": config["model"]}
    if prompt:
        fields["prompt"] = prompt
    body = bytearray()
    for key, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio{path.suffix}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    body.extend(path.read_bytes())
    body.extend(f'\r\n--{boundary}--\r\n'.encode())
    result = _request(config, "/audio/transcriptions", bytes(body), "multipart/form-data; boundary=" + boundary, 15)
    if not result.get("text"):
        raise ApiError("语音转文字失败", data=result)
    return result["text"]


def translate(connection, settings, text, language):
    prompt = f"你是一名资深的专业翻译专家，专门从事项目任务管理系统的多语言本地化工作。将文本翻译为{language}。保持专业术语一致、原文格式结构、HTML标签、特殊符号、数字日期及专有名词，只返回翻译结果，不含解释或标记。"
    return invoke(connection, settings, [{"role": "system", "content": prompt}, {"role": "user", "content": f"请将以下内容翻译为 {language}：\n\n{text}"}])
