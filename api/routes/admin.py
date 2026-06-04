"""
api/routes/admin.py
--------------------
Operational endpoints: health check, token usage metrics, configuration info.
Phase 4: Prometheus /metrics, circuit breaker status, human gate overrides.
Includes user/key management endpoints for admin and analyst roles.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime

from pydantic import BaseModel

from governance.rbac import Permission, Subject, Role, ROLE_META, require_permission

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Report purge (admin only) ─────────────────────────────────────────────────

import re as _re

# Real analyses always use a UUID request_id; seed/demo rows (digest-t1, ins0,
# dup2, purge-0, test-review-001 …) do not. Matching non-UUID ids cleanly
# targets demo data without risking real runs.
_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", _re.IGNORECASE
)


def _is_demo_id(request_id: str) -> bool:
    return not bool(_UUID_RE.match((request_id or "").strip()))


class PurgeRequest(BaseModel):
    repo_contains: str = ""     # delete reports whose repo URL/slug contains this substring (case-insensitive)
    older_than_days: int = 0    # delete reports older than N days (0 = no age filter)
    demo_only: bool = False     # delete only seed/demo rows (non-UUID request_ids) — safe one-click cleanup
    dry_run: bool = True        # preview only — does NOT delete unless explicitly false


@router.post("/reports/purge")
def purge_reports(body: PurgeRequest,
                  subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """
    Delete stored analysis reports matching a repo substring and/or age.
    Defaults to dry_run=true so you can preview what would be removed.
    At least one filter (repo_contains or older_than_days) is required to avoid
    accidentally wiping everything. Admin only.
    """
    from datetime import datetime, timezone, timedelta
    from api.app import get_report_store

    if not body.repo_contains and body.older_than_days <= 0 and not body.demo_only:
        raise HTTPException(400, detail="Provide repo_contains, older_than_days, or demo_only (refusing to match all reports).")

    store   = get_report_store()
    needle  = body.repo_contains.strip().lower()
    cutoff  = None
    if body.older_than_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=body.older_than_days)

    # Scan recent reports (cap high so we cover the store)
    metas = store.list_recent(limit=1000)
    matched: list[dict] = []
    for m in metas:
        repo = (m.get("repo") or "").lower()
        if body.demo_only and not _is_demo_id(m.get("request_id", "")):
            continue
        if needle and needle not in repo:
            continue
        if cutoff is not None:
            ts_raw = m.get("completed_at", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    continue
            except Exception:
                continue
        matched.append({"request_id": m["request_id"], "repo": m.get("repo", ""),
                        "completed_at": m.get("completed_at", "")})

    if body.dry_run:
        return {
            "dry_run": True,
            "would_delete": len(matched),
            "sample": matched[:25],
            "message": f"{len(matched)} report(s) match. Re-run with dry_run=false to delete.",
        }

    deleted = sum(1 for m in matched if store.delete(m["request_id"]))
    return {
        "dry_run": False,
        "deleted": deleted,
        "message": f"Deleted {deleted} report(s).",
    }


# ── Email digest ──────────────────────────────────────────────────────────────

@router.post("/digest/send")
def send_digest_now(days: int = 1,
                    subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """
    Build and send the daily digest email immediately.
    Useful for testing SMTP config or sending an ad-hoc summary. Admin only.
    """
    from output.digest import send_digest
    result = send_digest(days=days)
    if not result.get("ok"):
        raise HTTPException(400, detail=result.get("reason", "Digest send failed"))
    return result


@router.get("/digest/preview")
def preview_digest(days: int = 1,
                   subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """Return the digest HTML without sending — for previewing in the browser."""
    from output.digest import build_digest_data, render_digest_html
    from fastapi.responses import HTMLResponse
    data = build_digest_data(days=days)
    return HTMLResponse(render_digest_html(data))


# ── User / key management (admin only) ───────────────────────────────────────

@router.get("/users")
def list_users(subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """
    List all configured API key holders with their roles and permissions.
    Keys are redacted — only the first 10 characters are shown.
    Requires admin role.
    """
    from governance.rbac import get_registry
    return {
        "users":   get_registry().list_subjects(),
        "total":   len(get_registry().list_subjects()),
    }


@router.post("/users/reload")
def reload_keys(subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """
    Hot-reload API keys from settings and API_KEYS_FILE without restarting.
    Use this after editing keys.json to apply changes immediately.
    Requires admin role.
    """
    from governance.rbac import get_registry
    count = get_registry().reload()
    return {
        "ok":      True,
        "message": f"Reloaded {count} key(s) — no restart required.",
        "count":   count,
    }


@router.get("/roles")
def list_roles():
    """Return all available roles and their capabilities. No auth required."""
    return {
        role.value: {
            "label":       meta["label"],
            "description": meta["description"],
            "color":       meta["color"],
            "can_comment": meta["can_comment"],
            "can_override":meta["can_override"],
            "permissions": [p.value for p in _ROLE_PERMISSIONS_REF(role)],
        }
        for role, meta in ROLE_META.items()
    }


def _ROLE_PERMISSIONS_REF(role: Role):
    from governance.rbac import _ROLE_PERMISSIONS
    return _ROLE_PERMISSIONS.get(role, set())


@router.get("/metrics/tokens")
def token_metrics():
    """Aggregate token usage across all stored reports."""
    from api.app import get_report_store
    store   = get_report_store()
    reports = store.list_recent(limit=100)

    per_agent: dict[str, int] = {}
    grand_total = 0

    for meta in reports:
        rid    = meta["request_id"]
        report = store.get(rid)
        if not report:
            continue
        for usage in report.token_usage:
            key = usage.agent.value
            per_agent[key] = per_agent.get(key, 0) + usage.tokens_used
            grand_total    += usage.tokens_used

    return {
        "per_agent":   per_agent,
        "grand_total": grand_total,
        "reports":     len(reports),
    }


@router.get("/circuit-breakers")
def circuit_breaker_status():
    """Current state of all agent circuit breakers."""
    from governance.circuit_breaker import get_breaker_registry
    return {"breakers": get_breaker_registry().all_metrics()}


@router.get("/gate/pending")
def pending_gate_overrides():
    """List pending human-in-the-loop gate override requests."""
    from governance.human_gate import get_gate_service
    return {"pending": get_gate_service().pending_overrides()}


@router.post("/gate/{request_id}/override")
def request_gate_override(
    request_id: str,
    new_gate:   str,
    reason:     str,
    approver_id: str,
):
    """Submit a gate override request (requires a second approver for BLOCK)."""
    from governance.human_gate import get_gate_service
    from core.models import GateDecision
    try:
        gate = GateDecision(new_gate.upper())
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(400, f"Invalid gate value: {new_gate}")

    override = get_gate_service().request_override(
        request_id=request_id,
        new_gate=new_gate_val if (new_gate_val := gate) else gate,
        reason=reason,
        approver_id=approver_id,
    )
    return {
        "request_id": request_id,
        "approved":   override.approved,
        "approvers":  override.approvers,
        "new_gate":   gate.value,
    }


@router.post("/gate/{request_id}/approve")
def approve_gate_override(request_id: str, approver_id: str):
    """Second-approver sign-off for a gate override (four-eyes principle)."""
    from governance.human_gate import get_gate_service
    approved = get_gate_service().approve_override(request_id, approver_id)
    return {"request_id": request_id, "approved": approved}


@router.get("/config")
def config_info():
    """Non-sensitive configuration summary."""
    from config.settings import get_settings
    cfg  = get_settings()
    return {
        "phase":                 cfg.analysis_phase,
        "git_provider":          cfg.git_provider,
        "compliance_frameworks": cfg.compliance_frameworks,
        "post_pr_comments":      cfg.post_pr_comments,
        "redis_enabled":         bool(cfg.redis_url),
        "neo4j_enabled":         bool(cfg.neo4j_url),
        "chroma_host":           cfg.chroma_host,
        "budgets":               cfg.agent_budgets,
    }
