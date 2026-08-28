"""Operator authentication and authorisation.

Two distinct identities call this backend, and they are authenticated by
different mechanisms because they are different kinds of principal:

* **Sandboxes** present a projected ServiceAccount token, validated by the
  Kubernetes apiserver via TokenReview (``app.core.sandbox_auth``). Their
  identity is a cluster fact, so the cluster is asked.
* **Operators** — the people using the UI and the API — present a bearer token
  issued out of band and delivered through the ``meetings-runtime`` Secret.
  This module handles those.

Two operator roles, separated by what a mistake costs:

``viewer``
    Reads. Meetings, transcripts, artifacts, personas, capability profiles,
    system status. A viewer can watch and audit but cannot change what any
    agent is allowed to do.
``operator``
    Everything a viewer can do, plus every mutation: editing personas and their
    tool grants, starting and stopping meetings, changing runtime settings.

The split matters here more than in a typical CRUD application. A persona's
tool list resolves to a capability profile, and that profile decides which
ServiceAccount its sandbox runs under — so *editing a persona is a privilege
change*. Read access to a transcript is not.

Authorisation is derived from the HTTP method rather than declared per route.
Safe methods (GET/HEAD/OPTIONS) require ``viewer``; everything else requires
``operator``. Enumerating routes invites the opposite failure: a new mutating
endpoint added later that nobody remembers to protect. Method-derived rules
fail closed for code that has not been written yet.

Tokens are compared with :func:`hmac.compare_digest`, never ``==``.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Final

import structlog
from fastapi import Header, HTTPException, Request, WebSocket, status

from app.core.config import settings

logger = structlog.get_logger(__name__)

VIEWER: Final = "viewer"
OPERATOR: Final = "operator"

# Ordered least to most privileged; index comparison is the check.
ROLE_ORDER: Final[tuple[str, ...]] = (VIEWER, OPERATOR)

SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

# The subprotocol a browser uses to carry the token on a WebSocket handshake.
# Browsers cannot set headers on `new WebSocket(...)`; the subprotocol list is
# the only caller-controlled field, and unlike a query string it does not end up
# in proxy access logs or browser history.
WS_SUBPROTOCOL: Final = "bearer"


@dataclass(frozen=True)
class Principal:
    """An authenticated operator."""

    role: str

    def can(self, required: str) -> bool:
        return ROLE_ORDER.index(self.role) >= ROLE_ORDER.index(required)


# The principal used when authentication is switched off. Named rather than
# implicit so a log line or a response can say plainly that nothing was checked.
ANONYMOUS: Final = Principal(role=OPERATOR)


class AuthConfigurationError(RuntimeError):
    """Authentication is enabled but no operator token was supplied."""


def configuration_error() -> str | None:
    """Describe why authentication cannot be enforced, or ``None`` if it can.

    Called at startup so the process refuses to serve rather than starting in a
    state where every request is anonymous while the configuration claims
    otherwise. A security control that silently degrades to "off" is worse than
    one that was never claimed.
    """
    if not settings.AUTH_ENABLED:
        return None
    if not settings.OPERATOR_TOKEN:
        return (
            "AUTH_ENABLED is true but OPERATOR_TOKEN is empty. Supply it from the "
            "meetings-runtime Secret, or set AUTH_ENABLED=false for a trusted "
            "single-user development environment."
        )
    return None


def _match(candidate: str, configured: str) -> bool:
    return bool(configured) and hmac.compare_digest(candidate, configured)


def resolve_token(token: str) -> Principal | None:
    """Map a presented token to a principal, or ``None`` if it matches neither.

    The operator token is checked first and both comparisons always run, so the
    time taken does not reveal which token was closer to correct.
    """
    is_operator = _match(token, settings.OPERATOR_TOKEN)
    is_viewer = _match(token, settings.VIEWER_TOKEN)
    if is_operator:
        return Principal(role=OPERATOR)
    if is_viewer:
        return Principal(role=VIEWER)
    return None


def _bearer(header: str) -> str:
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing operator token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return header.removeprefix("Bearer ").strip()


def authenticate(header: str) -> Principal:
    """Resolve an ``Authorization`` header to a principal, or raise 401."""
    if not settings.AUTH_ENABLED:
        return ANONYMOUS
    principal = resolve_token(_bearer(header))
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator token rejected",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def _authorize(principal: Principal, required: str, *, what: str) -> Principal:
    if not principal.can(required):
        logger.warning("operator_forbidden", role=principal.role, required=required, target=what)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{required}' is required; this token is '{principal.role}'.",
        )
    return principal


async def require_api_access(
    request: Request,
    authorization: str = Header(default=""),
) -> Principal:
    """FastAPI dependency guarding the whole operator API surface.

    Applied once, to the router, so a route added tomorrow inherits it. Reads
    need ``viewer``; anything that can change state needs ``operator``.
    """
    principal = authenticate(authorization)
    required = VIEWER if request.method in SAFE_METHODS else OPERATOR
    return _authorize(principal, required, what=f"{request.method} {request.url.path}")


async def require_operator(
    authorization: str = Header(default=""),
) -> Principal:
    """Dependency for routes that must never be reachable by a viewer."""
    return _authorize(authenticate(authorization), OPERATOR, what="operator-only route")


async def authenticate_websocket(websocket: WebSocket, required: str = OPERATOR) -> Principal:
    """Authenticate a WebSocket handshake, closing it on failure.

    Raises :class:`WebSocketDisconnect`-adjacent behaviour by closing with an
    explicit policy-violation code, because a browser given a plain connection
    reset cannot distinguish "you are not signed in" from "the backend is down"
    — the same legibility argument the sandbox layer makes for returning a 403
    rather than dropping the packet.
    """
    if not settings.AUTH_ENABLED:
        await websocket.accept()
        return ANONYMOUS

    offered = [
        value.strip()
        for value in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if value.strip()
    ]
    principal = None
    if len(offered) == 2 and offered[0] == WS_SUBPROTOCOL:
        principal = resolve_token(offered[1])

    if principal is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthenticated")
        raise PermissionError("websocket token rejected")
    if not principal.can(required):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Forbidden")
        raise PermissionError("websocket role insufficient")

    # Echoing the subprotocol back is required by the WebSocket handshake: a
    # client that offered subprotocols and gets none accepted must fail.
    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    return principal
