"""
tests/unit/test_true_incremental_review.py
----------------------------------------------
End-to-end integration test for "true incremental re-review"
(api/routes/webhooks.py::_run_incremental_merge): a real push with actual new
content triggers analysis of ONLY the new commits, merges with the prior
full report, and re-runs the REAL _finalize pipeline (not mocked) — so
governance/correlation.py::correlate_findings and governance/gate_policy.py::
evaluate_policy both execute for real against the merged report.
"""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType, PRMetadata,
    SecurityResult, SecurityFinding, RiskLevel,
)
from core.orchestrator import ImpactAnalysisOrchestrator
from governance.review_session_store import SQLiteReviewSessionStore


class FakeGitClient:
    """Distinguishes the incremental-range diff from the full-PR diff by the
    specific (source, target) args passed — matching how
    _run_incremental_merge actually calls each."""

    def get_pr_diff(self, repo, pr_id):
        return (
            "diff --git a/old.py b/old.py\nindex 1..2 100644\n--- a/old.py\n+++ b/old.py\n"
            "@@ -1,1 +1,2 @@\n context\n+old_area_line = 1\n"
            "diff --git a/new.py b/new.py\nindex 1..2 100644\n--- a/new.py\n+++ b/new.py\n"
            "@@ -1,1 +1,2 @@\n context\n+password = \"hardcoded_secret_123\"\n"
        )

    def get_branch_diff(self, repo, source, target):
        # old_head -> new_head: just the new commit's content.
        return (
            "diff --git a/new.py b/new.py\nindex 1..2 100644\n--- a/new.py\n+++ b/new.py\n"
            "@@ -1,1 +1,2 @@\n context\n+password = \"hardcoded_secret_123\"\n"
        )


def make_prior_report() -> AnalysisReport:
    return AnalysisReport(
        request_id="prior-req", change_type=ChangeType.PR,
        repo_url="https://github.com/org/repo", source_ref="feature", target_ref="main",
        completed_at=datetime.now(timezone.utc),
        security=SecurityResult(findings=[
            SecurityFinding(file_path="old.py", line_range="2", severity=RiskLevel.MEDIUM,
                             description="Pre-existing medium finding", remediation="fix later",
                             cwe_id="CWE-000"),
        ]),
    )


class FakeReportStore:
    def __init__(self):
        self.saved = []
        self._by_id = {"prior-req": make_prior_report()}

    def get(self, request_id):
        return self._by_id.get(request_id)

    def save(self, report):
        self.saved.append(report)
        self._by_id[report.request_id] = report


def make_req(head_sha: str, pr_id: str = "42") -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()), change_type=ChangeType.PR,
        repo_url="https://github.com/org/repo", source_ref="feature", target_ref="main",
        metadata={"pr_id": pr_id, "repo_slug": "org/repo"},
        pr=PRMetadata(pr_number=42, head_sha=head_sha, base_sha="sha-1"),
    )


async def fake_analyse_async(req):
    """Stands in for the real (LLM-backed) agent pipeline — returns a
    deterministic partial report reflecting whatever hunks it was given, so
    the test can distinguish the incremental-only call from a hypothetical
    full-reanalysis call without needing a real LLM."""
    files = {h.file_path for h in req.hunks}
    findings = []
    if "new.py" in files:
        findings.append(SecurityFinding(
            file_path="new.py", line_range="2", severity=RiskLevel.HIGH,
            description="Hardcoded secret in code.", remediation="rotate and remove",
            cwe_id="CWE-798",
        ))
    report = AnalysisReport(
        request_id=req.request_id, change_type=req.change_type,
        repo_url=req.repo_url, source_ref=req.source_ref, target_ref=req.target_ref,
        pr=req.pr, completed_at=datetime.now(timezone.utc),
        security=SecurityResult(findings=findings, secrets_detected=bool(findings)) if findings else None,
    )
    return report


@pytest.fixture
def review_store(tmp_path, monkeypatch):
    store = SQLiteReviewSessionStore(str(tmp_path / "test.db"))
    monkeypatch.setattr("governance.review_session_store.get_review_store", lambda: store)
    return store


def test_true_incremental_merge_end_to_end(review_store):
    review_store.record_pr_head("github", "org/repo", "42", "sha-1", "prior-req")
    # A verdict recorded against the PRIOR analysis — must carry forward onto
    # the new merged report (item 4), computed via the same finding_key
    # formula the client uses.
    prior_key = "security|old.py|2|Pre-existing medium finding"
    review_store.set_triage("prior-req", prior_key, "developer", "wont_fix", "accepted risk", "alice")

    git = FakeGitClient()
    orch = ImpactAnalysisOrchestrator(api_key=None, phase=1)
    report_store = FakeReportStore()
    req = make_req(head_sha="sha-2")

    from api.routes.webhooks import _run_with_diff
    with patch("api.routes.webhooks.make_git_client", return_value=git), \
         patch.object(orch, "analyse_async", side_effect=fake_analyse_async), \
         patch("output.pr_commenter.make_pr_commenter", return_value=None), \
         patch("output.notification.make_notification_service") as mock_notif, \
         patch("ingestion.path_review_config.load_from_git_client", return_value=None), \
         patch("ingestion.path_review_config.load_team_default", return_value=None):
        mock_notif.return_value.notify = lambda *a, **k: None
        asyncio.run(_run_with_diff(req, "github", orch, report_store))

    assert len(report_store.saved) == 1, "exactly one merged report should be saved, not a separate partial + full"
    merged = report_store.saved[0]

    # Both the pre-existing finding AND the new one are present.
    descriptions = {f.description for f in merged.security.findings}
    assert "Pre-existing medium finding" in descriptions
    assert "Hardcoded secret in code." in descriptions

    # Real _finalize ran — top_issues is populated by the real correlate_findings.
    assert len(merged.top_issues) >= 1

    # Real evaluate_policy ran — a hardcoded secret is a hard BLOCK rule.
    assert merged.gate_decision.value == "BLOCK"

    # Triage carried forward from the prior request onto the new one.
    new_triage = {r["finding_key"]: r for r in review_store.list_triage(merged.request_id)}
    assert prior_key in new_triage
    assert new_triage[prior_key]["dev_verdict"] == "wont_fix"

    # Head tracking advanced to the new push.
    head = review_store.get_last_analyzed_head("github", "org/repo", "42")
    assert head["head_sha"] == "sha-2"
    assert head["request_id"] == merged.request_id
