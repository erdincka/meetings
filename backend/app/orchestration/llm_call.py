"""Getting a structured answer out of an arbitrary OpenAI-compatible model.

Every provider claims the same API and none of them implement the same subset
of it. Within one week this project met a provider that rejects `json_schema`
outright, one that accepts it and returns an empty body, one that gzips a
response the caller was told was plain JSON, one that treats `reasoning_effort:
none` as a reason to answer nothing, and one that reasons for the entire output
budget and gets truncated mid-tool-call. Each was fixed where it was found, and
each fix encoded an assumption about one provider.

So this stops guessing. A structured call is attempted through progressively
more permissive strategies, and the first that yields a usable answer wins:

  1. tool calling            -- the most widely implemented structured mode
  2. tool calling, larger    -- only if the last attempt was cut off at the cap
  3. plain text              -- no schema at all; ask for JSON and parse it

Plain text is the floor because a model that cannot do it is not a chat model.
The ladder is the point: nothing here requires a provider to support anything
beyond generating text, and support for more is used when it is there.

Nothing raises for model misbehaviour. Every attempt is recorded, and a caller
that ends up with nothing gets a sentence naming the actual cause -- truncation,
refusal, an unparseable body -- instead of "empty or malformed", which describes
five unrelated failures and distinguishes none of them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# How much more budget to allow when a reply was cut off at the cap. Enough to
# clear a model that reasons before answering, not so much that a runaway
# generation costs the meeting its remaining time.
TRUNCATION_RETRY_FACTOR = 3


@dataclass(frozen=True)
class Attempt:
    """One call to the model, and what came back.

    Kept whether it succeeded or not: the sequence is the diagnosis. "Tools
    truncated, then plain text worked" and "tools refused, then plain text
    worked" are the same outcome and completely different problems.
    """

    strategy: str
    ok: bool
    finish_reason: str | None = None
    output_tokens: int | None = None
    max_tokens: int | None = None
    tool_calls: str = ""
    content: str = ""
    provider_error: str | None = None

    def reason(self) -> str:
        """Why this attempt did not produce an answer, in one sentence."""
        if self.provider_error:
            return f"{self.strategy}: the provider rejected the call ({self.provider_error})"
        if self.finish_reason == "length":
            return (
                f"{self.strategy}: the reply was cut off at the {self.max_tokens}-token "
                f"cap before it was complete"
            )
        if not self.tool_calls and not self.content.strip():
            return f"{self.strategy}: the model returned an empty response"
        if not self.tool_calls:
            return f"{self.strategy}: the model answered in text that did not parse"
        return f"{self.strategy}: the structured fields could not be read"


@dataclass
class Outcome:
    """The result of the ladder: a value, or an account of why there is none."""

    value: dict[str, Any] | None = None
    strategy: str | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value is not None

    def failure_reason(self) -> str:
        if not self.attempts:
            return "no attempt was made"
        # Lead with the last thing tried; it is the most informative, and the
        # earlier ones are there to show the ladder was actually climbed.
        return "; ".join(a.reason() for a in self.attempts)


def _describe(raw: Any, max_tokens: int | None) -> dict[str, Any]:
    """Pull the diagnostic fields off a response, tolerating any shape.

    Providers disagree about where finish_reason and usage live, and a missing
    one must never be the thing that raises.
    """
    meta = getattr(raw, "response_metadata", None) or {}
    usage = getattr(raw, "usage_metadata", None) or {}
    tool_calls = getattr(raw, "tool_calls", None)
    content = getattr(raw, "content", None)
    if not isinstance(content, str):
        content = str(content) if content is not None else ""
    return {
        "finish_reason": meta.get("finish_reason"),
        "output_tokens": usage.get("output_tokens"),
        "max_tokens": max_tokens,
        "tool_calls": str(tool_calls)[:300] if tool_calls else "",
        "content": content[:300],
    }


def unwrap(result: Any) -> tuple[Any, Any, Any]:
    """Split ``with_structured_output(include_raw=True)`` into its parts.

    Returns ``(parsed, raw, parsing_error)``. A result that is not the envelope
    is treated as the parsed value itself *and* as the raw message, because it
    is both: providers return a model instance, a plain mapping, or the
    envelope, and an unstructured call returns the message directly. Reporting
    ``raw=None`` there threw away the only copy of the body -- which is the
    whole content of a plain-text reply, and so the entire point of that rung.

    A plain ``{"next_speaker": ...}`` must not be mistaken for an envelope that
    happens to lack its keys, hence the check on ``parsed`` rather than on dict.
    """
    if isinstance(result, dict) and "parsed" in result:
        return result.get("parsed"), result.get("raw"), result.get("parsing_error")
    return result, result, None


def to_mapping(value: Any) -> dict[str, Any] | None:
    """Normalise a parsed schema instance or mapping to a plain dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _has_field(value: Any, field_name: str) -> bool:
    """Does this look like the answer we asked for?

    The only definition that survives contact with real providers: a mapping
    carrying a non-blank value for the field the caller named. Anything looser
    accepts a serialised chat message as a decision.
    """
    return isinstance(value, dict) and bool(str(value.get(field_name, "") or "").strip())


