"""The sandbox's client to the backend must actually work.

Regression: the ServiceAccount auth hook was a plain function on an
AsyncClient. httpx awaits event hooks, so every request through it raised
"object NoneType can't be used in 'await' expression". Nothing caught it
because the client is only used when an agent calls a tool -- so it surfaced
as intermittent turn failures rather than as a broken client.
"""

from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest


def test_the_auth_hook_is_a_coroutine_function() -> None:
    """A sync hook on an AsyncClient is awaited, and returns None."""
    import runtime.server as server

    src = inspect.getsource(server.lifespan)
    assert "async def auth_headers" in src, "auth hook must be async"


@pytest.mark.asyncio
async def test_a_sync_hook_really_does_break_an_async_client() -> None:
    """Pin the behaviour this guards against, so the rule is not folklore."""

    def sync_hook(request: httpx.Request) -> None:
        request.headers["X-Test"] = "1"

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={}))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://x", event_hooks={"request": [sync_hook]}
    ) as client:
        # The exact wording moved between CPython releases -- 3.13 says "object
        # NoneType can't be used in 'await' expression", 3.14 says "'NoneType'
        # object can't be awaited". Both name the culprit; pin that instead.
        with pytest.raises(TypeError, match="NoneType"):
            await client.get("/")


@pytest.mark.asyncio
async def test_an_async_hook_sets_the_header() -> None:
    seen: dict[str, str] = {}

    async def async_hook(request: httpx.Request) -> None:
        request.headers["Authorization"] = "Bearer t"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://x", event_hooks={"request": [async_hook]}
    ) as client:
        await client.get("/")

    assert seen["auth"] == "Bearer t"


def test_asyncio_is_available() -> None:
    assert asyncio.get_event_loop_policy() is not None
