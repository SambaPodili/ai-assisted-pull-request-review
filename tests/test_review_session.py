"""
tests/test_review_session.py
-----------------------------
Developer → reviewer → PR review-session continuity store.

The handoff: developer triages findings (valid/false_positive), submits; reviewer
validates (confirmed/rejected); only DEV-valid ∩ REVIEWER-confirmed are the real
issues to post back to the PR. All state persists across the handoff.
"""
import pytest

from governance.review_session_store import SQLiteReviewSessionStore


@pytest.fixture
def store(tmp_path):
    return SQLiteReviewSessionStore(str(tmp_path / "rs.db"))


def _meta(**kw):
    base = dict(title="t", agent="security", file="a.py", line="10", severity="high")
    base.update(kw)
    return base


def test_session_starts_in_developer_stage(store):
    s = store.ensure_session("req1", repo="r", pr_id="42")
    assert s["stage"] == "developer" and s["repo"] == "r" and s["pr_id"] == "42"
    assert s["triage"] == []


def test_triage_persists_dev_then_reviewer_on_same_finding(store):
    store.set_triage("req1", "f1", "developer", "valid", "looks real", "dev@x", _meta())
    store.set_triage("req1", "f1", "reviewer", "confirmed", "agree", "rev@x", _meta())
    rows = store.list_triage("req1")
    assert len(rows) == 1
    r = rows[0]
    assert r["dev_verdict"] == "valid" and r["dev_by"] == "dev@x"
    assert r["reviewer_verdict"] == "confirmed" and r["reviewer_by"] == "rev@x"
    assert r["title"] == "t" and r["agent"] == "security"   # metadata kept


def test_submit_and_finalize_stage_transitions(store):
    store.set_triage("req1", "f1", "developer", "valid", "", "dev", _meta())
    assert store.submit("req1", "dev")["stage"] == "reviewer"
    s = store.finalize("req1", "HOLD", "rev", posted=True)
    assert s["stage"] == "done" and s["final_gate"] == "HOLD" and s["posted_to_pr"] is True


def test_validated_is_dev_valid_AND_reviewer_confirmed(store):
    # f1: dev valid + reviewer confirmed  → posted
    store.set_triage("req1", "f1", "developer", "valid", "", "dev", _meta(title="real bug"))
    store.set_triage("req1", "f1", "reviewer", "confirmed", "", "rev", _meta())
    # f2: dev valid but reviewer rejected → NOT posted
    store.set_triage("req1", "f2", "developer", "valid", "", "dev", _meta(title="dev thinks real"))
    store.set_triage("req1", "f2", "reviewer", "rejected", "actually fine", "rev", _meta())
    # f3: dev false_positive                → NOT posted
    store.set_triage("req1", "f3", "developer", "false_positive", "noise", "dev", _meta(title="fp"))
    # f4: dev valid, reviewer hasn't acted  → NOT posted (not yet confirmed)
    store.set_triage("req1", "f4", "developer", "valid", "", "dev", _meta(title="pending"))

    validated = store.validated_findings("req1")
    assert [v["finding_key"] for v in validated] == ["f1"]
    assert validated[0]["title"] == "real bug"


def test_invalid_verdicts_rejected(store):
    with pytest.raises(ValueError):
        store.set_triage("req1", "f1", "developer", "confirmed", "", "dev", _meta())  # reviewer verdict on dev
    with pytest.raises(ValueError):
        store.set_triage("req1", "f1", "reviewer", "valid", "", "rev", _meta())       # dev verdict on reviewer
    with pytest.raises(ValueError):
        store.set_triage("req1", "f1", "author", "valid", "", "x", _meta())           # bad role


def test_reopen_returns_to_developer(store):
    store.submit("req1", "dev")
    assert store.reopen("req1", "rev")["stage"] == "developer"


def test_continuity_session_reloads_from_disk(tmp_path):
    p = str(tmp_path / "rs.db")
    s1 = SQLiteReviewSessionStore(p)
    s1.set_triage("req9", "f1", "developer", "valid", "keep this", "dev", _meta())
    s1.submit("req9", "dev")
    # a fresh store object (e.g. different process/session) sees the same state
    s2 = SQLiteReviewSessionStore(p)
    sess = s2.get_session("req9")
    assert sess["stage"] == "reviewer"
    assert sess["triage"][0]["dev_comment"] == "keep this"
