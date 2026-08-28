"""
tests/test_pr_commenter_rendering.py
---------------------------------------
Regression tests for two fixes to output/pr_commenter.py:
  1. A file with fixes on more than one line previously rendered a
     "suggestion" code fence for EVERY fix, but GitHub only treats the fence
     matching the review comment's own anchor line as a real one-click-apply
     widget — other fixes rendered as inert code that looked clickable but
     wasn't. Fixed: only the anchor-line fix gets a real suggestion fence;
     others render as a plain (non-"suggestion") diff block.
  2. The GitHub Review API correlation-write assumed the response's
     `comments[]` array always has one entry per submitted comment, in
     order. Fixed: a length mismatch is now detected and logged, skipping
     only the (already-posted) comments' correlation recording rather than
     risking a silent mispairing via zip().
"""
from __future__ import annotations

from core.models import CodeFix
from output.pr_commenter import _render_file_comment


def make_fix(file_path: str, before: str, after: str) -> CodeFix:
    return CodeFix(file_path=file_path, before=before, after=after,
                   explanation="fix it", confidence="high")


def test_anchor_line_gets_real_suggestion_fence():
    findings = [
        {"file_path": "x.py", "line": 5, "severity": "high", "category": "security",
         "message": "hardcoded secret", "fix": make_fix("x.py", "old5", "new5")},
    ]
    body = _render_file_comment("x.py", findings, anchor_line=5)
    assert "```suggestion\nnew5\n```" in body
    assert "```diff" not in body


def test_non_anchor_line_gets_plain_diff_block_not_suggestion():
    findings = [
        {"file_path": "x.py", "line": 5, "severity": "high", "category": "security",
         "message": "hardcoded secret", "fix": make_fix("x.py", "old5", "new5")},
        {"file_path": "x.py", "line": 12, "severity": "medium", "category": "quality",
         "message": "weak hash", "fix": make_fix("x.py", "old12", "new12")},
    ]
    # Comment is anchored to line 5 (as GitHub's review-comment API requires) —
    # the fix at line 12 must NOT render as a ```suggestion fence, since it
    # would look clickable on GitHub but silently fail to apply to the wrong line.
    body = _render_file_comment("x.py", findings, anchor_line=5)
    assert "```suggestion\nnew5\n```" in body
    assert "```suggestion\nnew12\n```" not in body
    assert "```diff\n- old12\n+ new12\n```" in body


def test_no_fixes_renders_findings_table_only():
    findings = [
        {"file_path": "x.py", "line": 3, "severity": "low", "category": "quality",
         "message": "minor issue", "fix": None},
    ]
    body = _render_file_comment("x.py", findings, anchor_line=3)
    assert "```suggestion" not in body
    assert "```diff" not in body
    assert "minor issue" in body


def test_github_review_comment_count_mismatch_skips_correlation(monkeypatch):
    """Exercises the REAL post_inline_comments GitHub code path (via a mocked
    HTTP session) — a review-API response whose comments[] array is shorter
    than what was submitted must not zip() findings against the wrong ids;
    the guard should skip correlation recording entirely rather than risk a
    silent mispairing, while still reporting the comments as posted."""
    import output.pr_commenter as pc
    from core.models import AnalysisReport, ChangeType, GateDecision, RiskLevel, SecurityResult, SecurityFinding
    from datetime import datetime, timezone

    report = AnalysisReport(
        request_id="req-1", change_type=ChangeType.PR, repo_url="https://github.com/org/repo",
        source_ref="feature", target_ref="main", phase_run=1,
        gate_decision=GateDecision.HOLD, final_risk=RiskLevel.MEDIUM,
        completed_at=datetime.now(timezone.utc),
        security=SecurityResult(findings=[
            SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                             description="hardcoded secret", remediation="rotate it", cwe_id="CWE-798"),
            SecurityFinding(file_path="b.py", line_range="2", severity=RiskLevel.HIGH,
                             description="hardcoded secret", remediation="rotate it", cwe_id="CWE-798"),
        ]),
    )

    recorded = []

    class FakeReviewStore:
        def record_posted_comment(self, **kw):
            recorded.append(kw)

    import governance.review_session_store as rss
    monkeypatch.setattr(rss, "get_review_store", lambda: FakeReviewStore())

    class FakeResp:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body
            self.text = str(body)
        def json(self):
            return self._body

    def fake_post(url, headers=None, json=None, **kw):
        if url.endswith("/pulls/1"):
            return FakeResp(200, {"head": {"sha": "abc123"}})
        if url.endswith("/reviews"):
            # Only ONE comment id returned even though TWO files (a.py, b.py)
            # were submitted — the exact mismatch scenario the guard handles.
            return FakeResp(200, {"comments": [{"id": 999}]})
        return FakeResp(200, {})

    import requests
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: fake_post(url, **kw))
    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: fake_post(url, **kw))

    total_posted = pc.post_inline_comments(
        report=report, token="tok", provider="github",
        repo_slug="org/repo", pr_id="1",
    )

    assert total_posted > 0, "comments should still be reported as posted"
    assert recorded == [], "mismatched comment count must not record any (possibly wrong) correlation"


def test_bitbucket_server_reply_is_threaded_when_parent_id_given(monkeypatch):
    """Bitbucket Server reply_to_comment previously always posted a plain
    top-level comment. Now: with an in_reply_to_id, it must send the
    documented `parent.id` field to actually thread the reply."""
    import output.pr_commenter as pc

    captured = {}

    class FakeResp:
        status_code = 200
        content = b'{"id": 42}'
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": 42}

    def fake_post(url, json=None, headers=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    commenter = pc.PRCommenter("tok", "bitbucket_server", "WORKSPACE", "https://bb.example.com/rest/api/1.0")
    monkeypatch.setattr(commenter._session, "post", fake_post)

    result = commenter.reply_to_comment("PROJ/repo", 7, "here's why", in_reply_to_id=99)

    assert result == "42"
    assert captured["json"]["parent"] == {"id": 99}
    assert captured["json"]["text"] == "here's why"
    assert "PROJ/repos/repo/pull-requests/7/comments" in captured["url"]


def test_bitbucket_server_reply_falls_back_to_top_level_without_parent_id(monkeypatch):
    """No in_reply_to_id (e.g. a top-level question, not threaded to a
    specific comment) — must still fall back to the existing plain post,
    unchanged from before this fix."""
    import output.pr_commenter as pc

    captured = {}

    class FakeResp:
        status_code = 200
        content = b""
        def raise_for_status(self):
            pass
        def json(self):
            return {}

    def fake_post(url, json=None, headers=None, **kw):
        captured["json"] = json
        return FakeResp()

    commenter = pc.PRCommenter("tok", "bitbucket_server", "WORKSPACE", "https://bb.example.com/rest/api/1.0")
    monkeypatch.setattr(commenter._session, "post", fake_post)

    result = commenter.reply_to_comment("PROJ/repo", 7, "general reply", in_reply_to_id=None)

    assert result == "unknown"
    assert "parent" not in captured["json"]
