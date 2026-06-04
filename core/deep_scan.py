"""
core/deep_scan.py
------------------
Full-coverage scanning for large PRs.

Normally the per-file agents (security, code) receive a *prioritised sample* of
the changed files to stay within the model's context window — fast and cheap,
but low-priority files may be omitted. Deep-scan instead splits ALL changed
files into batches, runs the agent once per batch (each with its own token
budget so nothing is starved), and merges the results.

Trade-off: cost/time scale with the number of batches. Opt-in per analysis.
"""
from __future__ import annotations
import logging
from typing import Any, Callable

from core.models import (
    AnalysisRequest, DiffHunk, RiskLevel,
    SecurityResult, SecurityFinding, CodeAnalysisResult,
)

log = logging.getLogger(__name__)

_SEV_ORDER = {
    RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3,
}


def batch_hunks(hunks: list[DiffHunk], max_chars: int, max_batches: int) -> list[list[DiffHunk]]:
    """Greedily pack hunks into batches that each fit within `max_chars`.
    Caps the number of batches so cost stays bounded (remaining files ride along
    in the last batch, truncated by the agent's normal prompt budget)."""
    batches: list[list[DiffHunk]] = []
    cur: list[DiffHunk] = []
    cur_chars = 0
    for h in hunks:
        hlen = len(h.content or "")
        if cur and cur_chars + hlen > max_chars:
            batches.append(cur)
            cur, cur_chars = [], 0
            if len(batches) >= max_batches - 1:
                break
        cur.append(h)
        cur_chars += hlen
    # Everything not yet placed goes into the final batch.
    placed = sum(len(b) for b in batches)
    if placed < len(hunks):
        cur = hunks[placed:] if not cur else cur + [h for h in hunks[placed:] if h not in cur]
    if cur:
        batches.append(cur)
    return batches or [hunks]


def _dedup_security(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    seen: set = set()
    out: list[SecurityFinding] = []
    for f in findings:
        key = (f.file_path, f.line_range, f.cwe_id, (f.description or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def merge_security(results: list[SecurityResult]) -> SecurityResult:
    all_findings: list[SecurityFinding] = []
    flags: list[str] = []
    secrets = False
    max_sev = RiskLevel.LOW
    tokens = 0
    model = ""
    all_fallback = True
    for r in results:
        if r is None:
            continue
        all_findings.extend(r.findings or [])
        flags.extend(r.compliance_flags or [])
        secrets = secrets or bool(getattr(r, "secrets_detected", False))
        if _SEV_ORDER.get(r.overall_severity, 0) > _SEV_ORDER.get(max_sev, 0):
            max_sev = r.overall_severity
        tokens += getattr(r, "token_usage", 0) or 0
        model = model or getattr(r, "model_used", "")
        all_fallback = all_fallback and bool(getattr(r, "fallback_used", False))
    merged = _dedup_security(all_findings)
    # Recompute overall severity from the merged findings too
    for f in merged:
        if _SEV_ORDER.get(f.severity, 0) > _SEV_ORDER.get(max_sev, 0):
            max_sev = f.severity
    return SecurityResult(
        findings=merged,
        secrets_detected=secrets,
        compliance_flags=sorted(set(flags)),
        overall_severity=max_sev,
        token_usage=tokens,
        model_used=model,
        fallback_used=all_fallback,
    )


def merge_code(results: list[CodeAnalysisResult]) -> CodeAnalysisResult:
    summaries: list[str] = []
    findings: list = []
    complexity = 0
    types: list[str] = []
    tokens = 0
    model = ""
    all_fallback = True
    for r in results:
        if r is None:
            continue
        if r.summary:
            summaries.append(r.summary.strip())
        findings.extend(r.findings or [])
        complexity += getattr(r, "complexity_delta", 0) or 0
        if r.change_type:
            types.append(r.change_type)
        tokens += getattr(r, "token_usage", 0) or 0
        model = model or getattr(r, "model_used", "")
        all_fallback = all_fallback and bool(getattr(r, "fallback_used", False))
    change_type = "mixed" if len(set(types)) > 1 else (types[0] if types else "unknown")
    summary = " ".join(dict.fromkeys(summaries))[:1500] or "No summary available."
    return CodeAnalysisResult(
        summary=summary,
        change_type=change_type,
        complexity_delta=complexity,
        findings=findings,
        token_usage=tokens,
        model_used=model,
        fallback_used=all_fallback,
    )


def run_batched(agent, request: AnalysisRequest, ctx: dict[str, Any],
                merge_fn: Callable, budgets: dict, max_chars: int, max_batches: int):
    """Run `agent` over all changed files in batches and merge the results.
    Each batch gets a fresh token budget so no batch is starved."""
    from core.token_manager import TokenBudgetManager

    batches = batch_hunks(request.hunks, max_chars=max_chars, max_batches=max_batches)
    log.info("[%s] Deep-scan %s: %d files in %d batch(es)",
             request.request_id, getattr(agent, "agent_name", "?"), len(request.hunks), len(batches))
    results = []
    for i, batch in enumerate(batches):
        sub_req = request.model_copy(update={"hunks": batch})
        batch_budget = TokenBudgetManager(request.request_id, budgets)
        try:
            results.append(agent.run(sub_req, batch_budget, ctx))
        except Exception as exc:   # pragma: no cover - defensive
            log.warning("[%s] Deep-scan batch %d/%d failed: %s",
                        request.request_id, i + 1, len(batches), exc)
    return merge_fn(results) if results else None
