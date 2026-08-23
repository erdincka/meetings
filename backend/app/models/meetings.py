import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MeetingTemplate(Base, TimestampMixin):
    __tablename__ = "meeting_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str | None] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    expectations: Mapped[str | None] = mapped_column(Text)
    agenda: Mapped[str | None] = mapped_column(Text)
    default_selected_attendee_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    default_document_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class Meeting(Base, TimestampMixin):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # draft, queued, running, stopping, completed, terminated, failed
    status: Mapped[str] = mapped_column(String(50), default="draft")
    brief: Mapped[str | None] = mapped_column(Text)
    agenda: Mapped[str | None] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    expectations: Mapped[str | None] = mapped_column(Text)
    selected_attendee_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    turn_limit: Mapped[int] = mapped_column(Integer, default=50)
    current_turn: Mapped[int] = mapped_column(Integer, default=0)

    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meeting_templates.id"), nullable=True
    )

    meeting_log: Mapped[list[Any]] = mapped_column(JSON, default=list)
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    final_summary: Mapped[str | None] = mapped_column(Text)
    # UUID string or the literal 'supervisor'.
    active_agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    terminated: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_brief_docs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
