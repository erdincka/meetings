"""The operator surface, assembled: real router, real dependencies, real database.

Two things are being proved here that no unit test can prove. First, that the
guard is actually *attached* — a correct dependency that nobody wired reads
exactly like a working one. Second, that the role split holds across every
route rather than the handful someone remembered to decorate.
"""

from __future__ import annotations

import httpx
import pytest

from tests.integration.conftest import OPERATOR_HEADERS, VIEWER_HEADERS

pytestmark = pytest.mark.integration


def _persona(unique: str, tools: list[str] | None = None) -> dict:
    return {
        "display_name": f"Test Persona {unique}",
        "title": "Director",
        "department": "Finance",
        "default_tools": tools if tools is not None else ["retrieve_documents"],
    }


class TestUnauthenticated:
    """No token at all."""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/v1/roles"),
            ("GET", "/api/v1/meetings"),
            ("GET", "/api/v1/settings"),
            ("GET", "/api/v1/system/status"),
            ("POST", "/api/v1/roles"),
            ("PATCH", "/api/v1/settings"),
        ],
    )
    async def test_every_operator_route_demands_a_token(
        self, client: httpx.AsyncClient, method: str, path: str
    ) -> None:
        response = await client.request(method, path, json={})
        assert response.status_code == 401, path
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    async def test_auth_config_answers_without_one(self, client: httpx.AsyncClient) -> None:
        """A client cannot be asked to authenticate before it knows it must."""
        response = await client.get("/api/v1/auth/config")
        assert response.status_code == 200
        assert response.json()["data"]["auth_required"] is True

    @pytest.mark.parametrize("path", ["/health", "/readyz", "/metrics"])
    async def test_probes_stay_open(self, client: httpx.AsyncClient, path: str) -> None:
        """Kubernetes probes and the Prometheus scrape hold no credential."""
        response = await client.get(path)
        assert response.status_code in (200, 503), path


class TestViewerRole:
    async def test_viewer_reads(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/roles", headers=VIEWER_HEADERS)
        assert response.status_code == 200

    async def test_viewer_cannot_create_a_persona(
        self, client: httpx.AsyncClient, unique: str
    ) -> None:
        """The escalation this role split exists to stop.

        A persona's tool list resolves to a capability profile, and the profile
        decides which ServiceAccount its sandbox runs under. Editing a persona
        is a privilege change, so it is not a viewer's to make.
        """
        response = await client.post("/api/v1/roles", json=_persona(unique), headers=VIEWER_HEADERS)
        assert response.status_code == 403

    async def test_viewer_cannot_change_runtime_settings(self, client: httpx.AsyncClient) -> None:
        response = await client.patch(
            "/api/v1/settings", json={"default_turn_limit": 4}, headers=VIEWER_HEADERS
        )
        assert response.status_code == 403

    async def test_whoami_reports_the_role(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/auth/whoami", headers=VIEWER_HEADERS)
        assert response.json()["data"] == {"role": "viewer", "can_mutate": False}


class TestOperatorRole:
    async def test_persona_round_trip(self, client: httpx.AsyncClient, unique: str) -> None:
        created = await client.post(
            "/api/v1/roles", json=_persona(unique), headers=OPERATOR_HEADERS
        )
        assert created.status_code == 200, created.text
        role_id = created.json()["data"]["id"]

        fetched = await client.get(f"/api/v1/roles/{role_id}", headers=OPERATOR_HEADERS)
        assert fetched.json()["data"]["display_name"] == f"Test Persona {unique}"

        updated = await client.put(
            f"/api/v1/roles/{role_id}",
            json={"title": "Chief Executive Officer"},
            headers=OPERATOR_HEADERS,
        )
        assert updated.json()["data"]["title"] == "Chief Executive Officer"

    async def test_whoami_reports_the_role(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/auth/whoami", headers=OPERATOR_HEADERS)
        assert response.json()["data"] == {"role": "operator", "can_mutate": True}


class TestCredentialsAreNotSettable:
    """Infrastructure configuration is environment-only, and stays that way.

    The settings table holds prompts and limits. Credentials arrive from the
    meetings-runtime Secret and are never writable over HTTP -- a 422 rather
    than a silently ignored field, because a rejected write the operator can see
    is the only kind that teaches them anything.
    """

    @pytest.mark.parametrize(
        "field", ["inference_api_key", "database_url", "operator_token", "OPERATOR_TOKEN"]
    )
    async def test_setting_a_credential_is_rejected(
        self, client: httpx.AsyncClient, field: str
    ) -> None:
        response = await client.patch(
            "/api/v1/settings", json={field: "sneaked-in"}, headers=OPERATOR_HEADERS
        )
        assert response.status_code == 422, f"{field} was accepted"

    async def test_settings_response_leaks_no_credential(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/settings", headers=OPERATOR_HEADERS)
        body = response.text.lower()
        assert "test-operator-token" not in body
        assert "api_key" not in body


class TestCapabilityProfileResolution:
    """The profile a persona resolves to is visible over the API, not only in a log.

    The demo's central claim is that the cluster decides what an agent may do.
    That claim is only checkable if an operator can see, before the meeting
    starts, which profile each attendee landed in.
    """

    async def test_smallest_covering_profile_wins(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/v1/roles/capabilities/resolve",
            json={"tools": ["retrieve_documents", "query_business_metrics"]},
            headers=OPERATOR_HEADERS,
        )
        data = response.json()["data"]
        # Reading metrics is not a reason to be handed a Python interpreter.
        assert data["resolved"] == "analyst"
        assert data["can_execute_code"] is False

    async def test_code_execution_lands_in_quant(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/v1/roles/capabilities/resolve",
            json={"tools": ["run_python_analysis", "query_business_metrics"]},
            headers=OPERATOR_HEADERS,
        )
        data = response.json()["data"]
        assert data["resolved"] == "quant"
        assert data["can_execute_code"] is True

    async def test_catalog_is_readable(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/roles/capabilities/catalog", headers=VIEWER_HEADERS)
        assert response.status_code == 200
        assert response.json()["data"]
