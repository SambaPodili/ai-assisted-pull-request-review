"""
tests/test_triage_carry_forward.py
--------------------------------------
Regression tests for governance/review_session_store.py::carry_forward_triage
— review_triage was keyed only by request_id with no PR-level lookup, so a
reviewer's verdict on a finding was silently orphaned on any re-analysis
(new push -> new request_id). finding_key is a Python port of the client's
own triageKey formula (frontend/src/views/ResultsView.jsx:653) and must
match it exactly, including JS's `line || ''` falsy-zero semantics.
"""
from __future__ import annotations
import tempfile
import os

from governance.review_session_store import SQLiteReviewSessionStore
from core.models import CorrelatedIssue


def make_store(tmp_path):
    return SQLiteReviewSessionStore(str(tmp_path / "test.db"))


def make_issue(title="Hardcoded secret", file_path="x.py", line=42, agents=None):
    return CorrelatedIssue(title=title, file_path=file_path, line=line,
                            agents=["security"] if agents is None else agents)


def test_carries_matching_verdict_forward(tmp_path):
    store = make_store(tmp_path)
    issue = make_issue()
    # Compute the SAME key the store will compute for this issue, to record
    # a triage verdict against the OLD request_id.
    key = "security|x.py|42|Hardcoded secret"
    store.set_triage("req-A", key, "developer", "false_positive", "not exploitable", "alice")

    carried = store.carry_forward_triage("req-A", "req-B", [issue])

    assert carried == 1
    rows = {r["finding_key"]: r for r in store.list_triage("req-B")}
    assert key in rows
    assert rows[key]["dev_verdict"] == "false_positive"
    assert rows[key]["dev_by"] == "alice"


def test_no_matching_prior_verdict_carries_nothing(tmp_path):
    store = make_store(tmp_path)
    issue = make_issue(title="A totally different finding")
    carried = store.carry_forward_triage("req-A", "req-B", [issue])
    assert carried == 0
    assert store.list_triage("req-B") == []


def test_finding_with_no_verdict_recorded_is_not_carried(tmp_path):
    """A finding_key row can exist (e.g. session metadata only) without an
    actual dev/reviewer verdict — nothing meaningful to carry in that case."""
    store = make_store(tmp_path)
    key = "security|x.py|42|Hardcoded secret"
    # ensure_session creates the row shell but set_triage with an empty
    # verdict never happens in practice — simulate via direct row insert
    # with no verdict columns populated.
    store._conn.execute(
        "INSERT INTO review_triage (request_id, finding_key, title, agent, file, line) VALUES (?,?,?,?,?,?)",
        ("req-A", key, "Hardcoded secret", "security", "x.py", "42"),
    )
    store._conn.commit()

    issue = make_issue()
    carried = store.carry_forward_triage("req-A", "req-B", [issue])
    assert carried == 0


def test_line_zero_matches_js_falsy_semantics(tmp_path):
    """JS's `line || ''` treats line=0 as falsy -> empty string in the key,
    not '0'. The Python port must match this exactly or line=0 findings
    (legitimate for LLM findings with no resolvable line_range) would never
    carry forward correctly against the web app's own triage keys."""
    store = make_store(tmp_path)
    issue = make_issue(line=0)
    key = "security|x.py||Hardcoded secret"  # empty string for line, not '0'
    store.set_triage("req-A", key, "developer", "wont_fix", "", "bob")

    carried = store.carry_forward_triage("req-A", "req-B", [issue])
    assert carried == 1
    rows = {r["finding_key"]: r for r in store.list_triage("req-B")}
    assert key in rows


def test_no_agent_falls_back_to_issue(tmp_path):
    """JS: `it.agents && it.agents[0]` for a missing/empty agents list falls
    through to triageKey's `agent || 'issue'` default."""
    store = make_store(tmp_path)
    issue = make_issue(agents=[])
    key = "issue|x.py|42|Hardcoded secret"
    store.set_triage("req-A", key, "developer", "valid", "", "carol")

    carried = store.carry_forward_triage("req-A", "req-B", [issue])
    assert carried == 1


def test_no_prior_analysis_returns_zero(tmp_path):
    store = make_store(tmp_path)
    issue = make_issue()
    assert store.carry_forward_triage("nonexistent-req", "req-B", [issue]) == 0
