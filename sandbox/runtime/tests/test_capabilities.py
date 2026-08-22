"""Capability intersection.

The security property: a sandbox will not register a tool its SandboxTemplate
did not provision, no matter what the backend grants. This is the second of the
five enforcement layers, and the one that contains a compromised backend.
"""

from __future__ import annotations

import pytest

from runtime import capabilities


@pytest.fixture
def capability_file(tmp_path, monkeypatch):
    def _write(tools: list[str]) -> None:
        path = tmp_path / "capabilities"
        path.write_text("\n".join(["# provisioned by the SandboxTemplate", *tools]))
        monkeypatch.setattr(capabilities, "CAPABILITY_FILE", path)

    return _write


class TestResolveGrant:
    def test_granted_and_provisioned_tool_is_active(self, capability_file) -> None:
        capability_file(["retrieve_documents"])
        active, refused = capabilities.resolve_grant(["retrieve_documents"])
        assert active == ["retrieve_documents"]
        assert refused == []

    def test_backend_cannot_grant_an_unprovisioned_tool(self, capability_file) -> None:
        """The whole point: the template wins over the backend."""
        capability_file(["retrieve_documents"])
        active, refused = capabilities.resolve_grant(["retrieve_documents", "run_python_analysis"])
        assert active == ["retrieve_documents"]
        assert refused == ["run_python_analysis"]

    def test_all_tools_refused_when_none_provisioned(self, capability_file) -> None:
        capability_file([])
        active, refused = capabilities.resolve_grant(["retrieve_documents"])
        assert active == []
        assert refused == ["retrieve_documents"]

    def test_comments_and_blank_lines_ignored(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "capabilities"
        path.write_text("# a comment\n\n  retrieve_documents  \n\n")
        monkeypatch.setattr(capabilities, "CAPABILITY_FILE", path)
        assert capabilities.allowed_tools() == frozenset({"retrieve_documents"})

    def test_missing_file_falls_back_to_baseline_not_everything(
        self, tmp_path, monkeypatch
    ) -> None:
        """A missing capability file must not mean unrestricted."""
        monkeypatch.setattr(capabilities, "CAPABILITY_FILE", tmp_path / "absent")
        assert capabilities.allowed_tools() == capabilities.BASELINE_TOOLS
        _active, refused = capabilities.resolve_grant(["run_python_analysis"])
        assert refused == ["run_python_analysis"]

    def test_empty_grant_yields_nothing(self, capability_file) -> None:
        capability_file(["retrieve_documents"])
        assert capabilities.resolve_grant([]) == ([], [])
