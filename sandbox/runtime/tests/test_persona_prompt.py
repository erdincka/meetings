"""Prompt assembly.

Regression guard for the central complaint about the original app: a dozen
persona fields were persisted and editable in the UI while reaching no prompt,
so editing a persona changed nothing about how it behaved.
"""

from __future__ import annotations

from runtime.persona import build_system_prompt, render_attendee_list
from runtime.protocol import Attendee, PersonaSpec

PERSONA = PersonaSpec(
    display_name="Jane Roe",
    title="CFO",
    department="Finance",
    summary="Owns the numbers.",
    seniority="Executive",
    responsibilities=["own the budget", "sign the accounts"],
    kpis=["EBITDA"],
    objectives=["close FY"],
    priorities=["cost control"],
    risk_tolerance="Low",
    tone=["Direct", "Data-driven"],
    collaboration_style="Consultative",
    challenge_style="Analytical",
)


def _render(template: str) -> str:
    return build_system_prompt(
        template,
        PERSONA,
        objective="Decide on the recall",
        agenda="1. Facts",
        brief="A defect was found.",
        expectations="A decision today.",
        attendee_list="- Ann Lee (CEO, Executive)",
    )


class TestFieldSubstitution:
    def test_previously_dead_fields_all_render(self) -> None:
        template = (
            "{{DISPLAY_NAME}}|{{TITLE}}|{{DEPARTMENT}}|{{SENIORITY}}|"
            "{{RISK_TOLERANCE}}|{{CHALLENGE_STYLE}}|{{COLLABORATION_STYLE}}|"
            "{{RESPONSIBILITIES}}|{{KPIS}}|{{OBJECTIVES}}|{{PRIORITIES}}"
        )
        out = _render(template)
        for expected in (
            "Jane Roe",
            "CFO",
            "Finance",
            "Executive",
            "Low",
            "Analytical",
            "Consultative",
            "own the budget",
            "EBITDA",
            "close FY",
            "cost control",
        ):
            assert expected in out, f"{expected} did not reach the prompt"
        assert "{{" not in out

    def test_meeting_context_renders(self) -> None:
        out = _render("{{OBJECTIVE}}|{{AGENDA}}|{{BRIEF}}|{{EXPECTATIONS}}")
        assert "Decide on the recall" in out
        assert "A defect was found." in out
        assert "A decision today." in out

    def test_both_attendee_list_spellings_work(self) -> None:
        """The supervisor prompt used a space, the agent prompt an underscore."""
        assert "Ann Lee" in _render("{{ATTENDEE_LIST}}")
        assert "Ann Lee" in _render("{{ATTENDEE LIST}}")

    def test_unknown_placeholder_is_left_visible(self) -> None:
        """A typo should be obvious in the transcript, not silently blank."""
        assert "{{NOT_A_FIELD}}" in _render("x {{NOT_A_FIELD}} y")

    def test_empty_lists_render_readably(self) -> None:
        sparse = PersonaSpec(display_name="Bob", title="Eng", department="IT")
        out = build_system_prompt(
            "{{RESPONSIBILITIES}}|{{TONE}}|{{RISK_TOLERANCE}}",
            sparse,
            objective="",
            agenda="",
            brief="",
            expectations="",
            attendee_list="",
        )
        assert "None specified." in out
        assert "Neutral" in out
        assert "Medium" in out
        assert "[]" not in out, "raw Python list syntax must never reach a prompt"


class TestAttendeeList:
    def test_excludes_self(self) -> None:
        attendees = [
            Attendee(id="a", display_name="Jane", title="CFO", department="Finance"),
            Attendee(id="b", display_name="Ann", title="CEO", department="Exec"),
        ]
        out = render_attendee_list(attendees, exclude_id="a")
        assert "Ann" in out and "Jane" not in out

    def test_solo_meeting(self) -> None:
        attendees = [Attendee(id="a", display_name="Jane", title="CFO", department="Finance")]
        assert render_attendee_list(attendees, "a") == "No other participants."
