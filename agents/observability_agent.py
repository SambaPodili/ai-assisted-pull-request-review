"""
agents/observability_agent.py
-------------------------------
Observability analysis using static detection (zero tokens) + optional LLM.

Static rules (run first, always):
  - Removed log statements    : `-` lines containing log./logger./logging./console.log/fmt.Print
  - Unobserved code branches  : new if/else/try added with no logging within 8 lines
  - Unobserved error paths    : raise/throw/panic added with no log/metric nearby
  - Removed metrics           : `-` lines with .increment/.observe/.counter/Metrics./prometheus/statsd/Counter/Gauge
  - New HTTP endpoints/routes : added route handlers without logging/tracing middleware nearby
  - Missing correlation ID    : new outbound service calls without context/trace/header passing

Banking context: SRE / audit trail; MAS TRM requires comprehensive logging
for financial transactions and incident response.
"""
from __future__ import annotations

import re
import time
import logging
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, AgentResultBase,
    ObservabilityFinding, ObservabilityResult,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt

log = logging.getLogger(__name__)


# ── Detection patterns ────────────────────────────────────────────────────────

# Logging call patterns (any language)
_LOG_CALL = re.compile(
    r'\b(log\.|logger\.|logging\.|console\.log|console\.warn|console\.error'
    r'|fmt\.Print|fmt\.Fprintf|fmt\.Errorf'
    r'|slog\.|zerolog\.|zap\.)',
    re.IGNORECASE,
)

# Metric call patterns
_METRIC_CALL = re.compile(
    r'(\.(increment|observe|counter|gauge|histogram|record|inc|dec|set)\s*\('
    r'|Metrics\.|prometheus\.|statsd\.|Counter\s*\(|Gauge\s*\(|Histogram\s*\()',
    re.IGNORECASE,
)

# Control flow additions
_BRANCH_ADDED   = re.compile(r'^\+\s*(if |else\b|elif |try\s*:|except\s*[:\(]|catch\s*[(\s{])')
_ERROR_ADDED    = re.compile(r'^\+\s*(raise\b|throw\b|panic\s*\()')

# HTTP route/endpoint patterns (Flask, Express, FastAPI, Spring, Go net/http)
_ROUTE_ADDED = re.compile(
    r'^\+.*(@app\.(get|post|put|patch|delete)|'
    r'router\.(get|post|put|patch|delete)|'
    r'app\.(get|post|put|patch|delete)|'
    r'@RequestMapping|@GetMapping|@PostMapping|@PutMapping|@DeleteMapping|'
    r'http\.HandleFunc|mux\.HandleFunc|r\.Handle|router\.Handle)',
    re.IGNORECASE,
)

# Middleware / logging / tracing in the same area
_TRACING_NEARBY = re.compile(
    r'\b(middleware|logging|tracing|trace|span|opentelemetry|zipkin|jaeger'
    r'|correlat|request_id|x-request-id|x-correlation-id)\b',
    re.IGNORECASE,
)

# Outbound HTTP calls (Python/JS/Go)
_HTTP_CALL = re.compile(
    r'(requests\.(get|post|put|patch|delete)|'
    r'http\.(Get|Post|Do|NewRequest)|'
    r'fetch\s*\(|axios\.(get|post|put|patch|delete)|'
    r'httpx\.(get|post|put|patch|delete))',
    re.IGNORECASE,
)

