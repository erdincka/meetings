"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router
from app.core.config import settings
from app.core.database import check_db_ready
from app.core.exceptions import NexusBaseException
from app.domain.response import APIResponse

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    # Level is configurable now. It was pinned to INFO, so every logger.debug
    # call in the codebase was permanently dead.
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelNamesMapping().get(settings.LOG_LEVEL.upper(), logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


class HealthCheckFilter(logging.Filter):
    """Keep polling endpoints out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in ("/api/v1/system/status", "/health", "/readyz"))


logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

logger = structlog.get_logger(__name__)


async def _prepare_checkpointer() -> None:
    """Ensure the LangGraph checkpointer schema exists before serving traffic."""
    if not settings.DATABASE_URL:
        return
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        dsn = settings.DATABASE_URL.replace("+asyncpg", "")
        async with AsyncConnectionPool(
            dsn, max_size=2, min_size=1, kwargs={"autocommit": True}
        ) as pool:
            # Same row-factory mismatch as in meeting_executor: the saver sets
            # its own per-cursor factory, so this is safe at runtime.
            await AsyncPostgresSaver(pool).setup()  # type: ignore[arg-type]
        logger.info("checkpointer_schema_ready")
    except Exception as exc:
        # Not fatal: the executor still refuses to run a meeting it cannot
        # checkpoint, which is the guarantee that matters.
        logger.warning("checkpointer_prepare_failed", error=f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown. Replaces the deprecated @app.on_event hooks."""
    logger.info(
        "application_startup",
        app=settings.PROJECT_NAME,
        version=settings.VERSION,
        db_configured=bool(settings.DATABASE_URL),
        inference_configured=settings.inference_configured,
    )
    # Create the checkpointer schema now, not on the first meeting.
    #
    # AsyncPostgresSaver.setup() is idempotent but on a fresh database it
    # creates several tables and indexes, which can exceed the per-meeting
    # timeout. That made the *first* meeting on any new cluster fail and every
    # subsequent one succeed -- a self-healing symptom that looks like flakiness
    # and wastes an afternoon. Paying the cost once, at startup, removes it.
    await _prepare_checkpointer()

    # A backend killed mid-meeting never releases its sandboxes. Sweep any that
    # belong to meetings which are no longer running.
    try:
        from app.core.database import async_session_maker
        from app.sandbox.reaper import sweep_orphaned_sandboxes

        if async_session_maker is not None:
            async with async_session_maker() as session:
                reaped = await sweep_orphaned_sandboxes(session)
            if reaped:
                logger.info("startup_sandbox_sweep", reaped=reaped)
    except Exception as exc:  # never block startup on cleanup
        logger.warning("startup_sandbox_sweep_failed", error=str(exc))

    yield
    from app.core.database import engine

    if engine is not None:
        await engine.dispose()
    logger.info("application_shutdown")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# Explicit origins. allow_origins=["*"] with allow_credentials=True is
# rejected by the CORS spec, so the previous configuration was both a hole and
# non-functional for credentialed requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(NexusBaseException)
async def nexus_exception_handler(request: Request, exc: NexusBaseException) -> JSONResponse:
    logger.error("domain_exception_raised", error_code=exc.code, error_message=exc.message)
    return JSONResponse(
        status_code=400,
        content=APIResponse(
            status="error", message=exc.message, meta={"code": exc.code}
        ).model_dump(),
    )


@app.get("/health")
async def health_check() -> APIResponse:
    """Liveness: the process is up. Deliberately does no I/O."""
    return APIResponse(status="success", message="OK")


@app.get("/readyz")
async def readiness_check() -> JSONResponse:
    """Readiness: the process can actually serve traffic.

    Distinct from /health, which previously did double duty. A backend that
    cannot reach its database should leave the load-balancer rotation without
    being restarted.
    """
    db_status = await check_db_ready()
    ready = db_status in ("ready", "no_data")
    return JSONResponse(
        status_code=200 if ready else 503,
        content=APIResponse(
            status="success" if ready else "error",
            message=f"database: {db_status}",
            data={"database": db_status},
        ).model_dump(mode="json"),
    )


app.include_router(api_router, prefix="/api/v1")
