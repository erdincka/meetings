"""Fixtures for tests that need a real database and the real ASGI app.

The unit suite proves each piece works in isolation against fakes. These tests
exist for the failures that only appear once the pieces are assembled: a router
that was written but never mounted, a dependency that guards one surface and
silently skips another, a migration that the ORM has outgrown.

They need Postgres. If one is not reachable the whole package is skipped with a
reason rather than failing, so `pytest tests` stays useful on a laptop with no
database; CI always has one, so the coverage is never quietly optional there.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]

OPERATOR_HEADERS = {"Authorization": "Bearer test-operator-token"}
VIEWER_HEADERS = {"Authorization": "Bearer test-viewer-token"}


def _database_reachable() -> bool:
    async def probe() -> bool:
        engine = create_async_engine(settings.DATABASE_URL or "", pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                await conn.execute(sqlalchemy.text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    if not settings.DATABASE_URL:
        return False
    try:
        return asyncio.run(probe())
    except Exception:
        return False


if not _database_reachable():
    pytest.skip(
        "no Postgres at DATABASE_URL; integration tests need one "
        "(CI provides it, locally: docker run --rm -p 5432:5432 "
        "-e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test "
        "pgvector/pgvector:pg17)",
        allow_module_level=True,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Bring the schema to head once for the whole session.

    Running Alembic rather than ``metadata.create_all`` is the point: it is the
    migration path a deployment actually takes, and a model that has drifted
    from its migration fails here rather than on someone's cluster.
    """
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The real application, over an in-process transport."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def db_session():
    """A session against the same database the app is using."""
    from app.core import database

    async with database.require_session_maker()() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _engine_per_loop() -> AsyncIterator[None]:
    """Return the connection pool to a clean state between tests.

    The engine is module-level and created at import, but an asyncpg connection
    belongs to the event loop that opened it. pytest gives each test its own
    loop, so a pooled connection carried over from the previous test fails with
    "attached to a different loop" -- a message that points at asyncio and not
    at the pool that is actually holding the stale handle.
    """
    yield
    from app.core import database

    if database.engine is not None:
        await database.engine.dispose()


@pytest.fixture
def unique() -> str:
    """A suffix that keeps rows from one test out of another's queries."""
    return uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _deterministic_auth(monkeypatch) -> None:
    """Pin the tokens regardless of what the developer's environment exports."""
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "test-operator-token")
    monkeypatch.setattr(settings, "VIEWER_TOKEN", "test-viewer-token")
    os.environ["OPERATOR_TOKEN"] = "test-operator-token"
