# Linux Docker deployment

This runs the independent FastAPI application and shipped frontend with dedicated MariaDB, Redis and persistent application data. It does not use the legacy PHP installation or its accounts. The repository's current isolated-use restriction still applies: port 18080 binds to loopback, and the first administrator must be registered before opening access to others.

## Fresh installation

Requires Docker Engine, Docker Compose and Python 3 on the host. Run from the repository root:

```sh
python3 deploy/configure.py
docker compose config --quiet
docker compose build app
docker compose up -d --wait mariadb redis
docker compose run --rm --no-deps app python -m backend.manage init-db
docker compose up -d --wait app
```

`init-db` refuses a nonempty database. Never repeat it on an existing installation. `configure.py` refuses to overwrite secrets. Preserve `.env`, `.runtime/docker/redis.conf`, and all three named volumes. Only one app worker runs, matching the application's scheduler model. The app readiness probe checks HTTP, a real initialized database table, and authenticated Redis.

From a workstation, keep this SSH tunnel running:

```sh
ssh -N -L 18080:127.0.0.1:18080 <ssh-user>@<server-host>
```

Open `http://127.0.0.1:18080/login`. On an untouched installation, use normal registration to create the first local administrator. If deployment validation already created an administrator, use the private server-side credentials supplied in the deployment handover.

## Deployment validation

Only on a fresh isolated instance, run:

```sh
docker compose exec -T app python -m deploy.smoke --fresh
```

This explicitly creates `admin@example.test`, an ordinary synthetic user, and a sample project/task/file using the real HTTP API. Generated passwords and fixture IDs are stored privately as `/data/deployment-state.json` (mode 0600) in the application volume. It refuses an existing state file or a database with users. The account creation follows the normal registration and first-administrator rules. It does not reset data.

To check login, object permissions, uploaded bytes, frontend assets and authenticated WebSocket again (including after recreating containers):

```sh
docker compose exec -T app python -m deploy.smoke
```

## Operations

```sh
cd /opt/dootask-fastapi
docker compose ps
docker compose logs --tail=100 app
docker compose restart app
docker compose stop
# Reuse existing database and secrets:
docker compose up -d --wait
```

Do not run `down -v`: that deletes persistent volumes. Back up MariaDB, application data, `.env` (including PGP_STORAGE_KEY), and Redis configuration together before upgrades. The current project only implements fresh schema initialization; schema-changing upgrades need a reviewed migration and restore plan.

Docker image tags can be overridden by `PYTHON_IMAGE`, `MARIADB_IMAGE`, `REDIS_IMAGE`, and `APP_IMAGE_TAG` in `.env`. `PIP_INDEX_URL` controls the build-time package index (default `https://pypi.org/simple`); changing mirrors does not disable the official SHA256 checks. For a reproducible installation, pin base/service images to the validated repository digests. `deploy/requirements-linux.lock` preserves all 37 Windows runtime versions while admitting the official PyPI distribution hashes needed on Linux; it does not silently upgrade dependencies. The image reuses the checked-in frontend build.

External services such as SMTP, LDAP, Office, search, AI and the app store require separate configuration and acceptance. Basic Docker readiness does not verify those integrations. See the root README for the complete current support and security boundaries.
