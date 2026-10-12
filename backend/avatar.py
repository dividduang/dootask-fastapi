import colorsys
import hashlib
from io import BytesIO
import re
from PIL import Image, ImageColor, ImageDraw, ImageFont
from fastapi import HTTPException, Request
from fastapi.responses import Response

_FILTER_WORDS = "测试 测试号 测试账号 内测 体验 试用 test testing beta 账号 帐号 账户 帐户 account acc id uid 临时 暂用 备用 主号 副号 小号 大号 temp temporary backup 系统 管理员 admin administrator system sys root 用户 user 会员 member vip svip mvip premium 官方 正式 认证 official verified auth 客服 售后 服务 service support helper assistant game gaming player gamer ins instagram fb facebook tiktok tweet weibo wechat official real fake copy channel studio team group dev developer designer artist writer editor bot robot auto anonymous guest default new old".split()
_BRACKETS = re.compile(r"[(（\[【{［<＜『「](.*?)[)）\]】}］>＞』」]")
_SYMBOLS = "-_=+/\\|~@#$%^&* 　\t\n\r。，、；：？！．…‥′″℃.,;:?!\"'‘’“”`★☆○●◎◇◆□■△▲▽▼♀♂♪♫♯♭♬→←↑↓↖↗↙↘√×÷±∵∴♠♥♣♦"


def avatar_name(name):
    name = _BRACKETS.sub("", name) or _BRACKETS.sub(r"\1", name)
    name = re.sub("|".join(map(re.escape, _FILTER_WORDS)), "", name, flags=re.I) or name
    name = name.translate(str.maketrans("", "", _SYMBOLS)) or name
    if re.fullmatch(r"[\u4e00-\u9fa5]+", name):
        name = name[-2:]
    return name or "D"


def install_avatar(app, settings):
    font_path = settings.frontend_root / "resources/assets/statics/fonts/Source_Han_Sans_SC_Regular.otf"

    def avatar(request: Request, filename: str = ""):
        original = filename[:-4] if filename.lower().endswith(".png") else request.query_params.get("name", "D")
        if len(original) > 255:
            raise HTTPException(400, "Avatar name too long")
        name = avatar_name(original)
        try:
            size = int(request.query_params.get("size", "128"))
            if not 16 <= size <= 512:
                raise ValueError("size")
            foreground = ImageColor.getrgb(request.query_params.get("color") or "#ffffff")
            hue = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) / 0xffffffff
            # Stable dark color replaces the source's randomly cached dark color;
            # no operational state is invented and restart does not change avatars.
            background = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, .65, .52))
            if request.query_params.get("color") and request.query_params.get("background"):
                background = ImageColor.getrgb(request.query_params["background"])
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid avatar size/color")
        initials = "".join(word[0] for word in name.split()[:2]) if " " in name.strip() else name[:2]
        image = Image.new("RGB", (size, size), background)
        font = ImageFont.truetype(str(font_path), size=round(size / 2.9))
        draw = ImageDraw.Draw(image)
        draw.text((size / 2, size / 2), initials.upper(), font=font, fill=foreground, anchor="mm")
        stream = BytesIO()
        image.save(stream, "PNG")
        data = stream.getvalue()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        headers = {"Cache-Control": "public, max-age=1814400", "ETag": etag, "X-Content-Type-Options": "nosniff"}
        return Response(status_code=304, headers=headers) if request.headers.get("if-none-match") == etag else Response(data, media_type="image/png", headers=headers)

    app.add_api_route("/avatar", avatar, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/avatar/{filename}", avatar, methods=["GET", "HEAD"], include_in_schema=False)
