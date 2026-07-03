"""
api/routes/insights.py
-----------------------
Productivity dashboard endpoints:

  GET  /api/v1/insights/queue          PR priority queue — open analyses ranked by risk
  GET  /api/v1/insights/trend          Risk score trend per repo / team over time
  GET  /api/v1/insights/heatmap        File change frequency + risk colour map
  GET  /api/v1/insights/cost           LLM token spend by agent / model / week
  GET  /api/v1/insights/similar/{id}   Similar past PRs (semantic + file-overlap match)
  POST /api/v1/insights/dep-update     Trigger a dependency auto-update PR for a CVE
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

# ── LLM cost table (per 1M tokens, blended input+output estimate) ─────────────
# Update these when provider pricing changes.
_COST_PER_MTK: dict[str, float] = {
    "claude-sonnet-4-6":          9.00,
    "claude-haiku-4-5-20251001":  0.75,
    "claude-haiku-4-5":           0.75,
    "claude-opus-4-6":           75.00,
    "gpt-4o":                    10.00,
    "gpt-4o-mini":                0.375,
    "gpt-4-turbo":               30.00,
    "gpt-3.5-turbo":              2.00,
    "llama3.2":                   0.00,   # local
    "codellama":                  0.00,
    "default":                    9.00,   # conservative fallback
}

def _cost_usd(tokens: int, model: str) -> float:
    rate = _COST_PER_MTK.get(model.lower().split("/")[-1], _COST_PER_MTK["default"])
    return round(tokens / 1_000_000 * rate, 4)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_store():
    from api.app import get_report_store
    return get_report_store()


def _get_temporal():
    from storage.temporal_store import get_temporal_store
    return get_temporal_store()


def _load_reports(limit: int = 500) -> list:
    """Load recent full report objects from the store."""
    store  = _get_store()
    metas  = store.list_recent(limit=limit)
    result = []
    for m in metas:
        r = store.get(m["request_id"])
        if r:
            result.append(r)
    return result


def _risk_score(report) -> float:
    """
    Extract a normalised risk score on a 0-10 scale.
    RiskResult.risk_score is stored 0-100, so we divide by 10 here so the
    frontend (which colours on a 0-10 scale) stays consistent.
    """
    if report.risk and getattr(report.risk, "risk_score", 0):
        raw = float(report.risk.risk_score or 0)
        return round(raw / 10.0, 1) if raw > 10 else round(raw, 1)
    gate = (report.gate_decision.value if report.gate_decision else "HOLD")
    return {"BLOCK": 8.5, "HOLD": 5.0, "APPROVE": 2.0}.get(gate, 5.0)


def _week_label(dt: datetime) -> str:
    return dt.strftime("%Y-W%W")


def _short_repo(url: str) -> str:
    """
    Normalise any repo URL to a clean 'owner/repo' label.
    Handles https, ssh (git@host:owner/repo), .git suffix, bare slugs,
    and never returns host fragments like '/github.com' or empty strings.
    """
    import re
    if not url or not url.strip():
        return "(unknown)"
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    # strip ssh prefix:  git@host:owner/repo  →  owner/repo
    u = re.sub(r"^[\w.+-]+@[^:]+:", "", u)
    # strip http(s)://host/  →  path
    u = re.sub(r"^https?://[^/]+/?", "", u)
    parts = [p for p in u.split("/") if p and p not in ("scm",)]  # BB Server has /scm/
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    if parts:
        return parts[-1]
    return "(unknown)"


def _sev(value) -> str:
    """Normalise a severity that may be a RiskLevel enum or a plain string."""
    if value is None:
        return "low"
    v = getattr(value, "value", value)   # RiskLevel.HIGH → "high"
    return str(v).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  PR PRIORITY QUEUE
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/queue")
def pr_priority_queue(limit: int = 200, days: int = 0, repo: str = ""):
    """
    Return all recent analyses ranked by risk score (highest first).
    Reviewers use this to decide which PRs to look at first.

    Filters:
      days > 0  — only analyses completed in the last N days
      repo      — only this repository (short "owner/name" or full URL)
    """
    reports = _load_reports(limit=limit)
    cutoff  = datetime.utcnow() - timedelta(days=days) if days > 0 else None
    items   = []

    # Keep only the LATEST analysis per PR — a re-analyzed PR shouldn't flood the
    # queue with every historical run. Reports arrive newest-first, so the first
    # occurrence of each key wins.
    seen_prs: set = set()

    for r in reports:
        # Date filter
        if cutoff is not None:
            try:
                ts = r.completed_at if hasattr(r, "completed_at") else datetime.utcnow()
                if ts < cutoff:
                    continue
            except Exception:
                pass

        # De-dupe to the latest analysis per (repo, PR) — or per (repo, branch pair).
        # Use the NORMALISED repo name so the same PR re-run with a differently
        # formatted repo_url (https vs slug vs .git) still collapses to one entry.
        repo_key = _short_repo(r.repo_url)
        pr_no = r.pr.pr_number if r.pr else 0
        dedup_key = (repo_key, pr_no) if pr_no else (repo_key, r.source_ref, r.target_ref)
        if dedup_key in seen_prs:
            continue
        seen_prs.add(dedup_key)

        # Repo filter
        short = _short_repo(r.repo_url)
        if repo and short != repo and r.repo_url != repo:
            continue

        score = _risk_score(r)
        gate  = r.gate_decision.value if r.gate_decision else "HOLD"

        # Top finding summary
        findings = []
        if r.security:
            crit = [f for f in (r.security.findings or [])
                    if _sev(getattr(f, "severity", "")) in ("critical", "high")]
            for f in crit[:2]:
                findings.append({"category": "security",
                                  "severity": _sev(getattr(f, "severity", "high")),
                                  "title":    (getattr(f, "description", "") or "Security issue")[:80]})
        if r.performance_impact:
            for f in (r.performance_impact.findings or [])[:1]:
                findings.append({"category": "performance",
                                  "severity": _sev(f.severity),
                                  "title":    f.description[:80]})
        if r.data_privacy and r.data_privacy.pii_findings:
            findings.append({"category": "privacy",
                              "severity": "high",
                              "title":    f"{len(r.data_privacy.pii_findings)} PII field(s) detected"})

        # Elapsed since analysis
        try:
            completed = r.completed_at if hasattr(r, "completed_at") else datetime.utcnow()
            elapsed_h = (datetime.utcnow() - completed).total_seconds() / 3600
            elapsed_s = f"{int(elapsed_h)}h ago" if elapsed_h >= 1 else "just now"
        except Exception:
            elapsed_s = "—"

        items.append({
            "request_id":  r.request_id,
            "repo":        _short_repo(r.repo_url),
            "repo_url":    r.repo_url,
            "pr_number":   r.pr.pr_number if r.pr else 0,
            "pr_title":    r.pr.pr_title  if r.pr else "",
            "author":      r.pr.author    if r.pr else "",
            "source_ref":  r.source_ref,
            "target_ref":  r.target_ref,
            "risk_score":  score,
            "gate":        gate,
            "top_findings": findings[:3],
            "total_tokens": r.total_tokens,
            "duration_s":  getattr(r, "duration_s", 0),
            "elapsed":     elapsed_s,
            "change_type": r.change_type.value if r.change_type else "unknown",
        })

    # Sort: BLOCK first, then by risk score descending
    gate_order = {"BLOCK": 0, "HOLD": 1, "APPROVE": 2}
    items.sort(key=lambda x: (gate_order.get(x["gate"], 1), -x["risk_score"]))

    # Distinct repos across ALL recent reports (for the filter dropdown)
    all_repos = sorted({_short_repo(r.repo_url) for r in reports})

    return {"queue": items, "total": len(items), "repos": all_repos}


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  RISK TREND
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/trend")
def risk_trend(weeks: int = 12, repo: str = ""):
    """
    Weekly risk score trend per repository.
    Returns: per-repo series + overall average + top recurring finding categories.
    """
    reports  = _load_reports(limit=1000)
    now      = datetime.utcnow()
    cutoff   = now - timedelta(weeks=weeks)

    # Build week buckets per repo
    repo_weeks: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    category_counts: dict[str, int] = defaultdict(int)

    for r in reports:
        try:
            ts = r.completed_at if hasattr(r, "completed_at") else now
            if ts < cutoff:
                continue
        except Exception:
            continue

        short = _short_repo(r.repo_url)
        if repo and short != repo and r.repo_url != repo:
            continue

        wk = _week_label(ts)
        repo_weeks[short][wk].append(_risk_score(r))

        # Count finding categories
        if r.security and r.security.findings:
            category_counts["security"] += len(r.security.findings)
        if r.performance_impact and r.performance_impact.findings:
            category_counts["performance"] += len(r.performance_impact.findings)
        if r.data_privacy and r.data_privacy.pii_findings:
            category_counts["privacy"] += len(r.data_privacy.pii_findings)
        if r.maintainability and r.maintainability.issues:
            category_counts["maintainability"] += len(r.maintainability.issues)
        if r.observability and r.observability.findings:
            category_counts["observability"] += len(r.observability.findings)

    # Build all week labels for the window
    all_weeks = []
    ptr = cutoff
    while ptr <= now:
        all_weeks.append(_week_label(ptr))
        ptr += timedelta(weeks=1)
    all_weeks = sorted(set(all_weeks))

    # Build series per repo
    series = []
    for repo_name, weeks_data in repo_weeks.items():
        scores = [round(sum(weeks_data.get(w, [0])) / max(len(weeks_data.get(w, [1])), 1), 2)
                  for w in all_weeks]
        # Trend direction
        recent = [s for s in scores[-4:] if s > 0]
        older  = [s for s in scores[:4]  if s > 0]
        if len(recent) >= 2 and len(older) >= 2:
            avg_recent = sum(recent) / len(recent)
            avg_older  = sum(older)  / len(older)
            if avg_recent < avg_older - 0.5:
                trend = "improving"
            elif avg_recent > avg_older + 0.5:
                trend = "degrading"
            else:
                trend = "stable"
        else:
            trend = "stable"

        series.append({
            "repo":   repo_name,
            "weeks":  all_weeks,
            "scores": scores,
            "trend":  trend,
            "avg":    round(sum(s for s in scores if s) / max(sum(1 for s in scores if s), 1), 2),
        })

    series.sort(key=lambda x: -x["avg"])

    # Top recurring issues
    top_issues = sorted(category_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "series":       series,
        "weeks":        all_weeks,
        "top_issues":   [{"category": k, "count": v} for k, v in top_issues],
        "repos":        [s["repo"] for s in series],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  HEATMAP
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/heatmap")
def change_heatmap(repo: str = "", days: int = 90, limit: int = 60):
    """
    File change frequency heatmap with risk colour coding.
    Each cell = one file: size = change frequency, colour = max risk encountered.
    """
    reports = _load_reports(limit=500)
    cutoff  = datetime.utcnow() - timedelta(days=days)

    # Aggregate per file
    file_data: dict[str, dict] = {}

    for r in reports:
        try:
            ts = r.completed_at if hasattr(r, "completed_at") else datetime.utcnow()
            if ts < cutoff:
                continue
        except Exception:
            continue

        short = _short_repo(r.repo_url)
        if repo and short != repo and r.repo_url != repo:
            continue

        score    = _risk_score(r)
        gate     = r.gate_decision.value if r.gate_decision else "HOLD"
        req_id   = r.request_id

        # Extract changed files from hunks if available (report doesn't store them directly)
        # Fall back to high-impact files from reference impact
        files_in_pr: set[str] = set()
        if r.reference_impact:
            files_in_pr.update(r.reference_impact.high_impact_files or [])
        # Also from security findings
        for f in (r.security.findings if r.security else []):
            fp = getattr(f, "file_path", "")
            if fp:
                files_in_pr.add(fp)
        for f in (r.performance_impact.findings if r.performance_impact else []):
            if f.file_path:
                files_in_pr.add(f.file_path)
        for f in (r.data_privacy.pii_findings if r.data_privacy else []):
            if f.file_path:
                files_in_pr.add(f.file_path)
        for f in (r.maintainability.issues if r.maintainability else []):
            if f.file_path:
                files_in_pr.add(f.file_path)

        # Add also from temporal store hot-files if repo is available
        for fp in files_in_pr:
            if fp not in file_data:
                file_data[fp] = {
                    "file":        fp,
                    "repo":        short,
                    "changes":     0,
                    "max_risk":    0.0,
                    "gates":       [],
                    "request_ids": [],
                    "has_security": False,
                    "has_privacy":  False,
                }
            fd = file_data[fp]
            fd["changes"]      += 1
            fd["max_risk"]      = max(fd["max_risk"], score)
            fd["gates"].append(gate)
            fd["request_ids"].append(req_id)
            if r.security and r.security.findings:
                fd["has_security"] = True
            if r.data_privacy and r.data_privacy.pii_findings:
                fd["has_privacy"]  = True

    # Also pull hot files from temporal store
    try:
        tstore = _get_temporal()
        if repo:
            # reconstruct full URL if only short form given
            hot = tstore.get_hot_files(repo, days=days, min_changes=2)
            for h in hot:
                fp = h.file_path
                if fp not in file_data:
                    file_data[fp] = {
                        "file":        fp,
                        "repo":        repo,
                        "changes":     h.change_count,
                        "max_risk":    h.max_risk_score,
                        "gates":       h.gates,
                        "request_ids": [],
                        "has_security": any(s in ("high", "critical")
                                            for s in (h.security_severities or [])),
                        "has_privacy":  False,
                    }
                else:
                    # Merge counts
                    file_data[fp]["changes"]  = max(file_data[fp]["changes"], h.change_count)
                    file_data[fp]["max_risk"] = max(file_data[fp]["max_risk"], h.max_risk_score)
    except Exception as exc:
        log.debug("Temporal store not available for heatmap: %s", exc)

    # Sort by (max_risk * changes) descending
    cells = sorted(file_data.values(),
                   key=lambda x: -(x["max_risk"] * x["changes"]),
                   )[:limit]

    # Assign heat colour
    def _colour(risk: float) -> str:
        if risk >= 7:  return "#dc2626"   # red — critical
        if risk >= 5:  return "#f59e0b"   # amber — high
        if risk >= 3:  return "#3b82f6"   # blue — medium
        return "#10b981"                   # green — low

    for c in cells:
        c["colour"] = _colour(c["max_risk"])
        c["label"]  = c["file"].split("/")[-1]    # basename for display
        # Remove non-serialisable sets
        c["gates"]  = list(c["gates"])

    return {
        "cells":    cells,
        "total":    len(file_data),
        "days":     days,
        "repo":     repo,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  API COST DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/cost")
def api_cost(weeks: int = 12, repo: str = ""):
    """
    LLM token spend by agent, model, and calendar week.
    Includes estimated USD cost based on current provider pricing.
    Optional repo filter (short "owner/name" or full URL).
    """
    reports = _load_reports(limit=1000)
    if repo:
        reports = [r for r in reports if _short_repo(r.repo_url) == repo or r.repo_url == repo]
    now     = datetime.utcnow()
    cutoff  = now - timedelta(weeks=weeks)

    # Per-week totals
    weekly:  dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "analyses": 0})
    # Per-agent totals
    by_agent: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "calls": 0})
    # Per-model totals
    by_model: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "calls": 0})
    # Fallback stats
    fallback_count  = 0
    total_tokens    = 0
    total_cost      = 0.0

    for r in reports:
        try:
            ts = r.completed_at if hasattr(r, "completed_at") else now
            if ts < cutoff:
                continue
        except Exception:
            continue

        wk = _week_label(ts)
        weekly[wk]["analyses"] += 1

        for usage in (r.token_usage or []):
            t   = usage.tokens_used or 0
            m   = (usage.model or "unknown").split("/")[-1]
            c   = _cost_usd(t, m)
            a   = usage.agent.value if hasattr(usage.agent, "value") else str(usage.agent)

            weekly[wk]["tokens"]   += t
            weekly[wk]["cost_usd"] += c
            by_agent[a]["tokens"]  += t
            by_agent[a]["cost_usd"]+= c
            by_agent[a]["calls"]   += 1
            by_model[m]["tokens"]  += t
            by_model[m]["cost_usd"]+= c
            by_model[m]["calls"]   += 1
            total_tokens += t
            total_cost   += c

        # Count fallbacks across agent results
        for attr in ("code_analysis","security","dependency","risk","remediation",
                     "performance_impact","data_privacy","maintainability","observability"):
            result = getattr(r, attr, None)
            if result and getattr(result, "fallback_used", False):
                fallback_count += 1

    # Build all week labels
    all_weeks = []
    ptr = cutoff
    while ptr <= now:
        all_weeks.append(_week_label(ptr))
        ptr += timedelta(weeks=1)
    all_weeks = sorted(set(all_weeks))

    weekly_series = [
        {
            "week":      wk,
            "tokens":    weekly[wk]["tokens"],
            "cost_usd":  round(weekly[wk]["cost_usd"], 4),
            "analyses":  weekly[wk]["analyses"],
        }
        for wk in all_weeks
    ]

    # Sort agents/models by cost descending
    agents_sorted = sorted(
        [{"agent": k, **v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_agent.items()],
        key=lambda x: -x["cost_usd"],
    )
    models_sorted = sorted(
        [{"model": k, **v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_model.items()],
        key=lambda x: -x["cost_usd"],
    )

    return {
        "summary": {
            "total_tokens":   total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "fallback_count": fallback_count,
            "weeks_covered":  weeks,
        },
        "weekly":  weekly_series,
        "by_agent": agents_sorted,
        "by_model": models_sorted,
        "pricing_note": "Estimates only — based on blended input/output token rates.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  SIMILAR PR DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/similar/{request_id}")
def similar_prs(request_id: str, top_k: int = 5):
    """
    Find past analyses similar to the given one.
    Similarity = file overlap score + summary keyword overlap.
    Returns the top_k most similar past PRs with their risk outcomes.
    """
    store  = _get_store()
    target = store.get(request_id)
    if not target:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")

    all_reports = _load_reports(limit=500)

    # Build target fingerprint
    target_files = set()
    if target.reference_impact:
        target_files.update(f.file_path for f in (target.reference_impact.references or []))
    for attr in ("security","performance_impact","data_privacy","maintainability"):
        result = getattr(target, attr, None)
        if result:
            for f in getattr(result, "findings", None) or getattr(result, "pii_findings", []):
                fp = getattr(f, "file_path", "")
                if fp:
                    target_files.add(fp)

    target_summary = ""
    if target.code_analysis:
        target_summary = (target.code_analysis.summary or "").lower()
    target_words = set(target_summary.split())

    results = []
    for r in all_reports:
        if r.request_id == request_id:
            continue

        # File overlap
        r_files: set[str] = set()
        for attr in ("security","performance_impact","data_privacy","maintainability"):
            result = getattr(r, attr, None)
            if result:
                for f in getattr(result, "findings", None) or getattr(result, "pii_findings", []):
                    fp = getattr(f, "file_path", "")
                    if fp:
                        r_files.add(fp)

        file_overlap = len(target_files & r_files) / max(len(target_files | r_files), 1)

        # Keyword overlap in summary
        r_summary = ""
        if r.code_analysis:
            r_summary = (r.code_analysis.summary or "").lower()
        r_words    = set(r_summary.split())
        kw_overlap = len(target_words & r_words) / max(len(target_words | r_words), 1)

        # Same repo bonus
        repo_match = 0.2 if r.repo_url == target.repo_url else 0.0

        similarity = round(0.5 * file_overlap + 0.3 * kw_overlap + repo_match, 3)
        if similarity < 0.05:
            continue

        results.append({
            "request_id":   r.request_id,
            "repo":         _short_repo(r.repo_url),
            "pr_title":     r.pr.pr_title  if r.pr else "",
            "pr_number":    r.pr.pr_number if r.pr else 0,
            "source_ref":   r.source_ref,
            "risk_score":   _risk_score(r),
            "gate":         r.gate_decision.value if r.gate_decision else "HOLD",
            "similarity":   similarity,
            "shared_files": sorted(target_files & r_files)[:5],
            "elapsed":      _elapsed(r),
        })

    results.sort(key=lambda x: -x["similarity"])
    return {
        "target_id": request_id,
        "similar":   results[:top_k],
        "total":     len(results),
    }


def _elapsed(r) -> str:
    try:
        ts = r.completed_at if hasattr(r, "completed_at") else datetime.utcnow()
        h  = (datetime.utcnow() - ts).total_seconds() / 3600
        if h < 1:    return "< 1h ago"
        if h < 24:   return f"{int(h)}h ago"
        return f"{int(h/24)}d ago"
    except Exception:
        return "—"


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  DEPENDENCY AUTO-UPDATE PR
# ═══════════════════════════════════════════════════════════════════════════════

class DepUpdateRequest(BaseModel):
    request_id:  str
    provider:    str = "github"
    token:       str = ""
    base_url:    str = ""
    workspace:   str = ""
    repo_slug:   str = ""
    target_ref:  str = "main"   # base branch to create fix PR against


@router.post("/dep-update")
def create_dep_update_pr(body: DepUpdateRequest):
    """
    For each CVE finding in the given analysis, look up a safe version via
    OSV.dev and create one dependency-update PR per affected package.

    Returns a list of PRs created (or skipped if no safe version found).
    """
    from api.app import get_report_store
    from api.routes.git_proxy import _headers, _bb_server_base, _get, GitConfig
    from output.pr_commenter import _normalise_api_url
    import requests as req_lib

    store  = get_report_store()
    report = store.get(body.request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{body.request_id}' not found.")

    if not report.dependency:
        return {"prs_created": [], "message": "No dependency findings in this analysis."}

    # DependencyResult stores changed_packages (list[str]) + cve_hits (list[str]).
    # Strip any version specifier so "requests==2.25.0" → "requests".
    import re as _re
    raw_pkgs = getattr(report.dependency, "changed_packages", []) or []
    packages = []
    for p in raw_pkgs:
        name = _re.split(r"[=<>~!\[ ]", p.strip())[0]
        if name and name not in packages:
            packages.append(name)

    if not packages:
        return {"prs_created": [], "message": "No changed packages to check."}

    from ingestion.osv_client import fixed_version_for

    api_base = _normalise_api_url(body.base_url, body.provider)
    results  = []

    for pkg in packages[:5]:   # cap at 5 auto-PRs per analysis
        # Try the most common ecosystems in order until OSV returns a fix.
        safe, cve = "", ""
        for eco in ("PyPI", "npm", "Maven", "Go", "crates.io", "RubyGems"):
            try:
                safe, cve = fixed_version_for(pkg, eco)
            except Exception as exc:
                log.debug("OSV lookup failed for %s [%s]: %s", pkg, eco, exc)
                continue
            if safe:
                break

        if not safe:
            results.append({"package": pkg, "cve": cve or "—",
                             "status": "skipped",
                             "reason": "No published fix found in OSV (package may be safe)."})
            continue
        cve = cve or "OSV advisory"

        # Create PR on GitHub
        if body.provider in ("github", "github_enterprise"):
            headers = {
                "Authorization": f"token {body.token}",
                "Accept":        "application/vnd.github.v3+json",
            }
            slug     = body.repo_slug
            branch   = f"fix/ciaa-{pkg.lower().replace('/','-').replace('_','-')}-{safe}"
            pr_title = f"fix(deps): update {pkg} to {safe} [{cve}]"
            pr_body  = (
                f"## Automated dependency update\n\n"
                f"**Package:** `{pkg}`  \n"
                f"**Vulnerability:** `{cve}`  \n"
                f"**Safe version:** `{safe}`  \n\n"
                f"This PR was created automatically by Code Analysis & Review "
                f"after detecting a CVE in analysis [`{body.request_id[:8]}…`].\n\n"
                f"### What to do\n"
                f"1. Review the version bump in the manifest file\n"
                f"2. Run your test suite\n"
                f"3. Merge when CI passes\n\n"
                f"---\n*Generated by CAR · [View full analysis](#)*"
            )

            try:
                # Get default branch SHA to create branch from
                ref_resp = req_lib.get(
                    f"{api_base}/repos/{slug}/git/ref/heads/{body.target_ref}",
                    headers=headers, timeout=10,
                )
                if not ref_resp.ok:
                    results.append({"package": pkg, "cve": cve, "status": "failed",
                                     "reason": f"Could not get base branch SHA: {ref_resp.status_code}"})
                    continue

                sha = ref_resp.json()["object"]["sha"]

                # Create branch
                req_lib.post(
                    f"{api_base}/repos/{slug}/git/refs",
                    headers=headers, timeout=10,
                    json={"ref": f"refs/heads/{branch}", "sha": sha},
                )

                # Open PR
                pr_resp = req_lib.post(
                    f"{api_base}/repos/{slug}/pulls",
                    headers=headers, timeout=10,
                    json={"title": pr_title, "body": pr_body,
                          "head": branch, "base": body.target_ref},
                )
                if pr_resp.status_code in (200, 201):
                    pr_data = pr_resp.json()
                    results.append({
                        "package":    pkg,
                        "cve":        cve,
                        "safe_version": safe,
                        "status":     "created",
                        "pr_url":     pr_data.get("html_url", ""),
                        "pr_number":  pr_data.get("number", 0),
                    })
                else:
                    results.append({"package": pkg, "cve": cve, "status": "failed",
                                     "reason": pr_resp.text[:200]})
            except Exception as exc:
                results.append({"package": pkg, "cve": cve,
                                 "status": "error", "reason": str(exc)})
        else:
            results.append({"package": pkg, "cve": cve,
                             "status": "skipped",
                             "reason": f"Auto-PR not yet supported for {body.provider}"})

    created = [r for r in results if r["status"] == "created"]
    return {
        "prs_created": results,
        "created_count": len(created),
        "message": f"{len(created)} PR(s) created successfully.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  CSV EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _csv_response(rows: list[list], header: list[str], filename: str):
    """Build a streaming CSV download response."""
    import csv, io
    from fastapi.responses import StreamingResponse

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/queue.csv")
def export_queue_csv(days: int = 0, repo: str = ""):
    """Download the review queue as CSV."""
    data = pr_priority_queue(limit=500, days=days, repo=repo)
    rows = [
        [i["repo"], i["pr_number"], i["pr_title"], i["author"],
         i["gate"], i["risk_score"], i["change_type"],
         i["total_tokens"], i["elapsed"], i["request_id"]]
        for i in data["queue"]
    ]
    return _csv_response(
        rows,
        ["repo","pr_number","pr_title","author","gate","risk_score",
         "change_type","total_tokens","elapsed","request_id"],
        f"ciaa_review_queue_{datetime.utcnow():%Y%m%d}.csv",
    )


@router.get("/export/cost.csv")
def export_cost_csv(weeks: int = 12, repo: str = ""):
    """Download the API cost breakdown (by agent) as CSV."""
    data = api_cost(weeks=weeks, repo=repo)
    rows = [[a["agent"], a["calls"], a["tokens"], a["cost_usd"]] for a in data["by_agent"]]
    return _csv_response(
        rows,
        ["agent","calls","tokens","cost_usd"],
        f"ciaa_api_cost_{datetime.utcnow():%Y%m%d}.csv",
    )


@router.get("/export/trend.csv")
def export_trend_csv(weeks: int = 12, repo: str = ""):
    """Download the per-repo weekly risk-score trend as CSV (long format)."""
    data = risk_trend(weeks=weeks, repo=repo)
    rows = []
    for s in data["series"]:
        for wk, score in zip(s["weeks"], s["scores"]):
            rows.append([s["repo"], wk, score, s["trend"]])
    return _csv_response(
        rows,
        ["repo","week","avg_risk_score","trend"],
        f"ciaa_risk_trend_{datetime.utcnow():%Y%m%d}.csv",
    )
