"""Termination honours the operator's cleanup rule.

``cleanup_rules`` was persisted and editable while nothing read it, so every
termination kept the full transcript no matter what the operator had chosen.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.cleanup import (
    KEEP_HISTORY,
    PURGE_TRANSCRIPT,
    apply_cleanup_rules,
)


def _meeting() -> SimpleNamespace:
    return SimpleNamespace(
        id="m-1",
        meeting_log=[{"type": "message", "content": "something said"}],
        final_summary="a decision was reached",
    )


def test_keep_history_leaves_the_transcript_alone() -> None:
    m = _meeting()
    apply_cleanup_rules(m, KEEP_HISTORY)  # type: ignore[arg-type]
    assert m.meeting_log
    assert m.final_summary == "a decision was reached"


def test_purge_drops_the_transcript_but_keeps_the_meeting() -> None:
    m = _meeting()
    apply_cleanup_rules(m, PURGE_TRANSCRIPT)  # type: ignore[arg-type]
    assert m.meeting_log == []
    assert m.final_summary is None
    assert m.id == "m-1", "the meeting itself must survive"


def test_an_unknown_rule_keeps_history() -> None:
    """Losing a transcript to a typo is not a recoverable mistake."""
    m = _meeting()
    apply_cleanup_rules(m, "not-a-real-rule")  # type: ignore[arg-type]
    assert m.meeting_log
    assert m.final_summary == "a decision was reached"
