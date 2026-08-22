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
        ).model_dump(),
    )


app.include_router(api_router, prefix="/api/v1")
