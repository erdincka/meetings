"""Graph state.

The graph itself never leaves the backend: the supervisor, the router, this
state and the checkpointer all live here, and sandboxes execute individual
turns. Distributed checkpointing across sandboxes would be a research project,
not a demo.
"""

import operator
from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage

# Marks a message as something an attendee said out loud, as opposed to internal
# reasoning or a tool exchange.
#
# Both the supervisor and the agent node used to select public utterances with
# `content.startswith("[")`, which meant any agent whose sentence happened to
# open with a bracket poisoned the routing history. Metadata is unambiguous.
UTTERANCE_KIND = "utterance"


def merge_list(a: list[Any], b: list[Any]) -> list[Any]:
    if not a:
        return b
    if not b:
        return a
    return a + b


def last_write_wins(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Merge two dict updates, preferring the newer value per key."""
    return {**(a or {}), **(b or {})}


def make_utterance(content: str, agent_id: str) -> AIMessage:
    return AIMessage(
        content=content,
        additional_kwargs={"kind": UTTERANCE_KIND, "agent_id": agent_id},
    )


def is_utterance(message: BaseMessage) -> bool:
    return (
        isinstance(message, AIMessage) and message.additional_kwargs.get("kind") == UTTERANCE_KIND
    )


def public_transcript(messages: Sequence[BaseMessage]) -> list[AIMessage]:
    """Only what was actually said, in order."""
    return [m for m in messages if isinstance(m, AIMessage) and is_utterance(m)]


class MeetingState(TypedDict, total=False):
    meeting_id: str
    brief: str
    agenda: str
    objective: str
    expectations: str
    selected_attendee_ids: list[str]
    turn_limit: int
    current_turn: int

    messages: Annotated[Sequence[BaseMessage], operator.add]
    event_log: Annotated[list[dict[str, Any]], merge_list]

    # Artifacts produced during the meeting (charts, drafts). Populated from
    # Phase 3 onward; declared here so the reducer exists before the producers.
    artifacts: Annotated[list[dict[str, Any]], merge_list]

    # Every tool call and refusal, including denials. This is what makes least
    # privilege visible in the UI rather than merely asserted.
    tool_audit: Annotated[list[dict[str, Any]], merge_list]

    # agent_id -> sandbox name, so a re-entered node reuses its sandbox.
    sandboxes: Annotated[dict[str, str], last_write_wins]

    active_agent_id: str | None
    stop_requested: bool
    terminated: bool
    final_summary: str | None
    next_speaker: str | None
