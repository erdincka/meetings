"""Capability profiles.

A persona's `default_tools` column says what it should be able to do. This
module maps that request onto a *profile*, and the profile is what actually
gets provisioned in Kubernetes: a SandboxTemplate, a ServiceAccount, a
NetworkPolicy, and a set of mounted Secrets.

The distinction matters. The tool list in a prompt is a suggestion; the profile
is enforcement. One runtime image serves every profile, so nothing about the
image decides what an agent may do — only the Kubernetes objects its sandbox is
created from.

Profiles are ordered from least to most capable, and resolution picks the
*smallest* one that covers what a persona asked for. A persona that needs only
document retrieval never lands in a profile that can execute code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- the tool catalogue ----------------------------------------------------

RETRIEVE_DOCUMENTS = "retrieve_documents"
DRAFT_ARTIFACT = "draft_artifact"
READ_ARTIFACT = "read_artifact"
RECORD_ACTION_ITEM = "record_action_item"
QUERY_BUSINESS_METRICS = "query_business_metrics"
RUN_PYTHON_ANALYSIS = "run_python_analysis"
CHECK_POLICY_COMPLIANCE = "check_policy_compliance"
SEARCH_CORPUS = "search_corpus"

ALL_TOOLS = frozenset(
    {
        RETRIEVE_DOCUMENTS,
        DRAFT_ARTIFACT,
        READ_ARTIFACT,
        RECORD_ACTION_ITEM,
        QUERY_BUSINESS_METRICS,
        RUN_PYTHON_ANALYSIS,
        CHECK_POLICY_COMPLIANCE,
        SEARCH_CORPUS,
    }
)

# Every persona can do these: read the shared library, write a draft, and record
# an action item. None of them reach anything outside the backend's own API.
BASELINE_TOOLS = frozenset({RETRIEVE_DOCUMENTS, DRAFT_ARTIFACT, READ_ARTIFACT, RECORD_ACTION_ITEM})


@dataclass(frozen=True)
class Profile:
    """A provisioned capability set.

    ``name`` is not cosmetic: it selects the SandboxTemplate, the warm pool and
    the ServiceAccount, so renaming one means renaming Kubernetes objects.
    """

    name: str
    tools: frozenset[str]
    #: Requires apiserver permission to create a SandboxClaim for code execution.
    #: Only profiles with this get bound to the exec-sandbox-claimer Role, so a
    #: persona without it is refused by the apiserver rather than by a prompt.
    can_execute_code: bool = False
    #: Requires the read-only metrics DSN mounted from a Secret.
    needs_metrics_dsn: bool = False
    #: Requires egress to the corpus service.
    needs_corpus_egress: bool = False
    description: str = ""
    warm_replicas: int = 0
    #: Personas seeded into this profile, matched on RoleAgent.title.
    seed_titles: tuple[str, ...] = field(default_factory=tuple)


BASELINE = Profile(
    name="baseline",
    tools=BASELINE_TOOLS,
    description="Reads the shared library, drafts artifacts, records actions.",
    warm_replicas=2,
)

COUNSEL = Profile(
    name="counsel",
    tools=BASELINE_TOOLS | {CHECK_POLICY_COMPLIANCE},
    description="Baseline plus deterministic policy checks.",
    warm_replicas=1,
    seed_titles=("GC",),
)

STRATEGIST = Profile(
    name="strategist",
    tools=BASELINE_TOOLS | {SEARCH_CORPUS},
    needs_corpus_egress=True,
    description="Baseline plus the external corpus.",
    warm_replicas=0,
    seed_titles=("Architect",),
)

ANALYST = Profile(
    name="analyst",
    tools=BASELINE_TOOLS | {QUERY_BUSINESS_METRICS},
    needs_metrics_dsn=True,
    description="Baseline plus read-only business metrics. Cannot execute code.",
    warm_replicas=1,
    seed_titles=("VP",),
)

QUANT = Profile(
    name="quant",
    tools=BASELINE_TOOLS | {QUERY_BUSINESS_METRICS, RUN_PYTHON_ANALYSIS},
    can_execute_code=True,
    needs_metrics_dsn=True,
    description="Metrics plus model-authored analysis, executed in a network-isolated sandbox.",
    warm_replicas=1,
    seed_titles=("FD",),
)

CHIEF = Profile(
    name="chief",
    # Deliberately no code execution. Seniority is not a reason to hand someone
    # a Python interpreter, and the contrast between chief and analyst is what
    # makes the RBAC boundary legible.
    tools=BASELINE_TOOLS | {QUERY_BUSINESS_METRICS, CHECK_POLICY_COMPLIANCE, SEARCH_CORPUS},
    needs_metrics_dsn=True,
    needs_corpus_egress=True,
    description="Broad read access across metrics, policy and corpus. No code execution.",
    warm_replicas=1,
    seed_titles=("CEO",),
)

# Least to most capable. Resolution walks this in order, so a persona that asks
# only to read numbers lands in `analyst` and never in `quant` -- reading metrics
# is not a reason to be handed a Python interpreter.
PROFILES: tuple[Profile, ...] = (BASELINE, COUNSEL, STRATEGIST, ANALYST, QUANT, CHIEF)
PROFILES_BY_NAME = {p.name: p for p in PROFILES}


class ProfileDriftError(ValueError):
    """A persona asked for tools no provisioned profile can supply.

    Raised at meeting start rather than mid-turn, so the mismatch between what
    the UI advertises and what the cluster will permit surfaces as a refusal to
    start with a precise message — not as an agent that silently cannot do its
    job.
    """


def resolve(requested: list[str] | None) -> Profile:
    """Smallest profile covering the requested tools.

    An empty or unset request means baseline: a persona with nothing configured
    gets the least capability, never the most.
    """
    wanted = {t for t in (requested or []) if t}
    if not wanted:
        return BASELINE

    unknown = wanted - ALL_TOOLS
    if unknown:
        raise ProfileDriftError(
            f"Unknown tools requested: {', '.join(sorted(unknown))}. "
            f"Known tools: {', '.join(sorted(ALL_TOOLS))}."
        )

    for profile in PROFILES:
        if wanted <= profile.tools:
            return profile

    closest = max(PROFILES, key=lambda p: len(wanted & p.tools))
    missing = wanted - closest.tools
    raise ProfileDriftError(
        f"No profile provides {', '.join(sorted(missing))} together with the rest "
        f"of the request. Closest is '{closest.name}'."
    )


def for_persona(title: str) -> Profile:
    """Profile a seeded persona should get, matched on its title."""
    for profile in PROFILES:
        if title in profile.seed_titles:
            return profile
    return BASELINE
