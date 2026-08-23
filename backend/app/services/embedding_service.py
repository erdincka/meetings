"""Embedding generation against an OpenAI-compatible endpoint.

Some embedding models are *asymmetric*: they require an ``input_type`` telling
them whether the text is a document being indexed or a query being matched.
There is no capability endpoint for this, so the first call that fails with a
"input_type required" error records the fact and retries. The result is cached
per (endpoint, model) for the process lifetime.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from langchain_openai import OpenAIEmbeddings
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# (base_url, model_name) -> True when the model requires input_type.
_ASYMMETRIC_REGISTRY: dict[tuple[str | None, str | None], bool] = {}


def _embedding_config() -> tuple[str | None, str | None, str | None, bool]:
    """Embedding endpoint config, straight from the environment.

    No database session needed: embedding credentials are env-only, which
    keeps the many call sites simple.
    """
    from app.core.config import settings
    from app.core.network import normalize_v1_endpoint

    endpoint = (
        normalize_v1_endpoint(settings.EMBEDDING_ENDPOINT) if settings.EMBEDDING_ENDPOINT else None
    )
    return (
        endpoint,
        settings.EMBEDDING_API_KEY,
        settings.EMBEDDING_MODEL_NAME,
        settings.EMBEDDING_IGNORE_TLS,
    )


async def get_embedding_model(input_type: str | None = None) -> OpenAIEmbeddings:
    base_url, api_key, model_name, ignore_tls = _embedding_config()

    params: dict[str, Any] = {
        "openai_api_base": base_url,
        # Local providers such as Ollama expose an OpenAI-compatible API with no
        # authentication, but the client refuses to construct without *some*
        # key. Send a placeholder rather than failing on a provider that does
        # not want one.
        "openai_api_key": api_key or "not-required",
        "model": model_name,
        "check_embedding_ctx_length": False,
        "request_timeout": 30,
    }

    if _ASYMMETRIC_REGISTRY.get((base_url, model_name), False) and input_type:
        params["model_kwargs"] = {"extra_body": {"input_type": input_type}}

    if ignore_tls:
        from app.core.network import get_http_client, get_sync_http_client

        params["http_client"] = get_sync_http_client(ignore_tls=True)
        params["http_async_client"] = get_http_client(ignore_tls=True)

    return OpenAIEmbeddings(**params)


def _is_missing_input_type_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "input_type" in message and "required" in message


async def _wrap_embedding_call[T](
    func: Callable[[OpenAIEmbeddings, T], Awaitable[Any]],
    texts: T,
    input_type: str,
) -> Any:
    """Call ``func``, learning the model's asymmetry from a first failure."""
    base_url, _api_key, model_name, _ignore_tls = _embedding_config()
    key = (base_url, model_name)

    try:
        model = await get_embedding_model(input_type=input_type)
        return await func(model, texts)
    except Exception as exc:
        if _is_missing_input_type_error(exc) and not _ASYMMETRIC_REGISTRY.get(key):
            logger.info("asymmetric_model_detected", model_name=model_name)
            _ASYMMETRIC_REGISTRY[key] = True
            model = await get_embedding_model(input_type=input_type)
            return await func(model, texts)
        raise


async def generate_embeddings(
    texts: list[str], session: AsyncSession | None = None
) -> list[list[float]]:
    """Embed documents for indexing."""

    async def _call(model: OpenAIEmbeddings, batch: list[str]) -> list[list[float]]:
        return await model.aembed_documents(batch)

    try:
        result: list[list[float]] = await _wrap_embedding_call(_call, texts, input_type="passage")
        return result
    except Exception as exc:
        logger.error("embedding_generation_failed", error=str(exc))
        raise


async def generate_embedding(text: str, session: AsyncSession | None = None) -> list[float]:
    """Embed a single query."""

    async def _call(model: OpenAIEmbeddings, value: str) -> list[float]:
        return await model.aembed_query(value)

    try:
        result: list[float] = await _wrap_embedding_call(_call, text, input_type="query")
        return result
    except Exception as exc:
        logger.error("single_embedding_generation_failed", error=str(exc))
        raise
