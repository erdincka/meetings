"""Seeded personas must carry the fields the prompt interpolates.

The seed used to give every persona one templated summary, formulaic
priorities, and no responsibilities, KPIs or objectives at all -- so those
placeholders rendered empty no matter how correct the prompt was. A persona
added without depth would silently reintroduce that.
"""

from __future__ import annotations

from app.orchestration import profiles
from scripts.seed import PERSONA_DEPTH, SEED_PERSONAS

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


def test_titles_are_unique() -> None:
    """The chair distinguishes attendees by this field.

    It is handed one line per attendee -- Name, Role, Department -- and must
    answer with an id. Five personas titled "Manager" gave it five rows that
    differed only by a UUID, and the model reliably returned the id it had seen
    most recently, which is whoever spoke last. The chair then looked like it
    was contradicting itself: reasoning about one attendee, selecting another.
    """
    titles = [p["title"] for p in SEED_PERSONAS]
    duplicated = sorted({t for t in titles if titles.count(t) > 1})
    assert not duplicated, f"titles shared by more than one persona: {duplicated}"


def test_every_persona_has_depth_and_a_profile() -> None:
    """Title is also the capability grant, via profiles.for_persona()."""
    for persona in SEED_PERSONAS:
        assert persona["name"] in PERSONA_DEPTH, f"{persona['name']} has no depth entry"
        profile = profiles.for_persona(persona["title"])
        assert profile.name in {p.name for p in profiles.PROFILES}


def test_seed_titles_referenced_by_profiles_actually_exist() -> None:
    """A profile naming a title no persona carries grants nothing to anyone, and
    reads as though it does."""
    titles = {p["title"] for p in SEED_PERSONAS}
    for profile in profiles.PROFILES:
        for title in profile.seed_titles:
            assert title in titles, (
                f"profile {profile.name!r} seeds title {title!r}, "
                "which no persona in SEED_PERSONAS carries"
            )
