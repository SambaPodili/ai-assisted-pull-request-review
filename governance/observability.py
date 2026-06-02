"""
governance/observability.py
----------------------------
Observability for the impact analysis framework.

Two backends (both optional, graceful no-ops if not installed):
  1. OpenTelemetry  — distributed tracing per agent call (OTLP export)
  2. Prometheus     — metrics endpoint at GET /metrics

Key metrics exposed:
  • impact_analysis_total{phase, gate, risk}  — counter per run
  • impact_analysis_duration_seconds{phase}   — histogram
  • impact_agent_tokens_used{agent, model}    — counter
  • impact_agent_fallback_total{agent}        — counter (fallback invocations)
  • impact_gate_decisions{gate}               — counter
"""
from __future__ import annotations
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

log = logging.getLogger(__name__)

# ── Optional OpenTelemetry ────────────────────────────────────────────────────

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False
    log.info("opentelemetry not installed — tracing disabled")

# ── Optional Prometheus ───────────────────────────────────────────────────────

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _HAS_PROM = True

    _ANALYSIS_TOTAL = Counter(
        "impact_analysis_total",
        "Total analysis runs",
        ["phase", "gate", "risk"],
    )
    _ANALYSIS_DURATION = Histogram(
        "impact_analysis_duration_seconds",
        "Analysis run duration",
        ["phase"],
        buckets=[1, 5, 10, 30, 60, 120, 300],
    )
    _AGENT_TOKENS = Counter(
        "impact_agent_tokens_used_total",
        "Tokens consumed per agent",
        ["agent", "model"],
    )
    _AGENT_FALLBACK = Counter(
        "impact_agent_fallback_total",
        "Fallback path invocations per agent",
        ["agent"],
    )
    _GATE_DECISIONS = Counter(
        "impact_gate_decisions_total",
        "Gate decisions by type",
        ["gate"],
    )

except ImportError:
    _HAS_PROM = False
    log.info("prometheus_client not installed — metrics endpoint disabled")


# ── Tracer singleton ──────────────────────────────────────────────────────────

_tracer: Any = None


def init_tracing(otlp_endpoint: str = "") -> None:
    """Call once at startup to configure OTLP tracing."""
    global _tracer
    if not _HAS_OTEL:
        return
    provider = TracerProvider()
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        # BatchSpanProcessor logs retries at WARNING and final failures at ERROR
        # when the collector is unreachable.  Suppress both by raising the
        # threshold to CRITICAL so a missing Jaeger instance never floods logs.
        for _ln in ("opentelemetry.sdk.trace.export",
                    "opentelemetry.exporter.otlp.proto.grpc.exporter",
                    "opentelemetry.exporter.otlp.proto.common"):
            logging.getLogger(_ln).setLevel(logging.CRITICAL)
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("impact-analyzer")
    log.info("OpenTelemetry tracing initialised (endpoint=%s)", otlp_endpoint or "none")


def get_tracer() -> Any:
    return _tracer


# ── Context manager for agent spans ──────────────────────────────────────────

@contextmanager
def agent_span(agent_name: str, request_id: str) -> Generator[None, None, None]:
    """Wrap an agent call in an OpenTelemetry span."""
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("agent.name",      agent_name)
        span.set_attribute("analysis.request_id", request_id)
        try:
            yield
        except Exception as exc:
            span.record_exception(exc)
            raise


@contextmanager
def analysis_span(request_id: str, phase: int) -> Generator[None, None, None]:
    """Wrap a full analysis run in a root span + duration measurement."""
    start = time.monotonic()
    if _tracer:
        with _tracer.start_as_current_span("analysis.run") as span:
            span.set_attribute("analysis.request_id", request_id)
            span.set_attribute("analysis.phase",      phase)
            try:
                yield
            except Exception as exc:
                span.record_exception(exc)
                raise
            finally:
                duration = time.monotonic() - start
                if _HAS_PROM:
                    _ANALYSIS_DURATION.labels(phase=str(phase)).observe(duration)
    else:
        yield
        duration = time.monotonic() - start
        if _HAS_PROM:
            _ANALYSIS_DURATION.labels(phase=str(phase)).observe(duration)


# ── Metric recording helpers ──────────────────────────────────────────────────

def record_analysis_complete(phase: int, gate: str, risk: str) -> None:
    if _HAS_PROM:
        _ANALYSIS_TOTAL.labels(phase=str(phase), gate=gate, risk=risk).inc()
        _GATE_DECISIONS.labels(gate=gate).inc()


def record_agent_tokens(agent: str, model: str, tokens: int) -> None:
    if _HAS_PROM:
        _AGENT_TOKENS.labels(agent=agent, model=model).inc(tokens)


def record_agent_fallback(agent: str) -> None:
    if _HAS_PROM:
        _AGENT_FALLBACK.labels(agent=agent).inc()


# ── In-memory metrics collector (works without Prometheus) ────────────────────

@dataclass
class InMemoryMetrics:
    """Lightweight metrics store for when Prometheus is not installed."""
    analysis_count:   int = 0
    total_tokens:     int = 0
    gate_counts:      dict[str, int] = field(default_factory=dict)
    agent_tokens:     dict[str, int] = field(default_factory=dict)
    fallback_counts:  dict[str, int] = field(default_factory=dict)
    duration_sum_s:   float = 0.0

    def record_complete(self, gate: str, risk: str, tokens: int, duration: float) -> None:
        self.analysis_count += 1
        self.total_tokens   += tokens
        self.duration_sum_s += duration
        self.gate_counts[gate] = self.gate_counts.get(gate, 0) + 1

    def record_tokens(self, agent: str, tokens: int) -> None:
        self.agent_tokens[agent] = self.agent_tokens.get(agent, 0) + tokens
        self.total_tokens += tokens

    def record_fallback(self, agent: str) -> None:
        self.fallback_counts[agent] = self.fallback_counts.get(agent, 0) + 1

    def summary(self) -> dict:
        return {
            "analysis_count":    self.analysis_count,
            "total_tokens":      self.total_tokens,
            "avg_duration_s":    round(self.duration_sum_s / max(1, self.analysis_count), 2),
            "gate_distribution": self.gate_counts,
            "agent_token_usage": self.agent_tokens,
            "fallback_counts":   self.fallback_counts,
        }


_in_memory_metrics = InMemoryMetrics()


def get_in_memory_metrics() -> InMemoryMetrics:
    return _in_memory_metrics


# ── Prometheus endpoint helpers ───────────────────────────────────────────────

def prometheus_metrics_response() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    if not _HAS_PROM:
        body = b"# prometheus_client not installed\n"
        return body, "text/plain"
    return generate_latest(), CONTENT_TYPE_LATEST
