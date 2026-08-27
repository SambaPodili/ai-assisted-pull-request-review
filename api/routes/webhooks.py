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

from ingestion.webhook_parser import (
    parse_bitbucket_webhook, parse_github_webhook,
    parse_bitbucket_comment_webhook, parse_github_comment_webhook,
)
from ingestion.git_client import make_git_client
from ingestion.diff_parser import parse_diff
from governance.webhook_dedup import get_dedup_store

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
        reply = parse_bitbucket_comment_webhook(event, payload)
        if reply and not reply.is_bot:
            dedup = get_dedup_store()
            if dedup.is_duplicate("bitbucket_reply", reply.comment_id):
                log.info("[webhook] Bitbucket reply duplicate skipped: %s", reply.comment_id)
                return {"status": "duplicate"}
            dedup.mark_seen("bitbucket_reply", reply.comment_id)
            background.add_task(_handle_reply, reply)
            return {"status": "queued", "reply": True}
        return {"status": "skipped", "reason": f"Unhandled event: {event}"}

    # Deduplication: key on pr_id + head commit from metadata
    pr_id  = req.metadata.get("pr_id", "")
    sha    = req.metadata.get("head_sha", req.source_ref)
    dedup_key = f"{pr_id}:{sha}"
    dedup = get_dedup_store()
    if dedup.is_duplicate("bitbucket", dedup_key):
        log.info("[webhook] Bitbucket duplicate skipped: %s", dedup_key)
        return {"status": "duplicate", "request_id": req.request_id}
    dedup.mark_seen("bitbucket", dedup_key)

    get_audit_logger().log_webhook("bitbucket", event, req.request_id)
    background.add_task(_run_with_diff, req, "bitbucket", get_orchestrator(), get_report_store())
    return {"status": "queued", "request_id": req.request_id}


