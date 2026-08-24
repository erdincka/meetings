"""Tool guidance shown to agents.

Vendored from backend/app/orchestration/profiles.py by sync-shared.sh so the
sandbox image carries no dependency on the backend package. CI diffs the copies.
"""

from __future__ import annotations

# What each tool is for, in the words the agent sees.
#
# Kept here rather than in the prompt text because the prompt is operator-
# editable and the tool catalogue is not: a persona should never be told about a
# capability its profile does not provide, and the two would drift the moment
# someone edited one and not the other.
TOOL_GUIDANCE: dict[str, str] = {
    "retrieve_documents": (
        "search the company's document library for evidence -- use it before "
        "asserting any fact about company history, policy or prior decisions"
    ),
    "query_business_metrics": (
        "run read-only SQL against the business metrics warehouse -- use it "
        "whenever a number is in question rather than estimating"
    ),
    "run_python_analysis": (
        "write and run Python to analyse data or produce a chart -- use it when "
        "a trend or comparison would be clearer as a figure than a sentence"
    ),
    "check_policy_compliance": (
        "check a draft against compliance rule packs -- use it before any text "
        "is circulated outside this meeting"
    ),
    "search_corpus": (
        "search external industry literature -- use it for benchmarks, "
        "regulatory expectations and comparable cases"
    ),
    "draft_artifact": (
        "write a document, note or table into the meeting record -- use it when "
        "something is worth keeping beyond the transcript"
    ),
    "read_artifact": "read back something produced earlier in this meeting",
    "record_action_item": (
        "record a commitment with an owner -- use it whenever the meeting "
        "agrees someone will do something"
    ),
}


def render_tool_guidance(tools: list[str]) -> str:
    """A prompt-ready description of the tools a persona actually holds."""
    granted = [t for t in sorted(tools) if t in TOOL_GUIDANCE]
    if not granted:
        return "You have no tools available. Contribute from your own expertise."
    return "\n".join(f"- `{name}`: {TOOL_GUIDANCE[name]}" for name in granted)
