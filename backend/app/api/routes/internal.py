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

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

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
