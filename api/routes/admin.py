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

from governance.rbac import Permission, Subject, Role, ROLE_META, require_permission

router = APIRouter(prefix="/admin", tags=["admin"])


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
