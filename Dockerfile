# Single-container deploy: API + worker in one image, supervised
# by deploy/run_all.py — the container twin of the deploy/*.service units.
# Multi-stage: the builder's uv binary and download cache never reach the
# final image, so the registry push stays small.

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: rebuilt only when uv.lock / pyproject.toml change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code plus deployment artifacts: the run_all.py supervisor and the
# Alembic tree (migrations run at container start).
COPY src src
COPY alembic alembic
COPY alembic.ini README.md ./
COPY deploy deploy
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim

WORKDIR /app
COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH"

# Container defaults: the API binds 0.0.0.0:80 to be reachable through
# the platform's port mapping, and bot tokens belong on a mounted volume
# (a `docker run -e` / platform env var overrides either, same priority
# as against the env-file).
ENV GRAMMQ_API_HOST=0.0.0.0 \
    GRAMMQ_API_PORT=80 \
    GRAMMQ_BOTS_DIR=/data/bots

# Runs as root: binding port 80 (<1024) needs CAP_NET_BIND_SERVICE, and
# file capabilities (setcap) do not survive every builder's layer
# snapshots — the non-root user of the systemd deploy has no reliable
# container twin here; the container boundary provides the isolation.
RUN mkdir -p /data/bots
VOLUME /data/bots

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("GRAMMQ_API_PORT", "80"), timeout=3)'

# `alembic upgrade head` is idempotent; the supervisor then runs both
# services and stops the survivor when either one dies (run_all.py).
CMD ["sh", "-c", "alembic upgrade head && exec python deploy/run_all.py"]
