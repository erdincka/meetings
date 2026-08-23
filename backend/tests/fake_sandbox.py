"""An in-process stand-in for a persona sandbox.

Serves the same contract as the real runtime -- /v1/persona and an SSE /v1/turn
-- with scripted replies instead of a model. That makes a full meeting run
deterministic and dependency-free: no cluster, no gVisor, no inference endpoint,
no API key, and no wall-clock waiting on a model.

It is the harness the plan identified as the highest-value test in the project,
because it exercises the graph, the router, the RPC client and the state
reducers together, which unit tests cannot.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.orchestration.protocol import (
    PersonaBindRequest,
    PersonaBindResponse,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnRequest,
    TurnResult,
)


@dataclass
class FakeSandboxState:
    """Records what the backend actually sent, so tests can assert on it."""

    binds: list[PersonaBindRequest] = field(default_factory=list)
    turns: list[TurnRequest] = field(default_factory=list)
    # turn_key -> how many times the backend asked for it. Proves idempotency.
    turn_counts: dict[str, int] = field(default_factory=dict)
    reply_for: dict[str, str] = field(default_factory=dict)
    fail_next: bool = False
    emit_tool_call: bool = False


def build_fake_sandbox(state: FakeSandboxState) -> FastAPI:
    app = FastAPI()
    bound: dict[str, PersonaBindRequest] = {}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/persona", response_model=PersonaBindResponse)
    async def bind(request: PersonaBindRequest) -> PersonaBindResponse:
        state.binds.append(request)
        bound["current"] = request
        # Mirrors the real runtime: only baseline tools exist in Phase 2, so
        # anything else is refused rather than silently accepted.
        active = [t for t in request.granted_tools if t == "retrieve_documents"]
        refused = [t for t in request.granted_tools if t != "retrieve_documents"]
        return PersonaBindResponse(
            agent_id=request.agent_id, active_tools=active, refused_tools=refused
        )

    @app.post("/v1/turn")
    async def turn(request: TurnRequest) -> StreamingResponse:
        state.turns.append(request)
        state.turn_counts[request.turn_key] = state.turn_counts.get(request.turn_key, 0) + 1
        persona = bound["current"]

        async def stream() -> AsyncIterator[str]:
            if state.fail_next:
                state.fail_next = False
                yield TurnEvent(
                    type="turn.error",
                    error={"code": "internal", "message": "scripted failure"},  # type: ignore[arg-type]
                ).to_sse()
                return

            tool_calls: list[ToolCall] = []
            tool_results: list[ToolResult] = []
            if state.emit_tool_call:
                call = ToolCall(id="c1", name="retrieve_documents", args={"query": "evidence"})
                result = ToolResult(id="c1", name="retrieve_documents", ok=True, summary="found")
                tool_calls.append(call)
                tool_results.append(result)
                yield TurnEvent(type="tool.call", tool_call=call).to_sse()
                yield TurnEvent(type="tool.result", tool_result=result).to_sse()

            said = state.reply_for.get(
                persona.agent_id, f"{persona.persona.display_name} has a view."
            )
            yield TurnEvent(type="speech.delta", text=said).to_sse()
            yield TurnEvent(
                type="turn.result",
                result=TurnResult(
                    turn_key=request.turn_key,
                    agent_id=persona.agent_id,
                    public_content=(
                        f"[{persona.persona.display_name} - {persona.persona.title}] {said}"
                    ),
                    private_reasoning="scripted reasoning",
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                ),
            ).to_sse()

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