# Context / trace / header propagation
_CONTEXT_PASSING = re.compile(
    r'\b(context|ctx|headers|trace_id|correlation_id|x_request_id|span|'
    r'propagat|inject|extract)\b',
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guess_file(lines: list[str], up_to: int) -> str:
    for line in reversed(lines[:up_to]):
        if line.startswith("+++ b/"):
            return line[6:].strip()
    return "unknown"


def _window(lines: list[str], center: int, radius: int) -> str:
    """Return joined content of lines within [center-radius, center+radius]."""
    lo = max(0, center - radius)
    hi = min(len(lines), center + radius + 1)
    return "\n".join(lines[lo:hi])


# ── Static scanner ────────────────────────────────────────────────────────────

def _run_static(request: AnalysisRequest) -> list[ObservabilityFinding]:
    findings: list[ObservabilityFinding] = []

    from ingestion.diff_parser import source_line_map
    combined = "\n".join(h.content for h in request.hunks)
    lines    = combined.splitlines()
    src_map  = source_line_map(combined)
    def _ln(i): return src_map[i] if i < len(src_map) else i + 1

    for idx, line in enumerate(lines):
        is_added   = line.startswith("+") and not line.startswith("+++")
        is_removed = line.startswith("-") and not line.startswith("---")
        file_path  = _guess_file(lines, idx)

        # ── Removed log statements ─────────────────────────────────────────
        if is_removed and _LOG_CALL.search(line[1:]):
            findings.append(ObservabilityFinding(
                kind="log_removed",
                severity="high",
                description=f"Log statement removed: `{line[1:].strip()}`",
                file_path=file_path,
                line=_ln(idx),
                suggestion=(
                    "Ensure this log statement is not needed for audit, "
                    "debugging, or regulatory compliance before removing it."
                ),
            ))

        # ── Removed metrics ────────────────────────────────────────────────
        if is_removed and _METRIC_CALL.search(line[1:]):
            findings.append(ObservabilityFinding(
                kind="metric_removed",
                severity="high",
                description=f"Metric recording removed: `{line[1:].strip()}`",
                file_path=file_path,
                line=_ln(idx),
                suggestion=(
                    "Removing metrics can break dashboards and alerting. "
                    "Verify no alert depends on this metric before removing."
                ),
            ))

        # ── Unobserved code branches ───────────────────────────────────────
        if is_added and _BRANCH_ADDED.match(line):
            vicinity = _window(lines, idx, 8)
            if not _LOG_CALL.search(vicinity):
                findings.append(ObservabilityFinding(
                    kind="unobserved_branch",
                    severity="medium",
                    description=(
                        f"New control-flow branch (`{line[1:].strip()}`) "
                        "has no logging within 8 lines."
                    ),
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion=(
                        "Add a log statement (at DEBUG or INFO level) inside "
                        "this branch so its execution is observable."
                    ),
                ))

        # ── Unobserved error paths ─────────────────────────────────────────
        if is_added and _ERROR_ADDED.match(line):
            vicinity = _window(lines, idx, 5)
            has_log    = bool(_LOG_CALL.search(vicinity))
            has_metric = bool(_METRIC_CALL.search(vicinity))
            if not has_log and not has_metric:
                findings.append(ObservabilityFinding(
                    kind="unobserved_error",
                    severity="high",
                    description=(
                        f"Error raised/thrown (`{line[1:].strip()}`) "
                        "with no surrounding log or metric emission."
                    ),
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion=(
                        "Log the error before raising it and increment an error "
                        "counter so on-call engineers can detect and triage."
                    ),
                ))

        # ── New HTTP routes without tracing middleware ──────────────────────
        if is_added and _ROUTE_ADDED.match(line):
            vicinity = _window(lines, idx, 10)
            if not _TRACING_NEARBY.search(vicinity):
                findings.append(ObservabilityFinding(
                    kind="missing_tracing",
                    severity="medium",
                    description=(
                        f"New HTTP endpoint (`{line[1:].strip()}`) "
                        "has no logging or tracing middleware detected nearby."
                    ),
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion=(
                        "Apply request-logging and distributed-tracing middleware "
                        "(e.g. OpenTelemetry) to all new endpoints."
                    ),
                ))

        # ── Missing correlation ID propagation ─────────────────────────────
        if is_added and _HTTP_CALL.search(line[1:]):
            vicinity = _window(lines, idx, 5)
            if not _CONTEXT_PASSING.search(vicinity):
                findings.append(ObservabilityFinding(
                    kind="missing_correlation_id",
                    severity="medium",
                    description=(
                        f"Outbound service call (`{line[1:].strip()}`) "
                        "without apparent context/trace/correlation-ID propagation."
                    ),
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion=(
                        "Inject trace context (e.g. W3C traceparent or "
                        "X-Correlation-ID header) into outbound calls so distributed "
                        "traces remain contiguous."
                    ),
                ))

    return findings


def _dedup(findings: list[ObservabilityFinding]) -> list[ObservabilityFinding]:
    seen: set[tuple] = set()
    out:  list[ObservabilityFinding] = []
    for f in findings:
        key = (f.kind, f.line, f.file_path)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _build_result(findings: list[ObservabilityFinding]) -> ObservabilityResult:
    logs_removed        = sum(1 for f in findings if f.kind == "log_removed")
    metrics_removed     = sum(1 for f in findings if f.kind == "metric_removed")
    unobserved_branches = sum(1 for f in findings if f.kind in ("unobserved_branch", "unobserved_error"))

    if any(f.severity == "high" for f in findings):
        overall = "high"
    elif any(f.severity == "medium" for f in findings):
        overall = "medium"
    else:
        overall = "low"

    summary = (
        f"{len(findings)} observability issue(s) detected "
        f"({logs_removed} log(s) removed, {metrics_removed} metric(s) removed, "
        f"{unobserved_branches} unobserved branch(es))."
        if findings else "No observability issues detected."
    )

    return ObservabilityResult(
        findings=findings,
        logs_removed=logs_removed,
        metrics_removed=metrics_removed,
        unobserved_branches=unobserved_branches,
        overall_severity=overall,
        summary=summary,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class ObservabilityAgent(BaseAgent[ObservabilityResult]):

    agent_name   = AgentName.OBSERVABILITY
    output_model = ObservabilityResult

    system_prompt = (
        "You are an observability and SRE expert for enterprise banking systems. "
        "Analyse the provided code diff and identify: removed log statements, removed metrics, "
        "unobserved code branches or error paths, missing distributed tracing, "
        "and missing correlation-ID propagation in outbound service calls. "
        "For each finding provide: kind, severity (low|medium|high), description, "
        "file_path, line number, and a concrete suggestion. "
        "Also compute logs_removed, metrics_removed, unobserved_branches, "
        "overall_severity, and a brief summary. "
        "Output ONLY compact JSON."
    )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> ObservabilityResult:
        ctx = context or {}

        # ── Static analysis first (always) ────────────────────────────────────
        t0             = time.monotonic()
        static_findings = _run_static(request)

        # ── LLM enrichment ────────────────────────────────────────────────────
        try:
            llm_result   = super().run(request, budget, ctx)
            llm_findings = llm_result.findings if not llm_result.fallback_used else []
        except Exception as exc:
            log.warning("ObservabilityAgent: LLM call failed, using static only: %s", exc)
            llm_findings = []

        all_findings = _dedup(static_findings + llm_findings)
        result = _build_result(all_findings)
        result.duration_s = round(time.monotonic() - t0, 2)
        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        hunks_text = format_hunks_for_prompt(
            request.hunks,
            max_chars_per_hunk=2000,
            focus="general",
        )
        return (
            "Analyse the following code diff for observability issues "
            "(removed logs, removed metrics, unobserved branches, missing tracing).\n\n"
            f"{hunks_text}"
        )

    def fallback_result(self, request: AnalysisRequest) -> ObservabilityResult:
        findings = _run_static(request)
        result   = _build_result(findings)
        result.fallback_used = True
        return result
