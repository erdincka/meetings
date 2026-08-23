"""HTTP surface of the persona sandbox.

The backend reaches this over the per-sandbox Service published by the Agent
Sandbox controller (status.serviceFQDN). kubectl port-forward is not used and
would not work: it is incompatible with gVisor.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .agent import BoundPersona
from .idempotency import TurnCache
from .protocol import (
    PersonaBindRequest,
    PersonaBindResponse,
    TurnError,
    TurnEvent,
    TurnRequest,
)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
)
logger = structlog.get_logger(__name__)

BACKEND_URL = os.getenv("BACKEND_INTERNAL_URL", "http://meetings-backend.meetings.svc:8000")
API_KEY_FILE = Path(os.getenv("INFERENCE_API_KEY_FILE", "/etc/sandbox/secrets/inference-api-key"))
SA_TOKEN_FILE = Path(
    os.getenv("SA_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")
)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class RuntimeState:
    persona: BoundPersona | None = None
    cache: TurnCache = TurnCache()
    backend: httpx.AsyncClient | None = None


state = RuntimeState()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The projected ServiceAccount token is re-read per request rather than
    # captured once: kubelet rotates it, and a long-lived sandbox outlives the
    # original token's validity.
    def auth_headers(request: httpx.Request) -> None:
        token = _read(SA_TOKEN_FILE)
        if token:
            request.headers["Authorization"] = f"Bearer {token}"

    state.backend = httpx.AsyncClient(
        base_url=BACKEND_URL,
        timeout=30,
        event_hooks={"request": [auth_headers]},
    )
    logger.info("runtime_started", backend=BACKEND_URL)
    yield
    await state.backend.aclose()


app = FastAPI(title="Persona Sandbox Runtime", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately does not require a bound persona."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    return {
        "status": "ok",
        "bound": state.persona is not None,
        "agent_id": state.persona.bind.agent_id if state.persona else None,
    }


@app.post("/v1/persona", response_model=PersonaBindResponse)
async def bind_persona(request: PersonaBindRequest) -> PersonaBindResponse:
    """Bind this sandbox to an attendee.

    Idempotent for the same agent. Re-binding to a *different* agent is allowed
    and clears the turn cache, so a warm sandbox recycled between meetings never
    replays another persona's cached turn.
    """
    assert state.backend is not None

    if state.persona and state.persona.bind.agent_id != request.agent_id:
        logger.info("rebinding_sandbox", previous=state.persona.bind.agent_id)
        state.cache.clear()

    state.persona = BoundPersona(request, _read(API_KEY_FILE), state.backend)
    logger.info(
        "persona_bound",
        agent_id=request.agent_id,
        active_tools=state.persona.active_tools,
        refused_tools=state.persona.refused_tools,
    )
    return PersonaBindResponse(
        agent_id=request.agent_id,
        active_tools=state.persona.active_tools,
        refused_tools=state.persona.refused_tools,
    )


@app.post("/v1/turn")
async def run_turn(request: TurnRequest) -> StreamingResponse:
    persona = state.persona
    if persona is None:
        raise HTTPException(status_code=409, detail="No persona bound to this sandbox")

    async def stream() -> AsyncIterator[str]:
        cached = state.cache.get(request.turn_key)
        if cached is not None:
            logger.info("turn_replayed_from_cache", turn_key=request.turn_key)
            yield TurnEvent(type="turn.result", result=cached).to_sse()
            return

        try:
            async for event in persona.run_turn(request):
                if event.type == "turn.result" and event.result is not None:
                    state.cache.put(request.turn_key, event.result)
                yield event.to_sse()
        except Exception as exc:
            logger.error("turn_failed", turn_key=request.turn_key, error=str(exc))
            yield TurnEvent(
                type="turn.error",
                error=TurnError(code="internal", message=str(exc)[:500]),
            ).to_sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
