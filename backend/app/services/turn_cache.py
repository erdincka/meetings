"""Durable turn idempotency.

LangGraph replays the last uncompleted node after a crash-resume. Without this,
that replay re-invokes the model and re-runs every tool the turn performed --
duplicating whatever those tools produced.

The persona sandbox keeps an in-memory cache of the same mapping, but it dies
with the sandbox. This is the guarantee that survives both a sandbox and a
backend restart.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core import database
from app.models.turn_results import TurnResult as TurnResultRow

logger = structlog.get_logger(__name__)


def _rehydrate(stored: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a state update from its stored form.

    Messages are persisted as plain text (LangChain objects are not JSON) and
    reconstructed here with the metadata the router depends on.
    """
    from app.orchestration.state import make_utterance

    utterances = stored.pop("_utterances", [])
    stored["messages"] = [make_utterance(u["content"], u.get("agent_id", "")) for u in utterances]
    return stored


async def lookup(turn_key: str) -> dict[str, Any] | None:
    """Return a previously completed turn, if one exists."""
    maker = database.async_session_maker
    if maker is None:
        return None
    try:
        async with maker() as session:
            row = await session.scalar(
                select(TurnResultRow).where(TurnResultRow.turn_key == turn_key)
            )
            if row is None:
                return None
            logger.info("turn_replayed_from_store", turn_key=turn_key)
            return _rehydrate(dict(row.result_json))
    except Exception as exc:
        # A cache miss is always safe; a failed lookup must not fail the turn.
        logger.warning("turn_lookup_failed", turn_key=turn_key, error=str(exc))
        return None


async def record(turn_key: str, meeting_id: str, agent_id: str, payload: dict[str, Any]) -> None:
    """Persist a completed turn. Idempotent, so a concurrent replay is harmless."""
    maker = database.async_session_maker
    if maker is None:
        return
    try:
        async with maker() as session:
            await session.execute(
                insert(TurnResultRow)
                .values(
                    turn_key=turn_key,
                    meeting_id=meeting_id,
                    agent_id=agent_id,
                    result_json=payload,
                )
                .on_conflict_do_nothing(index_elements=["turn_key"])
            )
            await session.commit()
    except Exception as exc:
        logger.warning("turn_record_failed", turn_key=turn_key, error=str(exc))
