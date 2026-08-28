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


# What each tool is for, in the words the agent sees.
#
# Kept here rather than in the prompt text because the prompt is operator-
# editable and the tool catalogue is not: a persona should never be told about a
# capability its profile does not provide, and the two would drift the moment
# someone edited one and not the other.
TOOL_GUIDANCE: dict[str, str] = {
    RETRIEVE_DOCUMENTS: (
        "search the company's document library for evidence -- use it before "
        "asserting any fact about company history, policy or prior decisions"
    ),
    QUERY_BUSINESS_METRICS: (
        "run read-only SQL against the business metrics warehouse -- use it "
        "whenever a number is in question rather than estimating"
    ),
    RUN_PYTHON_ANALYSIS: (
        "write and run Python to analyse data or produce a chart -- use it when "
        "a trend or comparison would be clearer as a figure than a sentence"
    ),
    CHECK_POLICY_COMPLIANCE: (
        "check a draft against compliance rule packs -- use it before any text "
        "is circulated outside this meeting"
    ),
    SEARCH_CORPUS: (
        "search external industry literature -- use it for benchmarks, "
        "regulatory expectations and comparable cases"
    ),
    DRAFT_ARTIFACT: (
        "write a document, note or table into the meeting record -- use it when "
        "something is worth keeping beyond the transcript"
    ),
    READ_ARTIFACT: "read back something produced earlier in this meeting",
    RECORD_ACTION_ITEM: (
        "record a commitment with an owner -- use it whenever the meeting "
        "agrees someone will do something"
    ),
}


def render_tool_guidance(tools: list[str]) -> str:
    """A prompt-ready description of the tools a persona actually holds."""
    granted = [t for t in sorted(tools) if t in TOOL_GUIDANCE]
    if not granted:
        return "You have no tools available. Contribute from your own expertise."
    return "\n".join(f"- `{name}`: {TOOL_GUIDANCE[name]}" for name in granted)


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
    #: Size of this profile's SandboxWarmPool. Must be at least 1: a sandbox is
    #: only ever obtained through a SandboxClaim, and a claim can name nothing
    #: but a warm pool -- SandboxClaim.spec has `warmPoolRef` and no template
    #: ref, so there is no cold-start path to fall back to. A profile at 0 gets
    #: no SandboxWarmPool from the chart and is therefore selectable but
    #: unclaimable: the supervisor picks it, the claim fails with "SandboxWarmPool
    #: not found", and the meeting burns a turn per attempt without ever
    #: producing an utterance.
    warm_replicas: int = 1
    #: Personas seeded into this profile, matched on RoleAgent.title.
    seed_titles: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # At import time rather than at claim time. The failure this prevents is
        # invisible until a meeting happens to select the profile, which in a
        # small test may be never.
        if self.warm_replicas < 1:
            raise ValueError(
                f"profile {self.name!r} has warm_replicas={self.warm_replicas}; "
                "a claim can only name a warm pool, so anything below 1 is a "
                "profile no meeting can ever use."
            )


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
    # Quality owns the standards a build is checked against, which is the same
    # shape of work as legal compliance: deterministic checks, not opinion.
    seed_titles=("GC", "Quality"),
)

STRATEGIST = Profile(
    name="strategist",
    tools=BASELINE_TOOLS | {SEARCH_CORPUS},
    needs_corpus_egress=True,
    description="Baseline plus the external corpus.",
    warm_replicas=1,
    # Both architects. This used to be the single title "Architect", which two
    # personas shared -- so it worked by accident rather than by intent.
    seed_titles=("Chief Architect", "Solution Architect"),
)

ANALYST = Profile(
    name="analyst",
    tools=BASELINE_TOOLS | {QUERY_BUSINESS_METRICS},
    needs_metrics_dsn=True,
    description="Baseline plus read-only business metrics. Cannot execute code.",
    warm_replicas=1,
    # The inspector reads fact_quality for the same reason sales reads
    # fact_revenue: the numbers are the job. Neither may execute code.
    seed_titles=("VP", "Inspector"),
)

QUANT = Profile(
    name="quant",
    tools=BASELINE_TOOLS | {QUERY_BUSINESS_METRICS, RUN_PYTHON_ANALYSIS},
    can_execute_code=True,
    needs_metrics_dsn=True,
    description="Metrics plus model-authored analysis, executed in a network-isolated sandbox.",
    warm_replicas=1,
    # The ML engineer is the second code-execution persona, and the useful
    # contrast with the CEO: seniority does not grant an interpreter, the job
    # does.
    seed_titles=("FD", "Engineer"),
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
