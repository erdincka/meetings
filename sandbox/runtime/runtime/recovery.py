"""Pure helpers for interpreting model output.

Extracted from ``supervisor.py`` and ``agents.py`` so they can be tested
without a live LLM. Both paths existed before but had never been exercised.
"""

from __future__ import annotations

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


def split_thought(content: str | None) -> tuple[str, str]:
    """Separate a model's internal monologue from what it says out loud.

    Returns ``(public_text, thought)``. Agents are prompted to wrap private
    reasoning in <thought>/<thinking>; anything left in the public text would
    leak reasoning into the meeting transcript.
    """
    if not content:
        return "", ""

    thoughts = _THOUGHT_RE.findall(content)
    public = _THOUGHT_RE.sub("", content).strip()
    return public, "\n\n".join(t.strip() for t in thoughts if t.strip())


def strip_speaker_prefix(content: str) -> str:
    """Drop a hallucinated ``[Name - Title]`` prefix.

    The transcript prefix is added by the application; when the model emits one
    too the result is doubled.
    """
    return _LEADING_TAG_RE.sub("", content).strip()
