"""Artifacts and action items produced during a meeting.

Both live in Postgres rather than on a shared volume. The only StorageClass on
the target cluster is RWO, so a shared RWX artifact volume is not available --
but the API hop is the better design anyway: it gives every write an owner, a
meeting scope and an audit trail, and it keeps sandboxes without any filesystem
they could use to reach each other.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, _utcnow


class Artifact(Base, TimestampMixin):
    """Something an agent produced: a chart, a table, or a drafted document."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    # The persona that produced it, taken from the verified sandbox identity --
    # never from the request body.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # chart | table | document
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="text/markdown")

    # Text for documents and tables; base64 for binary output such as PNG charts.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActionItem(Base, TimestampMixin):
    """A commitment made during the meeting."""

    __tablename__ = "action_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    raised_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    due: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