@router.post("/github")
async def github_webhook(
    request:                  Request,
    background:               BackgroundTasks,
    x_hub_signature_256:      str | None = Header(None, alias="X-Hub-Signature-256"),
    x_github_delivery:        str | None = Header(None, alias="X-GitHub-Delivery"),
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
        reply = parse_github_comment_webhook(event, payload)
        if reply and not reply.is_bot:
            dedup = get_dedup_store()
            if dedup.is_duplicate("github_reply", reply.comment_id):
                log.info("[webhook] GitHub reply duplicate skipped: %s", reply.comment_id)
                return {"status": "duplicate"}
            dedup.mark_seen("github_reply", reply.comment_id)
            background.add_task(_handle_reply, reply)
            return {"status": "queued", "reply": True}
        return {"status": "skipped", "reason": f"Unhandled event: {event}"}

    # Deduplication: prefer the unique X-GitHub-Delivery header; fall back to pr+sha
    dedup_key = x_github_delivery or f"{req.metadata.get('pr_id','')}:{req.source_ref}"
    dedup = get_dedup_store()
    if dedup.is_duplicate("github", dedup_key):
        log.info("[webhook] GitHub duplicate skipped: %s", dedup_key)
        return {"status": "duplicate", "request_id": req.request_id}
    dedup.mark_seen("github", dedup_key)

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

    try:
        from ingestion.path_review_config import load_from_git_client, load_team_default, merge_path_review_configs
        # Resolve .gto.yaml from the TARGET branch, never the PR's own head —
        # a PR must not be able to weaken scrutiny of itself via its own
        # config file. `git` here is the same client just used for the diff.
        repo_cfg = load_from_git_client(git, repo, req.target_ref)
        team_cfg = load_team_default(git)
        req.path_review_config = merge_path_review_configs(repo_cfg, team_cfg)
    except Exception as exc:
        log.debug("[%s] .gto.yaml load failed (%s) — proceeding without path-scoped rules", req.request_id, exc)

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
        posted_ok = commenter.post(report)
        if posted_ok:
            _record_summary_comment(req, provider, report.request_id)
    notifier.notify(report)


def _record_summary_comment(req, provider: str, request_id: str) -> None:
    """Records a sentinel 'summary' row in pr_comment_map so a later reply
    that ISN'T threaded to a specific finding (a general top-level question,
    no in_reply_to_id) can still resolve to the most recent report for this
    PR — see governance/review_session_store.py::latest_comment_for_pr."""
    try:
        pid = str(req.metadata.get("pr_id") or "")
        if not pid:
            return
        slug = req.metadata.get("repo_slug") or req.repo_url.rstrip("/").split("/")[-1]
        provider_key = "github" if provider == "github" else "bitbucket"
        from governance.review_session_store import get_review_store
        get_review_store().record_posted_comment(
            provider=provider_key, repo_slug=slug, pr_id=pid,
            comment_id="summary", request_id=request_id,
        )
    except Exception:
        log.debug("Failed to record summary comment for reply correlation", exc_info=True)


async def _handle_reply(reply) -> None:
    """Answers an interactive PR chat reply (Item 1) — the is_bot guard is
    already enforced by the caller before this task is even scheduled.
    Never raises: a failure here should never surface as a 500 to the
    provider's webhook delivery, it's a background best-effort task."""
    from config.settings import get_settings
    from governance.review_session_store import get_review_store
    from governance.reply_answerer import answer_reply, CANNED_BLOCKED_REPLY
    from output.pr_commenter import make_pr_commenter
    from api.app import get_report_store

    cfg   = get_settings()
    store = get_review_store()

    rate_limit = getattr(cfg, "reply_rate_limit_per_hour", 10)
    recent = store.count_recent_replies(reply.provider, reply.repo_slug, reply.pr_id, reply.author, 3600)
    if recent >= rate_limit:
        log.warning("[reply] Rate limit hit for %s on %s#%s — skipping", reply.author, reply.repo_slug, reply.pr_id)
        store.log_reply(reply.provider, reply.repo_slug, reply.pr_id, reply.author, blocked=True)
        return

    # Resolve which finding/report this reply concerns.
    mapped = None
    if reply.in_reply_to_id:
        mapped = store.lookup_comment(reply.provider, reply.repo_slug, reply.pr_id, reply.in_reply_to_id)
    if not mapped:
        mapped = store.latest_comment_for_pr(reply.provider, reply.repo_slug, reply.pr_id)
    if not mapped:
        log.info("[reply] No correlated report for %s#%s — nothing to answer", reply.repo_slug, reply.pr_id)
        return

    report = get_report_store().get(mapped["request_id"])
    if not report:
        log.info("[reply] Correlated report %s no longer available — nothing to answer", mapped["request_id"])
        return

    finding_context = {}
    if mapped.get("file_path"):
        finding_context = {
            "file_path": mapped["file_path"],
            "line": mapped.get("line", ""),
            "top_issues": [
                {"title": it.title, "severity": it.severity, "line": it.line}
                for it in (report.top_issues or [])
                if it.file_path == mapped["file_path"]
            ],
        }
    report_summary = {
        "gate_decision": getattr(report.gate_decision, "value", report.gate_decision),
        "final_risk":    getattr(report.final_risk, "value", report.final_risk),
        "rationale":     getattr(report.risk, "rationale", "") if report.risk else "",
    }

    answer = answer_reply(reply.body, finding_context, report_summary)
    blocked = answer == CANNED_BLOCKED_REPLY

    commenter = make_pr_commenter(cfg)
    reply_comment_id = None
    if commenter:
        reply_comment_id = commenter.reply_to_comment(
            reply.repo_slug, reply.pr_id, answer, in_reply_to_id=reply.in_reply_to_id,
        )
    store.log_reply(reply.provider, reply.repo_slug, reply.pr_id, reply.author,
                     reply_comment_id=reply_comment_id or "", blocked=blocked)


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
