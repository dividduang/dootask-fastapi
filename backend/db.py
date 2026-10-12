from contextlib import contextmanager
import json
from sqlalchemy import MetaData, create_engine, event, select
from sqlalchemy.engine import URL

# Populate once at application startup, never per-request identity state.
tables = {}
_engine = None


def make_engine(settings):
    url = URL.create("mysql+pymysql", username=settings.db_username,
                     password=settings.db_password, host=settings.db_host,
                     port=settings.db_port, database=settings.db_database,
                     query={"charset": "utf8mb4"})
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                           isolation_level="READ COMMITTED", pool_size=10,
                           max_overflow=10, connect_args={"connect_timeout": 10})

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, _):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
    return engine


def configure(settings):
    global _engine
    _engine = make_engine(settings)
    metadata = MetaData()
    metadata.reflect(bind=_engine)
    tables.clear()
    tables.update(metadata.tables)
    for required in ("users", "settings", "user_devices", "auth_credentials", "auth_sessions", "auth_pgp_keys",
                     "auth_limits", "auth_captchas", "backend_outbox", "backend_deliveries", "backend_operations",
                     "backend_uploads", "backend_file_downloads", "account_picture_uploads", "account_identity_state"):
        if required not in tables:
            raise RuntimeError(f"Missing {required}; run python -m backend.manage init-db first")
    return _engine


@contextmanager
def transaction():
    if _engine is None:
        raise RuntimeError("Database is not configured")
    with _engine.begin() as connection:
        yield connection


def setting(connection, name, key=None, default=None):
    table = tables["settings"]
    value = connection.execute(select(table.c.setting).where(table.c.name == name)).scalar_one_or_none()
    result = json.loads(value) if value else {}
    if key is None:
        return result
    if not isinstance(result, dict):
        raise RuntimeError(f"Setting {name} is not an object")
    return result.get(key, default)


def insert_row(connection, table_name, **values):
    """Insert with source timestamps; callers own transaction, ACL and side effects."""
    from .context import utcnow
    table = tables[table_name]
    now = utcnow()
    for field in ("created_at", "updated_at"):
        if field in table.c:
            values.setdefault(field, now)
    result = connection.execute(table.insert().values(**values))
    return result.inserted_primary_key[0]
