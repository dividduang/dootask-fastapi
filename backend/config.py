from dataclasses import dataclass
import base64
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_database: str
    db_username: str
    db_password: str
    csrf_secret: str
    pgp_storage_key: bytes
    frontend_root: Path = ROOT / "frontend"
    storage_root: Path = ROOT / ".runtime" / "storage"
    system_alias: str = "DooTask"
    login_logo: str = ""
    debug: bool = False
    version: str = "1.9.66"
    locale: str = "zh"
    redis_url: str = "redis://127.0.0.1:16379/0"
    source_url: str = ""
    appstore_root: Path = ROOT / ".runtime" / "appstore"
    appstore_url: str = "http://appstore"
    app_key: str = ""
    ai_url: str = "http://nginx/ai"

    @classmethod
    def from_env(cls):
        secret = os.environ.get("CSRF_SECRET", "")
        if len(secret) < 32:
            raise ValueError("CSRF_SECRET must contain at least 32 random characters")
        try:
            key = base64.b64decode(os.environ.get("PGP_STORAGE_KEY", ""), validate=True)
        except ValueError as exc:
            raise ValueError("PGP_STORAGE_KEY must be base64 encoded 32 random bytes") from exc
        if len(key) != 32:
            raise ValueError("PGP_STORAGE_KEY must be base64 encoded 32 random bytes")
        password = os.environ.get("DB_PASSWORD", "")
        if not password:
            raise ValueError("DB_PASSWORD is required")
        return cls(
            db_host=os.environ.get("DB_HOST", "127.0.0.1"),
            db_port=int(os.environ.get("DB_PORT", "13306")),
            db_database=os.environ.get("DB_DATABASE", "dootask_fastapi"),
            db_username=os.environ.get("DB_USERNAME", "dootask_app"),
            db_password=password, csrf_secret=secret, pgp_storage_key=key,
            frontend_root=Path(os.environ.get("FRONTEND_ROOT", ROOT / "frontend")).resolve(),
            storage_root=Path(os.environ.get("STORAGE_ROOT", ROOT / ".runtime" / "storage")).resolve(),
            system_alias=os.environ.get("SYSTEM_ALIAS", "DooTask"),
            login_logo=os.environ.get("LOGIN_LOGO", ""),
            debug=os.environ.get("APP_DEBUG", "no") == "yes",
            version=os.environ.get("APP_VERSION", "1.9.66"),
            locale=os.environ.get("APP_LOCALE", "zh"),
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:16379/0"),
            source_url=os.environ.get("CORRESPONDING_SOURCE_URL", ""),
            appstore_root=Path(os.environ.get("APPSTORE_ROOT", ROOT / ".runtime" / "appstore")).resolve(),
            appstore_url=os.environ.get("APPSTORE_URL", "http://appstore"),
            app_key=os.environ.get("APP_KEY", ""),
            ai_url=os.environ.get("AI_URL", "http://nginx/ai"),
        )
