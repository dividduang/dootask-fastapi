"""Create private, server-local Docker settings once; never rotate existing secrets."""
import base64
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.umask(0o077)
    target = ROOT / ".env"
    runtime = ROOT / ".runtime/docker"
    config = runtime / "redis.conf"
    if target.exists() or config.exists():
        raise SystemExit("Refusing existing .env or redis.conf; preserve deployment secrets")
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    redis_password = secrets.token_hex(32)
    values = {
        "DB_PASSWORD": secrets.token_hex(32),
        "MARIADB_ROOT_PASSWORD": secrets.token_hex(32),
        "REDIS_PASSWORD": redis_password,
        "CSRF_SECRET": secrets.token_hex(32),
        "PGP_STORAGE_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "APP_PORT": "18080",
        "APP_IMAGE_TAG": "local",
    }
    with target.open("x", encoding="utf-8") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in values.items()))
    with config.open("x", encoding="utf-8") as handle:
        handle.write(f"bind 0.0.0.0\nprotected-mode yes\nport 6379\ndir /data\nappendonly yes\nrequirepass {redis_password}\n")
    # The Redis image drops privileges. Parent directories and .env stay private.
    config.chmod(0o644)
    print("Created private .env and .runtime/docker/redis.conf; no secrets printed")


if __name__ == "__main__":
    main()
