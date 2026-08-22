"""Attendee nodes.

Each node used to host a full ReAct loop: it built a ChatOpenAI client,
constructed tools, ran the agent, and parsed the model's output -- roughly 130
lines, all executing in the backend process alongside the database engine and
the master inference key.

The loop now runs inside that attendee's own gVisor-isolated sandbox. What
remains here is the proxy: claim a sandbox, bind the persona once, issue one
turn, map the result into graph state.

Keeping the whole loop remote (rather than RPC-ing individual tool calls back)
means model-chosen tool arguments never reach this process, and a four-step turn
costs one round trip instead of four.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from app.orchestration.prompts import DEFAULT_AGENT_PROMPT
from app.orchestration.protocol import (
    Attendee,
    ModelConfig,
    PersonaBindRequest,
    PersonaSpec,
    TurnLimits,
    TurnRequest,
    Utterance,
)
from app.orchestration.state import MeetingState, make_utterance, public_transcript
from app.sandbox.client import PersonaSandboxClient
from app.sandbox.manager import SandboxUnavailableError, manager

logger = structlog.get_logger(__name__)


def _persona_from_role(role: Any) -> PersonaSpec:
    """Map a RoleAgent row onto the wire persona.

    Every field here was persisted and editable in the UI while reaching no
    prompt at all before Phase 2.
    """
    return PersonaSpec(
        display_name=role.display_name,
        title=role.title,
        department=role.department,
        summary=role.summary,
        seniority=role.seniority,
        responsibilities=list(role.responsibilities or []),
        kpis=list(role.kpis or []),
        objectives=list(role.objectives or []),
        priorities=list(role.priorities or []),
        risk_tolerance=role.risk_tolerance,
        tone=list(role.tone or []) if isinstance(role.tone, list) else [],
        collaboration_style=role.collaboration_style,
        challenge_style=role.challenge_style,
        allowed_shared_library_access=bool(role.allowed_shared_library_access),
    )


def create_role_agent_node(
    agent_id: str,
) -> Callable[[MeetingState, RunnableConfig], Awaitable[dict[str, Any]]]:
    """Build the graph node for one attendee."""

    async def agent_node(state: MeetingState, config: RunnableConfig) -> dict[str, Any]:
        configurable = config["configurable"]
        attendees: dict[str, Any] = configurable["attendees"]
        role = attendees.get(agent_id)
        if role is None:
            logger.warning("unknown_attendee_node", agent_id=agent_id)
            return {}

        settings_obj = configurable["model_settings"]
        meeting_id = state.get("meeting_id", "")
        turn_no = state.get("current_turn", 0)

        # Idempotency key. LangGraph replays the last uncompleted node after a
        # crash-resume; without this the model call and every tool run happen
        # twice.
        turn_key = f"{meeting_id}:{turn_no}:{agent_id}"

        try:
            handle = await manager.acquire(
                meeting_id=meeting_id,
                agent_id=agent_id,
                profile=configurable.get("profile", "baseline"),
                warm_pool=configurable.get("warm_pool", "persona-baseline"),
            )
        except SandboxUnavailableError as exc:
            return _failure_state(agent_id, role, f"No sandbox available: {exc}")

        persona = _persona_from_role(role)
        bind = PersonaBindRequest(
            agent_id=agent_id,
            meeting_id=meeting_id,
            persona=persona,
            system_prompt_template=(
                getattr(settings_obj, "agent_prompt", None)
                or role.system_prompt
                or DEFAULT_AGENT_PROMPT
            ),
            granted_tools=list(role.default_tools or ["retrieve_documents"]),
            model=ModelConfig(
                endpoint=settings_obj.inference_endpoint or "",
                model_name=settings_obj.inference_model_name or "",
                temperature=settings_obj.inference_temperature,
                ignore_tls=bool(settings_obj.inference_ignore_tls),
            ),
            limits=TurnLimits(
                retrieval_limit=settings_obj.retrieval_limits_per_agent,
                max_evidence_per_message=settings_obj.max_evidence_per_message,
            ),
        )

        turn = TurnRequest(
            turn_key=turn_key,
            objective=state.get("objective", ""),
            agenda=state.get("agenda", ""),
            brief=state.get("brief", ""),
            expectations=state.get("expectations", ""),
            attendees=[
                Attendee(
                    id=aid,
                    display_name=a.display_name,
                    title=a.title,
                    department=a.department,
                )
                for aid, a in attendees.items()
            ],
            transcript=[
                Utterance(
                    speaker_id=str(m.additional_kwargs.get("agent_id", "")),
                    display_name="",
                    title="",
                    content=str(m.content),
                )
                for m in public_transcript(state.get("messages", []))
            ],
        )

        try:
            async with PersonaSandboxClient(handle.base_url) as client:
                await client.bind(bind)
                async for event in client.stream_turn(turn):
                    if event.type == "turn.error" and event.error is not None:
                        return _failure_state(agent_id, role, event.error.message)
                    if event.type == "turn.result" and event.result is not None:
                        return _success_state(agent_id, handle.sandbox_name, event.result)
        except Exception as exc:
            logger.error("sandbox_turn_failed", agent_id=agent_id, error=str(exc))
            return _failure_state(agent_id, role, str(exc))

        return _failure_state(agent_id, role, "Sandbox closed the stream without a result")

    return agent_node


def _success_state(agent_id: str, sandbox_name: str, result: Any) -> dict[str, Any]:
    audit = [
        {
            "turn_key": result.turn_key,
            "agent_id": agent_id,
            "tool": tr.name,
            "ok": tr.ok,
            "denied_reason": tr.denied_reason,
            "duration_ms": tr.duration_ms,
        }
        for tr in result.tool_results
    ]
    return {
        "messages": [make_utterance(result.public_content, agent_id)],
        "event_log": [
            {
                "type": "agent_spoke",
                "agent_id": agent_id,
                "content": result.public_content,
                "private_reasoning": result.private_reasoning,
                "sandbox": sandbox_name,
                "tool_calls": [tc.model_dump() for tc in result.tool_calls],
            }
        ],
        "tool_audit": audit,
        "sandboxes": {agent_id: sandbox_name},
        "active_agent_id": agent_id,
    }


def _failure_state(agent_id: str, role: Any, message: str) -> dict[str, Any]:
    """A failed turn is recorded, not swallowed.

    The meeting continues -- one attendee failing to speak should not end it --
    but the failure is visible in the transcript and the event log rather than
    appearing as silence.
    """
    content = f"[{role.display_name} - {role.title}] (unable to contribute this turn)"
    logger.error("agent_turn_failed", agent_id=agent_id, reason=message)
    return {
        "messages": [make_utterance(content, agent_id)],
        "event_log": [
            {
                "type": "agent_failed",
                "agent_id": agent_id,
                "content": content,
                "private_reasoning": message,
            }
        ],
        "tool_audit": [{"agent_id": agent_id, "tool": None, "ok": False, "error": message}],
        "active_agent_id": agent_id,
    }
