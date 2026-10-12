import base64
from datetime import timedelta
import hashlib
import hmac
from io import BytesIO
import secrets
from sqlalchemy import select, delete
from sqlalchemy.dialects.mysql import insert
from PIL import Image, ImageDraw, ImageFont
from . import db
from .context import utcnow
from .errors import ApiError


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768,
                         r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$32768$8$1$" + salt.hex() + "$" + key.hex()


def password_verify(password, encoded):
    try:
        kind, n, r, p, salt, expected = encoded.split("$")
        if (kind, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=32768, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def consume_limit(scope, limit, seconds):
    """Own transaction: a failed business operation must not undo abuse accounting."""
    now = utcnow()
    table = db.tables["auth_limits"]
    key = digest(scope)
    with db.transaction() as conn:
        conn.execute(insert(table).values(scope=key, attempts=0, expires_at=now + timedelta(seconds=seconds))
                     .on_duplicate_key_update(scope=key))
        row = conn.execute(select(table).where(table.c.scope == key).with_for_update()).mappings().one()
        attempts = row["attempts"] + 1 if row["expires_at"] > now else 1
        expiry = row["expires_at"] if row["expires_at"] > now else now + timedelta(seconds=seconds)
        conn.execute(table.update().where(table.c.scope == key).values(attempts=attempts, expires_at=expiry))
    if attempts > limit:
        raise ApiError("请求过于频繁，请稍后重试")


def mark_login_failure(email):
    table = db.tables["auth_limits"]
    key = digest("login-failure:" + email.casefold())
    with db.transaction() as conn:
        conn.execute(insert(table).values(scope=key, attempts=1, expires_at=utcnow() + timedelta(days=1))
                     .on_duplicate_key_update(attempts=1, expires_at=utcnow() + timedelta(days=1)))


def needs_captcha(connection, email):
    mode = db.setting(connection, "system", "login_code", "auto")
    if mode in ("open", "close"):
        return mode == "open"
    table = db.tables["auth_limits"]
    return connection.execute(select(table.c.scope).where(
        table.c.scope == digest("login-failure:" + email.casefold()),
        table.c.expires_at > utcnow())).first() is not None


def clear_login_failure(connection, email):
    table = db.tables["auth_limits"]
    connection.execute(delete(table).where(table.c.scope == digest("login-failure:" + email.casefold())))


def create_captcha(secret):
    answer = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5))
    key = secrets.token_hex(32)
    hashed = hmac.new(secret.encode(), (key + answer).encode(), hashlib.sha256).hexdigest()
    now = utcnow()
    with db.transaction() as conn:
        table = db.tables["auth_captchas"]
        conn.execute(delete(table).where(table.c.expires_at <= now))
        conn.execute(table.insert().values(captcha_key=key, answer_digest=hashed, expires_at=now + timedelta(minutes=5)))
    image = Image.new("RGB", (180, 60), "#f3f5f7")
    draw = ImageDraw.Draw(image)
    for _ in range(16):
        draw.line(tuple(secrets.randbelow(n) for n in (180, 60, 180, 60)), fill="#adb7c2", width=1)
    font = ImageFont.load_default(size=30)
    for index, character in enumerate(answer):
        draw.text((12 + index * 32, 10 + secrets.randbelow(12)), character, font=font, fill="#263e59")
    out = BytesIO()
    image.save(out, "PNG")
    return key, out.getvalue()


def verify_captcha(key, answer, secret):
    table = db.tables["auth_captchas"]
    # Consume independently even when subsequent credentials/transaction fail.
    with db.transaction() as conn:
        row = conn.execute(select(table).where(table.c.captcha_key == key).with_for_update()).mappings().first()
        conn.execute(delete(table).where(table.c.captcha_key == key))
    actual = hmac.new(secret.encode(), (key + answer.upper()).encode(), hashlib.sha256).hexdigest()
    if not row or row["expires_at"] <= utcnow() or not hmac.compare_digest(actual, row["answer_digest"]):
        raise ApiError("请输入正确的验证码", data={"code": "need"})
