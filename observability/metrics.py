"""
observability/metrics.py
-------------------------
Prometheus metrics for the impact analysis framework.

Exposes a /metrics endpoint (Prometheus scrape format) via FastAPI.
Also provides helper functions to record metrics from agent and orchestrator code.

Falls back gracefully when prometheus_client is not installed.
"""
from __future__ import annotations
import logging
import time

log = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter, Histogram, Gauge, Summary,
        generate_latest, CONTENT_TYPE_LATEST,
        CollectorRegistry, REGISTRY,
    )
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False
    log.info("prometheus_client not installed — metrics endpoint disabled")


# ── Metric definitions ────────────────────────────────────────────────────────

if HAS_PROMETHEUS:
    # Analysis-level metrics
    ANALYSES_TOTAL = Counter(
        "impact_analyses_total",
        "Total number of analysis runs started",
        ["provider", "change_type"],
    )
    ANALYSES_COMPLETED = Counter(
        "impact_analyses_completed_total",
        "Total number of analysis runs completed",
        ["gate_decision", "risk_level", "phase"],
    )
    ANALYSIS_DURATION = Histogram(
        "impact_analysis_duration_seconds",
        "End-to-end analysis duration in seconds",
        ["phase"],
        buckets=[1, 5, 15, 30, 60, 120, 300],
    )

    # Agent-level metrics
    AGENT_CALLS_TOTAL = Counter(
        "impact_agent_calls_total",
        "Total LLM API calls per agent",
        ["agent", "model", "fallback"],
    )
    AGENT_TOKENS_TOTAL = Counter(
        "impact_agent_tokens_total",
        "Total tokens consumed per agent",
        ["agent", "model"],
    )
    AGENT_DURATION = Histogram(
        "impact_agent_duration_seconds",
        "Agent execution duration in seconds",
        ["agent"],
        buckets=[0.5, 1, 2, 5, 10, 30],
    )
    AGENT_FAILURES = Counter(
        "impact_agent_failures_total",
        "Agent failures (LLM errors, parse errors)",
        ["agent"],
    )

    # Circuit breaker state gauge
    CIRCUIT_BREAKER_STATE = Gauge(
        "impact_circuit_breaker_open",
        "1 if circuit breaker is OPEN, 0 otherwise",
        ["agent"],
    )

    # Gate decisions
    GATE_DECISIONS = Counter(
        "impact_gate_decisions_total",
        "Gate decisions produced by the risk agent",
        ["decision"],
    )
    GATE_OVERRIDES = Counter(
        "impact_gate_overrides_total",
        "Human override events",
        ["original", "new"],
    )

    # Token budget
    TOKEN_BUDGET_USED = Histogram(
        "impact_token_budget_used_fraction",
        "Fraction of token budget consumed per run (0-1)",
        buckets=[0.1, 0.25, 0.5, 0.75, 0.9, 1.0],
    )


# ── Helper functions ──────────────────────────────────────────────────────────

def record_analysis_start(provider: str, change_type: str) -> float:
    if HAS_PROMETHEUS:
        ANALYSES_TOTAL.labels(provider=provider, change_type=change_type).inc()
    return time.monotonic()


def record_analysis_complete(
    start_time:   float,
    gate:         str,
    risk:         str,
    phase:        int,
    tokens_used:  int,
    tokens_budget: int,
) -> None:
    if not HAS_PROMETHEUS:
        return
    duration = time.monotonic() - start_time
    ANALYSES_COMPLETED.labels(gate_decision=gate, risk_level=risk, phase=str(phase)).inc()
    ANALYSIS_DURATION.labels(phase=str(phase)).observe(duration)
    GATE_DECISIONS.labels(decision=gate).inc()
    if tokens_budget > 0:
        TOKEN_BUDGET_USED.observe(min(1.0, tokens_used / tokens_budget))


def record_agent_call(
    agent:    str,
    model:    str,
    tokens:   int,
    duration: float,
    fallback: bool,
    failed:   bool = False,
) -> None:
    if not HAS_PROMETHEUS:
        return
    AGENT_CALLS_TOTAL.labels(agent=agent, model=model, fallback=str(fallback)).inc()
    AGENT_TOKENS_TOTAL.labels(agent=agent, model=model).inc(tokens)
    AGENT_DURATION.labels(agent=agent).observe(duration)
    if failed:
        AGENT_FAILURES.labels(agent=agent).inc()


def update_circuit_breaker(agent: str, is_open: bool) -> None:
    if HAS_PROMETHEUS:
        CIRCUIT_BREAKER_STATE.labels(agent=agent).set(1 if is_open else 0)


def record_gate_override(original: str, new_gate: str) -> None:
    if HAS_PROMETHEUS:
        GATE_OVERRIDES.labels(original=original, new=new_gate).inc()


# ── FastAPI route ─────────────────────────────────────────────────────────────

def make_metrics_response():
    """Return a FastAPI Response with Prometheus text exposition format."""
    if not HAS_PROMETHEUS:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("# prometheus_client not installed\n", status_code=200)
    from fastapi.responses import Response
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
