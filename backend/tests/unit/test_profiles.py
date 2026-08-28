"""Capability profile resolution.

A persona's requested tools decide which SandboxTemplate, ServiceAccount and
NetworkPolicy its sandbox is built from. Getting this wrong does not produce a
subtly worse prompt -- it hands an agent a capability the cluster then enforces.
"""

from __future__ import annotations

import pytest

from app.orchestration.profiles import (
    ALL_TOOLS,
    ANALYST,
    BASELINE,
    CHIEF,
    COUNSEL,
    PROFILES,
    QUANT,
    Profile,
    ProfileDriftError,
    for_persona,
    resolve,
)


class TestLeastPrivilege:
    def test_no_request_gets_the_smallest_profile(self) -> None:
        """A persona with nothing configured must not inherit the most power."""
        assert resolve(None) is BASELINE
        assert resolve([]) is BASELINE
        assert resolve([""]) is BASELINE

    def test_retrieval_only_stays_baseline(self) -> None:
        assert resolve(["retrieve_documents"]) is BASELINE

    def test_policy_check_resolves_to_counsel_not_something_larger(self) -> None:
        assert resolve(["check_policy_compliance"]) is COUNSEL

    def test_code_execution_resolves_to_quant(self) -> None:
        assert resolve(["run_python_analysis"]) is QUANT

    def test_reading_metrics_does_not_confer_code_execution(self) -> None:
        """Regression: metrics alone used to resolve to a code-executing profile."""
        assert resolve(["query_business_metrics"]) is ANALYST
        assert ANALYST.can_execute_code is False

    def test_resolution_picks_the_smallest_covering_profile(self) -> None:
        """Metrics alone must not land in analyst, which can also execute code."""
        resolved = resolve(["query_business_metrics"])
        assert resolved.can_execute_code is False
        assert "query_business_metrics" in resolved.tools

    def test_profiles_are_ordered_least_to_most_capable(self) -> None:
        sizes = [len(p.tools) for p in PROFILES]
        assert sizes == sorted(sizes), "resolution walks this order and depends on it"


class TestCodeExecutionIsNarrow:
    def test_exactly_one_profile_may_execute_code(self) -> None:
        """The RBAC split is the demonstrable control; keep it a single grant."""
        executors = [p.name for p in PROFILES if p.can_execute_code]
        assert executors == ["quant"]

    def test_seniority_does_not_confer_code_execution(self) -> None:
        """chief is the broadest profile and still cannot run code.

        This contrast is the point: capability follows the job, not the rank.
        """
        assert len(CHIEF.tools) > len(QUANT.tools)
        assert CHIEF.can_execute_code is False

    def test_only_code_executors_need_the_metrics_credential(self) -> None:
        for profile in PROFILES:
            if profile.needs_metrics_dsn:
                assert "query_business_metrics" in profile.tools


class TestDriftDetection:
    def test_unknown_tool_is_rejected(self) -> None:
        with pytest.raises(ProfileDriftError, match="Unknown tools"):
            resolve(["exfiltrate_everything"])

    def test_error_names_the_offending_tool(self) -> None:
        """The operator has to know which persona field to fix."""
        with pytest.raises(ProfileDriftError) as excinfo:
            resolve(["retrieve_documents", "sudo_make_me_a_sandwich"])
        assert "sudo_make_me_a_sandwich" in str(excinfo.value)
        assert "retrieve_documents" not in str(excinfo.value).split("Known tools")[0]

    def test_uncoverable_combination_is_rejected(self) -> None:
        """No profile grants code execution together with corpus search."""
        with pytest.raises(ProfileDriftError, match="No profile provides"):
            resolve(["run_python_analysis", "search_corpus"])

    def test_every_catalogued_tool_is_reachable_from_some_profile(self) -> None:
        """A tool nobody can be granted is dead code pretending to be a feature."""
        provisioned: set[str] = set()
        for profile in PROFILES:
            provisioned |= profile.tools
        assert provisioned == set(ALL_TOOLS)


class TestSeedMapping:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("FD", "quant"),
            ("VP", "analyst"),
            ("GC", "counsel"),
            ("CEO", "chief"),
            ("Inspector", "analyst"),
        ],
    )
    def test_titles_map_to_expected_profiles(self, title: str, expected: str) -> None:
        assert for_persona(title).name == expected

    def test_unknown_title_falls_back_to_baseline(self) -> None:
        assert for_persona("Chief Wombat Officer") is BASELINE


class TestMeetingStartValidation:
    """Drift is caught before the meeting starts, not three turns in."""

    def test_valid_attendees_resolve(self) -> None:
        from types import SimpleNamespace

        from app.orchestration.graph import validate_attendee_profiles

        attendees = {
            "a": SimpleNamespace(
                display_name="Jane", title="FD", default_tools=["run_python_analysis"]
            ),
            "b": SimpleNamespace(display_name="Ann", title="GC", default_tools=[]),
        }
        resolved = validate_attendee_profiles(attendees)  # type: ignore[arg-type]
        assert resolved == {"a": "quant", "b": "baseline"}

    def test_drift_refuses_the_meeting_and_names_the_persona(self) -> None:
        """A persona the cluster will not permit must fail loudly, with enough
        detail to fix the right field on the right persona."""
        from types import SimpleNamespace

        import pytest as _pytest

        from app.orchestration.graph import validate_attendee_profiles

        attendees = {
            "a": SimpleNamespace(display_name="Jane Roe", title="FD", default_tools=["mint_money"]),
        }
        with _pytest.raises(ProfileDriftError) as excinfo:
            validate_attendee_profiles(attendees)  # type: ignore[arg-type]

        message = str(excinfo.value)
        assert "Jane Roe" in message
        assert "mint_money" in message
        assert "Cannot start" in message


class TestWarmPoolInvariant:
    """Every profile must have a warm pool it can actually claim from.

    This is not a sizing preference. A sandbox is obtained only through a
    SandboxClaim, and SandboxClaim.spec offers `warmPoolRef` and nothing else --
    no template reference, so no cold start. A profile whose warm pool has zero
    replicas gets no SandboxWarmPool from the chart, stays selectable, and fails
    every claim with "SandboxWarmPool not found" while consuming a turn per
    attempt. `strategist` shipped that way and made any meeting including the
    Architect persona unable to hear from it.
    """

    def test_every_profile_has_at_least_one_warm_replica(self) -> None:
        zero = [p.name for p in PROFILES if p.warm_replicas < 1]
        assert not zero, (
            f"profiles with no warm pool: {zero}. These are selectable but "
            "unclaimable -- see the class docstring."
        )

    def test_every_seeded_persona_resolves_to_a_claimable_profile(self) -> None:
        """The path that actually broke: a seeded title selected a profile with
        no pool behind it."""
        for profile in PROFILES:
            for title in profile.seed_titles:
                assert for_persona(title).warm_replicas >= 1, (
                    f"seeded title {title!r} resolves to {profile.name!r}, "
                    "which has no warm pool to claim from"
                )

    def test_constructing_a_zero_replica_profile_is_refused(self) -> None:
        """At import time, not at claim time: a profile nobody selects during a
        small test would otherwise ship broken."""
        with pytest.raises(ValueError, match="warm_replicas"):
            Profile(name="unclaimable", tools=frozenset(), warm_replicas=0)
