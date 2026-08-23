"""Document retrieval, proxied through the backend.

The sandbox has no database credential and no direct route to Postgres. It calls
POST /internal/v1/retrieval/search with its ServiceAccount token; the backend
validates the token, derives the calling agent from it, and scopes the search
accordingly. An agent therefore cannot read another persona's private library by
asking nicely -- the scope is not a parameter it controls.
"""

from __future__ import annotations

import httpx
import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)


def build_retrieval_tool(
    *,
    client: httpx.AsyncClient,
    agent_id: str,
    meeting_id: str,
    library_access: bool,
    limit: int,
    **_ignored: object,
) -> StructuredTool:
    # Every builder receives the same context dict and takes what it needs, so
    # adding a key for one tool must not break the others.
    async def retrieve_documents(
        query: str,
        search_company_library: bool = True,
        search_private_library: bool = True,
    ) -> str:
        """Search the shared and private libraries for evidence.

        Returns exact excerpts with their source. Assess relevance to the
        meeting objective before using a result; if the excerpts are irrelevant,
        do not mention them. If you use one, quote it exactly.
        """
        scopes: list[str] = []
        if search_company_library and library_access:
            scopes.extend(("company", "meeting"))
        if search_private_library:
            scopes.append("agent")

        if not scopes:
            return "No libraries are available to you."

        try:
            response = await client.post(
                "/internal/v1/retrieval/search",
                json={
                    "query": query,
                    "scopes": scopes,
                    "limit": limit,
                    "meeting_id": meeting_id,
                    "agent_id": agent_id,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("retrieval_failed", error=str(exc))
            return f"Retrieval is unavailable: {exc}"

        results = response.json().get("data", {}).get("results", [])
        if not results:
            return "No matching documents found."

        chunks = []
        for item in results:
            page = item.get("page_number")
            location = f"p.{page}" if str(page).isdigit() else str(page or "?")
            chunks.append(
                f"Source: {item['document_name']} {location}\n"
                f'Excerpt: "{item["text"]}"\n'
                f"Scope: {item['library_scope']}"
            )
        return "\n\n---\n\n".join(chunks)

    return StructuredTool.from_function(
        coroutine=retrieve_documents,
        name="retrieve_documents",
        description=retrieve_documents.__doc__ or "",
    )
