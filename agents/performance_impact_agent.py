"""
agents/performance_impact_agent.py
------------------------------------
Performance impact analysis: static detection (zero tokens) + optional LLM
enhancement for banking microservices.

Static pass detects:
  - N+1 queries: loop on added lines followed by ORM/DB call
  - SELECT *: unbounded queries without LIMIT
  - Nested loops with large body suggesting O(n²)
  - Synchronous I/O inside async functions
  - Missing pagination on collection queries
  - Memory risk: reading entire files or all records into memory
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.models import (
    AgentName, AgentResultBase, AnalysisRequest, DiffHunk,
    PerformanceFinding, PerformanceImpactResult,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt

log = logging.getLogger(__name__)

# ── AgentName resolution ──────────────────────────────────────────────────────
try:
    _AGENT_NAME: AgentName = AgentName.PERFORMANCE_IMPACT  # type: ignore[attr-defined]
except AttributeError:
    _AGENT_NAME = "performance_impact"  # type: ignore[assignment]


# ═════════════════════════════════════════════════════════════════════════════
#  Static-detection patterns
# ═════════════════════════════════════════════════════════════════════════════

# ORM / DB call patterns
_DB_CALL_RE = re.compile(
    r'\.objects\.|\.query\s*\(|\.find\s*\(|\.execute\s*\(|\.fetch\s*\(|'
    r'cursor\.execute|session\.query|'
    # Java / Spring Data: repository.findById(…) / findByOrderId(…) / getOne(…),
    # jdbcTemplate.queryForObject(…), entityManager calls, getResultList()
    r'\.find[A-Z]\w*\s*\(|\.getOne\s*\(|\.getById\s*\(|'
    r'jdbc\w*\.|entityManager\.|\.queryFor\w+\s*\(|\.getResultList\s*\(',
    re.IGNORECASE,
)

# Loop keywords (for/while in added code; Java enhanced-for uses 'for (')
_LOOP_RE = re.compile(r'^\s*(?:for|while)\s*[\s(]', re.IGNORECASE)

# SELECT * pattern
_SELECT_STAR_RE = re.compile(r'SELECT\s+\*|\.all\(\)', re.IGNORECASE)

# LIMIT / paginate nearby guard
_LIMIT_RE = re.compile(r'\.limit\s*\(|\.paginate\s*\(|LIMIT\s+\d', re.IGNORECASE)

# Nested loop — second loop keyword within a block
_INNER_LOOP_RE = re.compile(r'^\s{4,}(?:for|while)\s+', re.IGNORECASE)

# Sync I/O inside async context
_SYNC_IO_RE = re.compile(r'requests\.(get|post|put|patch|delete|head)\s*\(', re.IGNORECASE)
_ASYNC_DEF_RE = re.compile(r'^\s*async\s+def\s+', re.IGNORECASE)

# Memory risk: read entire file or all records
_MEMORY_RISK_RE = re.compile(
    r'open\s*\(.*\)\s*\.read\s*\(\s*\)|'
    r'\.readlines\s*\(\s*\)|'
    r'\.read\s*\(\s*\)',
    re.IGNORECASE,
)

# Severity ladder
_SEVERITY_ORDER = ["low", "medium", "high", "critical"]


def _max_severity(sev_list: list[str]) -> str:
    """Return the highest severity from a list of severity strings."""
    if not sev_list:
        return "low"
    return max(sev_list, key=lambda s: _SEVERITY_ORDER.index(s) if s in _SEVERITY_ORDER else 0)


# ═════════════════════════════════════════════════════════════════════════════
#  Agent
# ═════════════════════════════════════════════════════════════════════════════

class PerformanceImpactAgent(BaseAgent[PerformanceImpactResult]):

    agent_name   = _AGENT_NAME
    output_model = PerformanceImpactResult

    system_prompt = (
        "You are a performance engineering expert specialising in banking microservices.\n"
        "Analyse the code diff for performance regressions:\n"
        "  • N+1 query patterns (loop + ORM/DB call)\n"
        "  • Unbounded SELECT * without LIMIT or pagination\n"
        "  • O(n²) complexity from nested loops over large datasets\n"
        "  • Synchronous blocking I/O inside async request handlers\n"
        "  • Missing pagination on collection endpoints\n"
        "  • Memory spikes from reading entire files or all DB records\n\n"
        "For each finding: category, severity (low/medium/high/critical), description, "
        "file_path, line number, and a concrete suggestion.\n"
        "Set has_db_risk=true when any DB performance issue is found.\n"
        "Set has_complexity_regression=true for O(n²) or worse.\n"
        "overall_severity = worst finding severity.\n"
        "Output ONLY compact JSON matching the PerformanceImpactResult schema."
    )

    # ── Public interface ──────────────────────────────────────────────────────

    def run(
        self,
        request: AnalysisRequest,
        budget: Any,
        context: dict[str, Any] | None = None,
    ) -> PerformanceImpactResult:
        ctx = context or {}
        t0  = time.monotonic()

        # Step 1: deterministic static pass (zero tokens)
        static_findings = self.detect_static_issues(request)

        # Step 2: attempt LLM enhancement if budget permits
        agent_key = self.agent_name if isinstance(self.agent_name, str) else self.agent_name.value
        remaining = budget.get_remaining(agent_key) if hasattr(budget, "get_remaining") else 0
        llm_result: PerformanceImpactResult | None = None

        llm_attempted = remaining > 1500
        if llm_attempted:
            try:
                # Pass static_findings via context so build_user_prompt reuses them
                # instead of running detect_static_issues a second time.
                ctx["_static_findings"] = static_findings
                llm_result = super().run(request, budget, ctx)
            except Exception as exc:
                log.warning("[%s] PerformanceImpactAgent LLM error: %s", request.request_id, exc)

        # Step 3: merge static + LLM findings
        if llm_result is not None:
            seen = {(f.file_path, f.line, f.category) for f in static_findings}
            for f in llm_result.findings:
                if (f.file_path, f.line, f.category) not in seen:
                    static_findings.append(f)
            result = llm_result
            result.findings = static_findings
        else:
            result = self._build_result_from_static(static_findings)
            result.fallback_used = True
            result.duration_s    = round(time.monotonic() - t0, 2)

        # Ensure derived fields are correct regardless of path
        result.has_db_risk = any(
            f.category in ("n_plus_1", "select_star", "no_pagination") for f in result.findings
        )
        result.has_complexity_regression = any(
            f.category == "nested_loop" for f in result.findings
        )
        if result.findings:
            result.overall_severity = _max_severity([f.severity for f in result.findings])

        # If we never entered the LLM branch, base run() never reported progress —
        # emit it here so the agent still shows in the live panel and Timings.
        if not llm_attempted:
            self.report_static_progress(request, result.duration_s)

        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        # Reuse static findings computed in run() if available — avoids double hunk scan
        static_findings = context.get("_static_findings") or self.detect_static_issues(request)
        findings_str = "\n".join(
            f"  [{f.severity.upper()}] {f.file_path}:{f.line} — {f.category}: {f.description}"
            for f in static_findings
        ) or "  (none detected by static scan)"

        diff_block = format_hunks_for_prompt(
            request.hunks,
            max_chars_per_hunk=2000,
            focus="general",
        )
        return (
            f"Repository: {request.repo_url}\n"
            f"Changed files: {request.changed_files}\n\n"
            f"Static performance findings:\n{findings_str}\n\n"
            f"Diff:\n{diff_block}"
        )

    def fallback_result(self, request: AnalysisRequest) -> PerformanceImpactResult:
        findings = self.detect_static_issues(request)
        return self._build_result_from_static(findings)

    # ── Static detection ──────────────────────────────────────────────────────

    def detect_static_issues(self, request: AnalysisRequest) -> list[PerformanceFinding]:
        """
        Scan all diff hunks for performance anti-patterns.
        Returns findings without any LLM calls.
        """
        findings: list[PerformanceFinding] = []
        for hunk in (request.hunks or []):
            try:
                findings.extend(self._scan_hunk(hunk))
            except Exception as exc:
                log.debug("PerformanceImpactAgent static scan error on %s: %s", hunk.file_path, exc)
        return findings

    def _scan_hunk(self, hunk: DiffHunk) -> list[PerformanceFinding]:
        findings: list[PerformanceFinding] = []
        if not hunk or not hunk.content:
            return findings

        # Use real source line numbers (parses @@ headers) instead of diff offsets
        from ingestion.diff_parser import iter_added_lines
        added_lines: list[tuple[int, str]] = list(iter_added_lines(hunk.content))

        if not added_lines:
            return findings

        # Build a flat list of added-line contents for window lookups
        added_contents = [content for _, content in added_lines]

        # Track whether we are inside an async def scope (best-effort, line-based)
        in_async_def = False

        for pos, (hunk_lineno, content) in enumerate(added_lines):
            stripped = content.strip()
            if not stripped:
                continue

            # Track async def scope
            if _ASYNC_DEF_RE.match(content):
                in_async_def = True
            elif re.match(r'^\s*(?:class|def)\s+', content) and not _ASYNC_DEF_RE.match(content):
                in_async_def = False

            # ── N+1 detection: loop line followed by DB call within 5 lines ──
            if _LOOP_RE.match(content):
                window = added_contents[pos + 1: pos + 6]
                if any(_DB_CALL_RE.search(w) for w in window):
                    findings.append(PerformanceFinding(
                        category="n_plus_1",
                        severity="high",
                        description=(
                            f"Potential N+1 query: loop at line {hunk_lineno} "
                            f"followed by a DB/ORM call inside the loop body."
                        ),
                        file_path=hunk.file_path,
                        line=hunk_lineno,
                        suggestion=(
                            "Batch the query before the loop using select_related / "
                            "prefetch_related (Django), eager loading (SQLAlchemy), or "
                            "an IN-clause query."
                        ),
                    ))

            # ── SELECT * / unbounded .all() ───────────────────────────────────
            if _SELECT_STAR_RE.search(content):
                # Check whether .limit() or .paginate() appears nearby (±5 lines)
                window_start = max(0, pos - 5)
                window_end   = min(len(added_contents), pos + 6)
                window = added_contents[window_start:window_end]
                if not any(_LIMIT_RE.search(w) for w in window):
                    findings.append(PerformanceFinding(
                        category="select_star",
                        severity="medium",
                        description=(
                            f"Unbounded SELECT * or .all() without LIMIT/pagination "
                            f"at line {hunk_lineno}. Can return millions of rows."
                        ),
                        file_path=hunk.file_path,
                        line=hunk_lineno,
                        suggestion=(
                            "Add .limit(n) / .paginate() or use explicit column projection "
                            "instead of SELECT *."
                        ),
                    ))

            # ── Nested loops → O(n²) ─────────────────────────────────────────
            if _LOOP_RE.match(content):
                # Look for a second loop keyword in the next 6 lines with deeper indent
                body = added_contents[pos + 1: pos + 7]
                if any(_INNER_LOOP_RE.match(b) for b in body):
                    body_size = sum(1 for b in body if b.strip())
                    sev = "high" if body_size > 5 else "medium"
                    findings.append(PerformanceFinding(
                        category="nested_loop",
                        severity=sev,
                        description=(
                            f"Nested loop at line {hunk_lineno} with body size ~{body_size} "
                            f"suggests O(n²) or worse time complexity."
                        ),
                        file_path=hunk.file_path,
                        line=hunk_lineno,
                        suggestion=(
                            "Consider replacing nested iteration with a hash-map lookup, "
                            "set intersection, or a vectorised operation."
                        ),
                    ))

            # ── Sync I/O in async function ────────────────────────────────────
            if in_async_def and _SYNC_IO_RE.search(content):
                findings.append(PerformanceFinding(
                    category="sync_in_async",
                    severity="high",
                    description=(
                        f"Synchronous `requests.*` call inside an async function "
                        f"at line {hunk_lineno}. Blocks the event loop."
                    ),
                    file_path=hunk.file_path,
                    line=hunk_lineno,
                    suggestion=(
                        "Replace with `httpx.AsyncClient` or `aiohttp` and await the call."
                    ),
                ))

            # ── Missing pagination: .all() without .limit()/.paginate() ───────
            if re.search(r'\.all\(\)', content, re.IGNORECASE):
                window_start = max(0, pos - 5)
                window_end   = min(len(added_contents), pos + 6)
                window = added_contents[window_start:window_end]
                if not any(_LIMIT_RE.search(w) for w in window):
                    # Only flag if not already flagged as select_star for same line
                    already = any(
                        f.line == hunk_lineno and f.category == "select_star"
                        for f in findings
                    )
                    if not already:
                        findings.append(PerformanceFinding(
                            category="no_pagination",
                            severity="medium",
                            description=(
                                f".all() query without pagination at line {hunk_lineno}. "
                                f"Could exhaust memory on large tables."
                            ),
                            file_path=hunk.file_path,
                            line=hunk_lineno,
                            suggestion=(
                                "Add .limit() / .paginate() or use cursor-based pagination."
                            ),
                        ))

            # ── Memory risk: read entire file / all records ───────────────────
            if _MEMORY_RISK_RE.search(content):
                findings.append(PerformanceFinding(
                    category="memory_risk",
                    severity="medium",
                    description=(
                        f"Reading entire file or all content into memory at line {hunk_lineno}. "
                        f"Risk of OOM on large files."
                    ),
                    file_path=hunk.file_path,
                    line=hunk_lineno,
                    suggestion=(
                        "Stream the file using `for line in f:` or chunked reads. "
                        "For DB records use queryset iteration / server-side cursors."
                    ),
                ))

        return findings

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_result_from_static(findings: list[PerformanceFinding]) -> PerformanceImpactResult:
        severities = [f.severity for f in findings]
        overall    = _max_severity(severities) if severities else "low"
        has_db     = any(f.category in ("n_plus_1", "select_star", "no_pagination") for f in findings)
        has_cx     = any(f.category == "nested_loop" for f in findings)
        summary    = (
            f"{len(findings)} performance issue(s) detected by static analysis. "
            f"Overall severity: {overall}."
            if findings else
            "No performance issues detected by static analysis."
        )
        return PerformanceImpactResult(
            findings=findings,
            has_db_risk=has_db,
            has_complexity_regression=has_cx,
            overall_severity=overall,
            summary=summary,
            fallback_used=True,
        )
