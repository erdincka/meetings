import structlog
from langchain_openai import OpenAIEmbeddings
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# (base_url, model_name) -> bool (True if asymmetric/requires input_type)
_ASYMMETRIC_REGISTRY: dict[tuple[str, str], bool] = {}


def _embedding_config() -> tuple[str | None, str | None, str | None, bool]:
    """Embedding endpoint config, straight from the environment.

    This does not need a database session: embedding credentials are env-only,
    so reading them here keeps the many call sites simple.
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

    embeddings_params = {
        "openai_api_base": base_url,
        "openai_api_key": api_key,
        "model": model_name,
        "check_embedding_ctx_length": False,
        "request_timeout": 30,
    }

    # Inject input_type if we've detected this model requires it
    is_asymmetric = _ASYMMETRIC_REGISTRY.get((base_url, model_name), False)
    if is_asymmetric and input_type:
        embeddings_params["model_kwargs"] = {"extra_body": {"input_type": input_type}}

    if ignore_tls:
        from app.core.network import get_http_client, get_sync_http_client

        embeddings_params["http_client"] = get_sync_http_client(ignore_tls=True)
        embeddings_params["http_async_client"] = get_http_client(ignore_tls=True)

    return OpenAIEmbeddings(**embeddings_params)


async def _wrap_embedding_call(
    func: callable, texts: list[str] | str, input_type: str, *args, **kwargs
) -> any:
    """Wrapper that detects if input_type is required and retries if so."""
    _base_url, _api_key, _model_name, _ignore_tls = _embedding_config()
    key = (_base_url, _model_name)

    # 1. Try with current registered capability
    try:
        model = await get_embedding_model(input_type=input_type)
        return await func(model, texts, *args, **kwargs)
    except Exception as e:
        error_str = str(e).lower()
        # Detect if we missed input_type for an asymmetric model
        if (
            "input_type" in error_str
            and "required" in error_str
            and not _ASYMMETRIC_REGISTRY.get(key)
        ):
            logger.info("asymmetric_model_detected", model_name=_model_name)
            _ASYMMETRIC_REGISTRY[key] = True
            model = await get_embedding_model(input_type=input_type)
            return await func(model, texts, *args, **kwargs)
        raise


async def generate_embeddings(
    texts: list[str], session: AsyncSession | None = None
) -> list[list[float]]:
    try:

        async def _call(model, t):
            return await model.aembed_documents(t)

        return await _wrap_embedding_call(_call, texts, input_type="passage")
    except Exception as e:
        logger.error("embedding_generation_failed", error=str(e))
        raise


async def generate_embedding(text: str, session: AsyncSession | None = None) -> list[float]:
    try:

        async def _call(model, t):
            return await model.aembed_query(t)

        return await _wrap_embedding_call(_call, text, input_type="query")
    except Exception as e:
        logger.error("single_embedding_generation_failed", error=str(e))
        raise
