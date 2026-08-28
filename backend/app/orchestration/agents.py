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

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from app.core.config import settings
from app.core.telemetry import (
    LLM_TOKENS,
    MEETING_TURNS,
    SANDBOX_ACQUIRE,
    SANDBOXES_ACTIVE,
    TURN_DURATION,
    record_tool_result,
)
from app.orchestration import profiles
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
from app.services import turn_cache

logger = structlog.get_logger(__name__)


def _persona_from_role(role: Any) -> PersonaSpec:
    """Map a RoleAgent row onto the wire persona.

    Every field here is persisted, editable in the UI, and reaches a prompt.
    Keeping that true is what this mapping is for: a field that is editable and
    inert is worse than one that does not exist, because it invites an operator
    to tune something that changes nothing.
    """
    return PersonaSpec(
        display_name=role.display_name,
        title=role.title,
        department=role.department,
        guidance=role.system_prompt,
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

        # Durable replay guard. The sandbox keeps an in-memory copy too, but
        # that dies with the sandbox; this survives a backend restart.
        cached = await turn_cache.lookup(turn_key)
        if cached is not None:
            return cached

        # The persona's requested tools decide which profile it runs as, and the
        # profile decides which SandboxTemplate, ServiceAccount and
        # NetworkPolicy its sandbox is built from. Drift is caught at meeting
        # start (see graph construction), so by here it is safe to resolve.
        profile = profiles.resolve(list(role.default_tools or []))
        turn_started = time.monotonic()

        persona = _persona_from_role(role)
        bind = PersonaBindRequest(
            agent_id=agent_id,
            meeting_id=meeting_id,
            persona=persona,
            # role.system_prompt used to sit in this position, so a persona
            # with any notes at all replaced the entire structured template
            # with ~200 characters of flavour text. Every placeholder went with
            # it: no responsibilities, no KPIs, no tool guidance -- which is why
            # agents never called a tool. It is persona guidance now, carried
            # inside the template (see PersonaSpec.guidance). Only an operator
            # editing the prompt itself replaces the template.
            system_prompt_template=(
                getattr(settings_obj, "agent_prompt", None) or DEFAULT_AGENT_PROMPT
            ),
            # Grant the profile's full tool set rather than only what the
            # persona asked for: resolution already picked the smallest profile
            # that covers the request, and the runtime intersects this with its
            # own capability file regardless.
            granted_tools=sorted(profile.tools),
            model=ModelConfig(
                timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
                # The proxy, not the provider. A sandbox has no route off the
                # cluster and no provider credential; it authenticates to this
                # backend with its ServiceAccount token and the backend forwards.
                # Falling back to the provider endpoint keeps a runtime running
                # outside the cluster working.
                endpoint=settings.SANDBOX_LLM_PROXY_URL or settings_obj.inference_endpoint or "",
                model_name=settings_obj.inference_model_name or "",
                temperature=settings_obj.inference_temperature,
                ignore_tls=bool(settings_obj.inference_ignore_tls),
                reasoning_effort=settings.INFERENCE_REASONING_EFFORT,
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
                    # Content already carries a "[Name - Title]" prefix.
                    content=str(m.content),
                )
                for m in public_transcript(state.get("messages", []))
            ],
            # The chair's reasoning for picking this speaker, so the agent knows
            # why it was called on rather than guessing.
            directive=str(state.get("next_speaker_reason") or ""),
        )

        # One retry, and exactly one, against a replacement sandbox.
        #
        # A pod can be evicted, drained or OOM-killed between the lease being
        # taken and the turn being issued, and the symptom is a transport error
        # against an address that no longer answers. Nothing durable lives in a
        # sandbox, so the recovery is to forget the lease, claim another, replay
        # the bind and re-issue the turn -- the turn_results table is what makes
        # re-issuing safe.
        #
        # Retrying more than once would be worse than not retrying at all: a
        # warm pool that is genuinely empty would be hammered by every attendee
        # in turn, and the meeting would stall instead of recording a failure
        # the transcript can show.
        last_error = "Sandbox closed the stream without a result"
        for attempt in range(2):
            try:
                handle = await _lease_sandbox(meeting_id, agent_id, profile.name)
            except SandboxUnavailableError as exc:
                MEETING_TURNS.labels(profile=profile.name, outcome="no_sandbox").inc()
                return _failure_state(agent_id, role, f"No sandbox available: {exc}")

            try:
                # Released by the meeting executor when the meeting ends, not
                # here: this sandbox serves every turn this persona takes.
                async with PersonaSandboxClient(handle.base_url) as client:
                    if manager.needs_bind(meeting_id, agent_id):
                        await client.bind(bind)
                        manager.mark_bound(meeting_id, agent_id)
                    async for event in client.stream_turn(turn):
                        if event.type == "turn.error" and event.error is not None:
                            # The sandbox answered; the turn failed inside it.
                            # Not a lost pod, so not something a new pod fixes.
                            MEETING_TURNS.labels(profile=profile.name, outcome="error").inc()
                            return _failure_state(agent_id, role, event.error.message)
                        if event.type == "turn.result" and event.result is not None:
                            _record_turn_metrics(profile.name, event.result, turn_started)
                            state_update = _success_state(
                                agent_id, handle.sandbox_name, event.result
                            )
                            await turn_cache.record(
                                turn_key, meeting_id, agent_id, _serialisable(state_update)
                            )
                            return state_update
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "sandbox_turn_failed",
                    agent_id=agent_id,
                    sandbox=handle.sandbox_name,
                    attempt=attempt,
                    error=last_error,
                    exc_info=True,
                )
                await manager.invalidate(meeting_id, agent_id)
                if attempt == 0:
                    continue
                MEETING_TURNS.labels(profile=profile.name, outcome="error").inc()
                return _failure_state(agent_id, role, last_error)

            # A stream that ended without a result is not a transport failure;
            # a replacement pod would end it the same way.
            break

        MEETING_TURNS.labels(profile=profile.name, outcome="error").inc()
        return _failure_state(agent_id, role, last_error)

    return agent_node


