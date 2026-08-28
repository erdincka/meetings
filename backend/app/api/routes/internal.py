"""Internal API consumed by persona sandboxes.

Not part of the public surface. Sandboxes reach it with their projected
ServiceAccount token, which the backend validates via TokenReview.

The security property this enforces: **a sandbox cannot choose whose data it
reads.** The caller's identity comes from the verified token, and the retrieval
scope is derived from it. An agent that asks for another persona's private
library gets its own, because the private-library owner is not a parameter the
model controls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_session
from app.core.sandbox_auth import SandboxIdentity, require_sandbox_identity
from app.domain.response import APIResponse
from app.services.vector_search import semantic_search

logger = structlog.get_logger(__name__)
router = APIRouter()

# Hard ceiling regardless of what the caller asks for, so a prompt-injected
# agent cannot exfiltrate a library one enormous page at a time.
MAX_RETRIEVAL_LIMIT = 20


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scopes: list[str] = Field(default_factory=list)
    limit: int = Field(default=3, ge=1, le=MAX_RETRIEVAL_LIMIT)
    meeting_id: str | None = None
    agent_id: str | None = None


@router.post("/v1/retrieval/search", response_model=APIResponse)
async def retrieval_search(
    payload: RetrievalRequest,
    request: Request,
    identity: SandboxIdentity = Depends(require_sandbox_identity),
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    """Vector search on behalf of a persona sandbox.

    ``agent_id`` and ``meeting_id`` in the body are advisory. The values that
    actually scope the query come from the verified sandbox identity, so a
    sandbox cannot read another persona's private library by changing the body.
    """
    if payload.agent_id and payload.agent_id != identity.agent_id:
        logger.warning(
            "sandbox_agent_id_mismatch",
            claimed=payload.agent_id,
            actual=identity.agent_id,
        )

    allowed_scopes = {"company", "meeting", "agent"}
    scopes = [s for s in payload.scopes if s in allowed_scopes]
    if not scopes:
        raise HTTPException(status_code=400, detail="No valid library scopes requested")

    try:
        meeting_uuid = uuid.UUID(identity.meeting_id) if identity.meeting_id else None
        agent_uuid = uuid.UUID(identity.agent_id) if identity.agent_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed identity: {exc}") from exc

    results = await semantic_search(
        query_text=payload.query,
        library_scopes=scopes,
        limit=min(payload.limit, MAX_RETRIEVAL_LIMIT),
        session=session,
        meeting_id=meeting_uuid,
        owner_agent_id=agent_uuid,
    )

    logger.info(
        "internal_retrieval",
        agent_id=identity.agent_id,
        scopes=scopes,
        hits=len(results),
    )
    return APIResponse(status="success", data={"results": results})


class ArtifactCreate(BaseModel):
    meeting_id: str
    kind: str = Field(default="document", max_length=32)
    title: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="text/markdown", max_length=128)
    # Text for documents and tables; base64 for binary output such as PNG.
    body: str = Field(max_length=8_000_000)


class ActionItemCreate(BaseModel):
    meeting_id: str
    text: str = Field(min_length=1, max_length=4000)
    owner: str | None = None
    due: str | None = None


@router.post("/v1/artifacts", response_model=APIResponse)
async def create_artifact(
    payload: ArtifactCreate,
    identity: SandboxIdentity = Depends(require_sandbox_identity),
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    """Persist something an agent produced.

    Authorship comes from the verified sandbox identity, never the body: an
    agent cannot attribute its work to a colleague.
    """
    from app.models.artifacts import Artifact

    artifact = Artifact(
        meeting_id=_meeting_uuid(identity, payload.meeting_id),
        agent_id=uuid.UUID(identity.agent_id) if identity.agent_id else None,
        kind=payload.kind,
        title=payload.title,
        mime_type=payload.mime_type,
        body=payload.body,
        meta={"profile": identity.profile},
    )
    session.add(artifact)
    await session.commit()
    await session.refresh(artifact)

    logger.info(
        "artifact_created",
        artifact_id=str(artifact.id),
        kind=artifact.kind,
        agent_id=identity.agent_id,
    )
    return APIResponse(status="success", data={"id": str(artifact.id)})


@router.get("/v1/artifacts/{artifact_id}", response_model=APIResponse)
async def read_artifact(
    artifact_id: uuid.UUID,
    identity: SandboxIdentity = Depends(require_sandbox_identity),
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    """Read an artifact, scoped to the caller's own meeting."""
    from app.models.artifacts import Artifact

    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Scoped deliberately: a sandbox may read what its meeting produced and
    # nothing from any other meeting, whatever id it asks for.
    if identity.meeting_id and str(artifact.meeting_id) != identity.meeting_id:
        raise HTTPException(status_code=403, detail="Artifact belongs to another meeting")

    return APIResponse(
        status="success",
        data={
            "id": str(artifact.id),
            "kind": artifact.kind,
            "title": artifact.title,
            "mime_type": artifact.mime_type,
            "body": artifact.body,
        },
    )


