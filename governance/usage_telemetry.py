"""
governance/usage_telemetry.py
------------------------------
GenAI usage telemetry → ELK (developer portal `genai_usage` index).

Emits one document per lifecycle event of an analysis run, in the portal schema:

    {id, user_id, action, task_id, description, timestamp,
     metadata{…}, tool_version, tool_id, tool_name, app_code, domain}

Per the agreed mapping:
  • user_id   = the ACTUAL user (Bitbucket id) — from an SSO/gateway header, the
                request metadata, or the authenticated subject; falls back to the
                repo slug only when no user identity is available. repo_slug is
                always kept as a metadata subfield.
  • app_code  = the LAST 3 CHARACTERS of the PROJECT KEY (segment before the repo)
  • task_id   = the analysis request_id
  • domain    = the logged user's domain (SSO/gateway header, fallback config)
  • metadata  = repo_slug + per-event subfields (result_length, findings, gate…)
  • tool_*/app_code defaults come from settings (G040 / Code Analysis and Review).

A run emits 2 docs: started → code_analysis_success (which consolidates duration,
gate, risk, security findings, top issues); a failed run emits started → failure.

ALL emission is best-effort and fire-and-forget: any error is logged and
swallowed so telemetry can never affect or fail an analysis.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _cfg(cfg=None):
    if cfg is not None:
        return cfg
    from config.settings import get_settings
    return get_settings()


def slug_parts(repo_url: str) -> tuple[str, str]:
    """Return (project_key, repo_slug) from a repo URL or 'PROJECT/repo' slug.

    Handles full HTTPS URLs (github.com/owner/repo, host/scm/PROJ/repo.git) and
    bare Bitbucket-Server slugs (PROJ/repo). The last path segment is the repo
    (→ user_id); the segment before it is the project key (→ app_code)."""
    s = (repo_url or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("@")[-1].rstrip("/")          # strip any user:pass@ and trailing /
    if s.endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.split("/") if p]
    # Drop a leading host segment (contains a dot, e.g. bitbucket.company.com)
    if parts and "." in parts[0]:
        parts = parts[1:]
    repo    = parts[-1] if parts else ""
    project = parts[-2] if len(parts) >= 2 else ""
    return project, repo


def _app_code(project_key: str, default: str) -> str:
    pk = (project_key or "").strip()
    return pk[-3:].upper() if pk else (default or "")


def _doc(cfg, *, action: str, task_id: str, repo_url: str, domain: str,
         user_id: str = "", description: str, metadata: dict | None = None) -> dict:
    project, repo = slug_parts(repo_url)
    # user_id = the ACTUAL user (Bitbucket id from SSO header / metadata / subject);
    # repo slug is kept as a metadata subfield and used only as a last-resort
    # fallback when no real user identity is available.
    uid = (user_id or "").strip() or repo
    md = {"repo_slug": repo}
    if metadata:
        md.update(metadata)
    return {
        "id":           str(uuid.uuid4()),
        "user_id":      uid,
        "action":       action,
        "task_id":      task_id or "",
        "description":  description,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
        "metadata":     md,
        "tool_version": cfg.elk_tool_version,
        "tool_id":      cfg.elk_tool_id,
        "tool_name":    cfg.elk_tool_name,
        "app_code":     _app_code(project, cfg.elk_app_code_default),
        "domain":       domain or cfg.elk_default_domain or "",
        "integration_id": getattr(cfg, "elk_integration_id", "") or "",
        "environment":    getattr(cfg, "elk_environment", "") or "",
    }


# ── Event builders ────────────────────────────────────────────────────────────

def started_doc(request_id: str, repo_url: str, domain: str,
                files_changed: int = 0, user_id: str = "", cfg=None) -> dict:
    cfg = _cfg(cfg)
    return _doc(cfg, action="code_analysis_started", task_id=request_id,
                repo_url=repo_url, domain=domain, user_id=user_id,
                description=f"Code analysis started for task: {request_id}",
                metadata={"files_changed": files_changed})


def _sev_counts(findings) -> tuple[int, int]:
    crit = high = 0
    for f in findings or []:
        s = str(getattr(getattr(f, "severity", ""), "value", getattr(f, "severity", ""))).lower()
        if s == "critical":
            crit += 1
        elif s == "high":
            high += 1
    return crit, high


def completion_docs(report, domain: str, result_length: int = 0,
                    duration_s: float = 0.0, user_id: str = "", cfg=None) -> list[dict]:
    """A SINGLE end-of-run success doc consolidating analysis + security + gate +
    report metadata (so a run emits just 2 ELK docs: started + completed)."""
    cfg = _cfg(cfg)
    rid  = getattr(report, "request_id", "") or ""
    repo = getattr(report, "repo_url", "") or ""
    gate = getattr(getattr(report, "gate_decision", None), "value", None) or "HOLD"
    risk = int(getattr(report, "risk_score", 0) or 0)
    sec  = getattr(report, "security", None)
    sec_findings = list(getattr(sec, "findings", None) or []) if sec else []
    crit, high = _sev_counts(sec_findings)
    top_issues = len(getattr(report, "top_issues", None) or [])

    return [
        _doc(cfg, action="code_analysis_success", task_id=rid, repo_url=repo, domain=domain, user_id=user_id,
             description=f"Code analysis completed for task: {rid}",
             metadata={
                 "result_length":     int(result_length),
                 "duration_s":        round(duration_s, 2),
                 "gate":              gate,
                 "risk_score":        risk,
                 "security_findings": len(sec_findings),
                 "critical":          crit,
                 "high":              high,
                 "top_issues":        top_issues,
             }),
    ]


def failure_doc(request_id: str, repo_url: str, domain: str, error: str,
                user_id: str = "", cfg=None) -> dict:
    cfg = _cfg(cfg)
    return _doc(cfg, action="code_analysis_failure", task_id=request_id,
                repo_url=repo_url, domain=domain, user_id=user_id,
                description=f"Code analysis failed for task: {request_id}",
                metadata={"error": str(error)[:300]})


# ── Emission ──────────────────────────────────────────────────────────────────

def emit(docs, cfg=None) -> int:
    """POST each doc to ELK. Returns the count successfully sent. Never raises."""
    cfg = _cfg(cfg)
    if not getattr(cfg, "elk_usage_enabled", False) or not docs:
        return 0
    try:
        import requests
    except Exception as exc:                       # pragma: no cover
        log.warning("ELK usage telemetry: requests unavailable: %s", exc)
        return 0
    headers = {
        "Content-Type": getattr(cfg, "elk_content_type", "") or "application/json",
        "Accept":       getattr(cfg, "elk_accept", "") or "application/json",
    }
    if getattr(cfg, "elk_auth_header", ""):
        headers["Authorization"] = cfg.elk_auth_header
    sent = 0
    for d in docs:
        try:
            r = requests.post(cfg.elk_usage_url, json=d, headers=headers,
                              timeout=getattr(cfg, "elk_timeout_s", 5.0),
                              verify=getattr(cfg, "elk_verify_ssl", True))
            if r.status_code < 300:
                sent += 1
            else:
                log.warning("ELK usage POST %s -> %s: %s",
                            d.get("action"), r.status_code, (r.text or "")[:200])
        except Exception as exc:
            log.warning("ELK usage POST failed (%s): %s", d.get("action"), exc)
    return sent
