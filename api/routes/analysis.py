"""
api/routes/analysis.py
-----------------------
REST endpoints for direct analysis submission and report retrieval.

Production changes vs. initial implementation:
  - Thread-safe in-flight tracker with TTL cleanup (no memory leak)
  - Request size limit (MAX_DIFF_BYTES) to prevent unbounded diff uploads
  - Per-analysis timeout (ANALYSIS_TIMEOUT_S) to prevent pipeline hangs
  - Request-ID propagation from middleware
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType,
)
from governance.rbac import (
    Permission, Subject, Role, ROLE_META,
    get_current_subject, require_permission,
)
from ingestion.diff_parser import parse_diff
from output.report_formatter import to_summary_json, to_markdown

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["analysis"])

# ── Thread-safe in-flight tracker ────────────────────────────────────────────

class _InFlight:
    """
    Thread-safe dict with TTL-based eviction so completed entries don't
    accumulate indefinitely in long-running processes.
    """
    _TTL = 3600      # keep entries for 1 hour after completion
    _GC_INTERVAL = 300  # run GC every 5 minutes

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}  # id → (status, expires_at)
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def set(self, request_id: str, status: str) -> None:
        expires = time.monotonic() + self._TTL
        with self._lock:
            self._data[request_id] = (status, expires)
            self._maybe_gc()

    def get(self, request_id: str) -> str | None:
        with self._lock:
            entry = self._data.get(request_id)
            if entry is None:
                return None
            status, expires = entry
            if time.monotonic() > expires:
                del self._data[request_id]
                return None
            return status

    def _maybe_gc(self) -> None:
        now = time.monotonic()
        if now - self._last_gc < self._GC_INTERVAL:
            return
        self._last_gc = now
        expired = [k for k, (_, exp) in self._data.items() if now > exp]
        for k in expired:
            del self._data[k]


_in_flight = _InFlight()


# ── Request / Response models ─────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    repo_url:     str
    source_ref:   str
    target_ref:   str
    change_type:  ChangeType = ChangeType.BRANCH
    diff_text:    str = ""
    metadata:     dict[str, Any] = {}
    llm_config:   dict[str, Any] = {}
    model_config_override: dict[str, Any] = {}
    deep_scan:    bool = False     # analyse ALL changed files in batches (no sampling)

    @field_validator("diff_text")
    @classmethod
    def _check_diff_size(cls, v: str) -> str:
        from config.settings import get_settings
        max_bytes = get_settings().max_diff_bytes
        if len(v.encode()) > max_bytes:
            raise ValueError(
                f"diff_text exceeds maximum allowed size ({max_bytes // 1024} KB). "
                "Split large diffs or submit per-file."
            )
        return v


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/analyse", status_code=202)
async def submit_analysis(
    payload: AnalyseRequest,
    background: BackgroundTasks,
    request: Request,
):
    """
    Submit a code diff for asynchronous analysis.
    Returns immediately with a request_id; poll /report/{id} for results.
    """
    from api.app import get_orchestrator, get_report_store
    from config.settings import get_settings
    from core.concurrency import get_admission
    orch    = get_orchestrator()
    store   = get_report_store()
    cfg     = get_settings()
    adm     = get_admission()

    # Admission control: shed load when the run-slots + queue are all full.
    if not adm.can_admit():
        raise HTTPException(
            503,
            detail=(f"Server busy — {adm.running} running, {adm.queued} queued (queue full). "
                    "Please retry shortly."),
        )

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    hunks      = parse_diff(payload.diff_text) if payload.diff_text else []
    llm_cfg    = payload.llm_config or payload.model_config_override or {}

    req = AnalysisRequest(
        request_id=request_id,
        change_type=payload.change_type,
        repo_url=payload.repo_url,
        source_ref=payload.source_ref,
        target_ref=payload.target_ref,
        hunks=hunks,
        metadata=payload.metadata,
        model_config_=llm_cfg,
        deep_scan=bool(payload.deep_scan),
    )

    # Mark queued up-front; flips to running once a slot is acquired.
    _in_flight.set(request_id, "queued")

    async def _run():
        timeout = getattr(cfg, "analysis_timeout_s", 600)
        try:
            await adm.acquire(request_id)          # waits here if all slots busy
        except asyncio.CancelledError:
            _in_flight.set(request_id, "error: cancelled while queued")
            return
        _in_flight.set(request_id, "running")       # timeout starts only now
        try:
            report = await asyncio.wait_for(orch.analyse_async(req), timeout=timeout)
            store.save(report)
            _in_flight.set(request_id, "done")
            log.info("[%s] Analysis complete — gate=%s", request_id, report.gate_decision.value)
        except asyncio.TimeoutError:
            log.error("[%s] Analysis timed out after %ds", request_id, timeout)
            _in_flight.set(request_id, f"error: timed out after {timeout}s")
            _save_error(store, req, f"Analysis timed out after {timeout}s")
        except Exception as exc:
            log.error("[%s] Analysis failed: %s", request_id, exc, exc_info=True)
            _in_flight.set(request_id, f"error: {exc}")
            _save_error(store, req, str(exc))
        finally:
            adm.release()

    background.add_task(_run)
    return {"request_id": request_id, "status": "queued"}


@router.get("/progress/{request_id}")
def get_agent_progress(request_id: str):
    """
    Per-agent live status while analysis is running.
    Returns the status, elapsed time, and tokens for every agent seen so far.
    Poll this every 1-2 seconds from the UI for a live progress display.
    """
    from core.progress import get_progress_store
    run = get_progress_store().get(request_id)
    if not run:
        return {"request_id": request_id, "agents": []}
    return {"request_id": request_id, "agents": run.snapshot()}


@router.get("/status/{request_id}")
def get_status(request_id: str):
    """Quick status check without retrieving the full report."""
    status = _in_flight.get(request_id)
    if status is None:
        # May be done and evicted from in-flight — check the store
        from api.app import get_report_store
        if get_report_store().get(request_id):
            return {"request_id": request_id, "status": "done"}
        return {"request_id": request_id, "status": "unknown"}
    resp = {"request_id": request_id, "status": status}
    if status == "queued":
        from core.concurrency import get_admission
        adm = get_admission()
        resp["queue_position"] = adm.position(request_id)
        resp["queue_total"] = adm.queued
    return resp


@router.get("/report/{request_id}")
def get_report(request_id: str, fmt: str = "json"):
    """
    Retrieve a completed analysis report.

    fmt=json  → compact summary JSON (default)
    fmt=full  → full JSON with all agent results
    fmt=md    → Markdown report
    Returns 202 while still processing.
    """
    from api.app import get_report_store
    store  = get_report_store()

    status = _in_flight.get(request_id)
    if status in ("running", "queued"):
        raise HTTPException(202, detail=f"Analysis {status} — please poll again shortly.")

    report = store.get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")

    if fmt == "md":
        return {"markdown": to_markdown(report)}
    if fmt == "full":
        # model_dump_with_gate injects gate_decision + final_risk as top-level keys
        # so CI gate consumers always receive them (private attrs are excluded from
        # plain model_dump() in Pydantic v2).
        return report.model_dump_with_gate()
    return to_summary_json(report)


@router.get("/reports")
def list_reports(limit: int = 20, offset: int = 0):
    """List recent analysis reports (newest first)."""
    from api.app import get_report_store
    limit = min(limit, 100)
    return get_report_store().list_recent(limit=limit, offset=offset)


# ── Reviewer feedback loop ────────────────────────────────────────────────────

class FindingFeedback(BaseModel):
    agent:     str                      # security | performance_impact | ...
    category:  str = ""                 # cwe / kind / category
    file_path: str = ""
    verdict:   str                      # false_positive | valid | fixed | wont_fix
    note:      str = ""


@router.post("/report/{request_id}/feedback")
def submit_finding_feedback(
    request_id: str,
    body: FindingFeedback,
    subject: Subject = Depends(get_current_subject),
):
    """
    Record a reviewer's verdict on a finding (false positive, valid, fixed).
    Aggregated over time this surfaces which checks are noisy on a given repo.
    """
    from api.app import get_report_store
    from governance.feedback_store import get_feedback_store
    report = get_report_store().get(request_id)
    repo = report.repo_url if report else ""
    try:
        get_feedback_store().record_finding(
            request_id=request_id, repo=repo, agent=body.agent, category=body.category,
            file_path=body.file_path, verdict=body.verdict, note=body.note,
            reviewer=subject.name or subject.key_id,
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"ok": True, "recorded": body.verdict}


@router.get("/feedback/stats")
def feedback_stats(repo: str = ""):
    """
    Aggregated feedback: noisy checks (false-positive rate per agent/category)
    and gate override stats (how often humans go stricter/looser than the system).
    """
    from governance.feedback_store import get_feedback_store
    fs = get_feedback_store()
    noisy = fs.noisy_checks(repo=repo)

    # Detection-accuracy rollup from reviewer verdicts (overall + per agent).
    tot_fp = sum(c.get("false_positives", 0) for c in noisy)
    tot_valid = sum(c.get("valid", 0) for c in noisy)
    tot_fixed = sum(c.get("fixed", 0) for c in noisy)
    judged = tot_fp + tot_valid + tot_fixed       # verdicts that imply correct/incorrect
    correct = tot_valid + tot_fixed
    by_agent: dict[str, dict] = {}
    for c in noisy:
        a = by_agent.setdefault(c.get("agent", "?"), {"fp": 0, "valid": 0, "fixed": 0})
        a["fp"] += c.get("false_positives", 0)
        a["valid"] += c.get("valid", 0)
        a["fixed"] += c.get("fixed", 0)
    agent_accuracy = []
    for a, v in by_agent.items():
        j = v["fp"] + v["valid"] + v["fixed"]
        agent_accuracy.append({
            "agent": a, "judged": j, "false_positives": v["fp"],
            "accuracy": round((v["valid"] + v["fixed"]) / j, 3) if j else None,
        })
    agent_accuracy.sort(key=lambda x: (x["accuracy"] if x["accuracy"] is not None else 1))

    return {
        "noisy_checks": noisy,
        "gate_stats":   fs.gate_stats(repo=repo),
        "accuracy": {
            "judged_findings":  judged,
            "confirmed_valid":  correct,
            "false_positives":  tot_fp,
            "overall_accuracy": round(correct / judged, 3) if judged else None,
            "by_agent":         agent_accuracy,
        },
        "repo": repo,
    }


@router.get("/me")
def get_me(subject: Subject = Depends(get_current_subject)):
    """
    Return the current user's identity, role, and permissions.
    The frontend calls this on connect to enforce role-based UI restrictions.
    """
    top_role = subject.roles[0] if subject.roles else Role.DEVELOPER
    meta     = ROLE_META.get(top_role, ROLE_META[Role.DEVELOPER])
    return {
        "key_id":       subject.key_id,
        "name":         subject.name or subject.key_id,
        "team":         subject.team,
        "roles":        [r.value for r in subject.roles],
        "primary_role": top_role.value,
        "permissions":  [p.value for p in subject.permissions],
        "can_comment":  meta["can_comment"],
        "can_override": meta["can_override"],
        "role_label":   meta["label"],
        "role_color":   meta["color"],
        "description":  meta["description"],
    }


# ── Developer productivity endpoints ──────────────────────────────────────────

@router.get("/report/{request_id}/checklist")
def get_reviewer_checklist(request_id: str):
    """
    Return a structured reviewer checklist derived from the analysis report.
    Each item has: domain, label, status (pass|warn|fail|skip), detail.
    """
    from api.app import get_report_store
    report = get_report_store().get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")
    return {"checklist": _build_checklist(report), "gate": report.gate_decision.value}


@router.get("/report/{request_id}/pr-description")
def get_pr_description(request_id: str):
    """
    Generate a ready-to-paste PR description from the analysis report.
    Returns markdown text the developer can paste into GitHub/Bitbucket.
    """
    from api.app import get_report_store
    report = get_report_store().get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")
    return {"markdown": _build_pr_description(report)}


class CommentPRRequest(BaseModel):
    provider:    str = "github"      # github | bitbucket | bitbucket_server
    token:       str = ""
    base_url:    str = ""
    workspace:   str = ""
    repo_slug:   str = ""
    pr_id:       str = ""
    inline:      bool = True         # post inline file-level comments in addition to summary


@router.post("/report/{request_id}/comment-pr")
def comment_pr(
    request_id: str,
    body: CommentPRRequest,
    subject: Subject = require_permission(Permission.PR_COMMENT),
):
    """
    Post analysis findings as PR comments on GitHub or Bitbucket.
    Idempotent — updates the existing bot comment if one already exists.
    Optionally posts inline comments on specific files where issues were found.
    """
    from api.app import get_report_store
    from output.pr_commenter import PRCommenter, post_inline_comments
    report = get_report_store().get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")

    from output.pr_commenter import _normalise_api_url, InsufficientScopeError
    normalised_url = _normalise_api_url(body.base_url, body.provider)

    pid  = body.pr_id or ""
    slug = body.repo_slug or ""

    if not pid:
        raise HTTPException(400, detail="pr_id is required to post PR comments")

    try:
        # post_inline_comments handles BOTH the per-file grouped comments AND
        # the overall PR-level summary in a single coordinated call.
        total_posted = post_inline_comments(
            report=report,
            token=body.token,
            provider=body.provider,
            workspace=body.workspace,
            base_url=normalised_url,
            repo_slug=slug,
            pr_id=pid,
        )
    except InsufficientScopeError as exc:
        # Return a structured 403 so the UI can show precise fix instructions
        raise HTTPException(
            status_code=403,
            detail={
                "error":    "insufficient_token_scope",
                "message":  str(exc),
                "hint":     exc.hint,
                "found":    sorted(exc.found),
                "required": sorted(exc.required),
                "fix_url":  "https://github.com/settings/tokens",
            },
        )

    ok = total_posted > 0
    return {
        "ok":             ok,
        "comments_posted": total_posted,
        "files_commented": max(0, total_posted - 1),   # minus the summary comment
    }


# ── Checklist builder ─────────────────────────────────────────────────────────

def _build_checklist(report) -> list[dict]:
    items = []

    def item(domain, label, status, detail=""):
        return {"domain": domain, "label": label, "status": status, "detail": detail}

    # Security
    if report.security:
        sec = report.security
        crit = [f for f in sec.findings if str(getattr(f, "severity", "")).lower() in ("critical", "high")]
        if crit:
            items.append(item("security", "No critical/high security vulnerabilities", "fail",
                               f"{len(crit)} critical/high finding(s): {', '.join(f.title for f in crit[:3] if hasattr(f,'title'))}"))
        else:
            items.append(item("security", "No critical/high security vulnerabilities", "pass"))
        if getattr(sec, "secrets_detected", False):
            items.append(item("security", "No hardcoded secrets or credentials", "fail", "Secrets/entropy detected in diff"))
        else:
            items.append(item("security", "No hardcoded secrets or credentials", "pass"))
    else:
        items.append(item("security", "Security analysis", "skip", "Agent did not run"))

    # Data privacy
    if report.data_privacy:
        dp = report.data_privacy
        if dp.unencrypted_pii_count > 0:
            items.append(item("privacy", "PII fields are encrypted/hashed", "fail",
                               f"{dp.unencrypted_pii_count} unencrypted PII field(s) found"))
        else:
            items.append(item("privacy", "PII fields are encrypted/hashed", "pass"))
        if dp.logging_violations:
            items.append(item("privacy", "No PII logged or printed", "fail",
                               f"{len(dp.logging_violations)} logging violation(s)"))
        else:
            items.append(item("privacy", "No PII logged or printed", "pass"))
    else:
        items.append(item("privacy", "Data privacy review", "skip", "Agent did not run"))

    # Performance
    if report.performance_impact:
        perf = report.performance_impact
        if perf.has_db_risk:
            items.append(item("performance", "No N+1 query or unbounded DB calls", "warn",
                               "Potential query performance issue detected"))
        else:
            items.append(item("performance", "No N+1 query or unbounded DB calls", "pass"))
        if perf.has_complexity_regression:
            items.append(item("performance", "No algorithmic complexity regression", "warn",
                               "Nested loop or O(n²) pattern detected"))
        else:
            items.append(item("performance", "No algorithmic complexity regression", "pass"))
    else:
        items.append(item("performance", "Performance review", "skip", "Agent did not run"))

    # Test coverage
    if report.test_coverage:
        tc = report.test_coverage
        if getattr(tc, "coverage_delta", 0) < -5:
            items.append(item("testing", "Test coverage not reduced", "warn",
                               f"Coverage delta: {tc.coverage_delta:+.1f}%"))
        else:
            items.append(item("testing", "Test coverage not reduced", "pass"))
        untested = getattr(tc, "untested_functions", [])
        if untested:
            items.append(item("testing", "All changed functions have tests", "warn",
                               f"Untested: {', '.join(str(u) for u in untested[:3])}"))
        else:
            items.append(item("testing", "All changed functions have tests", "pass"))
    else:
        items.append(item("testing", "Test coverage review", "skip", "Agent did not run"))

    # API / interface
    if report.interface:
        breaking = getattr(report.interface, "breaking_changes", [])
        if breaking:
            items.append(item("interface", "No breaking API changes (or versioned)", "fail",
                               f"{len(breaking)} breaking change(s): {', '.join(str(b) for b in breaking[:2])}"))
        else:
            items.append(item("interface", "No breaking API changes", "pass"))
    else:
        items.append(item("interface", "API contract review", "skip", "Agent did not run"))

    # Schema / DB
    if report.schema_change:
        sc = report.schema_change
        risky = [f for f in getattr(sc, "findings", []) if str(getattr(f, "risk_level", "")).lower() in ("high", "critical")]
        if risky:
            items.append(item("schema", "Database migration is safe", "fail",
                               f"{len(risky)} risky migration(s) found"))
        else:
            items.append(item("schema", "Database migration is safe", "pass" if getattr(sc, "findings", []) else "skip"))
    else:
        items.append(item("schema", "Schema migration review", "skip", "Agent did not run"))

    # License
    if report.license_compliance:
        lc = report.license_compliance
        if lc.has_copyleft:
            items.append(item("license", "No copyleft (GPL/AGPL) dependencies", "fail",
                               "Copyleft licence detected — legal review required"))
        else:
            items.append(item("license", "No copyleft dependencies", "pass"))
    else:
        items.append(item("license", "Licence compliance", "skip", "Agent did not run"))

    # Deployment / risk
    if report.risk:
        score = getattr(report.risk, "risk_score", 0)
        strategy = getattr(report.remediation, "deployment_strategy", None) if report.remediation else None
        strategy_val = strategy.value if strategy else "unknown"
        if score >= 7:
            items.append(item("deployment", "Deployment risk acceptable", "fail",
                               f"Risk score {score}/10 — {strategy_val} deployment required"))
        elif score >= 4:
            items.append(item("deployment", "Deployment risk acceptable", "warn",
                               f"Risk score {score}/10 — {strategy_val} recommended"))
        else:
            items.append(item("deployment", "Deployment risk acceptable", "pass",
                               f"Risk score {score}/10"))
    else:
        items.append(item("deployment", "Deployment risk review", "skip", "Agent did not run"))

    # Observability
    if report.observability:
        obs = report.observability
        if obs.logs_removed > 0 or obs.metrics_removed > 0:
            items.append(item("observability", "Logging/metrics not removed", "warn",
                               f"{obs.logs_removed} log(s), {obs.metrics_removed} metric(s) removed"))
        else:
            items.append(item("observability", "Logging and metrics intact", "pass"))
    else:
        items.append(item("observability", "Observability review", "skip", "Agent did not run"))

    return items


# ── PR description builder ────────────────────────────────────────────────────

def _build_pr_description(report) -> str:
    lines = []

    # Summary section
    ca      = report.code_analysis
    risk    = report.final_risk.value.upper() if report.final_risk else "UNKNOWN"
    gate    = report.gate_decision.value if report.gate_decision else "HOLD"
    gate_icon = {"APPROVE": "✅", "HOLD": "⚠️", "BLOCK": "🚫"}.get(gate, "❓")

    change_summary = ca.summary if ca and ca.summary else "This PR introduces changes to the codebase."
    lines += [
        "## Summary",
        "",
        change_summary,
        "",
        f"**Risk Level:** `{risk}` {gate_icon}  |  **Gate:** `{gate}`",
        "",
    ]

    # What changed
    if ca:
        lines += ["## Changes", ""]
        if ca.change_type:
            lines.append(f"- **Change type:** {ca.change_type}")
        if hasattr(ca, "complexity_delta") and ca.complexity_delta:
            direction = "increased" if ca.complexity_delta > 0 else "decreased"
            lines.append(f"- **Complexity:** {direction} by {abs(ca.complexity_delta)}")
        lines.append("")

    # Impact
    impacts = []
    if report.reference_impact and report.reference_impact.total_references > 0:
        impacts.append(f"- **Callers affected:** {report.reference_impact.total_references} reference(s) across "
                       f"{len(report.reference_impact.high_impact_files)} file(s)")
    if report.interface and getattr(report.interface, "breaking_changes", []):
        impacts.append(f"- **Breaking API changes:** {len(report.interface.breaking_changes)}")
    if report.dependency and getattr(report.dependency, "affected_services", []):
        impacts.append(f"- **Downstream services affected:** {', '.join(report.dependency.affected_services[:5])}")
    if impacts:
        lines += ["## Impact", ""] + impacts + [""]

    # Security notes
    if report.security and report.security.findings:
        crit = [f for f in report.security.findings if str(getattr(f, "severity", "")).lower() in ("critical", "high")]
        if crit:
            lines += ["## ⚠️ Security Notes", ""]
            for f in crit[:3]:
                lines.append(f"- {getattr(f, 'title', str(f))}")
            lines.append("")

    # Testing
    lines += ["## Testing", ""]
    if report.qa_scenarios and getattr(report.qa_scenarios, "scenarios", []):
        lines.append("Key test scenarios to verify:")
        for s in report.qa_scenarios.scenarios[:5]:
            lines.append(f"- [ ] {getattr(s, 'description', str(s))}")
    else:
        lines += [
            "- [ ] Unit tests pass",
            "- [ ] Integration tests pass",
            "- [ ] Manual smoke test completed",
        ]
    lines.append("")

    # Deployment
    if report.remediation:
        strategy = getattr(report.remediation, "deployment_strategy", None)
        if strategy:
            lines += [f"## Deployment", "", f"**Strategy:** `{strategy.value}`"]
        rollback = getattr(report.remediation, "rollback_plan", "")
        if rollback:
            lines += ["", f"**Rollback:** {rollback}"]
        lines.append("")

    lines += [
        "---",
        f"*Generated by CIAA Impact Analyzer · Analysis ID: `{report.request_id}`*",
    ]

    return "\n".join(lines)


@router.delete("/report/{request_id}", status_code=204)
def delete_report(request_id: str):
    from api.app import get_report_store
    if not get_report_store().delete(request_id):
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_error(store, req: AnalysisRequest, message: str) -> None:
    try:
        error_report = AnalysisReport(
            request_id=req.request_id,
            change_type=req.change_type,
            repo_url=req.repo_url,
            source_ref=req.source_ref,
            target_ref=req.target_ref,
            errors=[message],
        )
        store.save(error_report)
    except Exception as exc:
        log.error("[%s] Could not save error report: %s", req.request_id, exc)
