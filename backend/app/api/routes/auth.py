"""Operator session endpoints.

Deliberately thin. There is no user store, no registration and no password
reset: the tokens come from a Kubernetes Secret, and the browser's job is only
to discover whether authentication is on and to confirm the token it holds is
still good.

``/auth/config`` is the one route on the operator surface that is reachable
without a token, because a client cannot be asked to authenticate before it can
find out whether authentication exists. It reveals a single boolean and no
detail about the tokens themselves.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import Principal, require_api_access
from app.core.config import settings
from app.domain.response import APIResponse

router = APIRouter()


@router.get("/config", response_model=APIResponse)
async def auth_config() -> APIResponse:
    """Whether the operator API requires a token. Unauthenticated by design."""
    return APIResponse(
        status="success",
        data={
            "auth_required": settings.AUTH_ENABLED,
            "scheme": "bearer",
        },
    )


@router.get("/whoami", response_model=APIResponse)
async def whoami(principal: Principal = Depends(require_api_access)) -> APIResponse:
    """Validate the presented token and report the role it carries."""
    return APIResponse(
        status="success",
        data={
            "role": principal.role,
            "can_mutate": principal.can("operator"),
        },
    )
