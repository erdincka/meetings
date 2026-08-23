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
