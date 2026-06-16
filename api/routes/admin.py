"""
api/routes/admin.py
--------------------
Operational endpoints: health check, token usage metrics, configuration info.
Phase 4: Prometheus /metrics, circuit breaker status, human gate overrides.
Includes user/key management endpoints for admin and analyst roles.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime

from pydantic import BaseModel

from governance.rbac import Permission, Subject, Role, ROLE_META, require_permission

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _actor(subject: Subject) -> str:
    """Human-readable actor for audit logs (never the raw key)."""
    return getattr(subject, "name", None) or "unknown"


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

    filt = (f"repo_contains={body.repo_contains!r} older_than_days={body.older_than_days} "
            f"demo_only={body.demo_only}")
    if body.dry_run:
        log.info("Admin purge DRY-RUN by %s — %d report(s) match (%s)",
                 _actor(subject), len(matched), filt)
        return {
            "dry_run": True,
            "would_delete": len(matched),
            "sample": matched[:25],
            "message": f"{len(matched)} report(s) match. Re-run with dry_run=false to delete.",
        }

    # Destructive — audit at WARNING with the actor and filters.
    log.warning("Admin purge EXECUTE by %s — deleting %d matched report(s) (%s)",
                _actor(subject), len(matched), filt)
    deleted = sum(1 for m in matched if store.delete(m["request_id"]))
    log.warning("Admin purge by %s — %d report(s) deleted", _actor(subject), deleted)
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
    log.info("Admin digest send requested by %s (days=%d)", _actor(subject), days)
    result = send_digest(days=days)
    if not result.get("ok"):
        log.warning("Admin digest send failed (requested by %s): %s",
                    _actor(subject), result.get("reason", "?"))
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
    List API key holders — file/bootstrap keys (from keys.json) AND UI-managed
    users (SQLite, hashed keys). Keys are always redacted.
    """
    from governance.rbac import get_registry
    from governance.user_store import get_user_store
    file_users = [{**u, "source": "file"} for u in get_registry().list_subjects()]
    managed    = [{**u, "source": "managed"} for u in get_user_store().list_users()]
    # what roles THIS admin may create (drives the UI's role dropdown)
    creatable = sorted(r.value for r in subject.manageable_roles())
    return {
        "users":      file_users + managed,
        "total":      len(file_users) + len(managed),
        "creatable_roles": creatable,
    }


class CreateUserBody(BaseModel):
    name:    str = ""             # display name (e.g. Bitbucket displayName)
    team:    str = ""
    roles:   list[str] = ["developer"]
    user_id: str = ""            # external identity (e.g. Bitbucket slug)


class UpdateUserBody(BaseModel):
    name:    str | None = None
    team:    str | None = None
    roles:   list[str] | None = None
    user_id: str | None = None


def _parse_roles_or_400(role_strs: list[str]) -> list[Role]:
    try:
        return [Role(r) for r in (role_strs or [])]
    except ValueError as exc:
        raise HTTPException(400, f"Invalid role: {exc}")


@router.post("/users")
def create_user(body: CreateUserBody,
                subject: Subject = require_permission(Permission.USER_MANAGE)):
    """Create a UI-managed user. The role(s) must be within the caller's tier
    (a Super Admin can mint Admins; an Admin can mint Developers/Reviewers — never
    a peer/higher role). Returns the new API key ONCE."""
    roles = _parse_roles_or_400(body.roles)
    if not subject.can_manage(roles):
        raise HTTPException(403, detail=(
            f"You may only create users with roles: "
            f"{sorted(r.value for r in subject.manageable_roles())}."))
    from governance.user_store import get_user_store
    store = get_user_store()
    key, record = store.create_user(body.name, body.team, roles, _actor(subject), user_id=body.user_id)
    store.record_event("created", _actor(subject), record["id"], record["name"], roles)
    log.warning("Admin user-create by %s — %s roles=%s", _actor(subject), record["id"], record["roles"])
    return {"ok": True, "user": record, "api_key": key,
            "message": "Copy this API key now — it is shown only once and cannot be recovered."}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody,
                subject: Subject = require_permission(Permission.USER_MANAGE)):
    from governance.user_store import get_user_store
    store = get_user_store()
    existing = store.get(user_id)
    if not existing:
        raise HTTPException(404, "User not found")
    # Must be able to manage BOTH the current and the new roles.
    if not subject.can_manage(_parse_roles_or_400(existing["roles"])):
        raise HTTPException(403, "This user is outside your management tier.")
    new_roles = _parse_roles_or_400(body.roles) if body.roles is not None else None
    if new_roles is not None and not subject.can_manage(new_roles):
        raise HTTPException(403, "You cannot assign one or more of those roles.")
    record = store.update(user_id, name=body.name, team=body.team, roles=new_roles, user_id=body.user_id)
    store.record_event("updated", _actor(subject), user_id, (record or {}).get("name", ""),
                       new_roles or [], detail="role/profile update")
    log.warning("Admin user-update by %s — %s", _actor(subject), user_id)
    return {"ok": True, "user": record}


@router.delete("/users/{user_id}")
def revoke_user(user_id: str,
                subject: Subject = require_permission(Permission.USER_MANAGE)):
    from governance.user_store import get_user_store
    store = get_user_store()
    existing = store.get(user_id)
    if not existing:
        raise HTTPException(404, "User not found")
    if not subject.can_manage(_parse_roles_or_400(existing["roles"])):
        raise HTTPException(403, "This user is outside your management tier.")
    ok = store.revoke(user_id)
    if ok:
        store.record_event("revoked", _actor(subject), user_id, existing.get("name", ""),
                           _parse_roles_or_400(existing["roles"]))
    log.warning("Admin user-revoke by %s — %s (%s)", _actor(subject), user_id, existing.get("name"))
    return {"ok": ok, "message": "User revoked." if ok else "Nothing to revoke."}


@router.get("/users/audit")
def user_audit(limit: int = 100,
               subject: Subject = require_permission(Permission.USER_MANAGE)):
    """Audit trail of user-management actions (who created/updated/revoked whom)."""
    from governance.user_store import get_user_store
    return {"events": get_user_store().list_audit(limit=min(max(limit, 1), 500))}


@router.post("/users/reload")
def reload_keys(subject: Subject = require_permission(Permission.ADMIN_CONFIG)):
    """
    Hot-reload API keys from settings and API_KEYS_FILE without restarting.
    Use this after editing keys.json to apply changes immediately.
    Requires admin role.
    """
    from governance.rbac import get_registry
    count = get_registry().reload()
    log.warning("Admin API-key reload by %s — %d key(s) now active", _actor(subject), count)
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
