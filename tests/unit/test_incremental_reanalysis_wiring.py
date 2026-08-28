"""
tests/unit/test_incremental_reanalysis_wiring.py
----------------------------------------------------
Functional (not mock-heavy) test of api/routes/webhooks.py::_run_with_diff's
new skip-vs-full-reanalyze branch, using lightweight duck-typed fakes for the
git client / orchestrator / report store rather than a full mock harness —
exercises the real function's actual control flow end-to-end.
"""
from __future__ import annotations
import asyncio
import uuid
from unittest.mock import patch

import pytest

from core.models import AnalysisRequest, ChangeType, PRMetadata
from governance.review_session_store import SQLiteReviewSessionStore


class FakeGitClient:
    def __init__(self, branch_diffs: dict[tuple[str, str, str], str]):
        self._branch_diffs = branch_diffs
        self.pr_diff_calls = []
        self.branch_diff_calls = []

    def get_pr_diff(self, repo, pr_id):
        self.pr_diff_calls.append((repo, pr_id))
        return "diff --git a/x.py b/x.py\n+full pr diff content\n"

    def get_branch_diff(self, repo, source, target):
        self.branch_diff_calls.append((repo, source, target))
        return self._branch_diffs.get((repo, source, target), "")


class FakeOrchestrator:
    def __init__(self):
        self.calls = 0

    async def analyse_async(self, req):
        self.calls += 1
        report = type("FakeReport", (), {})()
        report.request_id = f"new-{self.calls}"
        return report


class FakeReportStore:
    def __init__(self):
        self.saved = []
        self._by_id = {}

    def get(self, request_id):
        return self._by_id.get(request_id)

    def save(self, report):
        self.saved.append(report)
        self._by_id[report.request_id] = report

    def seed(self, request_id):
        r = type("FakeReport", (), {})()
        r.request_id = request_id
        self._by_id[request_id] = r


def make_req(head_sha: str, pr_id: str = "42") -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/org/repo",
        source_ref="feature",
        target_ref="main",
        metadata={"pr_id": pr_id, "repo_slug": "org/repo"},
        pr=PRMetadata(pr_number=42, head_sha=head_sha, base_sha="base-sha"),
    )


@pytest.fixture
def review_store(tmp_path, monkeypatch):
    store = SQLiteReviewSessionStore(str(tmp_path / "test.db"))
    monkeypatch.setattr("governance.review_session_store.get_review_store", lambda: store)
    return store


def _run(req, git, orch, report_store):
    from api.routes.webhooks import _run_with_diff
    with patch("api.routes.webhooks.make_git_client", return_value=git), \
         patch("output.pr_commenter.make_pr_commenter", return_value=None), \
         patch("output.notification.make_notification_service") as mock_notif, \
         patch("ingestion.path_review_config.load_from_git_client", return_value=None), \
         patch("ingestion.path_review_config.load_team_default", return_value=None):
        mock_notif.return_value.notify = lambda *a, **k: None
        asyncio.run(_run_with_diff(req, "github", orch, report_store))


def test_first_analysis_runs_full_and_records_head(review_store):
    git = FakeGitClient({})
    orch = FakeOrchestrator()
    report_store = FakeReportStore()

    req = make_req(head_sha="sha-1")
    _run(req, git, orch, report_store)

    assert orch.calls == 1
    assert len(report_store.saved) == 1
    recorded = review_store.get_last_analyzed_head("github", "org/repo", "42")
    assert recorded["head_sha"] == "sha-1"
    assert recorded["request_id"] == report_store.saved[0].request_id


def test_trivial_push_skips_full_reanalysis(review_store):
    git = FakeGitClient({("org/repo", "sha-1", "sha-2"): ""})  # empty incremental diff
    orch = FakeOrchestrator()
    report_store = FakeReportStore()
    review_store.record_pr_head("github", "org/repo", "42", "sha-1", "prior-req")
    report_store.seed("prior-req")

    req = make_req(head_sha="sha-2")
    _run(req, git, orch, report_store)

    assert orch.calls == 0, "should not have run full analysis for a no-op push"
    assert len(report_store.saved) == 0
    # Head is NOT updated on a skipped push — still points at the prior real analysis.
    recorded = review_store.get_last_analyzed_head("github", "org/repo", "42")
    assert recorded["head_sha"] == "sha-1"


def test_real_content_push_falls_back_gracefully_when_merge_cant_run(review_store):
    """This file's FakeReportStore.seed() creates a bare stub object, not a
    real pydantic AnalysisReport — so the true-incremental-merge path
    (governance/report_merge.py::merge_reports, which needs a real
    .model_copy()) legitimately fails here, by construction of this test's
    lightweight fakes. What this test actually verifies: that failure is
    caught internally and never crashes the whole run — it falls back to a
    full re-analysis (a second analyse_async call) and still produces a
    saved, head-tracked result. The SUCCESSFUL incremental-merge path (real
    findings from both old+new correctly merged, real gate re-evaluated) is
    covered with real AnalysisReport objects and a real orchestrator in
    tests/unit/test_true_incremental_review.py — that's the test to look at
    for "does true incremental review actually work," not this one."""
    real_diff = (
        "diff --git a/y.py b/y.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/y.py\n"
        "+++ b/y.py\n"
        "@@ -1,1 +1,2 @@\n"
        " existing_line = 1\n"
        "+new_line = 1\n"
    )
    git = FakeGitClient({("org/repo", "sha-1", "sha-2"): real_diff})
    orch = FakeOrchestrator()
    report_store = FakeReportStore()
    review_store.record_pr_head("github", "org/repo", "42", "sha-1", "prior-req")
    report_store.seed("prior-req")

    req = make_req(head_sha="sha-2")
    _run(req, git, orch, report_store)

    # One call for the attempted (and internally-failed) incremental merge,
    # one for the full-reanalysis fallback it correctly degrades to.
    assert orch.calls == 2
    assert len(report_store.saved) == 1
    recorded = review_store.get_last_analyzed_head("github", "org/repo", "42")
    assert recorded["head_sha"] == "sha-2"
    assert recorded["request_id"] == report_store.saved[0].request_id
