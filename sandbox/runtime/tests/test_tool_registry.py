"""Tool construction and the code-execution denial path.

The security property under test: **building a tool never grants access.** Every
tool constructs happily in a sandbox that lacks the credential or permission it
needs, and reports the refusal honestly at call time. That is what keeps the
audit trail truthful -- a denial appears as a denial, not as a missing tool or a
silent no-op.
"""

from __future__ import annotations

import httpx
import pytest

from runtime.tools import KNOWN_TOOLS, build_tools
from runtime.tools.code_exec import DENIED_PREFIX, ExecutionDenied, _is_forbidden


def _context(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "client": httpx.AsyncClient(base_url="http://backend.test"),
        "agent_id": "agent-1",
        "meeting_id": "meeting-1",
        "library_access": True,
        "limit": 3,
        "artifact_writer": None,
    }
    base.update(overrides)
    return base


class TestRegistry:
    def test_builds_only_what_was_granted(self) -> None:
        tools = build_tools(["retrieve_documents"], **_context())
        assert [t.name for t in tools] == ["retrieve_documents"]

    def test_unknown_tool_is_skipped_not_fatal(self) -> None:
        """Backend and runtime version independently; a newer grant should
        degrade to fewer tools, not a failed meeting."""
        tools = build_tools(["retrieve_documents", "time_travel"], **_context())
        assert [t.name for t in tools] == ["retrieve_documents"]

    def test_empty_grant_builds_nothing(self) -> None:
        assert build_tools([], **_context()) == []

    def test_artifact_tools_are_individually_selectable(self) -> None:
        tools = build_tools(["draft_artifact"], **_context())
        assert [t.name for t in tools] == ["draft_artifact"]

    def test_every_known_tool_can_be_built(self) -> None:
        """A tool that cannot be constructed is a grant that silently does nothing."""
        tools = build_tools(sorted(KNOWN_TOOLS), **_context())
        assert {t.name for t in tools} == set(KNOWN_TOOLS)


class TestBuildingDoesNotGrantAccess:
    def test_metrics_tool_builds_without_a_dsn(self, monkeypatch, tmp_path) -> None:
        """No credential mounted must not stop the tool existing -- it must stop
        the tool *working*, with a message the model can act on."""
        from runtime.tools import metrics_sql

        monkeypatch.setattr(metrics_sql, "DSN_FILE", tmp_path / "absent")
        tools = build_tools(["query_business_metrics"], **_context())
        assert len(tools) == 1

    async def test_metrics_without_dsn_reports_rather_than_crashes(
        self, monkeypatch, tmp_path
    ) -> None:
        from runtime.tools import metrics_sql

        monkeypatch.setattr(metrics_sql, "DSN_FILE", tmp_path / "absent")
        tool = build_tools(["query_business_metrics"], **_context())[0]
        out = await tool.ainvoke({"sql": "SELECT 1"})
        assert "cannot query business metrics" in out


class TestCodeExecutionDenial:
    """The apiserver's 403 is the enforcing control, and must read as one."""

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("sandboxclaims.extensions.agents.x-k8s.io is forbidden"),
            Exception("HTTP 403: cannot create resource"),
            Exception("User cannot create resource sandboxclaims"),
        ],
    )
    def test_authorization_failures_are_recognised(self, exc: Exception) -> None:
        assert _is_forbidden(exc)

    def test_status_attribute_is_recognised(self) -> None:
        exc = Exception("nope")
        exc.status = 403  # type: ignore[attr-defined]
        assert _is_forbidden(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("connection refused"),
            Exception("warm pool exhausted"),
            Exception("timed out waiting for sandbox"),
        ],
    )
    def test_ordinary_failures_are_not_mistaken_for_denial(self, exc: Exception) -> None:
        """Misreporting an outage as a policy denial would be actively
        misleading in the audit matrix."""
        assert not _is_forbidden(exc)

    async def test_denial_is_reported_to_the_agent_not_raised(self, monkeypatch) -> None:
        """A denied persona should keep contributing, and the refusal should be
        legible to both the model and the audit trail."""
        from runtime.tools import code_exec

        async def refuse(**_kwargs: object) -> object:
            raise ExecutionDenied("sandboxclaims is forbidden")

        monkeypatch.setattr(code_exec, "_claim_exec_sandbox", refuse)
        tool = build_tools(["run_python_analysis"], **_context())[0]
        out = await tool.ainvoke({"code": "print(1)"})

        assert out.startswith(DENIED_PREFIX)
        assert "not permitted" in out
        assert "Kubernetes API server" in out

    async def test_infrastructure_failure_reads_differently(self, monkeypatch) -> None:
        from runtime.tools import code_exec

        async def fail(**_kwargs: object) -> object:
            raise RuntimeError("warm pool exhausted")

        monkeypatch.setattr(code_exec, "_claim_exec_sandbox", fail)
        tool = build_tools(["run_python_analysis"], **_context())[0]
        out = await tool.ainvoke({"code": "print(1)"})

        assert not out.startswith(DENIED_PREFIX), "an outage must not read as a policy denial"
        assert "unavailable" in out
