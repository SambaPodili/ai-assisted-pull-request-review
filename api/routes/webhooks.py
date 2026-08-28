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
    git    = None
    pr_id  = req.metadata.get("pr_id")
    repo   = req.metadata.get("repo_slug") or req.repo_url.rstrip("/").split("/")[-1]
    new_head = req.pr.head_sha if req.pr else ""
    prior: dict | None = None   # set below if this PR was analyzed before — also used for triage carry-forward
    report = None                # set by whichever path below produces the final report

    try:
        git = make_git_client()

        # Incremental re-analysis: if this exact PR was already analyzed,
        # decide what the new push actually requires.
        # See governance/review_session_store.py::pr_analysis_head.
        if pr_id and new_head:
            from governance.review_session_store import get_review_store
            review_store = get_review_store()
            prior = review_store.get_last_analyzed_head(provider, repo, str(pr_id))
            if prior and prior.get("head_sha") and prior["head_sha"] != new_head:
                try:
                    incremental_diff = git.get_branch_diff(repo, prior["head_sha"], new_head)
                    incremental_hunks = parse_diff(incremental_diff)
                    if not incremental_hunks:
                        # No net new code (a rebase, a merge commit, a
                        # whitespace-only commit) — skip the full pipeline and
                        # point back at the prior result. Real cost/time
                        # savings for the common "nothing meaningful changed"
                        # case. record_pr_head is deliberately NOT updated
                        # here — the old head remains the valid "last real
                        # analysis" point for the next push's diff range.
                        prior_report = store.get(prior["request_id"])
                        if prior_report is not None:
                            log.info("[%s] No net new code since %s for PR %s (repo=%s) — reusing prior result",
                                     req.request_id, prior["request_id"], pr_id, repo)
                            from config.settings import get_settings as _get_settings
                            from output.pr_commenter import make_pr_commenter as _make_pr_commenter
                            _commenter = _make_pr_commenter(_get_settings())
                            if _commenter:
                                _commenter.post_text(
                                    f"GTO: no net new code since the last analysis "
                                    f"(`{prior['request_id']}`) — reusing that result rather than "
                                    f"re-running the full review.",
                                    pr_id, repo,
                                )
                            return
                    else:
                        # Real new content — true incremental re-review:
                        # analyze ONLY the new commits, then merge with the
                        # prior full report, instead of re-analyzing the
                        # whole PR from scratch.
                        report = await _run_incremental_merge(
                            req, orch, store, git, repo, pr_id, prior, incremental_hunks)
                except Exception as exc:
                    log.debug("[%s] Incremental-diff check failed (%s) — proceeding with full analysis",
                              req.request_id, exc)

        if report is None:
            # No prior analysis for this PR, or the incremental path above
            # didn't produce a report (fell through / failed) — today's
            # unchanged full-PR-diff, full-pipeline behavior.
            if pr_id:
                raw_diff = git.get_pr_diff(repo, pr_id)
            else:
                raw_diff = git.get_branch_diff(repo, req.source_ref, req.target_ref)

            req.hunks = parse_diff(raw_diff)

            try:
                from ingestion.path_review_config import load_from_git_client, load_team_default, merge_path_review_configs
                # Resolve .gto.yaml from the TARGET branch, never the PR's own
                # head — a PR must not be able to weaken scrutiny of itself
                # via its own config file. `git` here is the same client just
                # used for the diff.
                repo_cfg = load_from_git_client(git, repo, req.target_ref)
                team_cfg = load_team_default(git)
                req.path_review_config = merge_path_review_configs(repo_cfg, team_cfg)
            except Exception as exc:
                log.debug("[%s] .gto.yaml load failed (%s) — proceeding without path-scoped rules", req.request_id, exc)

            report = await orch.analyse_async(req)
            store.save(report)
    except Exception as exc:
        log.warning("[%s] Diff fetch failed (%s) — proceeding without diff", req.request_id, exc)
        if report is None:
            report = await orch.analyse_async(req)
            store.save(report)

    if pr_id and new_head:
        from governance.review_session_store import get_review_store as _get_review_store
        _review_store = _get_review_store()
        # Fixes an orphaning bug that exists today independent of
        # incremental re-analysis: review_triage is keyed only by
        # request_id, so a reviewer's false_positive/won't_fix verdict on
        # the PRIOR request_id would otherwise be silently invisible on this
        # new one. Runs for every re-analysis of a tracked PR, not just an
        # incremental-merge one. Deliberately a SEPARATE try/except from
        # record_pr_head below — a carry-forward failure must never prevent
        # the new head from being recorded.
        if prior and prior.get("request_id"):
            try:
                carried = _review_store.carry_forward_triage(prior["request_id"], report.request_id, report.top_issues)
                if carried:
                    log.info("[%s] Carried forward %d triage verdict(s) from %s",
                             req.request_id, carried, prior["request_id"])
            except Exception as exc:
                log.debug("[%s] carry_forward_triage failed (%s)", req.request_id, exc)
        try:
            _review_store.record_pr_head(provider, repo, str(pr_id), new_head, report.request_id)
        except Exception as exc:
            log.debug("[%s] record_pr_head failed (%s)", req.request_id, exc)

    # Optional: post PR comment. Uses the same richer, per-file-grouped,
    # suggestion-fenced renderer (post_inline_comments) the manual "Post to
    # PR" button already uses, instead of the older, flatter PRCommenter.post()
    # — the automatic webhook-triggered flow gets the same polish. Credential
    # resolution mirrors make_pr_commenter's own branching exactly (same
    # cfg.post_pr_comments gate, same per-provider token/workspace/api_url
    # selection) since post_inline_comments is a free function, not a
    # PRCommenter method.
    from config.settings import get_settings
    from output.pr_commenter import post_inline_comments
    from output.notification import make_notification_service
    cfg      = get_settings()
    notifier = make_notification_service(cfg)
    if cfg.post_pr_comments and pr_id:
        if cfg.git_provider == "bitbucket_server":
            pr_token, pr_workspace, pr_base_url = cfg.bitbucket_token, cfg.bitbucket_workspace, cfg.bitbucket_api_url
        elif cfg.git_provider == "bitbucket":
            pr_token, pr_workspace, pr_base_url = cfg.bitbucket_token, cfg.bitbucket_workspace, cfg.bitbucket_api_url
        else:
            pr_token, pr_workspace, pr_base_url = cfg.github_token, "", cfg.github_api_url
        try:
            total_posted = post_inline_comments(
                report=report, token=pr_token, provider=cfg.git_provider,
                workspace=pr_workspace, base_url=pr_base_url,
                repo_slug=repo, pr_id=str(pr_id),
            )
            if total_posted > 0:
                _record_summary_comment(req, provider, report.request_id)
        except Exception as exc:
            log.warning("[%s] PR comment posting failed (%s)", req.request_id, exc)
    notifier.notify(report)


