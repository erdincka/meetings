"""HTTP response serialisation.

Regression guard for a real deployment failure: /readyz built a JSONResponse
from APIResponse.model_dump(), which leaves APIResponse.timestamp as a datetime
object. Starlette serialises with plain json.dumps, so every probe returned a
500 and the pod never became ready.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.domain.response import APIResponse


def test_timestamp_is_timezone_aware() -> None:
    ts = APIResponse(status="success").timestamp
    assert isinstance(ts, datetime)
    assert ts.tzinfo is not None, "naive timestamps are ambiguous across zones"


def test_plain_model_dump_is_not_json_serialisable() -> None:
    """Documents exactly why mode='json' is required at every JSONResponse."""
    payload = APIResponse(status="success", message="OK").model_dump()
    try:
        json.dumps(payload)
    except TypeError:
        return
    raise AssertionError("expected datetime to break json.dumps")


def test_json_mode_dump_is_serialisable() -> None:
    payload = APIResponse(status="success", message="OK").model_dump(mode="json")
    encoded = json.dumps(payload)
    assert "OK" in encoded
    assert isinstance(payload["timestamp"], str)


class TestModelMatching:
    """Endpoints disagree about tags; a present model must not read as absent."""

    def test_exact_match(self) -> None:
        from app.api.routes.system import _model_is_served

        assert _model_is_served("gpt-4o", ["gpt-4o", "gpt-4o-mini"])

    def test_ollama_implicit_latest_tag(self) -> None:
        """Regression: Ollama serves 'nomic-embed-text' as 'nomic-embed-text:latest'."""
        from app.api.routes.system import _model_is_served

        assert _model_is_served("nomic-embed-text", ["nomic-embed-text:latest", "qwen3:1.7b"])

    def test_configured_with_tag_against_untagged_listing(self) -> None:
        from app.api.routes.system import _model_is_served

        assert _model_is_served("qwen3:1.7b", ["qwen3:1.7b"])

    def test_absent_model_is_still_absent(self) -> None:
        from app.api.routes.system import _model_is_served

        assert not _model_is_served("llama3", ["nomic-embed-text:latest", "qwen3:1.7b"])

    def test_empty_listing(self) -> None:
        from app.api.routes.system import _model_is_served

        assert not _model_is_served("gpt-4o", [])
