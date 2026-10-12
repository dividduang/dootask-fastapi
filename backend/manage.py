"""Explicit new-database initialization; no account/permission bypass commands."""
import argparse
import hashlib
from pathlib import Path
from sqlalchemy import inspect
from .config import Settings
from .db import make_engine


def sql_statements(source):
    """Split our checked-in SQL while preserving semicolons/escapes in quoted data."""
    quote = None
    escaped = False
    line_comment = False
    buffer = []
    index = 0
    while index < len(source):
        character = source[index]
        if line_comment:
            if character == "\n":
                line_comment = False
                buffer.append("\n")
        elif quote:
            buffer.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    buffer.append(source[index + 1])
                    index += 1
                else:
                    quote = None
        elif source[index:index + 2] == "--":
            line_comment = True
            index += 1
        elif character in ("'", '"', "`"):
            quote = character
            buffer.append(character)
        elif character == ";":
            statement = "".join(buffer).strip()
            if statement:
                yield statement
            buffer.clear()
        else:
            buffer.append(character)
        index += 1
    if quote:
        raise ValueError("Unclosed SQL quote")
    remaining = "".join(buffer).strip()
    if remaining:
        yield remaining


def init_database(settings):
    engine = make_engine(settings)
    root = Path(__file__).parent
    try:
        with engine.connect() as connection:
            if inspect(connection).get_table_names():
                raise RuntimeError("Refusing nonempty database; new-install initialization only")
            version = connection.exec_driver_sql("SELECT VERSION()").scalar_one()
            if "MariaDB" not in version:
                raise RuntimeError("This schema requires MariaDB, not SQLite or MySQL emulation")
            # MariaDB DDL auto-commits. A failed run is deliberately NOT called atomic;
            # operator must inspect/recreate only the dedicated empty database.
            for filename in ("schema.sql", "security.sql", "schema_seed.sql"):
                raw = (root / filename).read_bytes()
                for statement in sql_statements(raw.decode("utf-8")):
                    connection.exec_driver_sql(statement)
                connection.commit()
                print(f"Applied {filename} sha256={hashlib.sha256(raw).hexdigest()}")
            print("Initialized empty MariaDB schema. No accounts or license were fabricated.")
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init-db"])
    parser.parse_args()
    init_database(Settings.from_env())


if __name__ == "__main__":
    main()