async def _run_incremental_merge(req, orch, store, git, repo, pr_id, prior: dict, incremental_hunks: list):
    """True incremental re-review: analyze ONLY the new commits, then merge
    with the prior full report — instead of re-running the whole PR's diff
    through every agent again. Returns the merged, re-finalized report, or
    None on any internal failure (the caller falls back to today's full
    re-analysis in that case — this function deliberately never raises).
    """
    try:
        prior_report = store.get(prior["request_id"])
        if prior_report is None:
            log.debug("[%s] Prior report %s not in store — falling back to full analysis",
                      req.request_id, prior["request_id"])
            return None

        # Run the existing, unmodified pipeline against ONLY the new commits'
        # hunks — no orchestrator changes needed, it already works on
        # whatever's in request.hunks.
        req.hunks = incremental_hunks
        partial_report = await orch.analyse_async(req)

        from governance.report_merge import merge_reports
        merged = merge_reports(prior_report, partial_report)

        # Full PR diff — cheap (no LLM cost), needed only to supply correct
        # changed_files/changed_lines/source_lines so old findings (already
        # verified against the full diff originally) don't get incorrectly
        # re-flagged unverified for touching files outside the new commits'
        # range. No agent re-runs against this — it's used purely for the
        # re-verification passes below.
        full_raw_diff = git.get_pr_diff(repo, pr_id)
        full_hunks = parse_diff(full_raw_diff)
        full_req_view = req.model_copy(update={"hunks": full_hunks})

        changed_files = {h.file_path for h in full_hunks}
        changed_lines = orch._changed_lines(full_req_view)
        source_lines = orch._source_lines(full_req_view)

        # _finalize needs a TokenBudgetManager only to record report.token_budget
        # (summary()["total_allocated"]) — no new agent calls happen here, so a
        # manager whose one custom budget equals the two real reports' already-
        # recorded totals is sufficient (not a fresh per-agent allocation).
        from core.token_manager import TokenBudgetManager
        total_budget = (prior_report.token_budget or 0) + (partial_report.token_budget or 0)
        budget = TokenBudgetManager(merged.request_id, custom_budgets={"_merge": total_budget})

        merged = orch._finalize(merged, budget, changed_files, changed_lines, source_lines)
        store.save(merged)
        log.info("[%s] Incremental re-review: merged %s (prior) + new commits — gate=%s",
                  req.request_id, prior["request_id"], merged.gate_decision)
        return merged
    except Exception as exc:
        log.warning("[%s] Incremental merge failed (%s) — falling back to full analysis",
                    req.request_id, exc, exc_info=True)
        return None


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
