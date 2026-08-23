"""Tracing inside the persona sandbox.

The backend sends a W3C ``traceparent`` header with each turn. FastAPI
instrumentation picks it up, so spans created here attach to the backend's trace
rather than starting a new one -- and httpx instrumentation carries it onward
when this sandbox claims an exec sandbox.

The result is one trace per turn spanning backend, persona sandbox and exec
sandbox: three processes, three network policies, one picture.

Degrades to a no-op with no OTLP endpoint configured. A sandbox that refuses to
serve because its tracing backend is unreachable would be a poor trade.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "meetings-persona-runtime")


def setup_telemetry(app: object) -> None:
    if not OTLP_ENDPOINT:
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
                    "service.name": SERVICE_NAME,
                    # The profile is on the resource rather than each span: it is
                    # a property of the sandbox, and it makes "show me every span
                    # from a counsel sandbox" a single filter.
                    "meetings.profile": os.getenv("MEETINGS_PROFILE", "unknown"),
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
        )
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)  # type: ignore[arg-type]
        HTTPXClientInstrumentor().instrument()
        logger.info("tracing_enabled", endpoint=OTLP_ENDPOINT, service=SERVICE_NAME)
    except Exception as exc:
        logger.warning("tracing_setup_failed", error=f"{type(exc).__name__}: {exc}")