async def _lease_sandbox(meeting_id: str, agent_id: str, profile: str) -> Any:
    """Take (or reuse) this persona's sandbox, timing only the real claims.

    Reusing a lease costs nothing, so folding it into the acquisition histogram
    would make the warm-pool claim latency look far better than it is -- the
    metric exists to show whether the pool is actually warm.
    """
    reused = not manager.needs_bind(meeting_id, agent_id)
    started = time.monotonic()
    handle = await manager.acquire(
        meeting_id=meeting_id,
        agent_id=agent_id,
        profile=profile,
        warm_pool=f"persona-{profile}",
    )
    if not reused:
        SANDBOX_ACQUIRE.labels(profile=profile).observe(time.monotonic() - started)
        SANDBOXES_ACTIVE.labels(profile=profile).inc()
    return handle


def _record_turn_metrics(profile: str, result: Any, started: float) -> None:
    """Record the outcome of a completed turn.

    Denials are counted separately from errors. Collapsing them would hide the
    one signal this project exists to surface: the cluster refusing a persona
    that overstepped.
    """
    TURN_DURATION.labels(profile=profile).observe(time.monotonic() - started)
    MEETING_TURNS.labels(profile=profile, outcome="ok").inc()

    for tool_result in result.tool_results:
        record_tool_result(
            profile=profile,
            tool=tool_result.name,
            ok=tool_result.ok,
            denied=bool(tool_result.denied_reason),
        )

    usage = getattr(result, "usage", None)
    if usage is not None:
        LLM_TOKENS.labels(profile=profile, direction="prompt").inc(usage.prompt_tokens)
        LLM_TOKENS.labels(profile=profile, direction="completion").inc(usage.completion_tokens)


def _serialisable(state_update: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy of a state update, for the turn store.

    LangChain message objects are not JSON-serialisable, so the utterance is
    stored as its text and rebuilt on replay.
    """
    stored = {k: v for k, v in state_update.items() if k != "messages"}
    messages = state_update.get("messages") or []
    stored["_utterances"] = [
        {"content": str(m.content), "agent_id": m.additional_kwargs.get("agent_id", "")}
        for m in messages
    ]
    return stored


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
