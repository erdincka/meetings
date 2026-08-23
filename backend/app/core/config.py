"""Process configuration, sourced entirely from the environment.

Previously this module read a plaintext ``config.json`` off a PVC -- holding
the Postgres password and both LLM API keys -- and re-parsed it from disk on
*every* attribute access, including the ``/system/status`` poll each browser
tab fires every few seconds. That file is gone.

Configuration is now split by lifetime and sensitivity:

* **Infrastructure** (this module): database URL, model endpoints and API
  keys, CORS origins. Injected as environment variables from the
  ``meetings-runtime`` Secret and ``meetings-config`` ConfigMap, read once at
  import. Never written at runtime.
* **Operator-tunable** (``app.services.settings_service``): prompts, turn
  limits, temperatures, retrieval limits. Stored in the ``system_settings``
  table and cached in-process.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "Agentic Meetings"
    VERSION: str = "0.2.0"
    DB_SCHEMA: str = "meetings"

    LOG_LEVEL: str = "INFO"

    # Explicit origins. The previous ``allow_origins=["*"]`` paired with
    # ``allow_credentials=True`` is rejected by the CORS spec anyway, so it was
    # both a hole and non-functional for credentialed requests.
    # NoDecode is required: without it pydantic-settings JSON-decodes complex
    # types straight from the environment *before* any validator runs, so a
    # plain comma-separated value from a ConfigMap raises SettingsError at
    # import and the process never starts.
    ALLOWED_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    DATABASE_URL: str | None = None

    INFERENCE_ENDPOINT: str | None = None
    INFERENCE_API_KEY: str | None = None
    INFERENCE_MODEL_NAME: str | None = None
    INFERENCE_IGNORE_TLS: bool = False

    EMBEDDING_ENDPOINT: str | None = None
    EMBEDDING_API_KEY: str | None = None
    EMBEDDING_MODEL_NAME: str | None = None
    EMBEDDING_IGNORE_TLS: bool = False

    # Per-request LLM timeout, seconds. The default suits a hosted endpoint;
    # CPU-only local inference (the Ollama profile) needs considerably more,
    # since a small model generating structured output on a few cores can take
    # minutes rather than seconds.
    LLM_TIMEOUT_SECONDS: int = 90

    # Vector width must match the embedding model. Changing it once chunks
    # exist is guarded by an Alembic migration.
    EMBEDDING_DIM: int = 2048

    # LangGraph checkpointing. The executor used to swallow any Postgres
    # checkpointer failure and silently downgrade to an in-memory saver, which
    # loses a meeting on restart while pretending to be durable. It now fails
    # loudly unless this is explicitly set (local development only).
    ALLOW_VOLATILE_CHECKPOINTS: bool = False

    # Telemetry. With no endpoint set, tracing is a no-op and metrics still
    # work -- observability must never be a reason the app fails to start.
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_SERVICE_NAME: str = "meetings-backend"

    SANDBOX_NAMESPACE: str = "meetings-sandboxes"
    SANDBOX_EXEC_NAMESPACE: str = "meetings-exec"

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string so the value can come from a ConfigMap."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _normalize_driver(cls, v: object) -> object:
        """Accept the URI forms operators actually paste and coerce to asyncpg."""
        if not isinstance(v, str) or not v:
            return v
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @property
    def inference_configured(self) -> bool:
        return bool(self.INFERENCE_ENDPOINT and self.INFERENCE_MODEL_NAME)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.EMBEDDING_ENDPOINT and self.EMBEDDING_MODEL_NAME)


settings = Settings()
