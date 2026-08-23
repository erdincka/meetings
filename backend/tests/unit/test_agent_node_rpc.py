"""The agent node as a sandbox RPC proxy.

Runs the real node against the fake sandbox over an in-process ASGI transport,
so the RPC client, the SSE parsing and the state reducers are all exercised --
with no cluster, no gVisor and no model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.orchestration import agents as agents_module
from app.orchestration.state import is_utterance, public_transcript
from app.sandbox.manager import SandboxHandle, SandboxUnavailableError
from tests.fake_sandbox import FakeSandboxState, build_fake_sandbox


@dataclass
class FakeRole:
    """Stands in for a RoleAgent row."""

    display_name: str = "Jane Roe"
    title: str = "CFO"
    department: str = "Finance"
    summary: str | None = "Owns the numbers."
    seniority: str | None = "Executive"
    responsibilities: list[str] = field(default_factory=lambda: ["own the budget"])
    kpis: list[str] = field(default_factory=lambda: ["EBITDA"])
    objectives: list[str] = field(default_factory=lambda: ["close FY"])
    priorities: list[str] = field(default_factory=lambda: ["cost control"])
    risk_tolerance: str | None = "Low"
    tone: list[str] = field(default_factory=lambda: ["Direct"])
    collaboration_style: str | None = "Consultative"
    challenge_style: str | None = "Analytical"
    allowed_shared_library_access: bool = True
    system_prompt: str | None = None
    default_tools: list[str] = field(default_factory=lambda: ["retrieve_documents"])


@dataclass
class FakeSettings:
    inference_endpoint: str | None = "http://inference.test/v1"
    inference_model_name: str | None = "test-model"
    inference_temperature: float = 0.42
    inference_ignore_tls: bool = False
    retrieval_limits_per_agent: int = 7
    max_evidence_per_message: int = 5
    agent_prompt: str | None = "You are {{DISPLAY_NAME}}, {{TITLE}}."


AGENT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def sandbox_state() -> FakeSandboxState:
    return FakeSandboxState()


@pytest.fixture
def patched_sandbox(monkeypatch, sandbox_state: FakeSandboxState):
    """Point the node at the in-process fake instead of a real sandbox."""
    app = build_fake_sandbox(sandbox_state)
    transport = httpx.ASGITransport(app=app)

    async def fake_acquire(**kwargs: Any) -> SandboxHandle:
        return SandboxHandle(
            claim_name="claim-1",
            sandbox_name="sandbox-1",
            namespace="meetings-sandboxes",
            base_url="http://sandbox-1.test",
        )

    monkeypatch.setattr(agents_module.manager, "acquire", fake_acquire)

    original_init = agents_module.PersonaSandboxClient.__aenter__

    async def enter_with_transport(self):  # type: ignore[no-untyped-def]
        self._http = httpx.AsyncClient(transport=transport, base_url="http://sandbox-1.test")
        self._owns_http = True
        return self

    monkeypatch.setattr(agents_module.PersonaSandboxClient, "__aenter__", enter_with_transport)
    yield sandbox_state
    del original_init


def _config(**overrides: Any) -> dict[str, Any]:
    return {
        "configurable": {
            "attendees": {AGENT_ID: FakeRole()},
            "model_settings": FakeSettings(),
            **overrides,
        }
    }


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "meeting_id": "meeting-1",
        "current_turn": 3,
        "objective": "Decide on the recall",
        "agenda": "1. Facts 2. Options",
        "brief": "A defect was found.",
        "expectations": "A decision today.",
        "messages": [],
    }
    base.update(overrides)
    return base


class TestSuccessfulTurn:
    async def test_node_returns_an_utterance(self, patched_sandbox: FakeSandboxState) -> None:
        node = agents_module.create_role_agent_node(AGENT_ID)
        result = await node(_state(), _config())  # type: ignore[arg-type]

        assert len(result["messages"]) == 1
        message = result["messages"][0]
        assert is_utterance(message), "must be tagged so routing can find it"
        assert "Jane Roe" in message.content
        assert result["active_agent_id"] == AGENT_ID

    async def test_sandbox_name_recorded_for_reuse(self, patched_sandbox: FakeSandboxState) -> None:
        node = agents_module.create_role_agent_node(AGENT_ID)
        result = await node(_state(), _config())  # type: ignore[arg-type]
        assert result["sandboxes"] == {AGENT_ID: "sandbox-1"}

    async def test_turn_key_is_deterministic(self, patched_sandbox: FakeSandboxState) -> None:
        """Same meeting, same turn, same agent -> same key, so a replay is a hit."""
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(current_turn=3), _config())  # type: ignore[arg-type]
        await node(_state(current_turn=3), _config())  # type: ignore[arg-type]
        assert patched_sandbox.turn_counts == {f"meeting-1:3:{AGENT_ID}": 2}
        assert list(patched_sandbox.turn_counts) == [f"meeting-1:3:{AGENT_ID}"]

    async def test_settings_reach_the_sandbox(self, patched_sandbox: FakeSandboxState) -> None:
        """Temperature and retrieval limit were hardcoded before Phase 1."""
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(), _config())  # type: ignore[arg-type]

        bind = patched_sandbox.binds[0]
        assert bind.model.temperature == 0.42, "settings temperature must win over any default"
        assert bind.limits.retrieval_limit == 7

    async def test_every_persona_field_is_sent(self, patched_sandbox: FakeSandboxState) -> None:
        """The dozen fields that used to reach no prompt."""
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(), _config())  # type: ignore[arg-type]

        persona = patched_sandbox.binds[0].persona
        assert persona.responsibilities == ["own the budget"]
        assert persona.kpis == ["EBITDA"]
        assert persona.objectives == ["close FY"]
        assert persona.risk_tolerance == "Low"
        assert persona.challenge_style == "Analytical"
        assert persona.seniority == "Executive"

    async def test_meeting_context_is_sent(self, patched_sandbox: FakeSandboxState) -> None:
        """brief and expectations were declared in state and never populated."""
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(), _config())  # type: ignore[arg-type]

        turn = patched_sandbox.turns[0]
        assert turn.brief == "A defect was found."
        assert turn.expectations == "A decision today."
        assert turn.objective == "Decide on the recall"

    async def test_no_credential_crosses_the_wire(self, patched_sandbox: FakeSandboxState) -> None:
        payload = patched_sandbox.binds[0].model_dump_json() if patched_sandbox.binds else ""
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(), _config())  # type: ignore[arg-type]
        payload = patched_sandbox.binds[0].model_dump_json()
        assert "api_key" not in payload


class TestToolAudit:
    async def test_tool_results_are_audited(self, patched_sandbox: FakeSandboxState) -> None:
        patched_sandbox.emit_tool_call = True
        node = agents_module.create_role_agent_node(AGENT_ID)
        result = await node(_state(), _config())  # type: ignore[arg-type]

        assert len(result["tool_audit"]) == 1
        entry = result["tool_audit"][0]
        assert entry["tool"] == "retrieve_documents"
        assert entry["ok"] is True
        assert entry["agent_id"] == AGENT_ID


class TestFailureHandling:
    async def test_sandbox_error_does_not_end_the_meeting(
        self, patched_sandbox: FakeSandboxState
    ) -> None:
        """One attendee failing should not stop the others speaking."""
        patched_sandbox.fail_next = True
        node = agents_module.create_role_agent_node(AGENT_ID)
        result = await node(_state(), _config())  # type: ignore[arg-type]

        assert result["event_log"][0]["type"] == "agent_failed"
        assert "scripted failure" in result["event_log"][0]["private_reasoning"]
        # Still produces a tagged utterance so the transcript shows the gap.
        assert is_utterance(result["messages"][0])
        assert "unable to contribute" in result["messages"][0].content

    async def test_unavailable_sandbox_is_reported_not_raised(self, monkeypatch) -> None:
        async def refuse(**kwargs: Any) -> SandboxHandle:
            raise SandboxUnavailableError("warm pool exhausted")

        monkeypatch.setattr(agents_module.manager, "acquire", refuse)
        node = agents_module.create_role_agent_node(AGENT_ID)
        result = await node(_state(), _config())  # type: ignore[arg-type]

        assert result["event_log"][0]["type"] == "agent_failed"
        assert "warm pool exhausted" in result["event_log"][0]["private_reasoning"]

    async def test_unknown_attendee_is_a_noop(self) -> None:
        node = agents_module.create_role_agent_node("not-an-attendee")
        assert await node(_state(), _config()) == {}  # type: ignore[arg-type]


class TestTranscriptPassing:
    async def test_only_utterances_are_forwarded(self, patched_sandbox: FakeSandboxState) -> None:
        """Regression: the old bracket heuristic swept up unrelated messages."""
        from langchain_core.messages import HumanMessage

        from app.orchestration.state import make_utterance

        messages = [
            make_utterance("[Ann - CEO] Welcome.", "agent-ceo"),
            HumanMessage(content="[system] this is not an utterance"),
        ]
        node = agents_module.create_role_agent_node(AGENT_ID)
        await node(_state(messages=messages), _config())  # type: ignore[arg-type]

        turn = patched_sandbox.turns[0]
        assert len(turn.transcript) == 1
        assert "Welcome" in turn.transcript[0].content
        assert len(public_transcript(messages)) == 1


class TestDurableIdempotency:
    """A replayed node must not re-invoke the model or re-run tools.

    LangGraph replays the last uncompleted node after a crash-resume. The
    sandbox's in-memory cache dies with the sandbox, so the backend keeps its
    own record.
    """

    async def test_cached_turn_short_circuits_the_sandbox(
        self, patched_sandbox: FakeSandboxState, monkeypatch
    ) -> None:
        from app.services import turn_cache

        stored: dict[str, dict[str, Any]] = {}

        async def fake_lookup(turn_key: str):
            return stored.get(turn_key)

        async def fake_record(turn_key, meeting_id, agent_id, payload):
            stored[turn_key] = turn_cache._rehydrate(dict(payload))

        monkeypatch.setattr(agents_module.turn_cache, "lookup", fake_lookup)
        monkeypatch.setattr(agents_module.turn_cache, "record", fake_record)

        node = agents_module.create_role_agent_node(AGENT_ID)
        first = await node(_state(), _config())  # type: ignore[arg-type]
        assert len(patched_sandbox.turns) == 1

        second = await node(_state(), _config())  # type: ignore[arg-type]
        # The sandbox must not have been asked a second time.
        assert len(patched_sandbox.turns) == 1, "replay re-invoked the model"
        assert second["messages"][0].content == first["messages"][0].content
        assert is_utterance(second["messages"][0]), "rehydrated message lost its metadata"

    def test_round_trip_preserves_audit_and_sandbox(self) -> None:
        from app.orchestration.agents import _serialisable
        from app.services.turn_cache import _rehydrate

        original = agents_module._success_state(
            AGENT_ID,
            "sandbox-1",
            type(
                "R",
                (),
                {
                    "turn_key": "m:1:a",
                    "public_content": "[Jane Roe - CFO] Revenue is up.",
                    "private_reasoning": "checked",
                    "tool_calls": [],
                    "tool_results": [],
                },
            )(),
        )
        restored = _rehydrate(_serialisable(original))
        assert restored["sandboxes"] == {AGENT_ID: "sandbox-1"}
        assert restored["messages"][0].content == original["messages"][0].content
        assert is_utterance(restored["messages"][0])
