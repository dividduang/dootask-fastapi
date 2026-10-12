import calendar
from datetime import timedelta
import json
import re
import secrets
from urllib.parse import parse_qsl
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pgpy import PGPKey, PGPUID, PGPMessage
from pgpy.constants import PubKeyAlgorithm, KeyFlags, HashAlgorithm, SymmetricKeyAlgorithm, CompressionAlgorithm
from sqlalchemy import select, delete
from sqlalchemy.dialects.mysql import insert
from . import db
from .context import utcnow
from .errors import ApiError, json_default


def parse_header(value):
    if len(value) > 24576:
        raise ApiError("加密参数过长")
    fields = {key.strip().lower(): val.strip() for key, val in parse_qsl(value.replace(";", "&"), keep_blank_values=True)}
    for key in ("encrypt_type", "client_type"):
        if fields.get(key) not in (None, "", "pgp"):
            raise ApiError("不支持的加密类型")
    if fields.get("client_type") == "pgp":
        packed = fields.get("client_key", "")
        if not packed:
            raise ApiError("缺少客户端公钥")
        fields["client_key"] = public_key_format(packed)
    return fields


def public_key_format(value):
    if "-----BEGIN PGP PUBLIC KEY BLOCK-----" in value:
        return value
    value = value.replace("-", "+").replace("_", "/").replace("$", "\n")
    return "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n" + value + "\n-----END PGP PUBLIC KEY BLOCK-----"


def key_exchange(connection, client_id, userid, email, storage_key):
    if client_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id):
        raise ApiError("客户端ID不正确")
    key_id = (client_id or secrets.token_hex(6)) + str(userid)
    table = db.tables["auth_pgp_keys"]
    now = utcnow()
    row = connection.execute(select(table).where(table.c.key_id == key_id)).mappings().first()
    if row and row["expires_at"] > now:
        return {"type": "pgp", "id": key_id, "key": row["public_key"]}
    connection.execute(delete(table).where(table.c.key_id == key_id, table.c.expires_at <= now))
    key = PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 3072)
    key.add_uid(PGPUID.new("DooTask", email=email or "aa@bb.cc"),
                usage={KeyFlags.Sign, KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
                hashes=[HashAlgorithm.SHA256], ciphers=[SymmetricKeyAlgorithm.AES256],
                compression=[CompressionAlgorithm.Uncompressed])
    nonce = secrets.token_bytes(12)
    encrypted = nonce + AESGCM(storage_key).encrypt(nonce, str(key).encode(), key_id.encode())
    month_index = now.year * 12 + now.month - 1 + 3
    year, month = divmod(month_index, 12)
    month += 1
    expiry = now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))
    connection.execute(insert(table).values(key_id=key_id, public_key=str(key.pubkey), private_cipher=encrypted,
                                           expires_at=expiry).on_duplicate_key_update(key_id=key_id))
    row = connection.execute(select(table).where(table.c.key_id == key_id)).mappings().one()
    return {"type": "pgp", "id": key_id, "key": row["public_key"]}


def decrypt_request(connection, encrypted, key_id, storage_key):
    table = db.tables["auth_pgp_keys"]
    row = connection.execute(select(table).where(table.c.key_id == key_id, table.c.expires_at > utcnow())).mappings().first()
    if row is None:
        raise ApiError("加密密钥已过期，请刷新页面重试")
    try:
        cipher = row["private_cipher"]
        private = AESGCM(storage_key).decrypt(cipher[:12], cipher[12:], key_id.encode())
        key, _ = PGPKey.from_blob(private)
        message = PGPMessage.from_blob("-----BEGIN PGP MESSAGE-----\n\n" + encrypted + "\n-----END PGP MESSAGE-----")
        clear = key.decrypt(message).message
        if isinstance(clear, (bytes, bytearray)):
            clear = bytes(clear).decode("utf-8")
        data = json.loads(clear)
        if not isinstance(data, dict):
            raise ValueError("Request must be an object")
        return data
    except Exception as exc:
        raise ApiError("加密请求无法验证或解密") from exc


def encrypt_response(payload, public_key):
    try:
        key, _ = PGPKey.from_blob(public_key)
        if not key.is_public:
            raise ValueError("Expected public key")
        message = PGPMessage.new(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=json_default),
                                 compression=CompressionAlgorithm.Uncompressed)
        encrypted = str(key.encrypt(message, cipher=SymmetricKeyAlgorithm.AES256))
        body = re.sub(r"\s*-----(?:BEGIN|END) PGP MESSAGE-----\s*", "", encrypted).strip()
        return {"encrypted": body}
    except Exception as exc:
        raise ApiError("客户端公钥无效") from exc
