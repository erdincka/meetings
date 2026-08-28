"""The sandbox-facing surface, end to end through the real application.

This is the surface a persona pod calls: retrieval, artifacts and action items,
authenticated by a projected ServiceAccount token rather than an operator one.

The failure these tests exist for is not subtle logic — it is a router that was
written, reviewed and never mounted. Every handler in ``internal.py`` can be
individually correct while the whole surface answers 404, and nothing in a unit
test of those handlers would notice. Asserting on the assembled app is the only
place that shows up.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.sandbox_auth import (
    AGENT_LABEL,
    MEETING_LABEL,
    PROFILE_LABEL,
    SandboxIdentity,
)
from tests.integration.conftest import OPERATOR_HEADERS

pytestmark = pytest.mark.integration

AGENT_A = "11111111-1111-1111-1111-111111111111"
AGENT_B = "22222222-2222-2222-2222-222222222222"
MEETING = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def as_sandbox(monkeypatch):
    """Stand in for the apiserver's TokenReview.

    The identity is still constructed the way the real path constructs it —
    from the cluster's view of the pod, never from the request body — so the
    property under test (a sandbox cannot choose whose data it reads) is
    exercised rather than bypassed.
    """

    def _identity(agent_id: str = AGENT_A, profile: str = "baseline") -> SandboxIdentity:
        return SandboxIdentity(
            service_account=f"persona-{profile}",
            namespace="meetings-sandboxes",
            pod_name=f"pod-{agent_id[:4]}",
            agent_id=agent_id,
            meeting_id=MEETING,
            profile=profile,
        )

    def _install(agent_id: str = AGENT_A, profile: str = "baseline") -> None:
        from app.core import sandbox_auth

        async def fake_authenticate(self, token: str) -> SandboxIdentity:
            if not token:
                raise AssertionError("authenticate called without a token")
            return _identity(agent_id, profile)

        monkeypatch.setattr(sandbox_auth.SandboxAuthenticator, "authenticate", fake_authenticate)

    return _install


class TestSurfaceIsReachable:
    async def test_internal_routes_are_mounted(self, client: httpx.AsyncClient) -> None:
        """A 401 proves the route exists; a 404 would mean it was never wired."""
        response = await client.post("/internal/v1/retrieval/search", json={"query": "x"})
        assert response.status_code == 401, (
            "the sandbox API answered as if it does not exist -- every persona "
            "tool call would fail with a bare 404"
        )

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/internal/v1/retrieval/search"),
            ("POST", "/internal/v1/artifacts"),
            ("POST", "/internal/v1/action-items"),
            ("POST", "/internal/v1/llm/chat/completions"),
        ],
    )
    async def test_every_internal_route_demands_a_sandbox_token(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={"query": "x"})
        assert response.status_code == 401, path

    async def test_an_operator_token_is_not_a_sandbox_token(
        self, client: httpx.AsyncClient
    ) -> None:
        """The two authentication schemes are separate on purpose.

        An operator token is a shared secret held by a person; a sandbox token
        is a cluster-issued identity bound to one pod. Accepting the first here
        would let anyone with UI access read as any persona.
        """
        response = await client.post(
            "/internal/v1/retrieval/search",
            json={"query": "x"},
            headers=OPERATOR_HEADERS,
        )
        assert response.status_code in (401, 403)


@pytest.fixture
def captured_search(monkeypatch) -> dict:
    """Record what the route actually asked the search layer for.

    The embedding call is the one hop that needs a live model endpoint, and it
    is not what these tests are about. Capturing the arguments is also the
    stronger assertion: the property under test is *which scope was queried*,
    which a result set of zero rows would not distinguish from a query that was
    never narrowed at all.
    """
    seen: dict = {}

    async def fake_search(**kwargs):
        seen.update(kwargs)
        return []

    from app.api.routes import internal

    monkeypatch.setattr(internal, "semantic_search", fake_search)
    return seen


class TestIdentityComesFromTheCluster:
    async def test_body_cannot_override_the_calling_agent(
        self, client: httpx.AsyncClient, as_sandbox, captured_search: dict
    ) -> None:
        """The prompt-injection case: an agent asking to read as someone else.

        ``agent_id`` in the body is advisory. The scope that actually runs comes
        from the verified identity, so a model that has been talked into naming
        another persona still reads its own library.
        """
        as_sandbox(agent_id=AGENT_A)
        response = await client.post(
            "/internal/v1/retrieval/search",
            json={
                "query": "quarterly numbers",
                "scopes": ["company", "meeting"],
                "agent_id": AGENT_B,
                "limit": 3,
            },
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 200, response.text
        assert str(captured_search["owner_agent_id"]) == AGENT_A, (
            "the search ran as the agent named in the request body"
        )
        assert str(captured_search["meeting_id"]) == MEETING

    async def test_an_unrequested_scope_is_not_invented(
        self, client: httpx.AsyncClient, as_sandbox
    ) -> None:
        """A scope the caller did not ask for is refused, not silently widened."""
        as_sandbox()
        response = await client.post(
            "/internal/v1/retrieval/search",
            json={"query": "anything", "scopes": ["everything"]},
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 400

    async def test_retrieval_limit_is_capped_server_side(
        self, client: httpx.AsyncClient, as_sandbox
    ) -> None:
        """A ceiling the caller cannot raise, so a library cannot be paged out."""
        as_sandbox()
        response = await client.post(
            "/internal/v1/retrieval/search",
            json={"query": "everything", "scopes": ["company"], "limit": 5000},
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 422


class TestLabelContract:
    """The labels the backend sets at creation are the ones auth reads back.

    They are a wire contract between ``SandboxManager`` and ``sandbox_auth``.
    If one side renames a key, callers silently become anonymous rather than
    failing, so the constant is asserted rather than assumed.
    """

    def test_labels_carry_the_domain_the_controller_accepts(self) -> None:
        for label in (MEETING_LABEL, AGENT_LABEL, PROFILE_LABEL):
            assert label.startswith("sandbox.users.io/")

    def test_manager_and_auth_agree(self) -> None:
        from app.sandbox import manager

        assert manager.AGENT_LABEL is AGENT_LABEL
        assert manager.MEETING_LABEL is MEETING_LABEL
        assert manager.PROFILE_LABEL is PROFILE_LABEL


class TestModelProxy:
    """Personas reach the model through the backend, never directly.

    The property that matters most here is negative: the caller's Authorization
    header is a *cluster* credential -- a projected ServiceAccount token that
    TokenReview accepts -- and forwarding it to an off-cluster provider would
    hand a third party a token the apiserver honours. The proxy must replace it,
    not pass it along.
    """

    async def test_sandbox_token_is_never_forwarded_upstream(
        self, client: httpx.AsyncClient, as_sandbox, monkeypatch
    ) -> None:
        as_sandbox()
        seen: dict[str, str] = {}

        class FakeResponse:
            status_code = 200
            headers = httpx.Headers({"content-type": "application/json"})

            async def aiter_bytes(self):
                yield b'{"ok": true}'

            async def aclose(self) -> None:
                return None

        class FakeClient:
            def build_request(self, method, url, content=None, headers=None):
                return httpx.Request(method, url, content=content, headers=headers)

            async def send(self, request, stream=False):
                seen.update({k.lower(): v for k, v in request.headers.items()})
                return FakeResponse()

            async def aclose(self) -> None:
                return None

        # The seam, not httpx itself: patching httpx.AsyncClient globally would
        # also intercept the ASGI client this test uses to call the app, so the
        # request would never reach the code under test.
        monkeypatch.setattr(
            "app.core.config.settings.INFERENCE_ENDPOINT", "https://provider.test/v1"
        )
        monkeypatch.setattr("app.core.config.settings.INFERENCE_API_KEY", "provider-secret")
        monkeypatch.setattr("app.api.routes.internal._proxy_client", lambda: FakeClient())

        response = await client.post(
            "/internal/v1/llm/chat/completions",
            json={"model": "m", "messages": []},
            headers={"Authorization": "Bearer sandbox-serviceaccount-token"},
        )
        assert response.status_code == 200

        assert seen.get("authorization") == "Bearer provider-secret", (
            "the proxy must present the provider credential, not relay the caller's"
        )
        assert "sandbox-serviceaccount-token" not in seen.get("authorization", ""), (
            "the sandbox's cluster token was forwarded to an off-cluster provider"
        )

    async def test_unreachable_provider_is_a_gateway_error(
        self, client: httpx.AsyncClient, as_sandbox, monkeypatch
    ) -> None:
        """502 rather than 500: the agent can report an upstream failure as such
        instead of appearing to have malfunctioned itself."""
        as_sandbox()

        class FailingClient:
            def build_request(self, method, url, content=None, headers=None):
                return httpx.Request(method, url, content=content, headers=headers)

            async def send(self, request, stream=False):
                raise httpx.ConnectError("no route to host")

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            "app.core.config.settings.INFERENCE_ENDPOINT", "https://provider.test/v1"
        )
        monkeypatch.setattr("app.api.routes.internal._proxy_client", lambda: FailingClient())

        response = await client.post(
            "/internal/v1/llm/chat/completions",
            json={"model": "m", "messages": []},
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 502

    async def test_no_configured_endpoint_is_reported_not_guessed(
        self, client: httpx.AsyncClient, as_sandbox, monkeypatch
    ) -> None:
        as_sandbox()
        monkeypatch.setattr("app.core.config.settings.INFERENCE_ENDPOINT", None)
        response = await client.post(
            "/internal/v1/llm/chat/completions",
            json={"model": "m", "messages": []},
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 503

    async def test_a_compressed_upstream_reaches_the_caller_as_json(
        self, client: httpx.AsyncClient, as_sandbox, monkeypatch
    ) -> None:
        """The body and the headers must agree about encoding.

        Forwarding the raw body while stripping content-encoding hands the
        sandbox gzip bytes labelled as JSON, and every turn dies with
        `'utf-8' codec can't decode byte 0x8b in position 1` -- 0x1f 0x8b being
        the gzip magic number. It passed against Ollama, which does not compress,
        and failed against OpenRouter, which does.
        """
        import gzip

        as_sandbox()
        payload = b'{"choices":[{"message":{"content":"hello"}}]}'

        class GzipResponse:
            status_code = 200
            # As a real provider answers: compressed body, header saying so.
            headers = httpx.Headers(
                {"content-type": "application/json", "content-encoding": "gzip"}
            )

            async def aiter_bytes(self):
                # httpx decodes content-encoding for aiter_bytes; aiter_raw would
                # not, which is the distinction this test exists to pin.
                yield payload

            async def aiter_raw(self):
                yield gzip.compress(payload)

            async def aclose(self) -> None:
                return None

        class GzipClient:
            def build_request(self, method, url, content=None, headers=None):
                return httpx.Request(method, url, content=content, headers=headers)

            async def send(self, request, stream=False):
                return GzipResponse()

            async def aclose(self) -> None:
                return None

        monkeypatch.setattr(
            "app.core.config.settings.INFERENCE_ENDPOINT", "https://provider.test/v1"
        )
        monkeypatch.setattr("app.api.routes.internal._proxy_client", lambda: GzipClient())

        response = await client.post(
            "/internal/v1/llm/chat/completions",
            json={"model": "m", "messages": []},
            headers={"Authorization": "Bearer sandbox-token"},
        )
        assert response.status_code == 200
        assert "content-encoding" not in {k.lower() for k in response.headers}, (
            "content-encoding was forwarded alongside a body this proxy re-frames"
        )
        # The real assertion: it parses. A gzip body would raise here.
        assert response.json()["choices"][0]["message"]["content"] == "hello"
