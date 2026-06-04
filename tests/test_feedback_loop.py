"""
tests/test_feedback_loop.py
----------------------------
Reviewer feedback store: gate overrides + per-finding verdicts, and the
aggregations that surface noisy checks / override direction.
"""
from __future__ import annotations
import tempfile, os
import pytest

from governance.feedback_store import SQLiteFeedbackStore


@pytest.fixture
def store():
    path = tempfile.mktemp(suffix=".db")
    s = SQLiteFeedbackStore(path)
    yield s
    try: os.remove(path)
    except OSError: pass


def test_record_and_count_false_positives(store):
    store.record_finding("r1", "org/repo", "security", "CWE-89", "a.py", "false_positive", "", "alice")
    store.record_finding("r2", "org/repo", "security", "CWE-89", "b.py", "false_positive", "", "bob")
    store.record_finding("r3", "org/repo", "security", "CWE-89", "c.py", "valid", "", "alice")
    assert store.fp_count("security", "CWE-89") == 2
    assert store.fp_count("security", "CWE-89", repo="org/repo") == 2
    assert store.fp_count("security", "CWE-999") == 0


def test_noisy_checks_ranks_by_fp(store):
    for i in range(3):
        store.record_finding(f"r{i}", "org/repo", "maintainability", "magic_number", "x.py", "false_positive", "", "a")
    store.record_finding("r9", "org/repo", "security", "CWE-89", "y.py", "valid", "", "a")
    noisy = store.noisy_checks(repo="org/repo")
    assert noisy[0]["agent"] == "maintainability"
    assert noisy[0]["false_positives"] == 3
    assert noisy[0]["fp_rate"] == 1.0


def test_gate_direction_classification(store):
    # Human went looser than policy (policy BLOCK → human APPROVE)
    store.record_gate("r1", "org/repo", ai_gate="HOLD", policy_gate="BLOCK",
                      human_gate="APPROVE", reason="false alarm, reviewed manually", reviewer="lead")
    # Human went stricter (policy APPROVE → human BLOCK)
    store.record_gate("r2", "org/repo", ai_gate="APPROVE", policy_gate="APPROVE",
                      human_gate="BLOCK", reason="domain concern not captured", reviewer="lead")
    stats = store.gate_stats(repo="org/repo")
    assert stats["total_overrides"] == 2
    assert stats["looser"] == 1
    assert stats["stricter"] == 1


def test_invalid_verdict_rejected(store):
    with pytest.raises(ValueError):
        store.record_finding("r", "repo", "security", "x", "f.py", "bogus", "", "a")


def test_survives_reopen(store):
    store.record_finding("r1", "org/repo", "privacy", "email", "u.py", "false_positive", "", "a")
    path = store._path
    s2 = SQLiteFeedbackStore(path)
    assert s2.fp_count("privacy", "email") == 1
