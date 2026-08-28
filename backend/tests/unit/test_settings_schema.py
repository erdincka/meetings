"""Settings update contract.

The security property under test: credentials are environment-only. The
settings payload once accepted inference_api_key and wrote it to a plaintext
file; this is the boundary that replaced that, and a 422 rather than a silently
ignored field is the part worth keeping.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.settings import (
    RuntimeSettings,
    SystemSettingsTunables,
    SystemSettingsUpdate,
)


class TestCredentialsAreNotSettable:
    @pytest.mark.parametrize(
        "field",
        [
            "inference_api_key",
            "inference_endpoint",
            "inference_model_name",
            "embedding_api_key",
            "embedding_endpoint",
            "embedding_model_name",
        ],
    )
    def test_credential_fields_are_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            SystemSettingsUpdate(**{field: "value"})

    def test_unknown_fields_are_rejected(self) -> None:
        """extra='forbid' turns a typo into a 422 instead of a silent no-op."""
        with pytest.raises(ValidationError):
            SystemSettingsUpdate(inference_temprature=0.5)  # typo, deliberately

    def test_tunables_schema_carries_no_credentials(self) -> None:
        assert not [f for f in SystemSettingsTunables.model_fields if "api_key" in f]


class TestBounds:
    @pytest.mark.parametrize("value", [-0.1, 2.1, 99])
    def test_temperature_out_of_range_rejected(self, value: float) -> None:
        with pytest.raises(ValidationError):
            SystemSettingsUpdate(inference_temperature=value)

    @pytest.mark.parametrize("value", [0.0, 0.7, 2.0])
    def test_temperature_in_range_accepted(self, value: float) -> None:
        assert SystemSettingsUpdate(inference_temperature=value).inference_temperature == value

    @pytest.mark.parametrize("value", [0, -1, 21])
    def test_retrieval_limit_out_of_range_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError):
            SystemSettingsUpdate(retrieval_limits_per_agent=value)


class TestPartialUpdate:
    def test_unset_fields_are_excluded(self) -> None:
        """Only what the caller sent is applied; the rest keeps its stored value."""
        payload = SystemSettingsUpdate(inference_temperature=0.42)
        assert payload.model_dump(exclude_unset=True) == {"inference_temperature": 0.42}


class TestRuntimeSettings:
    def test_merges_tunables_with_env_credentials(self) -> None:
        merged = RuntimeSettings(
            **SystemSettingsTunables(inference_temperature=0.3).model_dump(),
            inference_endpoint="http://x/v1",
            inference_api_key="secret",
            inference_model_name="m",
        )
        assert merged.inference_temperature == 0.3
        assert merged.inference_api_key == "secret"

    def test_defaults_match_the_documented_values(self) -> None:
        """These are the values agents.py and supervisor.py used to hardcode."""
        t = SystemSettingsTunables()
        assert t.inference_temperature == 0.7
        assert t.supervisor_temperature == 0.1
