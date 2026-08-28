"""
tests/unit/test_incremental_reanalysis.py
--------------------------------------------
Unit tests for incremental re-analysis v1 (skip on trivial pushes):
  - PRMetadata now gets populated on webhook-triggered AnalysisRequests
    (previously always empty — a real prerequisite gap).
  - governance/review_session_store.py's new pr_analysis_head table.
  - The "no net new code" detection signal (parse_diff on an empty/no-op
    range) that api/routes/webhooks.py::_run_with_diff gates on.
"""
from __future__ import annotations
import tempfile
import os

from ingestion.webhook_parser import parse_github_webhook, parse_bitbucket_webhook
from ingestion.diff_parser import parse_diff
from governance.review_session_store import SQLiteReviewSessionStore


def test_github_pr_webhook_populates_pr_metadata():
    payload = {
        "action": "synchronize",
        "repository": {"html_url": "https://github.com/org/repo"},
        "pull_request": {
            "number": 42,
            "title": "Add feature",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "abc123"},
            "base": {"ref": "main", "sha": "def456"},
        },
    }
    req = parse_github_webhook("pull_request", payload)
    assert req is not None
    assert req.pr.pr_number == 42
    assert req.pr.pr_title == "Add feature"
    assert req.pr.author == "alice"
    assert req.pr.head_sha == "abc123"
    assert req.pr.base_sha == "def456"


def test_bitbucket_cloud_pr_webhook_populates_pr_metadata():
    payload = {
        "repository": {"full_name": "team/repo", "links": {"html": {"href": "https://bitbucket.org/team/repo"}}},
        "pullrequest": {
            "id": 7,
            "title": "Fix bug",
            "author": {"display_name": "bob"},
            "source": {"branch": {"name": "fix"}, "commit": {"hash": "aaa111"}},
            "destination": {"branch": {"name": "main"}, "commit": {"hash": "bbb222"}},
        },
    }
    req = parse_bitbucket_webhook("pullrequest:updated", payload)
    assert req is not None
    assert req.pr.pr_number == 7
    assert req.pr.head_sha == "aaa111"
    assert req.pr.base_sha == "bbb222"


def test_bitbucket_server_pr_webhook_populates_pr_metadata():
    payload = {
        "pullRequest": {
            "id": 3,
            "title": "Server PR",
            "author": {"user": {"displayName": "carol"}},
            "fromRef": {"displayId": "feature", "latestCommit": "ccc333",
                        "repository": {"project": {"key": "PROJ"}, "slug": "repo"}},
            "toRef": {"displayId": "main", "latestCommit": "ddd444",
                      "repository": {"project": {"key": "PROJ"}, "slug": "repo"}},
        },
    }
    req = parse_bitbucket_webhook("pr:modified", payload)
    assert req is not None
    assert req.pr.pr_number == 3
    assert req.pr.head_sha == "ccc333"
    assert req.pr.base_sha == "ddd444"


def test_pr_analysis_head_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        store = SQLiteReviewSessionStore(db_path)

        assert store.get_last_analyzed_head("github", "org/repo", "42") is None

        store.record_pr_head("github", "org/repo", "42", "sha-1", "req-1")
        row = store.get_last_analyzed_head("github", "org/repo", "42")
        assert row is not None
        assert row["head_sha"] == "sha-1"
        assert row["request_id"] == "req-1"

        # A later push updates the recorded head (INSERT OR REPLACE, primary
        # keyed on provider+repo_slug+pr_id — one row per PR, not history).
        store.record_pr_head("github", "org/repo", "42", "sha-2", "req-2")
        row = store.get_last_analyzed_head("github", "org/repo", "42")
        assert row["head_sha"] == "sha-2"
        assert row["request_id"] == "req-2"

        # A different PR is tracked independently.
        assert store.get_last_analyzed_head("github", "org/repo", "99") is None


def test_identical_range_diff_parses_to_no_hunks():
    """The exact signal _run_with_diff gates on: a rebase/merge-commit-only
    push whose content is identical to what's already on the base produces
    an empty diff, which parse_diff correctly reduces to zero hunks."""
    assert parse_diff("") == []
    assert parse_diff("   \n  \n") == []


def test_real_diff_still_parses_to_hunks():
    diff = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 import os
+password = "secret"
 def main(): pass
"""
    hunks = parse_diff(diff)
    assert len(hunks) == 1
    assert hunks[0].file_path == "x.py"
