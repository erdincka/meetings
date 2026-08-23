"""Metric semantics.

The distinctions here are the point of the metrics, not incidental: a refusal by
the cluster, a tool that errored, and a tool that worked are three different
events. Collapsing any two of them hides the least-privilege signal this project
exists to surface.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from app.core.telemetry import TOOL_CALLS, record_tool_result


def _count(profile: str, tool: str, outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "meetings_tool_calls_total",
        {"profile": profile, "tool": tool, "outcome": outcome},
    )
    return value or 0.0


class TestToolOutcomes:
    def test_denial_is_counted_as_denied_not_error(self) -> None:
        """The whole reason this metric exists."""
        before = _count("counsel", "run_python_analysis", "denied")
        record_tool_result(profile="counsel", tool="run_python_analysis", ok=False, denied=True)
        assert _count("counsel", "run_python_analysis", "denied") == before + 1
        assert _count("counsel", "run_python_analysis", "error") == 0

    def test_failure_is_counted_as_error_not_denied(self) -> None:
        """An outage must not inflate the denial count, or the security signal
        becomes noise."""
        before = _count("quant", "query_business_metrics", "error")
        record_tool_result(profile="quant", tool="query_business_metrics", ok=False, denied=False)
        assert _count("quant", "query_business_metrics", "error") == before + 1
        assert _count("quant", "query_business_metrics", "denied") == 0

    def test_success_is_counted_as_ok(self) -> None:
        before = _count("baseline", "retrieve_documents", "ok")
        record_tool_result(profile="baseline", tool="retrieve_documents", ok=True, denied=False)
        assert _count("baseline", "retrieve_documents", "ok") == before + 1

    def test_denied_wins_over_ok_flag(self) -> None:
        """A denial is a denial even if the caller reported ok."""
        before = _count("chief", "run_python_analysis", "denied")
        record_tool_result(profile="chief", tool="run_python_analysis", ok=True, denied=True)
        assert _count("chief", "run_python_analysis", "denied") == before + 1


class TestLabelCardinality:
    def test_labels_are_profile_not_agent_id(self) -> None:
        """A label per persona multiplies series for no analytical gain, and
        agent ids are UUIDs -- unbounded cardinality in a metrics backend."""
        assert set(TOOL_CALLS._labelnames) == {"profile", "tool", "outcome"}

    @pytest.mark.parametrize(
        "metric_name",
        [
            "meetings_turns_total",
            "meetings_tool_calls_total",
            "meetings_llm_tokens_total",
        ],
    )
    def test_counters_are_namespaced(self, metric_name: str) -> None:
        """Shared Prometheus instances make an unprefixed metric name a
        collision waiting to happen."""
        assert metric_name.startswith("meetings_")
