"""
tests/test_pr_decision.py
-------------------------
Reviewer gate decision → Bitbucket Server PR actions (review participant status +
commit build-status check). Verifies the decision→action mapping and that it never
merges/closes the PR. HTTP is fully mocked.
"""
from __future__ import annotations
import json
from output.pr_commenter import post_bb_server_decision


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)
    def json(self):
        return self._body


def _mock_session(calls, *, ausername="uncs16", head="abc123"):
    class _S:
        def request(self, method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if url.endswith("/inbox/pull-requests/count"):
                return _Resp(200, {"count": 0}, {"X-AUSERNAME": ausername})
            if "/pull-requests/" in url and method == "GET":
                return _Resp(200, {"fromRef": {"latestCommit": head}})
            return _Resp(200, {})
    return _S()


def test_approve_maps_to_approved_and_successful(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _mock_session(calls))
    res = post_bb_server_decision("APPROVE", "tok", "https://bb.local", "PROJ/repo", "42")
    assert res["ok"] is True
    # review = participant PUT with status APPROVED
    put = next(c for c in calls if c[0] == "PUT")
    assert "/participants/uncs16" in put[1] and put[2]["status"] == "APPROVED"
    # status check = build-status POST SUCCESSFUL on the head commit, NOT a merge
    bs = next(c for c in calls if c[0] == "POST" and "build-status" in c[1])
    assert "/commits/abc123" in bs[1] and bs[2]["state"] == "SUCCESSFUL"
    assert not any("/merge" in c[1] or "/decline" in c[1] for c in calls)   # never merges/closes


def test_block_maps_to_needs_work_and_failed(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _mock_session(calls))
    res = post_bb_server_decision("BLOCK", "tok", "https://bb.local", "PROJ/repo", "42")
    put = next(c for c in calls if c[0] == "PUT")
    bs  = next(c for c in calls if c[0] == "POST" and "build-status" in c[1])
    assert put[2]["status"] == "NEEDS_WORK" and bs[2]["state"] == "FAILED"
    assert res["ok"] is True


def test_hold_maps_to_inprogress(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _mock_session(calls))
    post_bb_server_decision("HOLD", "tok", "https://bb.local", "PROJ/repo", "42")
    bs = next(c for c in calls if c[0] == "POST" and "build-status" in c[1])
    put = next(c for c in calls if c[0] == "PUT")
    assert bs[2]["state"] == "INPROGRESS" and put[2]["status"] == "NEEDS_WORK"


def test_unknown_decision_is_safe():
    res = post_bb_server_decision("NONSENSE", "tok", "https://bb.local", "PROJ/repo", "42")
    assert res["ok"] is False and res["review"] is None


def _gh_session(calls, *, sha="deadbeef"):
    class _S:
        def request(self, method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if method == "GET" and "/pulls/" in url:
                return _Resp(200, {"head": {"sha": sha}})
            return _Resp(201, {})
    return _S()


def test_github_approve_review_and_status(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _gh_session(calls))
    res = post_bb = m.post_github_decision("APPROVE", "tok", "https://github.com", "org/repo", "7")
    assert res["ok"] is True
    rev = next(c for c in calls if c[0] == "POST" and "/reviews" in c[1])
    st  = next(c for c in calls if c[0] == "POST" and "/statuses/" in c[1])
    assert rev[2]["event"] == "APPROVE"
    assert "/statuses/deadbeef" in st[1] and st[2]["state"] == "success"
    assert not any("/merge" in c[1] for c in calls)


def test_github_block_requests_changes_and_failure(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _gh_session(calls))
    m.post_github_decision("BLOCK", "tok", "https://github.com", "org/repo", "7", reason="bad")
    rev = next(c for c in calls if c[0] == "POST" and "/reviews" in c[1])
    st  = next(c for c in calls if c[0] == "POST" and "/statuses/" in c[1])
    assert rev[2]["event"] == "REQUEST_CHANGES" and rev[2]["body"]
    assert st[2]["state"] == "failure"


def _bbc_session(calls, *, sha="cafe123"):
    class _S:
        def request(self, method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if method == "GET" and "/pullrequests/" in url and not url.endswith(("approve", "request-changes")):
                return _Resp(200, {"source": {"commit": {"hash": sha}}})
            return _Resp(200, {})
    return _S()


def test_bb_cloud_approve(monkeypatch):
    calls = []
    import output.pr_commenter as m
    monkeypatch.setattr(m.requests, "Session", lambda: _bbc_session(calls))
    res = m.post_bb_cloud_decision("APPROVE", "tok", "repo", "9", workspace="ws")
    assert res["ok"] is True
    assert any(c[0] == "POST" and c[1].endswith("/approve") for c in calls)
    st = next(c for c in calls if "/statuses/build" in c[1])
    assert "/commit/cafe123/" in st[1] and st[2]["state"] == "SUCCESSFUL"
    assert not any("/merge" in c[1] or "/decline" in c[1] for c in calls)


def test_dispatcher_routes_by_provider():
    from output.pr_commenter import post_pr_decision
    r = post_pr_decision("APPROVE", "unsupported_x", "t", "u", "r", "1")
    assert r["ok"] is False and "unsupported" in r["errors"][0]
