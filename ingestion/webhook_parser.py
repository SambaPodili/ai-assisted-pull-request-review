"""
ingestion/webhook_parser.py
----------------------------
Converts raw Bitbucket / GitHub webhook payloads into AnalysisRequest objects.
Pure functions — no I/O, no HTTP calls.
"""
from __future__ import annotations
import uuid
from core.models import AnalysisRequest, ChangeType


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
