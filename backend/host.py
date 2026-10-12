"""Production host for the unchanged public tree; install after domain routes."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from backend.config import Settings


_CSRF_COOKIE = "dootask_csrf"
_CSRF_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_STATIC_ROOTS = frozenset({"assets", "audio", "css", "docs", "images", "js", "language", "tools"})
_STATIC_FILES = frozenset({"favicon.ico", "privacy.html", "robots.txt"})
_PRIVATE_SUFFIXES = frozenset({
    ".php", ".php3", ".php4", ".php5", ".php7", ".php8", ".phtml", ".phar",
    ".config", ".conf", ".env", ".ini", ".toml", ".yaml", ".yml", ".lock",
    ".map", ".vue", ".ts", ".tsx", ".jsx", ".py", ".pyc", ".sh", ".bat",
    ".ps1", ".sql", ".sqlite", ".db", ".log", ".bak", ".pem", ".key",
})
# Exact routes.js grammar, excluding the frontend's catch-all 404 component.
_VUE_ROUTES = re.compile(
    r"(?:/|/(?:main|pro|token|login|preload)|/meeting(?:/[^/]+(?:/[^/]+)?)?"
    r"|/manage(?:/(?:dashboard|calendar|application|messenger(?:/[^/]+)?"
    r"|setting(?:/(?:personal|checkin|language|theme|keyboard|license|password|email"
    r"|system|device|system-update|version|delete))?"
    r"|project/(?:invite(?:/[^/]+)?|[^/]+)|file(?:/[^/]+(?:/[^/]+)?)?))?"
    r"|/single/(?:file/(?:msg/[^/]+|task/[^/]+|[^/]+)"
    r"|task/(?:content/[^/]+|[^/]+)|dialog/[^/]+|apps/[^/]+|valid/email"
    r"|report/(?:edit|detail)/[^/]+))\Z",
    re.IGNORECASE,
)
_TEMPLATE_NAMES = frozenset({"main", "desktop", "download", "report", "email/unread", "ie"})


def _safe_url(value: str) -> str:
    """Reject executable URL schemes, independently of HTML attribute escaping."""
    if not isinstance(value, str):
        raise ValueError("Template URLs must be strings")
    if value != value.strip() or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Template URL contains boundary whitespace or control characters")
    if urlsplit(value).scheme.lower() not in {"", "http", "https"}:
        raise ValueError("Template URL must use HTTP(S) or a relative path")
    return value


_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).with_name("templates")),
    autoescape=select_autoescape(("html",)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
_TEMPLATES.filters["safe_url"] = _safe_url


def _secret_bytes(secret: str) -> bytes:
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("CSRF_SECRET must contain at least 32 characters")
    return secret.encode("utf-8")


def _sign_csrf(token: str, secret: str) -> str:
    signature = hmac.new(_secret_bytes(secret), token.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def _cookie_csrf(request: Request, secret: str) -> str | None:
    cookie = request.cookies.get(_CSRF_COOKIE, "")
    if len(cookie) != 129:
        return None
    token, separator, signature = cookie.partition(".")
    if not separator or not _CSRF_NONCE.fullmatch(token) or not _CSRF_NONCE.fullmatch(signature):
        return None
    return token if hmac.compare_digest(cookie, _sign_csrf(token, secret)) else None


def verify_csrf(request: Request, secret: str, token: str | None = None) -> bool:
    """Validate an explicit form token or X-CSRF-TOKEN against the signed cookie.

    This is not authentication. Source token-authenticated /api/* and DAV are
    CSRF-exempt; future cookie-authenticated mutations must call this explicitly.
    """
    expected = _cookie_csrf(request, secret)
    supplied = token if token is not None else request.headers.get("x-csrf-token", "")
    return bool(expected and _CSRF_NONCE.fullmatch(supplied) and hmac.compare_digest(expected, supplied))


def render_template(
    name: str, context: Mapping[str, Any], request: Request | None = None,
) -> HTMLResponse:
    """Render a named source-derived template; business data is always caller-owned.

    Accepts Blade-style names (email.unread) or slash names, with optional .html.
    Report cells and email previews escape plain strings. Domains may supply
    markupsafe.Markup only for HTML they have generated or sanitized themselves.
    Without a request, desktop/download callers must supply their own csrf_token.
    """
    name = name.removesuffix(".html").replace(".", "/")
    if name not in _TEMPLATE_NAMES:
        raise ValueError("Unknown host template")
    values = dict(context)
    values["legacy_ie"] = bool(request and any(
        version in request.headers.get("user-agent", "") for version in ("MSIE 6", "MSIE 7", "MSIE 8")
    ))
    origin = f"//{request.url.netloc}" if request else ""
    values["asset_main"] = lambda path: origin + "/" + path.lstrip("/")
    secret = None
    if request is not None:
        secret = request.app.state.host_settings.csrf_secret
        values["csrf_token"] = _cookie_csrf(request, secret) or secrets.token_hex(32)
    response = HTMLResponse(_TEMPLATES.get_template(name + ".html").render(values))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if secret is not None:
        response.set_cookie(
            _CSRF_COOKIE, _sign_csrf(values["csrf_token"], secret),
            httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/",
        )
    return response


def _public_path_allowed(path: str) -> bool:
    if "\\" in path or "%" in path or ":" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    parts = path.split("/")
    for part in parts:
        if not part or part.startswith(".") or part.endswith((".", " ")):
            return False
        # Reject PHP suffixes even in backup/double-extension filenames.
        if any("." + suffix in _PRIVATE_SUFFIXES for suffix in part.lower().split(".")[1:]):
            return False
    return True


def _manifest_entry(public: Path) -> tuple[str, str]:
    with public.joinpath("manifest.json").open(encoding="utf-8") as handle:
        entry = json.load(handle)["resources/assets/js/app.js"]
    script, style = entry["file"], entry["css"][0]
    for path in (script, style):
        if not isinstance(path, str) or not _public_path_allowed(path):
            raise ValueError("Invalid application manifest resource path")
        resolved = public.joinpath(path).resolve()
        if not resolved.is_relative_to(public) or not resolved.is_file():
            raise ValueError(f"Missing application manifest resource: {path}")
    return script, style


def install_host(app: FastAPI, settings: Settings) -> None:
    """Install last: API, WS, DAV, uploads and service routes retain precedence.

    frontend_root is the copied frontend tree, not its public subdirectory.
    No PHP, Vite hot server, source tree or blanket SPA fallback is exposed.
    """
    _secret_bytes(settings.csrf_secret)
    public = settings.frontend_root.joinpath("public").resolve()
    script, style = _manifest_entry(public)
    static = StaticFiles(directory=public, html=False, follow_symlink=False)
    app.state.host_settings = settings

    async def host(request: Request, path: str) -> Response:
        # ASGI already decoded percent escapes; a residual '%' is rejected rather
        # than decoded twice. This also excludes Windows ADS and dotfile aliases.
        clean = path.rstrip("/")
        if clean and not _public_path_allowed(clean):
            raise HTTPException(status_code=404)
        if clean == "version":
            return RedirectResponse(str(request.base_url) + "api/system/version", status_code=301)
        if clean == "health":
            return PlainTextResponse("ok")
        if clean == "api":
            return RedirectResponse(str(request.base_url) + "docs/index.html", status_code=301)
        if clean == "default":
            return HTMLResponse("")
        if _VUE_ROUTES.fullmatch("/" + clean):
            logo = settings.login_logo
            if logo and not logo.startswith(("/", "http://", "https://", "ftp://", "data:image/")):
                logo = str(request.base_url) + logo
            origin = f"//{request.url.netloc}/"
            return render_template("main", {
                "locale": settings.locale,
                "system_alias": settings.system_alias,
                "login_logo": logo,
                "debug": "yes" if settings.debug else "no",
                "version": settings.version,
                "script": origin + script,
                "style": origin + style,
            }, request)
        if clean in _STATIC_FILES or clean.split("/", 1)[0] in _STATIC_ROOTS:
            # StaticFiles does canonical containment checks, conditional GET and
            # byte ranges. html=False means no directory index or SPA fallback.
            response = await static.get_response(path, request.scope)
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        raise HTTPException(status_code=404)

    app.add_api_route("/{path:path}", host, methods=["GET", "HEAD"], include_in_schema=False)
