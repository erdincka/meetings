"""Runtime settings: env-sourced credentials merged with DB-stored tunables.

The rest of the application consumes a single ``RuntimeSettings`` object and
does not care which half a field came from. Field names match what
``agents.py`` and ``supervisor.py`` already expect, so orchestration code needs
no changes to read configuration.

The DB half is cached in-process behind a version counter: reads are free,
and a settings write bumps the version so the next read refreshes. This
replaces a synchronous ``open()`` + ``json.load()`` that ran on every single
attribute access.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.network import normalize_v1_endpoint
from app.domain.settings import RuntimeSettings, SystemSettingsTunables
from app.models.system_settings import SETTINGS_ROW_ID, SystemSettings

logger = structlog.get_logger(__name__)

_cache: SystemSettingsTunables | None = None
_version = 0


def invalidate_cache() -> None:
    """Drop the cached tunables so the next read refetches."""
    global _cache, _version
    _cache = None
    _version += 1


def cache_version() -> int:
    return _version


async def _load_tunables(session: AsyncSession) -> SystemSettingsTunables:
    """Read the singleton settings row, creating it with defaults if absent."""
    global _cache
    if _cache is not None:
        return _cache

    row = await session.get(SystemSettings, SETTINGS_ROW_ID)
    if row is None:
        row = SystemSettings(id=SETTINGS_ROW_ID)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info("system_settings_row_created")

    _cache = SystemSettingsTunables.model_validate(row, from_attributes=True)
    return _cache


async def get_runtime_settings(session: AsyncSession) -> RuntimeSettings:
    """The merged view: credentials from env, tunables from the database."""
    tunables = await _load_tunables(session)
    return RuntimeSettings(
        **tunables.model_dump(),
        inference_endpoint=(
            normalize_v1_endpoint(settings.INFERENCE_ENDPOINT)
            if settings.INFERENCE_ENDPOINT
            else None
        ),
        inference_api_key=settings.INFERENCE_API_KEY,
        inference_model_name=settings.INFERENCE_MODEL_NAME,
        inference_ignore_tls=settings.INFERENCE_IGNORE_TLS,
        embedding_endpoint=(
            normalize_v1_endpoint(settings.EMBEDDING_ENDPOINT)
            if settings.EMBEDDING_ENDPOINT
            else None
        ),
        embedding_api_key=settings.EMBEDDING_API_KEY,
        embedding_model_name=settings.EMBEDDING_MODEL_NAME,
        embedding_ignore_tls=settings.EMBEDDING_IGNORE_TLS,
    )


async def update_tunables(session: AsyncSession, changes: dict[str, object]) -> SystemSettings:
    """Apply a partial update to the settings row and invalidate the cache."""
    row = await session.get(SystemSettings, SETTINGS_ROW_ID)
    if row is None:
        row = SystemSettings(id=SETTINGS_ROW_ID)
        session.add(row)

    rejected = [k for k in changes if not hasattr(SystemSettings, k)]
    if rejected:
        # Credentials are the likely culprit: they are env-only by design and
        # must not be silently accepted and dropped.
        raise ValueError(f"Not settable at runtime: {', '.join(sorted(rejected))}")

    for key, value in changes.items():
        setattr(row, key, value)

    await session.commit()
    await session.refresh(row)
    invalidate_cache()
    logger.info("system_settings_updated", fields=sorted(changes))
    return row


async def get_settings_row(session: AsyncSession) -> SystemSettings:
    """The ORM row itself, for responses that need id/created_at/updated_at."""
    row = await session.get(SystemSettings, SETTINGS_ROW_ID)
    if row is None:
        await _load_tunables(session)
        row = await session.get(SystemSettings, SETTINGS_ROW_ID)
    assert row is not None
    return row
