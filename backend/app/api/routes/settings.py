"""Operator-tunable settings.

Reads and writes the ``system_settings`` table. Credentials are NOT settable
here -- they come from the environment (``meetings-runtime`` Secret) -- and
``SystemSettingsUpdate`` forbids extra fields, so an attempt to set one is a
422 rather than a silent no-op.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.network import normalize_v1_endpoint
from app.domain.response import APIResponse
from app.domain.settings import (
    SettingsDiscoveryRequest,
    SystemSettingsResponse,
    SystemSettingsUpdate,
)
from app.orchestration.prompts import PROMPT_METADATA
from app.services import settings_service

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get("", response_model=APIResponse)
async def get_settings(session: AsyncSession = Depends(get_db_session)) -> APIResponse:
    row = await settings_service.get_settings_row(session)
    return APIResponse(
        status="success",
        data=SystemSettingsResponse.model_validate(row).model_dump(),
    )


@router.patch("", response_model=APIResponse)
async def update_settings(
    payload: SystemSettingsUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields supplied")

    try:
        row = await settings_service.update_tunables(session, changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return APIResponse(
        status="success",
        data=SystemSettingsResponse.model_validate(row).model_dump(),
    )


@router.post("/discover", response_model=APIResponse)
async def discover_models(req: SettingsDiscoveryRequest) -> APIResponse:
    logger.info("model_discovery_requested", endpoint=req.endpoint)

    endpoint = normalize_v1_endpoint(req.endpoint)
    url = f"{endpoint}/models"
    headers = {"Authorization": f"Bearer {req.api_key}"} if req.api_key else {}

    try:
        async with httpx.AsyncClient(verify=not req.ignore_tls, timeout=10.0) as client:
            logger.info("discovery_request_attempt", url=url)
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # Handle various response formats:
                # 1. OpenAI: {"data": [...]}
                # 2. Key-based list: {"models": [...], "results": [...]}
                # 3. Simple list: [...]
                if isinstance(data, dict):
                    models = data.get("data") or data.get("models")  # Look for known keys
                    if models is None:
                        # Unknown shape: hand it back as-is rather than guessing.
                        models = data
                else:
                    models = data

                logger.info(
                    "discovery_success",
                    count=len(models) if isinstance(models, list) else "unknown",
                )
                return APIResponse(status="success", data={"models": models})
            else:
                error_msg = f"Endpoint returned status {resp.status_code}: {resp.text[:100]}"
                logger.warning(
                    "discovery_endpoint_error", status=resp.status_code, text=resp.text[:100]
                )
                return APIResponse(
                    status="error", message=error_msg, data={"models": [], "error": error_msg}
                )
    except httpx.ConnectError:
        error_msg = (
            f"Discovery failed: Could not connect to {url}. Check if the service is running."
        )
        return APIResponse(
            status="error", message=error_msg, data={"models": [], "error": error_msg}
        )
    except httpx.TimeoutException:
        error_msg = f"Discovery failed: Request to {url} timed out after 10s."
        return APIResponse(
            status="error", message=error_msg, data={"models": [], "error": error_msg}
        )
    except Exception as e:
        logger.error("discovery_failed", error=str(e))
        error_msg = f"Discovery failed: {str(e)}"
        return APIResponse(
            status="error", message=error_msg, data={"models": [], "error": error_msg}
        )


@router.get("/prompts/metadata", response_model=APIResponse)
async def get_prompt_metadata() -> APIResponse:
    """Returns metadata for all configurable prompts, including defaults and placeholders."""
    return APIResponse(status="success", data=PROMPT_METADATA)
