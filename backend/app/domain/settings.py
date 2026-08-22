"""Settings schemas.

The split matters: ``SystemSettingsTunables`` is what an operator may change
at runtime; credentials are env-only and appear on ``RuntimeSettings`` (the
merged internal view) but are never accepted on an update payload.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SystemSettingsTunables(BaseModel):
    """Operator-tunable values, persisted in the ``system_settings`` table."""

    debug: bool = False
    retrieval_limits_per_agent: int = Field(default=2, ge=1, le=20)
    max_evidence_per_message: int = Field(default=5, ge=1, le=50)
    default_turn_limit: int = Field(default=50, ge=1, le=500)
    cleanup_rules: str = "terminate_keeps_history"

    inference_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    supervisor_temperature: float = Field(default=0.1, ge=0.0, le=2.0)

    supervisor_prompt: str | None = None
    agent_prompt: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SystemSettingsUpdate(BaseModel):
    """Partial update. Credential fields are deliberately absent."""

    debug: bool | None = None
    retrieval_limits_per_agent: int | None = Field(default=None, ge=1, le=20)
    max_evidence_per_message: int | None = Field(default=None, ge=1, le=50)
    default_turn_limit: int | None = Field(default=None, ge=1, le=500)
    cleanup_rules: str | None = None

    inference_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    supervisor_temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    supervisor_prompt: str | None = None
    agent_prompt: str | None = None

    model_config = ConfigDict(extra="forbid")


class RuntimeSettings(SystemSettingsTunables):
    """Merged view consumed by orchestration code.

    Field names match what ``agents.py`` and ``supervisor.py`` already read.
    """

    inference_endpoint: str | None = None
    inference_api_key: str | None = None
    inference_model_name: str | None = None
    inference_ignore_tls: bool = False

    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_model_name: str | None = None
    embedding_ignore_tls: bool = False


class SystemSettingsResponse(SystemSettingsTunables):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SettingsDiscoveryRequest(BaseModel):
    endpoint: str
    api_key: str | None = None
    ignore_tls: bool = False
