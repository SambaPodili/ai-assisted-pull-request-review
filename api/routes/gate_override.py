"""
api/routes/gate_override.py
----------------------------
Human-in-the-loop gate override endpoint.

Allows authorized analysts (gate:override permission) to override an automated
BLOCK or HOLD decision before re-triggering the CI/CD pipeline.

Banking compliance: all overrides are written to the immutable audit log with
the overrider's identity, reason, and timestamp.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.models import GateDecision
from governance.rbac import Permission, resolve_subject
from governance.audit_logger import AuditEvent
from config.settings import get_settings

router = APIRouter(prefix="/api/v1/gate", tags=["gate-override"])


class OverrideRequest(BaseModel):
    override_to: str   # APPROVE | HOLD | BLOCK
    reason:      str   # mandatory justification (audit requirement)


@router.post("/{request_id}/override")
def override_gate(request_id: str, body: OverrideRequest, request: Request):
    """
    Override an automated gate decision.

    Requires:  gate:override permission
    Recorded:  immutable audit log entry
    """
    from api.app import get_report_store, get_audit_logger
    cfg = get_settings()

    # RBAC check
    subject = resolve_subject(request, skip_auth=cfg.skip_auth)
    if not subject:
        raise HTTPException(401, "Authentication required")
    subject.require(Permission.GATE_OVERRIDE)

    # Validate target gate
    try:
        new_gate = GateDecision(body.override_to.upper())
    except ValueError:
        raise HTTPException(400, f"Invalid gate value: {body.override_to}")

    if not body.reason or len(body.reason.strip()) < 10:
        raise HTTPException(400, "reason must be at least 10 characters (audit requirement)")

    # Load report
    store  = get_report_store()
    report = store.get(request_id)
    if not report:
        raise HTTPException(404, f"Report '{request_id}' not found")

    original_gate = report.gate_decision.value

    # Record override in gate override store
    from governance.rbac import GateOverride, get_gate_override_store
    override = GateOverride(
        request_id=request_id,
        original_gate=original_gate,
        override_to=new_gate.value,
        reason=body.reason,
        override_by=subject.name or subject.key_id,
        override_team=subject.team,
    )
    get_gate_override_store().record(override)

    # Record into the feedback loop (AI vs policy vs human) for tuning over time
    try:
        from governance.feedback_store import get_feedback_store
        get_feedback_store().record_gate(
            request_id=request_id,
            repo=report.repo_url,
            ai_gate=getattr(report, "ai_proposed_gate", "") or original_gate,
            policy_gate=original_gate,
            human_gate=new_gate.value,
            reason=body.reason,
            reviewer=subject.name or subject.key_id,
        )
    except Exception as exc:
        import logging; logging.getLogger(__name__).debug("gate feedback record failed: %s", exc)

    # Write immutable audit record
    audit = get_audit_logger()
    audit.log(AuditEvent.GATE_OVERRIDE, {
        "request_id":    request_id,
        "original_gate": original_gate,
        "override_to":   new_gate.value,
        "reason":        body.reason,
        "override_by":   subject.name or subject.key_id,
        "override_team": subject.team,
    })

    return {
        "request_id":    request_id,
        "original_gate": original_gate,
        "override_to":   new_gate.value,
        "override_by":   subject.name or subject.key_id,
        "status":        "recorded",
        "message":       (
            f"Gate override recorded. Original: {original_gate} → Override: {new_gate.value}. "
            "This override is permanently logged for audit purposes."
        ),
    }


@router.get("/overrides")
def list_overrides(request: Request):
    """List all gate overrides (auditor/admin only)."""
    from api.app import get_audit_logger
    cfg = get_settings()

    subject = resolve_subject(request, skip_auth=cfg.skip_auth)
    if not subject:
        raise HTTPException(401, "Authentication required")
    subject.require(Permission.AUDIT_READ)

    from governance.rbac import get_gate_override_store
    overrides = get_gate_override_store().list_all()
    return {
        "count":     len(overrides),
        "overrides": [
            {
                "request_id":    o.request_id,
                "original_gate": o.original_gate,
                "override_to":   o.override_to,
                "reason":        o.reason,
                "override_by":   o.override_by,
                "override_team": o.override_team,
            }
            for o in overrides
        ],
    }
