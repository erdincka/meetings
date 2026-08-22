"""Talking to a persona sandbox over its Service.

One HTTP call per turn, consuming an SSE stream that ends in a single
`turn.result`. Interim events are forwarded to the browser so the transcript
fills in as the model speaks rather than appearing all at once.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import structlog

from app.orchestration.protocol import (
    PersonaBindRequest,
    PersonaBindResponse,
    TurnEvent,
    TurnRequest,
)

logger = structlog.get_logger(__name__)

BIND_TIMEOUT = 30.0
# A turn is a full ReAct loop: several model calls plus tool round trips. The
# read timeout has to exceed the slowest plausible turn or a working meeting
# gets cut off mid-sentence.
TURN_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class SandboxRPCError(RuntimeError):
    pass


class PersonaSandboxClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> PersonaSandboxClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TURN_TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise SandboxRPCError("Client used outside an async context")
        return self._http

    async def bind(self, request: PersonaBindRequest) -> PersonaBindResponse:
        response = await self.http.post(
            f"{self.base_url}/v1/persona",
            json=request.model_dump(mode="json"),
            timeout=BIND_TIMEOUT,
        )
        response.raise_for_status()
        bound = PersonaBindResponse.model_validate(response.json())
        if bound.refused_tools:
            # The sandbox's capability file did not provide something the
            # backend granted. Not fatal, but it means the persona's advertised
            # abilities and the cluster's enforcement disagree.
            logger.warning(
                "sandbox_refused_tools",
                agent_id=bound.agent_id,
                refused=bound.refused_tools,
            )
        return bound

    async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        """Run one turn, yielding each SSE event as it arrives."""
        async with self.http.stream(
            "POST",
            f"{self.base_url}/v1/turn",
            json=request.model_dump(mode="json"),
            timeout=TURN_TIMEOUT,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ").strip()
                if not payload:
                    continue
                try:
                    yield TurnEvent.model_validate(json.loads(payload))
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning("malformed_sse_event", error=str(exc), payload=payload[:200])
