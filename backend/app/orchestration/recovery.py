"""Pure helpers for interpreting model output.

Extracted from ``supervisor.py`` and ``agents.py`` so they can be tested
without a live LLM. Both paths existed before but had never been exercised.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

# A model returning a very short string ("a", "the", "") used to fuzzy-match
# the first attendee whose name merely contained it, silently handing the turn
# to an arbitrary person. Require enough signal to be meaningful.
MIN_FUZZY_MATCH_LEN = 3

FINISH = "FINISH"


def as_text(content: object) -> str:
    """Flatten a LangChain message ``content`` to plain text.

    ``BaseMessage.content`` is ``str | list[str | dict]`` -- multimodal models
    return content blocks. Every call site here wants the text, so the union is
    resolved once rather than being guessed at (and mis-typed) in five places.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


_THOUGHT_RE = re.compile(r"<(?:thought|thinking)>(.*?)</(?:thought|thinking)>", re.DOTALL)
# An opening tag with no closing one. Models truncated at max_tokens emit
# these, and treating the remainder as public text leaks the whole monologue
# into the transcript -- which is what happened to a General Counsel turn.
_UNCLOSED_THOUGHT_RE = re.compile(r"<(?:thought|thinking)>(.*)\Z", re.DOTALL)
_LEADING_TAG_RE = re.compile(r"^\[.*?\]\s*")


class AttendeeLike(Protocol):
    display_name: str
    title: str


def recover_speaker_id(
    raw: str | None,
    valid_ids: set[str],
    attendees: dict[str, AttendeeLike],
) -> str:
    """Map a supervisor's chosen speaker onto a real attendee id.

    Small models routinely return a display name or title instead of the UUID
    they were asked for, so an exact-match-only rule ends meetings early. This
    resolves the common near-misses and falls back to ``FINISH`` rather than
    routing to an arbitrary agent.
    """
    if raw is None:
        return FINISH

    candidate = raw.strip()
    if not candidate:
        return FINISH

    if candidate in valid_ids:
        return candidate

    if candidate.upper() == FINISH:
        return FINISH

    needle = candidate.lower()
    if len(needle) < MIN_FUZZY_MATCH_LEN:
        return FINISH

    # Prefer an unambiguous match. Returning the *first* of several candidates
    # picks a speaker essentially at random, so ambiguity ends the meeting
    # instead -- a visible, correct outcome rather than a silent wrong one.
    matches = [
        aid
        for aid, a in attendees.items()
        if needle in a.display_name.lower() or needle in a.title.lower() or needle in aid.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    return FINISH


def salvage_decision(
    tool_calls: list[dict] | None,
    content: str | None,
) -> dict[str, str] | None:
    """Pull a chair's decision out of a response strict parsing threw away.

    ``with_structured_output`` returns ``None`` for anything that does not
    validate, which collapses several very different situations into one: a
    model that emitted no tool call at all, one that emitted a tool call with a
    field missing, and one that answered in prose. The first is unrecoverable;
    the other two usually contain a perfectly good speaker id.

    Without this the supervisor had a recovery helper it could never reach --
    ``recover_speaker_id`` exists precisely for models that do not comply, and
    a ``None`` decision meant nothing ever reached it. Three retries later the
    chair gave up and the meeting ended at turn 0 having produced nothing, with
    a log line that quoted none of what the model actually said.

    Returns a raw ``{next_speaker, reasoning}`` mapping, still unvalidated: the
    caller resolves the id, because an id salvaged from prose deserves the same
    scrutiny as one that arrived properly.
    """
    for call in tool_calls or []:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        speaker = args.get("next_speaker")
        if isinstance(speaker, str) and speaker.strip():
            return {
                "next_speaker": speaker.strip(),
                "reasoning": str(args.get("reasoning") or "").strip(),
            }

    text = (content or "").strip()
    if not text:
        return None

    # A model that meant to call the tool but wrote it out instead.
    #
    # Tool calling is a convention layered on text generation, and when a
    # provider does not parse a model's chosen encoding the markup arrives here
    # verbatim with `tool_calls` empty. These are the encodings in common use;
    # none is specific to one model, and a decision written in any of them is
    # still a decision. Anything unrecognised is left alone -- guessing at prose
    # would let an id merely mentioned in reasoning become the routing decision.
    for extract in (_json_object, _fenced_json, _arg_key_markup, _function_markup):
        found = extract(text)
        if found:
            speaker = found.get("next_speaker")
            if isinstance(speaker, str) and speaker.strip():
                return {
                    "next_speaker": speaker.strip(),
                    "reasoning": str(found.get("reasoning") or "").strip(),
                }
    return None


def _loads(blob: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_object(text: str) -> dict[str, object] | None:
    """A bare JSON object, possibly with prose around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return _loads(match.group(0)) if match else None


def _fenced_json(text: str) -> dict[str, object] | None:
    """```json ... ``` -- the habit of every instruction-tuned model."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    return _loads(match.group(1)) if match else None


def _arg_key_markup(text: str) -> dict[str, object] | None:
    """``<arg_key>name</arg_key><arg_value>value</arg_value>`` pairs.

    Emitted inside a ``<tool_call>`` block by several open-weight families when
    the serving stack does not translate them into a real tool call.
    """
    pairs = re.findall(
        r"<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
        text,
        re.DOTALL,
    )
    return dict(pairs) if pairs else None


def _function_markup(text: str) -> dict[str, object] | None:
    """``<function=Name>{...}</function>``, the other common textual form."""
    match = re.search(r"<function=[^>]*>\s*(\{.*?\})\s*</function>", text, re.DOTALL)
    return _loads(match.group(1)) if match else None


def split_thought(content: str | None) -> tuple[str, str]:
    """Separate a model's internal monologue from what it says out loud.

    Returns ``(public_text, thought)``. Agents are prompted to wrap private
    reasoning in <thought>/<thinking>; anything left in the public text would
    leak reasoning into the meeting transcript.
    """
    if not content:
        return "", ""

    thoughts = _THOUGHT_RE.findall(content)
    public = _THOUGHT_RE.sub("", content)

    # Anything after an unclosed opening tag is reasoning too. Erring the other
    # way publishes it.
    unclosed = _UNCLOSED_THOUGHT_RE.search(public)
    if unclosed:
        thoughts.append(unclosed.group(1))
        public = public[: unclosed.start()]

    return public.strip(), "\n\n".join(t.strip() for t in thoughts if t.strip())


def strip_speaker_prefix(content: str) -> str:
    """Drop a hallucinated ``[Name - Title]`` prefix.

    The transcript prefix is added by the application; when the model emits one
    too the result is doubled.
    """
    return _LEADING_TAG_RE.sub("", content).strip()
