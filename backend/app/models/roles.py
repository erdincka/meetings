import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RoleAgent(Base, TimestampMixin):
    __tablename__ = "role_agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    seniority: Mapped[str | None] = mapped_column(String(50))
    summary: Mapped[str | None] = mapped_column(Text)

    responsibilities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    kpis: Mapped[list[Any]] = mapped_column(JSON, default=list)
    priorities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    objectives: Mapped[list[Any]] = mapped_column(JSON, default=list)

    risk_tolerance: Mapped[str | None] = mapped_column(String(50))
    tone: Mapped[list[Any]] = mapped_column(JSON, default=list)
    collaboration_style: Mapped[str | None] = mapped_column(String(100))
    challenge_style: Mapped[str | None] = mapped_column(String(100))

    allowed_shared_library_access: Mapped[bool] = mapped_column(Boolean, default=True)

    system_prompt: Mapped[str | None] = mapped_column(Text)

    # The tool grant. Resolved to a capability profile that is enforced by
    # Kubernetes objects rather than by prompt text -- which makes editing this
    # field a privilege change, not a preference.
    default_tools: Mapped[list[Any]] = mapped_column(JSON, default=list)

    ui_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
