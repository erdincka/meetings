"""Prompt assembly.

Regression guard for the central complaint about the original app: a dozen
persona fields were persisted and editable in the UI while reaching no prompt,
so editing a persona changed nothing about how it behaved.
"""

from __future__ import annotations

from runtime.persona import build_system_prompt, render_attendee_list
from runtime.prompts import DEFAULT_AGENT_PROMPT
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
        tools="- `retrieve_documents`: search the library",
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


class TestShippedPromptIsComplete:
    """The default prompt must actually use the persona it is given.

    The regression this guards against: every field below was
    persisted, editable in the UI, and referenced by no template -- so editing a
    persona changed precisely nothing about how it behaved. Substituting a value
    nobody interpolates is not the same as using it.
    """

    def test_every_persona_field_reaches_the_default_prompt(self) -> None:
        from runtime.protocol import PersonaSpec

        # Distinctive values, so a match cannot be coincidental.
        persona = PersonaSpec(
            display_name="Jane Roe",
            title="CFO",
            department="Finance",
            summary="SUMMARY-MARKER",
            seniority="SENIORITY-MARKER",
            responsibilities=["RESP-MARKER"],
            kpis=["KPI-MARKER"],
            objectives=["OBJ-MARKER"],
            priorities=["PRIO-MARKER"],
            risk_tolerance="RISK-MARKER",
            tone=["TONE-MARKER"],
            collaboration_style="COLLAB-MARKER",
            challenge_style="CHALLENGE-MARKER",
        )
        out = build_system_prompt(
            DEFAULT_AGENT_PROMPT,
            persona,
            objective="OBJECTIVE-MARKER",
            agenda="AGENDA-MARKER",
            brief="BRIEF-MARKER",
            expectations="EXPECT-MARKER",
            attendee_list="ATTENDEE-MARKER",
            tools="TOOLS-MARKER",
        )
        for marker in (
            "SUMMARY-MARKER",
            "SENIORITY-MARKER",
            "RESP-MARKER",
            "KPI-MARKER",
            "OBJ-MARKER",
            "PRIO-MARKER",
            "RISK-MARKER",
            "TONE-MARKER",
            "COLLAB-MARKER",
            "CHALLENGE-MARKER",
            "OBJECTIVE-MARKER",
            "AGENDA-MARKER",
            "BRIEF-MARKER",
            "EXPECT-MARKER",
            "ATTENDEE-MARKER",
            "TOOLS-MARKER",
        ):
            assert marker in out, f"{marker} never reached the prompt"
        assert "{{" not in out, "an unsubstituted placeholder remains"

    def test_prompt_instructs_the_agent_to_use_tools(self) -> None:
        """Agents talked instead of acting because nothing told them to act."""
        assert "USE YOUR TOOLS" in DEFAULT_AGENT_PROMPT

    def test_prompt_tells_the_agent_what_a_refusal_means(self) -> None:
        """A denied tool should read as a policy decision, not a malfunction."""
        assert "refuses" in DEFAULT_AGENT_PROMPT.lower()


class TestPersonaGuidanceIsContentNotTemplate:
    """A persona's own notes must not displace the structured prompt.

    Regression: RoleAgent.system_prompt was passed as the whole template, so a
    persona carrying ~200 characters of flavour text lost every placeholder --
    responsibilities, KPIs, the objective, and the tool guidance that tells the
    agent it can act at all. Agents talked and never called a tool.
    """

    def test_guidance_appears_in_the_rendered_prompt(self) -> None:
        from runtime.protocol import PersonaSpec

        persona = PersonaSpec(
            display_name="Jane Roe",
            title="CFO",
            department="Finance",
            guidance="GUIDANCE-MARKER",
            responsibilities=["RESP-MARKER"],
        )
        out = build_system_prompt(
            DEFAULT_AGENT_PROMPT,
            persona,
            objective="o",
            agenda="a",
            brief="b",
            expectations="e",
            attendee_list="x",
            tools="TOOLS-MARKER",
        )
        # The guidance is present *and* so is everything it used to displace.
        assert "GUIDANCE-MARKER" in out
        assert "RESP-MARKER" in out
        assert "TOOLS-MARKER" in out

    def test_absent_guidance_leaves_no_placeholder_behind(self) -> None:
        from runtime.protocol import PersonaSpec

        out = build_system_prompt(
            DEFAULT_AGENT_PROMPT,
            PersonaSpec(display_name="A", title="B", department="C"),
            objective="o",
            agenda="a",
            brief="b",
            expectations="e",
            attendee_list="x",
            tools="t",
        )
        assert "{{" not in out
