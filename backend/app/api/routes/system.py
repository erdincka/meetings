"""System status and one-time setup.

The wizard validates what the operator configured, runs migrations, and
seeds. It deliberately does *not* receive credentials over HTTP: the database
URI and both API keys are environment-supplied from the ``meetings-runtime``
Secret, so there is nothing here for a misdirected request to write.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import check_db_ready, get_db_session
from app.core.network import normalize_v1_endpoint
from app.domain.response import APIResponse

logger = structlog.get_logger(__name__)
router = APIRouter()

# Setup progress lives in memory rather than in a config file. It is
# per-process and intentionally ephemeral: the durable signal is the database
# itself, which check_db_ready() reports on.
_last_operation: dict[str, Any] | None = None


async def _verify_endpoint(
    endpoint: str | None,
    model_name: str | None,
    api_key: str | None,
    ignore_tls: bool,
) -> tuple[bool, str]:
    """Confirm an OpenAI-compatible endpoint is reachable and serves the model."""
    if not endpoint or not model_name:
        return False, "Not configured"

    url = f"{normalize_v1_endpoint(endpoint)}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(verify=not ignore_tls, timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"

            data = resp.json()
            if isinstance(data, dict):
                models = data.get("data") or data.get("models") or []
            else:
                models = data

            names = [str(m if isinstance(m, str) else m.get("id", "")) for m in models]
            if _model_is_served(model_name, names):
                return True, "Verified"
            preview = ", ".join(names[:3])
            return False, f"Model '{model_name}' not served. Available: {preview}"
    except Exception as exc:
        logger.warning("endpoint_verification_failed", error=str(exc), url=url)
        return False, f"Connection failed: {str(exc)[:60]}"


def _model_is_served(wanted: str, available: list[str]) -> bool:
    """Match a model name against what an endpoint advertises.

    Registries differ on tags: Ollama reports "nomic-embed-text:latest" for what
    the user configured as "nomic-embed-text", and an exact comparison rejects a
    model that is plainly present. Compare on the untagged name when either side
    omits a tag.
    """
    if wanted in available:
        return True

    def base(name: str) -> str:
        return name.split(":", 1)[0]

    wanted_has_tag = ":" in wanted
    for name in available:
        if wanted_has_tag or ":" in name:
            if base(name) == base(wanted) and not (wanted_has_tag and ":" in name):
                return True
        if base(name) == base(wanted):
            return True
    return False


@router.get("/status", response_model=APIResponse)
async def system_status() -> APIResponse:
    """Configuration and readiness.

    This is polled continuously by the frontend setup guard, so it must stay
    cheap. It previously re-read and re-parsed a JSON file from disk on every
    call; it now touches only in-memory config plus one DB count.
    """
    reasons: list[str] = []

    db_configured = False
    if settings.DATABASE_URL:
        db_status = await check_db_ready()
        if db_status == "ready":
            db_configured = True
        else:
            reasons.append(f"Database: {db_status}")
    else:
        reasons.append("DATABASE_URL is not set")

    inference_configured = settings.inference_configured
    inference_verified, inference_status = (False, "Not configured")
    if inference_configured:
        inference_verified, inference_status = await _verify_endpoint(
            settings.INFERENCE_ENDPOINT,
            settings.INFERENCE_MODEL_NAME,
            settings.INFERENCE_API_KEY,
            settings.INFERENCE_IGNORE_TLS,
        )
        if not inference_verified:
            reasons.append(f"Inference: {inference_status}")
    else:
        reasons.append("INFERENCE_ENDPOINT / INFERENCE_MODEL_NAME are not set")

    embedding_configured = settings.embedding_configured
    embedding_verified, embedding_status = (False, "Not configured")
    if embedding_configured:
        embedding_verified, embedding_status = await _verify_endpoint(
            settings.EMBEDDING_ENDPOINT,
            settings.EMBEDDING_MODEL_NAME,
            settings.EMBEDDING_API_KEY,
            settings.EMBEDDING_IGNORE_TLS,
        )
        if not embedding_verified:
            reasons.append(f"Embedding: {embedding_status}")
    else:
        reasons.append("EMBEDDING_ENDPOINT / EMBEDDING_MODEL_NAME are not set")

    configured = bool(settings.DATABASE_URL) and inference_configured and embedding_configured

    return APIResponse(
        status="success",
        data={
            "db_configured": db_configured,
            "inference_configured": inference_configured,
            "inference_verified": inference_verified,
            "inference_status": inference_status,
            "embedding_configured": embedding_configured,
            "embedding_verified": embedding_verified,
            "embedding_status": embedding_status,
            "configured": configured,
            "ready": configured and db_configured and inference_verified and embedding_verified,
            "reasons": reasons,
            "last_op": _last_operation,
            # Credentials are operator-managed now, so the UI needs to tell the
            # user how to supply them rather than offering an input box.
            "config_source": "environment",
            "remediation": _remediation(reasons),
        },
    )


def _remediation(reasons: list[str]) -> str | None:
    """The exact command an operator needs when required config is missing."""
    if not reasons:
        return None
    return (
        "kubectl -n meetings create secret generic meetings-runtime "
        "--from-literal=DATABASE_URL=... "
        "--from-literal=INFERENCE_ENDPOINT=... "
        "--from-literal=INFERENCE_API_KEY=... "
        "--from-literal=INFERENCE_MODEL_NAME=... "
        "--from-literal=EMBEDDING_ENDPOINT=... "
        "--from-literal=EMBEDDING_API_KEY=... "
        "--from-literal=EMBEDDING_MODEL_NAME=... "
        "--dry-run=client -o yaml | kubectl apply -f -"
    )


@router.post("/setup", response_model=APIResponse)
async def run_setup(
    background_tasks: BackgroundTasks,
    reseed: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    """Run migrations and seed reference data.

    Schema changes are applied by Alembic, not ``Base.metadata.create_all``,
    so upgrades are reviewable and repeatable.
    """
    global _last_operation

    if not settings.DATABASE_URL:
        return APIResponse(
            status="error",
            message="DATABASE_URL is not set. Configure the meetings-runtime Secret.",
        )

    _last_operation = {"status": "pending", "type": "reseed" if reseed else "setup"}
    background_tasks.add_task(_run_setup_sequence, reseed)
    return APIResponse(
        status="success", message="Setup started.", data={"last_op": _last_operation}
    )


async def _run_setup_sequence(reseed: bool) -> None:
    global _last_operation
    op_type = "reseed" if reseed else "setup"
    try:
        await _run_migrations()

        # Seeding is awaited in-process. It used to be
        # subprocess.run(["python", "-m", "scripts.seed"], check=True) inside
        # an async BackgroundTask, which blocked the event loop for the entire
        # seed -- embedding network calls included -- and started a second
        # process with its own database engine.
        from scripts.seed import seed_data

        await seed_data()

        _last_operation = {
            "status": "success",
            "type": op_type,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        logger.info("setup_sequence_complete", type=op_type)
    except Exception as exc:
        logger.error("setup_sequence_failed", error=str(exc), type=op_type)
        _last_operation = {
            "status": "error",
            "type": op_type,
            "message": str(exc),
            "timestamp": datetime.now(UTC).isoformat(),
        }


async def _run_migrations() -> None:
    """Apply Alembic migrations without blocking the event loop."""

    def _upgrade() -> None:
        from alembic.config import Config

        from alembic import command

        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")

    logger.info("running_migrations")
    await asyncio.to_thread(_upgrade)
    logger.info("migrations_applied")
