"""
observability/telemetry.py
---------------------------
OpenTelemetry distributed tracing for the impact analysis framework.

Every agent call gets a child span under the parent analysis span,
recording: agent name, model used, tokens consumed, fallback status,
and the gate decision on the final span.

Falls back gracefully when opentelemetry is not installed.
"""
from __future__ import annotations
import contextlib
import logging
from typing import Any, Generator

log = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Span, Status, StatusCode
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False
    log.info("opentelemetry not installed — tracing disabled")

# Service name used in all spans
_SERVICE_NAME = "impact-analyzer"


def setup_tracing(otlp_endpoint: str = "") -> None:
    """
    Initialise the global tracer provider.
    Call once at app startup in api/app.py.

    otlp_endpoint: e.g. "http://jaeger:4317" — leave empty for console export.
    """
    if not HAS_OTEL:
        return

    resource = Resource.create({"service.name": _SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        except ImportError:
            log.warning("OTLP exporter not installed — falling back to console")
            exporter = ConsoleSpanExporter()
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    log.info("OpenTelemetry tracing initialised (endpoint=%s)", otlp_endpoint or "console")


def _get_tracer():
    if HAS_OTEL:
        return trace.get_tracer(_SERVICE_NAME)
    return None


@contextlib.contextmanager
def analysis_span(request_id: str, repo_url: str) -> Generator[Any, None, None]:
    """Root span for one complete analysis run."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span("analysis_run") as span:
        span.set_attribute("request.id",  request_id)
        span.set_attribute("repo.url",    repo_url)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            raise


@contextlib.contextmanager
def agent_span(
    agent_name: str,
    model:      str = "",
    parent_ctx: Any = None,
) -> Generator[Any, None, None]:
    """Child span for a single agent execution."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("agent.name",  agent_name)
        span.set_attribute("agent.model", model)
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, str(exc))
            raise


def record_agent_result(span: Any, tokens: int, fallback: bool, gate: str = "") -> None:
    """Attach result metadata to an agent span."""
    if span is None or not HAS_OTEL:
        return
    span.set_attribute("agent.tokens",   tokens)
    span.set_attribute("agent.fallback", fallback)
    if gate:
        span.set_attribute("gate.decision", gate)
