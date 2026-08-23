import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'company', 'agent' or 'meeting'
    library_scope: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("role_agents.id"), nullable=True, index=True
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id"), nullable=True, index=True
    )
    file_type: Mapped[str | None] = mapped_column(String(50))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Page number "4" or a section marker like "§Incident Response".
    page_number: Mapped[str | None] = mapped_column(String(50))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text)

    # Width follows the configured embedding model rather than a literal.
    embedding: Mapped[Any | None] = mapped_column(Vector(settings.EMBEDDING_DIM))

    document: Mapped["Document"] = relationship(back_populates="chunks")
