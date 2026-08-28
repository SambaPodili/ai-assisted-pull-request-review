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
import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.models import GateDecision
from governance.rbac import Permission, resolve_subject
from governance.audit_logger import AuditEvent
from config.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/gate", tags=["gate-override"])


class OverrideRequest(BaseModel):
    override_to: str   # APPROVE | HOLD | BLOCK
    reason:      str   # mandatory justification (audit requirement)
    # Optional git context — when present, the decision is ALSO reflected on the PR
    # (review status + commit status check). Never merges or closes the PR.
    provider:   str = ""
    token:      str = ""
    base_url:   str = ""
    repo_slug:  str = ""
    pr_id:      str = ""
    workspace:  str = ""


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

    # Human override of an automated gate is a compliance-significant event —
    # log it (the immutable audit record is written separately below).
    log.warning("Gate OVERRIDE on %s by %s (team=%s): %s -> %s — reason: %s",
                request_id, subject.name or subject.key_id, subject.team,
                original_gate, new_gate.value, body.reason.strip()[:200])

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
        log.debug("gate feedback record failed: %s", exc)

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

    # Reflect the decision ON the PR (review status + commit status check) — never
    # merges or closes it. Best-effort: a PR-action failure never fails the audit
    # record (which is the source of truth).
    pr_action = None
    if body.provider and body.token and body.pr_id:
        try:
            from output.pr_commenter import post_pr_decision
            pr_action = post_pr_decision(
                new_gate.value, body.provider, body.token, body.base_url, body.repo_slug,
                body.pr_id, body.workspace, reason=body.reason, report_id=request_id)
            log.info("Gate override PR action (%s) on %s: %s", new_gate.value, request_id, pr_action)
        except Exception as exc:
            pr_action = {"ok": False, "errors": [str(exc)]}
            log.warning("Gate override PR action failed for %s: %s", request_id, exc)

    return {
        "request_id":    request_id,
        "original_gate": original_gate,
        "override_to":   new_gate.value,
        "override_by":   subject.name or subject.key_id,
        "pr_action":     pr_action,
        "status":        "recorded",
        "message":       (
            f"Gate override recorded. Original: {original_gate} → Override: {new_gate.value}. "
            "This override is permanently logged for audit purposes."
        ),
    }


class ApprovePrRequest(BaseModel):
    provider:   str
    token:      str   # required — the reviewer's OWN credential, never the shared bot.
                       # A blank/missing token would defeat the entire point of this
                       # endpoint (the approval must show as the real reviewer on the
                       # PR, not "GTO Bot") — no server-side fallback, unlike /comment-pr.
    base_url:   str = ""
    workspace:  str = ""
    repo_slug:  str
    pr_id:      str


@router.post("/{request_id}/approve-pr")
def approve_pr(request_id: str, body: ApprovePrRequest, request: Request):
    """
    Reviewer sign-off on a PR — approval only, never merges. Distinct from
    /override: this doesn't change GTO's own gate decision or touch
    GateOverrideStore, it's a side-channel reviewer action reusing the same
    provider "approve" mechanism /override already relies on for its own
    optional PR reflection (output.pr_commenter.post_pr_decision).

    Requires: pr:approve permission. Recorded: immutable audit log entry
    (AuditEvent.PR_APPROVED), keyed to the calling Subject's identity —
    cross-reference against the PR's own approval identity (tied to
    whichever token was supplied) for audit purposes.
    """
    from api.app import get_report_store, get_audit_logger
    cfg = get_settings()

    subject = resolve_subject(request, skip_auth=cfg.skip_auth)
    if not subject:
        raise HTTPException(401, "Authentication required")
    subject.require(Permission.PR_APPROVE)

    if not body.token:
        raise HTTPException(400, "token is required — approval must show as your own identity on the PR")

    report = get_report_store().get(request_id)
    if not report:
        raise HTTPException(404, f"Report '{request_id}' not found")

    from output.pr_commenter import post_pr_decision
    try:
        pr_action = post_pr_decision(
            "APPROVE", body.provider, body.token, body.base_url, body.repo_slug,
            body.pr_id, body.workspace, reason="", report_id=request_id)
    except Exception as exc:
        log.warning("PR approve failed for %s: %s", request_id, exc)
        raise HTTPException(502, f"Could not approve the PR: {exc}")

    audit = get_audit_logger()
    audit.log(AuditEvent.PR_APPROVED, {
        "request_id":  request_id,
        "repo_slug":   body.repo_slug,
        "pr_id":       body.pr_id,
        "provider":    body.provider,
        "approved_by": subject.name or subject.key_id,
        "user_id":     subject.user_id,
    })

    return {
        "request_id": request_id,
        "pr_action":  pr_action,
        "status":     "approved" if pr_action.get("ok") else "failed",
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
