# Persona sandbox runtime.
#
# One image serves every capability profile. What a given sandbox may actually
# do is decided by its SandboxTemplate -- the mounted capability file, the
# ServiceAccount, the NetworkPolicy and the Secrets it references -- not by
# which image it runs. That separation is the point.

ARG PYTHON_VERSION=3.13

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY runtime/ runtime/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
RUN groupadd --gid 1001 sandbox && useradd --uid 1001 --gid 1001 --create-home sandbox
WORKDIR /app
COPY --from=builder --chown=1001:1001 /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 1001
EXPOSE 8080
CMD ["uvicorn", "runtime.server:app", "--host", "0.0.0.0", "--port", "8080"]
