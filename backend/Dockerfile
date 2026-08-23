# Single multi-stage Dockerfile with a `dev` and a `runtime` target, replacing
# the previous Dockerfile / Dockerfile.prod pair that had already drifted
# (different Python versions, different users, different CMD forms).
#
# Build dev:      docker build --target dev .
# Build runtime:  docker build --target runtime .

ARG PYTHON_VERSION=3.13

# ---------- builder ----------
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app/ app/
COPY scripts/ scripts/
COPY alembic/ alembic/
COPY alembic.ini ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- dev ----------
# Sources are bind-mounted by Tilt; --reload picks up the sync.
FROM builder AS dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# UID/GID 1001 matches the chart's runAsUser/runAsGroup. The previous image
# used `useradd -r` with a system-allocated UID, which did not match the
# fsGroup the chart set, so ownership depended on whatever the base image
# happened to allocate.
RUN groupadd --gid 1001 app && useradd --uid 1001 --gid 1001 --create-home app

WORKDIR /app
COPY --from=builder --chown=1001:1001 /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
