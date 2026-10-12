"""Source-compatible rich chat normalization with a strict HTML trust boundary."""
import base64
from html import escape
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit, urljoin
from PIL import Image
from .. import db
from ..errors import ApiError

ALLOWED = {"blockquote", "strong", "pre", "ol", "ul", "li", "em", "p", "s", "u", "a", "span", "br", "img"}
VOID = {"br", "img"}
STYLES = {"color", "font-size", "background-color", "font-weight", "font-family", "text-decoration", "font-style"}


def public_image(url, redirects=3):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError("图片地址无效")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ApiError("不允许访问内部图片地址")
    address = addresses[0][4][0]
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=15)
    connection.sock = socket.create_connection((address, port), timeout=15)
    if parsed.scheme == "https":
        connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
    try:
        connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else "") or "/", headers={"Accept": "image/*"})
        response = connection.getresponse()
        if response.status in (301, 302, 303, 307, 308) and redirects:
            return public_image(urljoin(url, response.getheader("Location", "")), redirects - 1)
        if response.status != 200:
            raise ApiError(f"获取图片失败: HTTP {response.status}")
        raw = response.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ApiError("图片太大")
        return raw
    finally:
        connection.close()


def normalize_html(connection, context, text, settings):
    from .upload import generic_upload

    def image_source(source, attrs):
        raw = None
        if source.startswith("data:image/"):
            match = re.fullmatch(r"data:image/(png|jpg|jpeg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)", source)
            if not match:
                raise ApiError("图片格式错误")
            raw = base64.b64decode(match[2], validate=True)
        elif source.startswith(("http://", "https://")) and db.setting(connection, "system", "image_save_local") != "close":
            raw = public_image(source)
        if raw is not None:
            if len(raw) > 20 * 1024 * 1024:
                raise ApiError("图片太大")
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    ext = image.format.lower()
                    width, height = image.size
                    image.verify()
            except OSError as exc:
                raise ApiError("图片格式错误") from exc
            uploaded = generic_upload(connection, context, io.BytesIO(raw), "image." + ext, {}, settings, image=True)
            attrs["width"], attrs["height"] = str(width), str(height)
            return uploaded["path"]
        if source.startswith(("{{RemoteURL}}", "/", "uploads/", "images/", "http://", "https://")) and not source.startswith("//"):
            return source
        raise ApiError("图片地址无效")

    class Cleaner(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.output = []
            self.suppressed = 0
            self.stack = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if self.suppressed:
                if tag not in VOID:
                    self.suppressed += 1
                return
            if tag == "span" and attrs.get("class") == "mention" and attrs.get("data-denotation-char") in ("@", "#", "~", "%"):
                char, key, value = attrs["data-denotation-char"], attrs.get("data-id", ""), attrs.get("data-value", "")
                if char in ("@", "#"):
                    if not str(key).isdigit():
                        raise ApiError("提及参数错误")
                    kind = "user" if char == "@" else "task"
                    self.output.append(f'<span class="mention {kind}" data-id="{int(key)}">{char}{escape(value)}</span>')
                else:
                    if str(key).isdigit():
                        if char == "~":
                            from . import files
                            key = files.link(connection, context, {"id": int(key), "refresh": "no", "guest_access": "no"}, settings)["code"]
                        else:
                            from . import reports
                            key = reports.generate_link(connection, context, int(key))["code"]
                    elif "/single/" in key:
                        key = key.rsplit("/", 1)[-1]
                    if not re.fullmatch(r"[A-Za-z0-9_+/=-]{8,191}", str(key)):
                        raise ApiError("分享链接错误")
                    route, kind = ("file", "file") if char == "~" else ("report/detail", "report")
                    self.output.append(f'<a class="mention {kind}" href="{{{{RemoteURL}}}}single/{route}/{escape(str(key))}" target="_blank">{char}{escape(value)}</a>')
                self.suppressed = 1
                return
            if tag not in ALLOWED:
                self.stack.append(None)
                return
            safe = {}
            if tag == "img":
                source = attrs.get("src") or attrs.get("data-asset", "")
                safe["class"] = "emoticon" if attrs.get("class") == "emoticon" else "browse"
                safe["src"] = image_source(source, attrs)
                for key in ("width", "height"):
                    if str(attrs.get(key, "")).isdigit():
                        safe[key] = attrs[key]
                safe["alt"] = attrs.get("alt", attrs.get("data-name", ""))
            elif tag == "a":
                href = attrs.get("href", "")
                if href.startswith(("http://", "https://", "mailto:", "{{RemoteURL}}", "/")) and not href.startswith("//"):
                    safe.update(href=href, target="_blank", rel="noopener noreferrer")
                if attrs.get("class") in ("mention file", "mention report"):
                    safe["class"] = attrs["class"]
            elif tag == "li" and attrs.get("data-list") in ("bullet", "ordered", "checked", "unchecked"):
                safe["data-list"] = attrs["data-list"]
            elif tag == "span":
                if attrs.get("class") in ("mention user", "mention task") and str(attrs.get("data-id", "")).isdigit():
                    safe.update({"class": attrs["class"], "data-id": attrs["data-id"]})
                if attrs.get("data-quick-key"):
                    safe["data-quick-key"] = attrs["data-quick-key"].replace(":", "")
            styles = []
            for style in attrs.get("style", "").split(";"):
                key, sep, value = style.partition(":")
                if sep and key.strip().lower() in STYLES and not re.search(r"url|expression|[<>\\]", value, re.I):
                    styles.append(key.strip().lower() + ":" + value.strip())
            if styles:
                safe["style"] = ";".join(styles)
            self.output.append("<" + tag + "".join(f' {key}="{escape(str(value), quote=True)}"' for key, value in safe.items()) + ">")
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.suppressed:
                self.suppressed -= 1
                return
            if self.stack:
                opening = self.stack.pop()
                if opening:
                    self.output.append("</" + opening + ">")

        def handle_data(self, data):
            if not self.suppressed:
                self.output.append(escape(data))

    parser = Cleaner()
    parser.feed(text)
    parser.close()
    return re.sub(r"^(?:<p></p>)+|(?:<p></p>)+$", "", "".join(parser.output))
