"""
api/routes/webhooks.py
-----------------------
Webhook receivers for Bitbucket and GitHub.
Performs HMAC-SHA256 signature verification before processing.
"""
from __future__ import annotations
import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from ingestion.webhook_parser import parse_bitbucket_webhook, parse_github_webhook
from ingestion.git_client import make_git_client
from ingestion.diff_parser import parse_diff

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhooks"])


@router.post("/bitbucket")
async def bitbucket_webhook(
    request:            Request,
    background:         BackgroundTasks,
    x_hub_signature:    str | None = Header(None, alias="X-Hub-Signature"),
):
    """Receive Bitbucket Cloud / Server webhooks."""
    from api.app import get_orchestrator, get_report_store, get_audit_logger
    from config.settings import get_settings

    cfg  = get_settings()
    body = await request.body()
    _verify_hmac(body, x_hub_signature, cfg.bitbucket_webhook_secret)

    payload = await request.json()
    event   = request.headers.get("X-Event-Key", "")
    req     = parse_bitbucket_webhook(event, payload)

    if not req:
        return {"status": "skipped", "reason": f"Unhandled event: {event}"}

    get_audit_logger().log_webhook("bitbucket", event, req.request_id)
    background.add_task(_run_with_diff, req, "bitbucket", get_orchestrator(), get_report_store())
    return {"status": "queued", "request_id": req.request_id}


@router.post("/github")
async def github_webhook(
    request:                  Request,
    background:               BackgroundTasks,
    x_hub_signature_256:      str | None = Header(None, alias="X-Hub-Signature-256"),
):
    """Receive GitHub / GitHub Enterprise webhooks."""
    from api.app import get_orchestrator, get_report_store, get_audit_logger
    from config.settings import get_settings

    cfg  = get_settings()
    body = await request.body()
    _verify_hmac(body, x_hub_signature_256, cfg.github_webhook_secret)

    payload = await request.json()
    event   = request.headers.get("X-GitHub-Event", "")
    req     = parse_github_webhook(event, payload)

    if not req:
        return {"status": "skipped", "reason": f"Unhandled event: {event}"}

    get_audit_logger().log_webhook("github", event, req.request_id)
    background.add_task(_run_with_diff, req, "github", get_orchestrator(), get_report_store())
    return {"status": "queued", "request_id": req.request_id}


# ── Background task ────────────────────────────────────────────────────────────

async def _run_with_diff(req, provider, orch, store):
    """Fetch the actual diff then run analysis."""
    try:
        git    = make_git_client()
        pr_id  = req.metadata.get("pr_id")
        repo   = req.metadata.get("repo_slug") or req.repo_url.rstrip("/").split("/")[-1]

        if pr_id:
            raw_diff = git.get_pr_diff(repo, pr_id)
        else:
            raw_diff = git.get_branch_diff(repo, req.source_ref, req.target_ref)

        from ingestion.diff_parser import parse_diff
        req.hunks = parse_diff(raw_diff)
    except Exception as exc:
        log.warning("[%s] Diff fetch failed (%s) — proceeding without diff", req.request_id, exc)

    report = await orch.analyse_async(req)
    store.save(report)

    # Optional: post PR comment
    from config.settings import get_settings
    from output.pr_commenter import make_pr_commenter
    from output.notification import make_notification_service
    cfg        = get_settings()
    commenter  = make_pr_commenter(cfg)
    notifier   = make_notification_service(cfg)
    if commenter:
        commenter.post(report)
    notifier.notify(report)


# ── HMAC verification ─────────────────────────────────────────────────────────

def _verify_hmac(body: bytes, signature: str | None, secret: str) -> None:
    """Raise HTTP 401 if signature is invalid. No-op when secret is not configured."""
    if not secret:
        return
    if not signature:
        raise HTTPException(401, "Missing webhook signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, "Invalid webhook signature")
