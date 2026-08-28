"""Prompt assembly for a bound persona.

Roughly a dozen RoleAgent fields were once persisted, editable in
the UI, and read by nothing: responsibilities, kpis, objectives, seniority,
risk_tolerance, challenge_style and system_prompt among them. Only display_name,
title, department, summary, tone and collaboration_style ever reached a prompt.

Every field is substituted here, so editing a persona changes how it behaves.
"""

from __future__ import annotations

from .protocol import Attendee, PersonaSpec


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "None specified."


def _join(items: list[str], empty: str = "Unspecified") -> str:
    return ", ".join(items) if items else empty


def render_attendee_list(attendees: list[Attendee], exclude_id: str) -> str:
    others = [a for a in attendees if a.id != exclude_id]
    if not others:
        return "No other participants."
    return "\n".join(f"- {a.display_name} ({a.title}, {a.department})" for a in others)


def build_system_prompt(
    template: str,
    persona: PersonaSpec,
    *,
    objective: str,
    agenda: str,
    brief: str,
    expectations: str,
    attendee_list: str,
    tools: str = "",
) -> str:
    """Substitute {{PLACEHOLDER}} tokens.

    Unknown placeholders are left untouched rather than blanked, so a typo in a
    custom prompt is visible in the transcript instead of silently erasing the
    instruction.
    """
    substitutions = {
        "DISPLAY_NAME": persona.display_name,
        "TITLE": persona.title,
        "DEPARTMENT": persona.department,
        "SUMMARY": persona.summary or "",
        "PERSONA_GUIDANCE": persona.guidance or "",
        "SENIORITY": persona.seniority or "Unspecified",
        "TONE": _join(persona.tone, "Neutral"),
        "COLLABORATION_STYLE": persona.collaboration_style or "Collaborative",
        "CHALLENGE_STYLE": persona.challenge_style or "Constructive",
        "RISK_TOLERANCE": persona.risk_tolerance or "Medium",
        "RESPONSIBILITIES": _bullets(persona.responsibilities),
        "KPIS": _bullets(persona.kpis),
        "OBJECTIVES": _bullets(persona.objectives),
        "PRIORITIES": _bullets(persona.priorities),
        "OBJECTIVE": objective,
        "AGENDA": agenda,
        "BRIEF": brief,
        "EXPECTATIONS": expectations,
        "ATTENDEE_LIST": attendee_list,
        # The tools this persona actually holds. Substituted from the runtime's
        # own active set rather than the requested grant, so the prompt can
        # never advertise a capability the sandbox was not provisioned for.
        "TOOLS": tools or "You have no tools available.",
        # The supervisor prompt has always used a space here while the agent
        # prompt used an underscore. Both are accepted so neither silently
        # fails to substitute.
        "ATTENDEE LIST": attendee_list,
    }

    rendered = template
    for key, value in substitutions.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered
