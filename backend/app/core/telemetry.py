"""Tracing and metrics.

The interesting property of this system is that a single meeting turn crosses
three trust boundaries -- backend, persona sandbox, exec sandbox -- and each is
a separate process with its own network policy. A trace that stops at the first
boundary would describe the least interesting third of the work.

So the W3C ``traceparent`` header is propagated over the sandbox RPC, and the
sandbox propagates it again when it claims an exec sandbox. One turn renders as
one trace spanning all three tiers, which is the clearest single artifact this
project can produce.

Everything here degrades quietly: with no OTLP endpoint configured, tracing
becomes a no-op and metrics still work. Observability that refuses to start the
application is worse than no observability.
"""

from __future__ import annotations

import structlog
from prometheus_client import Counter, Gauge, Histogram

from app.core.config import settings

logger = structlog.get_logger(__name__)

# --- metrics ---------------------------------------------------------------
#
# Labels are deliberately low-cardinality: agent *profile* rather than agent id,
# because a label per persona multiplies series for no analytical gain.

MEETING_TURNS = Counter(
    "meetings_turns_total",
    "Agent turns attempted, by capability profile and outcome.",
    ["profile", "outcome"],
)

TURN_DURATION = Histogram(
    "meetings_turn_duration_seconds",
    "Wall time for one agent turn, including sandbox acquisition and the model call.",
    ["profile"],
    # A turn is seconds-to-minutes depending on the model; the default buckets
    # top out far too low to be useful here.
    buckets=(1, 2.5, 5, 10, 20, 40, 80, 160, 320),
)

TOOL_CALLS = Counter(
    "meetings_tool_calls_total",
    "Tool invocations by profile, tool and outcome. outcome=denied means the "
    "cluster refused -- this is the least-privilege signal.",
    ["profile", "tool", "outcome"],
)

SANDBOX_ACQUIRE = Histogram(
    "meetings_sandbox_acquire_seconds",
    "Time to obtain a persona sandbox. Warm claims are sub-second; a cold "
    "gVisor pod start is seconds, which is what the warm pool exists to avoid.",
    ["profile"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 180),
)

SANDBOXES_ACTIVE = Gauge(
    "meetings_sandboxes_active",
    "Persona sandboxes currently held by running meetings.",
    ["profile"],
)

LLM_TOKENS = Counter(
    "meetings_llm_tokens_total",
    "Tokens consumed, by profile and direction.",
    ["profile", "direction"],
)


# A labelled Prometheus metric has no child series until something observes it,
# so on a freshly deployed backend /metrics carries no meetings_* series at all
# and every dashboard panel reads "no data" -- identical to the panel you get
# when scraping is genuinely broken. That ambiguity cost real debugging time, so
# the series that can exist are created at zero up front. "No data" then means
# something is actually wrong, and a profile that never speaks is visibly idle
# rather than absent.
#
# Only combinations that can actually occur: a profile is pre-seeded with its
# own tools, not with every tool, so the tool panels show the capability matrix
# rather than a grid of impossible pairs.
TURN_OUTCOMES = ("ok", "error", "no_sandbox")
TOOL_OUTCOMES = ("ok", "error", "denied")
TOKEN_DIRECTIONS = ("prompt", "completion")


def preregister_metric_series() -> None:
    """Create every series that can occur, at zero. Safe to call more than once."""
    from app.orchestration.profiles import PROFILES

    for profile in PROFILES:
        for outcome in TURN_OUTCOMES:
            MEETING_TURNS.labels(profile=profile.name, outcome=outcome)
        for direction in TOKEN_DIRECTIONS:
            LLM_TOKENS.labels(profile=profile.name, direction=direction)
        TURN_DURATION.labels(profile=profile.name)
        SANDBOX_ACQUIRE.labels(profile=profile.name)
        SANDBOXES_ACTIVE.labels(profile=profile.name)
        for tool in sorted(profile.tools):
            for outcome in TOOL_OUTCOMES:
                TOOL_CALLS.labels(profile=profile.name, tool=tool, outcome=outcome)


# --- tracing ---------------------------------------------------------------

_tracer = None


def setup_telemetry(app: object) -> None:
    """Wire tracing to the configured OTLP endpoint, if there is one."""
    global _tracer

    # Unconditional, and before the tracing check below: metrics are served
    # whether or not traces have anywhere to go, and this is what makes an
    # idle-but-healthy backend distinguishable from an unscraped one.
    preregister_metric_series()

    if not settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        logger.info("tracing_disabled_no_endpoint")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.OTEL_SERVICE_NAME,
                    "service.version": settings.VERSION,
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True)
            )
        )
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)  # type: ignore[arg-type]
        # Instrumenting httpx is what carries traceparent to the sandboxes, so
        # the three tiers land in one trace rather than three unrelated ones.
        HTTPXClientInstrumentor().instrument()

        _tracer = trace.get_tracer(__name__)
        logger.info("tracing_enabled", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    except Exception as exc:
        # Never fatal. An application that will not start because its telemetry
        # backend is down has made observability a liability.
        logger.warning("tracing_setup_failed", error=f"{type(exc).__name__}: {exc}")


def tracer() -> object | None:
    return _tracer


def record_tool_result(profile: str, tool: str, ok: bool, denied: bool) -> None:
    """Record one tool invocation.

    `denied` is kept distinct from `ok=False` on purpose: a refusal by the
    cluster and a tool that errored are different events, and collapsing them
    would hide precisely the signal this project exists to show.
    """
    outcome = "denied" if denied else ("ok" if ok else "error")
    TOOL_CALLS.labels(profile=profile, tool=tool, outcome=outcome).inc()
