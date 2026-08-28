"""Operator authentication and the role split.

These are the tests that would have to fail before a privilege escalation
shipped: a viewer editing a persona is a capability-profile change, and a
capability-profile change is what decides which ServiceAccount an agent's
sandbox runs under.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core import auth
from app.core.config import settings


@pytest.fixture(autouse=True)
def configured_tokens(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "op-secret")
    monkeypatch.setattr(settings, "VIEWER_TOKEN", "view-secret")


class _FakeRequest:
    def __init__(self, method: str, path: str = "/api/v1/roles") -> None:
        self.method = method
        self.url = type("U", (), {"path": path})()


class TestTokenResolution:
    def test_operator_token_resolves_to_operator(self) -> None:
        assert auth.resolve_token("op-secret") == auth.Principal(role=auth.OPERATOR)

    def test_viewer_token_resolves_to_viewer(self) -> None:
        assert auth.resolve_token("view-secret") == auth.Principal(role=auth.VIEWER)

    def test_unknown_token_resolves_to_nothing(self) -> None:
        assert auth.resolve_token("guess") is None

    def test_empty_token_never_matches_an_unset_role(self, monkeypatch) -> None:
        """An unconfigured viewer token must not make the empty string valid."""
        monkeypatch.setattr(settings, "VIEWER_TOKEN", "")
        assert auth.resolve_token("") is None


class TestRoleOrdering:
    def test_operator_satisfies_viewer(self) -> None:
        assert auth.Principal(role=auth.OPERATOR).can(auth.VIEWER)

    def test_viewer_does_not_satisfy_operator(self) -> None:
        assert not auth.Principal(role=auth.VIEWER).can(auth.OPERATOR)


class TestMethodDerivedAuthorisation:
    """The rule that protects endpoints nobody has written yet."""

    async def test_viewer_may_read(self) -> None:
        principal = await auth.require_api_access(
            _FakeRequest("GET"),  # type: ignore[arg-type]
            authorization="Bearer view-secret",
        )
        assert principal.role == auth.VIEWER

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_viewer_may_not_mutate(self, method: str) -> None:
        with pytest.raises(HTTPException) as exc:
            await auth.require_api_access(
                _FakeRequest(method),  # type: ignore[arg-type]
                authorization="Bearer view-secret",
            )
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def test_operator_may_do_everything(self, method: str) -> None:
        principal = await auth.require_api_access(
            _FakeRequest(method),  # type: ignore[arg-type]
            authorization="Bearer op-secret",
        )
        assert principal.role == auth.OPERATOR

    async def test_missing_header_is_401_not_403(self) -> None:
        """Unauthenticated and forbidden are different answers to different questions."""
        with pytest.raises(HTTPException) as exc:
            await auth.require_api_access(_FakeRequest("GET"), authorization="")  # type: ignore[arg-type]
        assert exc.value.status_code == 401
        assert exc.value.headers is not None
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"

    async def test_bad_token_is_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            await auth.require_api_access(
                _FakeRequest("GET"),  # type: ignore[arg-type]
                authorization="Bearer wrong",
            )
        assert exc.value.status_code == 401


class TestFailFastConfiguration:
    def test_enabled_without_a_token_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "OPERATOR_TOKEN", "")
        problem = auth.configuration_error()
        assert problem is not None and "OPERATOR_TOKEN" in problem

    def test_explicitly_disabled_is_allowed(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "AUTH_ENABLED", False)
        monkeypatch.setattr(settings, "OPERATOR_TOKEN", "")
        assert auth.configuration_error() is None

    async def test_disabled_treats_every_caller_as_operator(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "AUTH_ENABLED", False)
        principal = await auth.require_api_access(
            _FakeRequest("DELETE"),  # type: ignore[arg-type]
            authorization="",
        )
        assert principal is auth.ANONYMOUS


class _FakeWebSocket:
    def __init__(self, protocols: str | None) -> None:
        self.headers = {"sec-websocket-protocol": protocols} if protocols else {}
        self.accepted_with: str | None = None
        self.closed: tuple[int, str] | None = None

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted_with = subprotocol

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


class TestWebSocketHandshake:
    async def test_operator_subprotocol_is_accepted_and_echoed(self) -> None:
        ws = _FakeWebSocket("bearer, op-secret")
        principal = await auth.authenticate_websocket(ws)  # type: ignore[arg-type]
        assert principal.role == auth.OPERATOR
        # Required by the protocol: offering subprotocols and getting none back
        # is a handshake failure in every browser.
        assert ws.accepted_with == auth.WS_SUBPROTOCOL

    async def test_viewer_may_not_drive_a_meeting(self) -> None:
        ws = _FakeWebSocket("bearer, view-secret")
        with pytest.raises(PermissionError):
            await auth.authenticate_websocket(ws)  # type: ignore[arg-type]
        assert ws.closed == (1008, "Forbidden")

    async def test_no_token_is_closed_with_a_policy_code(self) -> None:
        ws = _FakeWebSocket(None)
        with pytest.raises(PermissionError):
            await auth.authenticate_websocket(ws)  # type: ignore[arg-type]
        # Not a reset: the browser must be able to tell "sign in" from "down".
        assert ws.closed == (1008, "Unauthenticated")
