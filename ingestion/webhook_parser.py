"""
ingestion/webhook_parser.py
----------------------------
Converts raw Bitbucket / GitHub webhook payloads into AnalysisRequest objects
(new-diff-to-analyze events), or into ReplyEvent objects (PR-comment events —
a reply/new comment, not a new diff). Pure functions — no I/O, no HTTP calls.
"""
from __future__ import annotations
import uuid
from core.models import AnalysisRequest, ChangeType, ReplyEvent


def parse_bitbucket_webhook(event: str, payload: dict) -> AnalysisRequest | None:
    """
    Map a Bitbucket webhook event to an AnalysisRequest.

    Supported events:
      pullrequest:created, pullrequest:updated, pullrequest:fulfilled
      repo:push  (commit push)
    """
    request_id = str(uuid.uuid4())
    repo_url   = _bb_repo_url(payload)

    if event in ("pullrequest:created", "pullrequest:updated", "pullrequest:fulfilled"):
        pr  = payload.get("pullrequest", {})
        src = _bb_nested(pr, "source",      "branch", "name")
        dst = _bb_nested(pr, "destination", "branch", "name") or "main"
        return AnalysisRequest(
            request_id=request_id,
            change_type=ChangeType.PR,
            repo_url=repo_url,
            source_ref=src,
            target_ref=dst,
            metadata={
                "pr_id":    pr.get("id"),
                "pr_title": pr.get("title", ""),
                "author":   _bb_nested(pr, "author", "display_name"),
                "provider": "bitbucket",
            },
        )

    if event in ("repo:push", "repo:refs_changed"):
        changes = payload.get("push", {}).get("changes", [{}])
        change  = changes[0] if changes else {}
        new_ref = _bb_nested(change, "new",    "name")  or "HEAD"
        old_sha = _bb_nested(change, "old", "target", "hash") or "HEAD~1"
        return AnalysisRequest(
            request_id=request_id,
            change_type=ChangeType.COMMIT,
            repo_url=repo_url,
            source_ref=new_ref,
            target_ref=old_sha,
            metadata={"provider": "bitbucket"},
        )

    return None   # unsupported event


def parse_github_webhook(event: str, payload: dict) -> AnalysisRequest | None:
    """
    Map a GitHub webhook event to an AnalysisRequest.

    Supported events:
      pull_request  (action: opened, synchronize, reopened)
      push
    """
    request_id = str(uuid.uuid4())
    repo_url   = payload.get("repository", {}).get("html_url", "")

    if event == "pull_request":
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return None
        pr  = payload.get("pull_request", {})
        src = _gh_nested(pr, "head", "ref")
        dst = _gh_nested(pr, "base", "ref") or "main"
        return AnalysisRequest(
            request_id=request_id,
            change_type=ChangeType.PR,
            repo_url=repo_url,
            source_ref=src,
            target_ref=dst,
            metadata={
                "pr_id":       pr.get("number"),
                "pr_title":    pr.get("title", ""),
                "author":      _gh_nested(pr, "user", "login"),
                "head_sha":    _gh_nested(pr, "head", "sha"),
                "base_sha":    _gh_nested(pr, "base", "sha"),
                "provider":    "github",
            },
        )

    if event == "push":
        before = payload.get("before", "")
        after  = payload.get("after",  "")
        ref    = payload.get("ref", "").removeprefix("refs/heads/")
        # Ignore branch deletion (after = 000...0)
        if after.startswith("000000"):
            return None
        return AnalysisRequest(
            request_id=request_id,
            change_type=ChangeType.COMMIT,
            repo_url=repo_url,
            source_ref=after,
            target_ref=before or f"{after}~1",
            metadata={
                "branch":    ref,
                "pusher":    _gh_nested(payload, "pusher", "name"),
                "provider":  "github",
            },
        )

    return None   # unsupported event


def parse_github_comment_webhook(event: str, payload: dict) -> ReplyEvent | None:
    """
    Map a GitHub comment webhook event to a ReplyEvent (interactive PR chat
    replies — see governance/reply_answerer.py). NOT an AnalysisRequest —
    this is a reply to answer, not a diff to analyze.

    Supported events:
      issue_comment              (action: created) — top-level PR conversation
                                  comment. Only when the issue IS a PR
                                  (payload["issue"]["pull_request"] present).
      pull_request_review_comment (action: created) — threaded review comment,
                                  may carry in_reply_to_id.
    """
    if event not in ("issue_comment", "pull_request_review_comment"):
        return None
    if payload.get("action") != "created":
        return None

    comment = payload.get("comment", {})
    user    = comment.get("user", {}) or {}
    repo_slug = payload.get("repository", {}).get("full_name", "")

    if event == "issue_comment":
        issue = payload.get("issue", {})
        if "pull_request" not in issue:
            return None   # a plain issue comment, not a PR — nothing to answer
        pr_id = str(issue.get("number", ""))
        in_reply_to = None   # issue_comment is always top-level, never threaded
    else:
        pr = payload.get("pull_request", {})
        pr_id = str(pr.get("number", ""))
        raw_reply_to = comment.get("in_reply_to_id")
        in_reply_to = str(raw_reply_to) if raw_reply_to is not None else None

    if not repo_slug or not pr_id or not comment.get("id"):
        return None

    return ReplyEvent(
        provider=      "github",
        repo_slug=     repo_slug,
        pr_id=         pr_id,
        comment_id=    str(comment.get("id")),
        in_reply_to_id=in_reply_to,
        body=          comment.get("body", "") or "",
        author=        user.get("login", ""),
        is_bot=        user.get("type", "") == "Bot",
    )


def parse_bitbucket_comment_webhook(event: str, payload: dict) -> ReplyEvent | None:
    """
    Map a Bitbucket comment webhook event to a ReplyEvent.

    Supported events:
      pullrequest:comment_created (Bitbucket Cloud)

    NOTE: Bitbucket's webhook payload has no standardized bot-account marker
    the way GitHub's `user.type == "Bot"` does — is_bot is always False here.
    Rate-limiting (governance/reply_answerer.py) is the backstop for this
    provider, not the is_bot guard.
    """
    if event != "pullrequest:comment_created":
        return None

    pr      = payload.get("pullrequest", {})
    comment = payload.get("comment", {})
    repo_slug = payload.get("repository", {}).get("full_name", "")
    pr_id     = str(pr.get("id", ""))
    body      = _bb_nested(comment, "content", "raw")
    parent_id = comment.get("parent", {}).get("id") if isinstance(comment.get("parent"), dict) else None

    if not repo_slug or not pr_id or not comment.get("id"):
        return None

    return ReplyEvent(
        provider=      "bitbucket",
        repo_slug=     repo_slug,
        pr_id=         pr_id,
        comment_id=    str(comment.get("id")),
        in_reply_to_id=str(parent_id) if parent_id is not None else None,
        body=          body,
        author=        _bb_nested(comment, "user", "display_name"),
        is_bot=        False,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _bb_repo_url(payload: dict) -> str:
    return payload.get("repository", {}).get("links", {}).get("html", {}).get("href", "")


def _bb_nested(d: dict, *keys, default: str = "") -> str:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if isinstance(d, str) else default


def _gh_nested(d: dict, *keys, default: str = "") -> str:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if isinstance(d, str) else default
