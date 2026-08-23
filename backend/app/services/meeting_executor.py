import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import select

from app.core import database
from app.core.config import settings
from app.models.meetings import Meeting
from app.models.roles import RoleAgent
from app.orchestration.graph import build_meeting_graph
from app.sandbox.reaper import release_meeting_sandboxes
from app.services.settings_service import get_runtime_settings

logger = structlog.get_logger(__name__)


# Generous: only the first call does real work, and failing here refuses the
# meeting outright.
CHECKPOINTER_SETUP_TIMEOUT = 60


def _now_iso() -> str:
    """UTC timestamp in the Z-suffixed form the frontend expects."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def run_meeting_execution(meeting_id: str) -> AsyncGenerator[dict[str, Any]]:
    """Runs a meeting session using a StateGraph and yields serializable UI events."""
    async with database.require_session_maker()() as session:
        # 1. Fetch & Validate Simulation Target
        meeting_uuid = UUID(meeting_id)
        query = select(Meeting).where(Meeting.id == meeting_uuid)
        result = await session.execute(query)
        meeting = result.scalar_one_or_none()

        if not meeting or meeting.status not in ["queued", "running", "draft"]:
            yield {
                "type": "error",
                "content": "Simulation target unreachable or invalid status.",
                "timestamp": _now_iso(),
            }
            return

        # 2. Transition State
        meeting.status = "running"
        yield {"type": "meeting_started", "meeting_id": meeting_id, "timestamp": _now_iso()}

        # 3. Configure Orchestration
        attendee_query = select(RoleAgent).where(RoleAgent.id.in_(meeting.selected_attendee_ids))
        attendees = {str(r.id): r for r in (await session.execute(attendee_query)).scalars().all()}
        system_settings = await get_runtime_settings(session)

        graph = build_meeting_graph(attendees)

        initial_state = {
            "meeting_id": str(meeting.id),
            "brief": meeting.brief or "",
            "agenda": meeting.agenda or "",
            "objective": meeting.objective or "",
            "expectations": meeting.expectations or "",
            "turn_limit": meeting.turn_limit or system_settings.default_turn_limit,
            "current_turn": meeting.current_turn or 0,
            "messages": [],
            "event_log": [],
        }

        thread_config = {
            "configurable": {
                "thread_id": meeting_id,
                "model_settings": system_settings,
                "app_settings": system_settings,
                "attendees": attendees,
            }
        }

        # 4. Resolve Persistent Checkpointer
        pg_url = settings.DATABASE_URL.replace("+asyncpg", "") if settings.DATABASE_URL else None

        async def _execute_with_checkpointer(cp: Any):
            """Executes graph with provided checkpointer and yields events."""
            app_graph = graph.compile(checkpointer=cp)
            has_error = False
            accumulated_events = []
            final_summary = None

            async for event in _stream_graph(app_graph, initial_state, thread_config, meeting_id):
                if event.get("type") == "error":
                    has_error = True

                accumulated_events.append(event)
                if event.get("is_conclusion") and event.get("reasoning"):
                    final_summary = event.get("reasoning")

                yield event

            # Persistence layer update
            await _update_meeting_status(
                meeting_id,
                "failed" if has_error else "completed",
                event_log=accumulated_events,
                final_summary=final_summary,
            )

            # Hand the sandboxes back. The startup sweep covers the case where
            # the backend dies before reaching this point.
            sandbox_names = sorted(
                {
                    event["sandbox"]
                    for event in accumulated_events
                    if isinstance(event.get("sandbox"), str)
                }
            )
            await release_meeting_sandboxes(sandbox_names)

            if not has_error:
                yield {
                    "type": "meeting_completed",
                    "meeting_id": meeting_id,
                    "timestamp": _now_iso(),
                }

        # Durable checkpointing is required unless explicitly waived.
        #
        # This block used to swallow every exception and fall through to an
        # in-memory saver while logging only a warning. The meeting then ran
        # with no durability at all -- a backend restart lost it -- and nothing
        # in the API or UI indicated that had happened. Silent downgrades of a
        # durability guarantee are worse than a hard failure, so it now raises
        # unless ALLOW_VOLATILE_CHECKPOINTS is set for local development.
        if pg_url:
            try:
                async with AsyncConnectionPool(
                    pg_url, max_size=5, kwargs={"autocommit": True}
                ) as pool:
                    # AsyncPostgresSaver declares a dict row factory; the pool is
                    # created with psycopg's default tuple factory. The saver sets
                    # its own row factory per cursor, so this is safe at runtime.
                    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
                    # setup() is idempotent but on a fresh database it creates
                    # several tables and indexes, which comfortably exceeds a
                    # 10s budget on a small cluster. Timing out there meant the
                    # very first meeting always failed, and the second
                    # succeeded -- a confusing, self-healing symptom.
                    async with asyncio.timeout(CHECKPOINTER_SETUP_TIMEOUT):
                        await checkpointer.setup()

                    logger.info("durable_checkpoint_active", meeting_id=meeting_id)
                    async for event in _execute_with_checkpointer(checkpointer):
                        yield event
                    return
            except Exception as exc:
                # asyncio.TimeoutError and friends stringify to "", which makes
                # the operator-facing message useless. Always name the type.
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if not settings.ALLOW_VOLATILE_CHECKPOINTS:
                    logger.error(
                        "durable_checkpoint_unavailable",
                        error=detail,
                        meeting_id=meeting_id,
                    )
                    await _update_meeting_status(meeting_id, "failed")
                    yield {
                        "type": "error",
                        "content": (
                            "Durable checkpointing is unavailable, so this meeting "
                            "cannot be recovered if the backend restarts. Refusing to "
                            "run. Set ALLOW_VOLATILE_CHECKPOINTS=true to override in "
                            f"development. Cause: {detail}"
                        ),
                        "timestamp": _now_iso(),
                    }
                    return
                logger.warning(
                    "durable_checkpoint_failed_volatile_override_active",
                    error=detail,
                    meeting_id=meeting_id,
                )
        elif not settings.ALLOW_VOLATILE_CHECKPOINTS:
            await _update_meeting_status(meeting_id, "failed")
            yield {
                "type": "error",
                "content": "DATABASE_URL is not set, so no durable checkpointer is available.",
                "timestamp": _now_iso(),
            }
            return

        logger.warning("using_volatile_checkpointer", meeting_id=meeting_id)
        async for event in _execute_with_checkpointer(MemorySaver()):
            yield event


async def _stream_graph(
    app_graph: Any, initial_state: dict[str, Any], thread_config: dict[str, Any], meeting_id: str
) -> AsyncGenerator[dict[str, Any]]:
    """Streams orchestration updates and yields serializable events."""
    logger.info("astream_execution_starting", meeting_id=meeting_id)
    try:
        async for event in app_graph.astream(initial_state, thread_config, stream_mode="updates"):
            for node_name, state_update in event.items():
                logger.info("orchestration_update", node=node_name)

                if "event_log" in state_update and state_update["event_log"]:
                    for e in state_update["event_log"]:
                        if "timestamp" not in e:
                            e["timestamp"] = _now_iso()
                        yield e

                if node_name == "supervisor":
                    next_spk = state_update.get("next_speaker")
                    if next_spk:
                        yield {
                            "type": "supervisor_selected_next_agent",
                            "agent_id": next_spk,
                            "reasoning": state_update.get("reasoning"),
                            "timestamp": _now_iso(),
                        }
                elif node_name == "__start__":
                    continue
                else:
                    # Typing status clears naturally via the agent_spoke
                    # yield above.
                    pass

    except Exception as e:
        logger.error("graph_runtime_exception", error=str(e), meeting_id=meeting_id)
        yield {
            "type": "error",
            "content": f"Nexus Runtime Exception: {str(e)}",
            "timestamp": _now_iso(),
        }


async def _update_meeting_status(
    meeting_id: str,
    status: str,
    event_log: list[dict[str, Any]] | None = None,
    final_summary: str | None = None,
) -> None:
    """Synchronizes simulation results with the persistent layer."""
    assert database.async_session_maker is not None
    async with database.require_session_maker()() as session:
        query = select(Meeting).where(Meeting.id == UUID(meeting_id))
        meeting = (await session.execute(query)).scalar_one_or_none()
        if meeting:
            meeting.status = status
            if event_log is not None:
                logger.info(
                    "persisting_telemetry_logs", meeting_id=meeting_id, count=len(event_log)
                )
                meeting.meeting_log = event_log
            if final_summary is not None:
                meeting.final_summary = final_summary
            await session.commit()
            logger.info(
                "simulation_state_persisted",
                meeting_id=meeting_id,
                status=status,
                log_size=len(event_log) if event_log else 0,
            )
