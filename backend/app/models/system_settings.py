"""Operator-tunable settings, one row, id=1.

``SystemSettingsResponse`` has always declared ``id``/``created_at``/
``updated_at`` as though a table existed behind it; the route then synthesised
them with ``datetime.now()`` on every request. This makes that real.

Credentials deliberately do NOT live here -- they come from the environment
(see ``app.core.config``). This table holds only values an operator may safely
change at runtime through the UI.
"""

from sqlalchemy import Boolean, Column, Float, Integer, String, Text

from app.models.base import Base, TimestampMixin

SETTINGS_ROW_ID = 1


class SystemSettings(Base, TimestampMixin):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, default=SETTINGS_ROW_ID)

    debug = Column(Boolean, nullable=False, default=False)
    retrieval_limits_per_agent = Column(Integer, nullable=False, default=2)
    max_evidence_per_message = Column(Integer, nullable=False, default=5)
    default_turn_limit = Column(Integer, nullable=False, default=50)
    cleanup_rules = Column(String(64), nullable=False, default="terminate_keeps_history")

    # Agent sampling temperature. agents.py hardcoded 0.7 while this field sat
    # unread in the UI; supervisor.py hardcoded 0.1. Both now read from here.
    inference_temperature = Column(Float, nullable=False, default=0.7)
    supervisor_temperature = Column(Float, nullable=False, default=0.1)

    supervisor_prompt = Column(Text, nullable=True)
    agent_prompt = Column(Text, nullable=True)
