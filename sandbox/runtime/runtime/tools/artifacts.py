"""Drafting and reading artifacts, and recording action items.

All three go through the backend's internal API. Sandboxes hold no database
credential and share no filesystem, so this is the only way anything they
produce becomes visible -- which also means every write is attributed and
scoped to the meeting automatically.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)


def build_artifact_tools(
    *,
    client: httpx.AsyncClient,
    meeting_id: str,
    **_ignored: Any,
) -> list[StructuredTool]:
    async def draft_artifact(title: str, markdown: str, kind: str = "document") -> str:
        """Write a document, note or table for the meeting record.

        Use this for anything worth keeping beyond the transcript: a draft
        statement, a summary of a position, a comparison table.
        """
        try:
            response = await client.post(
                "/internal/v1/artifacts",
                json={
                    "meeting_id": meeting_id,
                    "kind": kind,
                    "title": title,
                    "mime_type": "text/markdown",
                    "body": markdown,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("artifact_write_failed", error=str(exc))
            return f"Could not save the artifact: {exc}"
        artifact_id = response.json().get("data", {}).get("id")
        return f"Saved as artifact {artifact_id}: {title}"

    async def read_artifact(artifact_id: str) -> str:
        """Read back an artifact produced earlier in this meeting."""
        try:
            response = await client.get(f"/internal/v1/artifacts/{artifact_id}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return f"Could not read artifact {artifact_id}: {exc}"
        data = response.json().get("data", {})
        return f"{data.get('title')}\n\n{data.get('body')}"

    async def record_action_item(text: str, owner: str = "", due: str = "") -> str:
        """Record a commitment made in the meeting, with an owner and a date."""
        try:
            response = await client.post(
                "/internal/v1/action-items",
                json={
                    "meeting_id": meeting_id,
                    "text": text,
                    "owner": owner or None,
                    "due": due or None,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return f"Could not record the action: {exc}"
        return f"Recorded: {text}" + (f" (owner: {owner})" if owner else "")

    return [
        StructuredTool.from_function(
            coroutine=draft_artifact,
            name="draft_artifact",
            description=draft_artifact.__doc__ or "",
        ),
        StructuredTool.from_function(
            coroutine=read_artifact, name="read_artifact", description=read_artifact.__doc__ or ""
        ),
        StructuredTool.from_function(
            coroutine=record_action_item,
            name="record_action_item",
            description=record_action_item.__doc__ or "",
        ),
    ]
