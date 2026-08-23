"""Search an external corpus of industry material.

Backed by a small in-cluster service rather than the live internet. A demo that
reaches the real web is not reproducible -- results drift between runs -- and it
would need sandbox egress to arbitrary hosts, which is exactly what the network
policy exists to prevent. The corpus is fixed, so a citation means the same
thing tomorrow.

Only profiles granted this tool have egress to the service at all; for everyone
else the route does not exist, quite apart from the tool not being registered.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog
from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

CORPUS_URL = os.getenv("CORPUS_URL", "http://meetings-corpus.meetings.svc:8080")
MAX_RESULTS = 5


def build_corpus_tool(**_ignored: Any) -> StructuredTool:
    async def search_corpus(query: str, top_k: int = 3) -> str:
        """Search external industry literature for context beyond company documents.

        Use this for benchmarks, regulatory expectations and comparable cases --
        material the company's own library would not contain. Cite the source and
        date when you use a result.
        """
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{CORPUS_URL}/search",
                    params={"q": query, "top_k": min(top_k, MAX_RESULTS)},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("corpus_search_failed", error=str(exc))
            return f"The external corpus is unreachable: {exc}"

        results = response.json().get("results", [])
        if not results:
            return "No external material matched that query."

        chunks = [
            f'Source: {r["title"]} ({r["source"]}, {r["date"]})\nExcerpt: "{r["excerpt"]}"'
            for r in results
        ]
        return "\n\n---\n\n".join(chunks)

    return StructuredTool.from_function(
        coroutine=search_corpus,
        name="search_corpus",
        description=search_corpus.__doc__ or "",
    )
