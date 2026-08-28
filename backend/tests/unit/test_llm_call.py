"""The strategy ladder for getting a structured answer out of any model.

Each scenario here is a provider failure this project actually met. They are
kept as tests rather than as fixes at the call site because the lesson was that
patching per-model is endless: the ladder has to degrade on its own.
"""

from __future__ import annotations

import pytest  # noqa: F401

from app.orchestration.llm_call import Attempt, structured_call, to_mapping, unwrap
from app.orchestration.recovery import salvage_decision


class FakeRaw:
    def __init__(self, tool_calls=None, content="", finish_reason="stop", output_tokens=10):
        self.tool_calls = tool_calls or []
        self.content = content
        self.response_metadata = {"finish_reason": finish_reason}
        self.usage_metadata = {"output_tokens": output_tokens}


def envelope(parsed, raw):
    return {"parsed": parsed, "raw": raw, "parsing_error": None}


async def _call(invoke_tools, invoke_text=None, max_tokens=100):
    return await structured_call(
        invoke_tools=invoke_tools,
        invoke_text=invoke_text,
        salvage=salvage_decision,
        required_field="next_speaker",
        max_tokens=max_tokens,
    )


class TestHappyPath:
    async def test_a_clean_tool_call_wins_on_the_first_rung(self) -> None:
        async def tools(budget):
            return envelope({"next_speaker": "a", "reasoning": "r"}, FakeRaw())

        out = await _call(tools)
        assert out.ok and out.strategy == "tools"
        assert out.value == {"next_speaker": "a", "reasoning": "r"}
        assert len(out.attempts) == 1, "no rung should be climbed unnecessarily"


class TestTruncation:
    async def test_a_truncated_reply_is_retried_with_a_larger_budget(self) -> None:
        """The failure that killed meetings: the model reasons, hits the cap,
        and the half-written tool call arrives as no tool call at all."""
        budgets: list[int] = []

        async def tools(budget):
            budgets.append(budget)
            if budget <= 100:
                return envelope(None, FakeRaw(finish_reason="length", output_tokens=100))
            return envelope({"next_speaker": "a", "reasoning": "r"}, FakeRaw())

        out = await _call(tools)
        assert out.ok and out.strategy == "tools_retry"
        assert budgets[1] > budgets[0], "the retry must actually raise the cap"

    async def test_a_non_truncated_failure_is_not_retried_identically(self) -> None:
        """Repeating the same call after a refusal buys nothing."""
        calls = []

        async def tools(budget):
            calls.append(budget)
            return envelope(None, FakeRaw(finish_reason="stop", content=""))

        async def text(budget):
            return FakeRaw(content='{"next_speaker": "a"}')

        out = await _call(tools, text)
        assert out.ok and out.strategy == "text"
        assert len(calls) == 1, "tools_retry should be skipped when nothing was truncated"


class TestProviderRefusal:
    async def test_a_rejected_call_falls_through_to_text(self) -> None:
        """`json_schema not supported` and friends: a fact about one strategy,
        not about the model's ability to answer."""

        async def tools(budget):
            raise RuntimeError("400 INVALID_REQUEST_BODY: structured outputs not support")

        async def text(budget):
            return FakeRaw(content='{"next_speaker": "a", "reasoning": "r"}')

        out = await _call(tools, text)
        assert out.ok and out.strategy == "text"

    async def test_the_rejection_is_reported_verbatim(self) -> None:
        async def tools(budget):
            raise RuntimeError("401 invalid api key")

        out = await _call(tools)
        assert not out.ok
        assert "invalid api key" in out.failure_reason()
        assert "provider rejected" in out.failure_reason()


class TestFailureReporting:
    async def test_truncation_is_named_as_truncation(self) -> None:
        async def tools(budget):
            return envelope(None, FakeRaw(finish_reason="length", output_tokens=budget))

        out = await _call(tools)
        assert not out.ok
        assert "cut off" in out.failure_reason()
        assert "token cap" in out.failure_reason()

    async def test_an_empty_body_is_named_as_empty(self) -> None:
        async def tools(budget):
            return envelope(None, FakeRaw(content="", finish_reason="stop"))

        out = await _call(tools)
        assert "empty response" in out.failure_reason()

    async def test_every_rung_is_accounted_for(self) -> None:
        """The sequence is the diagnosis: 'tools truncated then text refused' is
        a different problem from 'tools refused then text refused'."""

        async def tools(budget):
            return envelope(None, FakeRaw(finish_reason="length"))

        async def text(budget):
            raise RuntimeError("503 overloaded")

        out = await _call(tools, text)
        assert not out.ok
        assert [a.strategy for a in out.attempts] == ["tools", "tools_retry", "text"]
        assert "503 overloaded" in out.failure_reason()


class TestShapeTolerance:
    """Providers return a model instance, a bare mapping, or the envelope."""

    def test_a_plain_mapping_is_not_mistaken_for_an_envelope(self) -> None:
        """It is the parsed value, and also the only copy of the body."""
        parsed, raw, err = unwrap({"next_speaker": "a"})
        assert parsed == {"next_speaker": "a"}
        assert raw == {"next_speaker": "a"}, (
            "a non-envelope result is its own raw message; discarding it drops "
            "the body of every plain-text reply"
        )
        assert err is None

    def test_an_envelope_is_unwrapped(self) -> None:
        parsed, raw, _ = unwrap(
            {"parsed": {"next_speaker": "a"}, "raw": "R", "parsing_error": None}
        )
        assert parsed == {"next_speaker": "a"} and raw == "R"

    def test_a_pydantic_instance_is_normalised(self) -> None:
        from pydantic import BaseModel

        class D(BaseModel):
            next_speaker: str
            reasoning: str = ""

        assert to_mapping(D(next_speaker="a")) == {"next_speaker": "a", "reasoning": ""}

    async def test_a_blank_required_field_is_not_an_answer(self) -> None:
        async def tools(budget):
            return envelope({"next_speaker": "   ", "reasoning": "r"}, FakeRaw())

        out = await _call(tools)
        assert not out.ok


class TestAttemptReason:
    def test_reason_prefers_the_provider_error(self) -> None:
        a = Attempt(strategy="tools", ok=False, provider_error="boom", finish_reason="length")
        assert "boom" in a.reason()


class TestUnstructuredReplies:
    """The plain-text rung is the floor: it must work when nothing else does."""

    async def test_json_in_a_chat_message_is_recovered(self) -> None:
        """An AIMessage is itself a pydantic model, so it dumps to
        {content, type, id, ...} -- a perfectly good dict with none of the
        fields we asked for. Accepting that as the value meant the floor never
        salvaged, while the answer sat in `content` as clean JSON."""
        from langchain_core.messages import AIMessage

        async def tools(budget):
            raise RuntimeError("400 tool calling not supported")

        async def text(budget):
            return AIMessage(content='{"next_speaker": "a", "reasoning": "r"}')

        out = await _call(tools, text)
        assert out.ok and out.strategy == "text"
        assert out.value["next_speaker"] == "a"

    async def test_a_message_with_no_answer_is_still_a_failure(self) -> None:
        from langchain_core.messages import AIMessage

        async def tools(budget):
            raise RuntimeError("400 nope")

        async def text(budget):
            return AIMessage(content="I am not sure who should speak.")

        out = await _call(tools, text)
        assert not out.ok
