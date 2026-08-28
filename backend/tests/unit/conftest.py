"""Unit-suite isolation.

A unit test must produce the same result whether or not a database happens to
be listening on ``DATABASE_URL``. Two modules here reach through to Postgres by
design -- the turn cache in particular -- and while it is unreachable they fail
soft and every test passes. Run the same suite on a developer machine with a
database up, or in CI once the migrations have been applied, and turns start
being served from a real table: results leak between tests and the failures look
like logic bugs in code nobody touched.

So the durable turn cache is stubbed out here, per test, with an in-memory
mapping. Tests that are *about* idempotency install their own stub over this
one and keep exercising the behaviour on purpose.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def isolated_turn_cache(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """Replace the Postgres-backed turn cache with a per-test dictionary."""
    from app.services import turn_cache

    store: dict[str, dict[str, Any]] = {}

    async def lookup(turn_key: str) -> dict[str, Any] | None:
        return store.get(turn_key)

    async def record(
        turn_key: str, meeting_id: str, agent_id: str, payload: dict[str, Any]
    ) -> None:
        store[turn_key] = dict(payload)

    monkeypatch.setattr(turn_cache, "lookup", lookup)
    monkeypatch.setattr(turn_cache, "record", record)
    return store
