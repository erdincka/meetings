from collections.abc import Hashable

import structlog
from langgraph.graph import END, START, StateGraph

from app.models.roles import RoleAgent
from app.orchestration import profiles
from app.orchestration.agents import create_role_agent_node
from app.orchestration.state import MeetingState
from app.orchestration.supervisor import supervisor_node

logger = structlog.get_logger(__name__)


def validate_attendee_profiles(attendees: dict[str, RoleAgent]) -> dict[str, str]:
    """Resolve every attendee's profile, refusing the meeting on drift.

    Checked here, once, rather than when an agent first speaks. A persona whose
    configured tools no provisioned profile can supply is a mismatch between
    what the UI advertises and what the cluster will permit -- and discovering
    that three turns in, as one participant silently failing, is much worse than
    refusing to start with a precise message.
    """
    resolved: dict[str, str] = {}
    problems: list[str] = []
    for agent_id, role in attendees.items():
        try:
            resolved[agent_id] = profiles.resolve(list(role.default_tools or [])).name
        except profiles.ProfileDriftError as exc:
            problems.append(f"{role.display_name} ({role.title}): {exc}")

    if problems:
        raise profiles.ProfileDriftError(
            "Cannot start: some attendees request capabilities no profile provides.\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return resolved


def build_meeting_graph(attendees: dict[str, RoleAgent]) -> StateGraph:
    """Builds the supervisor-led StateGraph based on selected attendees."""
    builder = StateGraph(MeetingState)

    # Add supervisor
    builder.add_node("supervisor", supervisor_node)

    # Add agents and edge back to supervisor
    for agent_id in attendees.keys():
        node_func = create_role_agent_node(agent_id)
        # LangGraph's node protocol is a broad union that an explicitly typed
        # async callable does not structurally satisfy; it is invoked correctly
        # at runtime.
        builder.add_node(agent_id, node_func)  # type: ignore[arg-type]
        builder.add_edge(agent_id, "supervisor")

    builder.add_edge(START, "supervisor")

    # Routing logic from supervisor
    def router(state: MeetingState) -> str:
        next_speaker = state.get("next_speaker", "FINISH")
        if next_speaker == "FINISH":
            return "FINISH"
        if next_speaker not in attendees:
            logger.warning("invalid_router_target", target=next_speaker)
            return "FINISH"
        return next_speaker

    # Keyed Hashable to match add_conditional_edges' signature. Note the old
    # comprehension bound the loop variable to `id`, shadowing the builtin.
    valid_targets: dict[Hashable, str] = {agent_id: agent_id for agent_id in attendees}
    valid_targets["FINISH"] = END

    builder.add_conditional_edges("supervisor", router, valid_targets)

    return builder
