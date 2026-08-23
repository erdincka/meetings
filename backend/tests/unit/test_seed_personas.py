"""Seeded personas must carry the fields the prompt interpolates.

The seed used to give every persona one templated summary, formulaic
priorities, and no responsibilities, KPIs or objectives at all -- so those
placeholders rendered empty no matter how correct the prompt was. A persona
added without depth would silently reintroduce that.
"""

from __future__ import annotations

from scripts.seed import PERSONA_DEPTH

REQUIRED = ("summary", "responsibilities", "kpis", "objectives", "priorities", "guidance")


def test_every_persona_carries_every_field() -> None:
    for name, depth in PERSONA_DEPTH.items():
        for field in REQUIRED:
            assert depth.get(field), f"{name} is missing {field}"


def test_personas_are_distinct_not_templated() -> None:
    """Formulaic text is what the old seed produced; it teaches the model nothing."""
    summaries = [d["summary"] for d in PERSONA_DEPTH.values()]
    assert len(set(summaries)) == len(summaries), "two personas share a summary"

    guidance = [d["guidance"] for d in PERSONA_DEPTH.values()]
    assert len(set(guidance)) == len(guidance), "two personas share their guidance"


def test_guidance_is_not_a_prompt_template() -> None:
    """Guidance is content inside the template, never a replacement for it.

    A placeholder here would render literally, since nothing substitutes into
    persona guidance.
    """
    for name, depth in PERSONA_DEPTH.items():
        assert "{{" not in depth["guidance"], f"{name} guidance contains a placeholder"
