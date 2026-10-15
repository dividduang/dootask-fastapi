ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS dependencies
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY deploy/requirements-linux.lock ./requirements.lock
RUN python -m venv /venv && /venv/bin/pip install --require-hashes -r requirements.lock

FROM ${PYTHON_IMAGE}
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    FRONTEND_ROOT=/app/frontend STORAGE_ROOT=/data/storage APPSTORE_ROOT=/data/appstore
WORKDIR /app
COPY --from=dependencies /venv /venv
RUN groupadd --gid 10001 dootask && useradd --uid 10001 --gid 10001 --no-create-home dootask \
    && mkdir -p /data/storage /data/appstore && chown -R 10001:10001 /data
COPY backend ./backend
COPY tests ./tests
COPY frontend/public ./frontend/public
COPY frontend/language ./frontend/language
COPY frontend/resources/assets/statics/fonts ./frontend/resources/assets/statics/fonts
COPY frontend/package.json frontend/LICENSE ./frontend/
COPY deploy ./deploy
USER 10001:10001
EXPOSE 18080
CMD ["python", "-m", "uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "18080", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
