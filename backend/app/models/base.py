"""Declarative base.

Uses SQLAlchemy 2.0 typed declarative (``DeclarativeBase`` + ``Mapped`` /
``mapped_column``). The legacy ``Column()`` style types every attribute as
``Column[T]`` rather than ``T``, so any assignment to a model attribute is a
type error and static analysis is effectively useless on the ORM layer.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# Every model lives in the application schema.
Base.metadata.schema = settings.DB_SCHEMA


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
