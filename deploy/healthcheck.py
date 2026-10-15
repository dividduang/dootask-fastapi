"""Container readiness: HTTP host, initialized database, and authenticated Redis."""
import os
import urllib.request

from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def main():
    with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=3) as response:
        assert response.status == 200 and response.read() == b"ok"
    engine = create_engine(URL.create(
        "mysql+pymysql", username=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]), database=os.environ["DB_DATABASE"],
    ), connect_args={"connect_timeout": 3, "read_timeout": 3})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM users LIMIT 1")).all()
    finally:
        engine.dispose()
    with Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3, socket_timeout=3) as client:
        assert client.ping()


if __name__ == "__main__":
    main()