@router.post("/v1/action-items", response_model=APIResponse)
async def create_action_item(
    payload: ActionItemCreate,
    identity: SandboxIdentity = Depends(require_sandbox_identity),
    session: AsyncSession = Depends(get_db_session),
) -> APIResponse:
    """Record a commitment made during the meeting."""
    from app.models.artifacts import ActionItem

    item = ActionItem(
        meeting_id=_meeting_uuid(identity, payload.meeting_id),
        raised_by_agent_id=uuid.UUID(identity.agent_id) if identity.agent_id else None,
        text=payload.text,
        due=payload.due,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)

    logger.info("action_item_recorded", item_id=str(item.id), agent_id=identity.agent_id)
    return APIResponse(status="success", data={"id": str(item.id)})


def _meeting_uuid(identity: SandboxIdentity, claimed: str) -> uuid.UUID:
    """Prefer the verified meeting over whatever the body claims."""
    value = identity.meeting_id or claimed
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Malformed meeting id: {exc}") from exc


# --- model proxy -----------------------------------------------------------
#
# Persona sandboxes reach the model through here rather than calling the
# provider themselves. Two things fall out of that, and both were problems:
#
# 1. A sandbox needs no route off the cluster. The direct path required a
#    NetworkPolicy ipBlock naming every address the endpoint resolves to, which
#    is unanswerable for a hosted provider behind a CDN -- the only rule that
#    worked was 0.0.0.0/0, i.e. the whole internet, granted to the least-trusted
#    component in the system in order to reach one host.
#
# 2. The provider credential never enters the sandbox. It used to be mounted
#    there from a Secret. Now the backend holds it and the sandbox authenticates
#    with its own ServiceAccount token, so a persona cannot leak a key it does
#    not have.
#
# The path is OpenAI-shaped so the runtime keeps using a stock client: it points
# its base_url here and the usual /chat/completions hangs off it.

# Hop-by-hop headers, plus the ones that describe a body this proxy re-frames.
# Forwarding these corrupts the response: a re-encoded body with the original
# Content-Length is truncated, and a streamed body with the original
# Transfer-Encoding is double-chunked.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "server",
    }
)


def _proxy_client() -> httpx.AsyncClient:
    """The client used to reach the provider.

    A function rather than an inline constructor so a test can replace it
    without patching httpx globally -- which would also intercept the ASGI
    client the test uses to call this app, and make the test appear to pass by
    never reaching the code under test.

    Streaming matters here: a turn that waits for the whole completion before
    any byte moves turns a slow model into an apparently hung one, and the
    runtime's client asks for SSE.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(settings.LLM_TIMEOUT_SECONDS, connect=10.0))


def _provider_url(path: str) -> str:
    base = (settings.INFERENCE_ENDPOINT or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="No inference endpoint is configured")
    return f"{base}/{path.lstrip('/')}"


@router.post("/v1/llm/{path:path}")
async def llm_proxy(
    path: str,
    request: Request,
    identity: SandboxIdentity = Depends(require_sandbox_identity),
) -> Response:
    """Forward an OpenAI-compatible request to the configured provider.

    The caller is a verified sandbox. Its identity is logged rather than passed
    on: the provider has no notion of our personas, and the useful record is
    which persona spent the tokens.
    """
    body = await request.body()

    # Only the headers the provider needs. The sandbox's Authorization header is
    # its ServiceAccount token and must never be forwarded -- it is a cluster
    # credential, and the provider is off-cluster.
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if settings.INFERENCE_API_KEY:
        headers["authorization"] = f"Bearer {settings.INFERENCE_API_KEY}"

    url = _provider_url(path)
    client = _proxy_client()
    req = client.build_request("POST", url, content=body, headers=headers)

    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning(
            "llm_proxy_upstream_failed",
            agent_id=identity.agent_id,
            profile=identity.profile,
            error=str(exc),
        )
        # 502, not 500: the failure is upstream of us, and an agent that sees a
        # gateway error can say so rather than reporting its own malfunction.
        raise HTTPException(
            status_code=502, detail=f"Inference endpoint unreachable: {exc}"
        ) from exc

    logger.info(
        "llm_proxy_forwarded",
        agent_id=identity.agent_id,
        profile=identity.profile,
        path=path,
        status=upstream.status_code,
    )

    async def body_stream() -> AsyncIterator[bytes]:
        try:
            # aiter_bytes, not aiter_raw. aiter_raw yields the body exactly as it
            # arrived -- still gzip-compressed, if the provider compressed it --
            # while the headers above deliberately drop content-encoding. The
            # combination hands the caller gzip bytes labelled as JSON, and it
            # fails as `'utf-8' codec can't decode byte 0x8b in position 1`:
            # 0x1f 0x8b is the gzip magic number, so the error names the symptom
            # and nothing about the cause.
            #
            # It survived local testing because Ollama does not compress and
            # OpenRouter does, which made it look like a provider incompatibility
            # rather than a bug here. aiter_bytes decodes the transfer encoding,
            # so the stripped header and the body now agree.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            # Both, and in this order: the response holds the connection the
            # client owns, so closing the client first strands it.
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers={
            k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS
        },
        media_type=upstream.headers.get("content-type"),
    )
