"""Durable turn idempotency.

LangGraph replays the last uncompleted node after a crash-resume. Without a
guard that re-invokes the model and re-runs every tool the turn performed --
duplicating a chart, a draft, or an action item.

The persona sandbox keeps an in-memory cache of the same thing, but that is an
optimisation: it dies with the sandbox. This table is the actual guarantee,
because it survives both a sandbox and a backend restart.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _utcnow


class TurnResult(Base):
    __tablename__ = "turn_results"

    # "<meeting_id>:<turn_no>:<agent_id>" -- deterministic, so a replayed node
    # computes the same key and hits this row.
    turn_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    meeting_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
