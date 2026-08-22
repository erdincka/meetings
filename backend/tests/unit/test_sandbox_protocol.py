"""Wire protocol round-trip.

protocol.py is vendored into the sandbox runtime image, so a change that breaks
serialisation breaks both sides at once. These tests pin the shape.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.orchestration.protocol import (
    ModelConfig,
    PersonaBindRequest,
    PersonaSpec,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnRequest,
    TurnResult,
)


def _persona() -> PersonaSpec:
    return PersonaSpec(display_name="Jane Roe", title="CFO", department="Finance")


class TestSSEEncoding:
    def test_event_encodes_as_sse_frame(self) -> None:
        event = TurnEvent(type="speech.delta", text="Revenue is up.")
        frame = event.to_sse()
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        assert json.loads(frame.removeprefix("data: ").strip())["text"] == "Revenue is up."

    def test_none_fields_are_omitted(self) -> None:
        """Keeps frames small; every turn streams several."""
        payload = json.loads(
            TurnEvent(type="speech.delta", text="hi").to_sse().removeprefix("data: ")
        )
        assert set(payload) == {"type", "text"}

    def test_result_event_round_trips(self) -> None:
        original = TurnEvent(
            type="turn.result",
            result=TurnResult(
                turn_key="m:1:a",
                agent_id="a",
                public_content="[Jane Roe - CFO] Revenue is up.",
                private_reasoning="checked the numbers",
                tool_calls=[ToolCall(id="c1", name="retrieve_documents", args={"query": "rev"})],
                tool_results=[ToolResult(id="c1", name="retrieve_documents", ok=True)],
            ),
        )
        decoded = TurnEvent.model_validate(
            json.loads(original.to_sse().removeprefix("data: ").strip())
        )
        assert decoded.result is not None
        assert decoded.result.public_content == original.result.public_content  # type: ignore[union-attr]
        assert decoded.result.tool_calls[0].name == "retrieve_documents"

    def test_unknown_field_is_rejected(self) -> None:
        """extra='forbid' turns a protocol drift into a loud failure."""
        with pytest.raises(ValidationError):
            TurnEvent(type="speech.delta", text="hi", speaker="nope")

    def test_unknown_event_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TurnEvent(type="turn.finished")


class TestBindRequest:
    def test_carries_no_api_key(self) -> None:
        """The key is mounted into the sandbox from a Secret, never sent.

        A backend that does not transmit the credential cannot leak it through
        a request log, a trace, or an error report.
        """
        fields = set(PersonaBindRequest.model_fields) | set(ModelConfig.model_fields)
        assert not any("key" in f or "secret" in f or "token" in f for f in fields)

    def test_every_persona_field_survives_serialisation(self) -> None:
        """Regression guard for the dozen fields that reached no prompt."""
        persona = PersonaSpec(
            display_name="Jane Roe",
            title="CFO",
            department="Finance",
            seniority="Executive",
            responsibilities=["own the budget"],
            kpis=["EBITDA"],
            objectives=["close FY"],
            priorities=["cost control"],
            risk_tolerance="Low",
            tone=["Direct", "Data-driven"],
            collaboration_style="Consultative",
            challenge_style="Analytical",
        )
        restored = PersonaSpec.model_validate(json.loads(persona.model_dump_json()))
        assert restored == persona

    def test_bind_request_round_trips(self) -> None:
        request = PersonaBindRequest(
            agent_id="a",
            meeting_id="m",
            persona=_persona(),
            system_prompt_template="You are {{DISPLAY_NAME}}.",
            granted_tools=["retrieve_documents"],
            model=ModelConfig(endpoint="http://x/v1", model_name="gpt-test"),
        )
        restored = PersonaBindRequest.model_validate(json.loads(request.model_dump_json()))
        assert restored == request


class TestTurnRequest:
    def test_turn_key_is_required(self) -> None:
        with pytest.raises(ValidationError):
            TurnRequest()  # type: ignore[call-arg]

    def test_defaults_are_empty_not_none(self) -> None:
        """Prompt assembly substitutes these directly; None would render 'None'."""
        turn = TurnRequest(turn_key="m:0:a")
        assert turn.objective == "" and turn.agenda == ""
        assert turn.attendees == [] and turn.transcript == []
