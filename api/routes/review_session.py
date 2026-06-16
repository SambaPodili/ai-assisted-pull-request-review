"""
api/routes/review_session.py
-----------------------------
Developer → reviewer → PR review workflow ("continuity").

  • Developer (analysis:submit) triages each finding (valid / false_positive /
    wont_fix + comment), then SUBMITs for review.
  • Reviewer (gate:override) validates each (confirmed / rejected + comment),
    picks the final gate, and FINALIZEs.
  • On finalize, the "real issues" — developer VALID ∩ reviewer CONFIRMED — are
    returned (and posted to the PR by the caller); false-positives feed the
    suppression learning loop.

State is persisted in governance/review_session_store so the handoff survives
reloads and different people.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from governance.rbac import Permission, resolve_subject
from governance.review_session_store import get_review_store, STAGES
from config.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/review", tags=["review-workflow"])


def _subject(request: Request):
    cfg = get_settings()
    subject = resolve_subject(request, skip_auth=cfg.skip_auth)
    if not subject:
        raise HTTPException(401, "Authentication required")
    return subject


def _actor(subject) -> str:
    return getattr(subject, "name", None) or getattr(subject, "key_id", "") or "unknown"


class TriageBody(BaseModel):
    finding_key: str
    role:        str               # developer | reviewer
    verdict:     str               # dev: valid|false_positive|wont_fix · reviewer: confirmed|rejected
    comment:     str = ""
    # finding metadata (stored on first triage so the session is self-contained)
    title:    str = ""
    agent:    str = ""
    file:     str = ""
    line:     str = ""
    severity: str = ""
    repo:     str = ""
    pr_id:    str = ""


class FinalizeBody(BaseModel):
    gate:        str = "APPROVE"   # reviewer's final decision: APPROVE | HOLD | BLOCK
    post_to_pr:  bool = True
    repo:        str = ""
    pr_id:       str = ""
    # git config to actually post the validated issues to the PR (optional)
    provider:    str = ""
    token:       str = ""
    workspace:   str = ""
    base_url:    str = ""
    repo_slug:   str = ""


@router.get("/{request_id}")
def get_session(request_id: str, request: Request):
    """Return the review session (stage + per-finding triage). Read access."""
    subject = _subject(request)
    subject.require(Permission.ANALYSIS_READ)
    store = get_review_store()
    return store.get_session(request_id) or {
        "request_id": request_id, "stage": "developer", "triage": [],
        "final_gate": "", "posted_to_pr": False,
    }


@router.post("/{request_id}/triage")
def set_triage(request_id: str, body: TriageBody, request: Request):
    """Record a developer or reviewer verdict + comment on one finding."""
    subject = _subject(request)
    if body.role == "developer":
        subject.require(Permission.ANALYSIS_SUBMIT)
    elif body.role == "reviewer":
        subject.require(Permission.GATE_OVERRIDE)   # REVIEWER role
    else:
        raise HTTPException(400, "role must be 'developer' or 'reviewer'")

    meta = {"title": body.title, "agent": body.agent, "file": body.file,
            "line": body.line, "severity": body.severity, "repo": body.repo, "pr_id": body.pr_id}
    try:
        row = get_review_store().set_triage(
            request_id, body.finding_key, body.role, body.verdict,
            body.comment, _actor(subject), meta)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("Review triage %s by %s on %s: %s=%s",
             request_id, _actor(subject), body.finding_key, body.role, body.verdict)
    return {"ok": True, "triage": row}


@router.post("/{request_id}/submit")
def submit_for_review(request_id: str, request: Request):
    """Developer hands the session off to a reviewer."""
    subject = _subject(request)
    subject.require(Permission.ANALYSIS_SUBMIT)
    session = get_review_store().submit(request_id, _actor(subject))
    log.info("Review %s submitted for review by %s", request_id, _actor(subject))
    return {"ok": True, "session": session}


@router.post("/{request_id}/reopen")
def reopen(request_id: str, request: Request):
    """Reviewer sends the session back to the developer for rework."""
    subject = _subject(request)
    subject.require(Permission.GATE_OVERRIDE)
    session = get_review_store().reopen(request_id, _actor(subject))
    return {"ok": True, "session": session}


@router.post("/{request_id}/finalize")
def finalize(request_id: str, body: FinalizeBody, request: Request):
    """Reviewer finalizes: record the gate, return the VALIDATED real issues
    (dev-valid ∩ reviewer-confirmed) + a ready-to-post Markdown block, and feed
    false-positives to the suppression learning loop."""
    subject = _subject(request)
    subject.require(Permission.GATE_OVERRIDE)
    store = get_review_store()

    validated = store.validated_findings(request_id)
    markdown = _validated_markdown(validated)

    # Post the reviewer-validated "real issues" back to the PR (server-side) when
    # git config + a PR id are supplied. Best-effort — never fails the finalize.
    posted = False
    if body.post_to_pr and validated and body.pr_id and body.token:
        subject.require(Permission.PR_COMMENT)
        try:
            from output.pr_commenter import PRCommenter
            pc = PRCommenter(token=body.token, provider=body.provider,
                             workspace=body.workspace, api_url=body.base_url)
            posted = pc.post_text(markdown, body.pr_id, body.repo_slug)
        except Exception as exc:
            log.warning("Review finalize: PR post failed: %s", exc)

    session = store.finalize(request_id, body.gate.upper(), _actor(subject), posted=posted)

    # Feed the learning loop: developer-marked false positives + the reviewer's gate.
    try:
        from governance.feedback_store import get_feedback_store
        fb = get_feedback_store()
        for t in store.list_triage(request_id):
            if t.get("dev_verdict") == "false_positive" or t.get("reviewer_verdict") == "rejected":
                fb.record_finding(request_id, body.repo, t.get("agent", ""), t.get("severity", ""),
                                  t.get("file", ""), "false_positive",
                                  t.get("reviewer_comment") or t.get("dev_comment") or "",
                                  _actor(subject))
    except Exception as exc:
        log.debug("Review finalize: feedback learning skipped: %s", exc)

    log.warning("Review %s FINALIZED by %s — gate=%s, %d validated issue(s)%s",
                request_id, _actor(subject), body.gate.upper(), len(validated),
                " (posted)" if posted else "")
    return {"ok": True, "session": session, "gate": body.gate.upper(),
            "validated": validated, "validated_count": len(validated),
            "markdown": markdown, "posted": posted}


def _validated_markdown(validated: list[dict]) -> str:
    if not validated:
        return ""
    lines = ["### ✅ Validated issues to fix (reviewer-confirmed)", ""]
    sev_icon = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🔵"}
    for t in validated:
        loc = f" — `{t.get('file','')}{':' + str(t['line']) if t.get('line') else ''}`" if t.get("file") else ""
        note = f"  \n  _reviewer: {t['reviewer_comment']}_" if t.get("reviewer_comment") else ""
        lines.append(f"- {sev_icon.get((t.get('severity') or '').lower(),'•')} "
                     f"**{(t.get('severity') or '').upper()}** {t.get('title','')}{loc}{note}")
    lines.append("")
    return "\n".join(lines)
