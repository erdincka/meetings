"""Route composition, and where each surface's authentication is attached.

Four surfaces, because they have different callers and different proofs of
identity:

``api_router`` (mounted at ``/api/v1``)
    The operator HTTP surface. Guarded once, here, by ``require_api_access``
    rather than route by route -- a mutating endpoint added later is protected
    by default instead of by memory.
``auth_router`` (mounted at ``/api/v1``)
    Session discovery. Carries per-route dependencies because ``/auth/config``
    has to answer before a caller has a token to present.
``ws_router`` (mounted at ``/api/v1``)
    The meeting WebSocket. Separate because a browser cannot set an
    ``Authorization`` header on a WebSocket handshake, so it authenticates
    inside the handler from the negotiated subprotocol instead.
``internal_router`` (mounted at ``/internal``)
    The sandbox surface. Authenticated by Kubernetes TokenReview, not by an
    operator token, and never routed through the Gateway.
"""

from fastapi import APIRouter, Depends

from app.api.routes import (
    auth,
    documents,
    internal,
    meetings,
    roles,
    settings,
    system,
    templates,
)
from app.core.auth import require_api_access

api_router = APIRouter(dependencies=[Depends(require_api_access)])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(roles.router, prefix="/roles", tags=["roles"])
api_router.include_router(templates.router, prefix="/templates", tags=["templates"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(meetings.router, prefix="/meetings", tags=["meetings"])
api_router.include_router(system.router, prefix="/system", tags=["system"])

auth_router = APIRouter()
auth_router.include_router(auth.router, prefix="/auth", tags=["auth"])

ws_router = APIRouter()
ws_router.include_router(meetings.ws_router, prefix="/meetings", tags=["meetings"])

internal_router = APIRouter()
internal_router.include_router(internal.router, prefix="/internal", tags=["internal"])
