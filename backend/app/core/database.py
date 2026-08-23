"""Async engine and session factory.

The engine is built once from ``settings.DATABASE_URL`` at import. Previously
it could also be swapped at runtime by the setup wizard (``init_db``) because
the DB URI arrived over HTTP and was written to a config file; the URI is now
environment-supplied, so the engine is immutable for the process lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

logger = structlog.get_logger(__name__)

engine: AsyncEngine | None = None
async_session_maker: async_sessionmaker[AsyncSession] | None = None


def init_db(db_url: str) -> None:
    global engine, async_session_maker
    engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    async_session_maker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    logger.info("database_engine_initialized")


if settings.DATABASE_URL:
    init_db(settings.DATABASE_URL)


def require_session_maker() -> async_sessionmaker[AsyncSession]:
    """Session factory or a clear error.

    Several call sites used a bare ``assert`` for this, which is stripped
    entirely under ``python -O`` and would surface as an opaque ``NoneType``
    error instead of a diagnosable one.
    """
    if async_session_maker is None:
        raise RuntimeError("DATABASE_NOT_CONFIGURED: set DATABASE_URL")
    return async_session_maker


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    if async_session_maker is None:
        raise HTTPException(status_code=503, detail="DATABASE_NOT_CONFIGURED")
    async with async_session_maker() as session:
        yield session


async def check_db_ready() -> str:
    """Classify database readiness.

    Returns one of: ``ready`` (migrated and seeded), ``no_data`` (migrated,
    empty), ``no_tables`` (reachable, unmigrated), ``not_configured``,
    ``error``.
    """
    if async_session_maker is None:
        return "not_configured"
    try:
        async with async_session_maker() as session:
            # Schema-qualified deliberately. Models set
            # Base.metadata.schema, so SQLAlchemy-generated queries are
            # qualified, but this raw probe is not -- and appuser's default
            # search_path ("$user", public) does not include the app schema.
            # Unqualified, this reports "no_tables" forever against a fully
            # migrated database.
            result = await session.execute(
                text(f"SELECT count(*) FROM {settings.DB_SCHEMA}.role_agents")
            )
            return "no_data" if result.scalar() == 0 else "ready"
    except Exception as exc:
        error_str = str(exc).lower()
        if any(p in error_str for p in ("does not exist", "undefinedtableerror", "42p01")):
            logger.info("database_accessible_but_tables_missing")
            return "no_tables"
        logger.error("db_not_ready_check_failed", error=str(exc), type=type(exc).__name__)
        return "error"


def _ensure_models_registered():  # type: ignore[no-untyped-def]
    """Import every model so ``Base.metadata`` is complete for Alembic autogenerate."""
    import app.models.artifacts  # noqa: F401
    import app.models.documents  # noqa: F401
    import app.models.meetings  # noqa: F401
    import app.models.roles  # noqa: F401
    import app.models.system_settings  # noqa: F401
    import app.models.turn_results  # noqa: F401
    from app.models.base import Base

    return Base