async def structured_call(
    *,
    invoke_tools: Callable[[int], Any],
    invoke_text: Callable[[int], Any] | None,
    salvage: Callable[[Any, str], dict[str, Any] | None],
    required_field: str,
    max_tokens: int,
    strategies: Sequence[str] = ("tools", "tools_retry", "text"),
    log_context: dict[str, Any] | None = None,
) -> Outcome:
    """Climb the ladder until something usable comes back.

    ``invoke_tools``/``invoke_text`` take a token budget and return an awaitable,
    so the caller owns prompt construction and this owns only the strategy. Both
    may fail outright; a provider rejection is recorded and the ladder continues,
    because "tools are not supported here" is a fact about one strategy and not
    about the model's ability to answer.
    """
    ctx = log_context or {}
    outcome = Outcome()

    async def run(strategy: str, invoke: Callable[[int], Any], budget: int) -> bool:
        try:
            result = await invoke(budget)
        except Exception as exc:
            # Includes provider 4xx/5xx. Recorded, not raised: the next rung may
            # well work, and if none do the caller gets every reason at once.
            outcome.attempts.append(
                Attempt(
                    strategy=strategy,
                    ok=False,
                    max_tokens=budget,
                    provider_error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            return False

        parsed, raw, _ = unwrap(result)

        # "Usable" means it carries the field we asked for -- not merely that it
        # is not None. Anything pydantic dumps to a dict, and an unstructured
        # reply is an AIMessage, which dumps happily to {content, type, id, ...}.
        # Treating that as a value meant the plain-text rung -- the one rung that
        # needs no provider support at all -- silently never salvaged, while the
        # body sitting in `content` was perfectly good JSON.
        value = to_mapping(parsed)
        if not _has_field(value, required_field):
            value = salvage(getattr(raw, "tool_calls", None), _describe(raw, budget)["content"])

        described = _describe(raw, budget)
        got = _has_field(value, required_field)
        outcome.attempts.append(Attempt(strategy=strategy, ok=got, **described))
        if got:
            outcome.value = value
            outcome.strategy = strategy
            if strategy != strategies[0]:
                # Worth a line: the primary strategy failing is a property of
                # the provider that will keep costing an extra round trip.
                logger.warning("llm_call_fell_back", strategy=strategy, **ctx)
        return got

    for strategy in strategies:
        if strategy == "tools":
            if await run("tools", invoke_tools, max_tokens):
                return outcome
        elif strategy == "tools_retry":
            # Only meaningful after a truncation; otherwise the same call would
            # be repeated for no reason.
            last = outcome.attempts[-1] if outcome.attempts else None
            if last is not None and last.finish_reason == "length":
                if await run("tools_retry", invoke_tools, max_tokens * TRUNCATION_RETRY_FACTOR):
                    return outcome
        elif strategy == "text" and invoke_text is not None:
            if await run("text", invoke_text, max_tokens * TRUNCATION_RETRY_FACTOR):
                return outcome

    return outcome
