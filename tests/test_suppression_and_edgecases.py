"""
tests/test_suppression_and_edgecases.py
-----------------------------------------
Quality: auto-suppression of reviewer-confirmed false positives.
Reliability: the diff-prompt builder + JSON repair survive edge cases
(empty diff, huge single file, very large PRs, truncated LLM output).
"""
from __future__ import annotations
import tempfile

from agents.base_agent import format_hunks_for_prompt, _repair_truncated_json
from governance.feedback_store import SQLiteFeedbackStore
from governance.suppression import apply_suppressions
from core.models import (
    AnalysisReport, ChangeType, DiffHunk,
    SecurityResult, SecurityFinding, RiskLevel,
)


# ── Suppression (reviewer feedback loop) ──────────────────────────────────────

def _store():
    return SQLiteFeedbackStore(tempfile.mktemp(suffix=".db"))


def _report_with(cwes):
    # medium severity → suppressible noise (high/critical are never auto-suppressed)
    findings = [SecurityFinding(file_path=f"{c}.py", line_range="1", severity=RiskLevel.MEDIUM,
                                cwe_id=c, description="x", remediation="y") for c in cwes]
    return AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                          source_ref="a", target_ref="b",
                          security=SecurityResult(overall_severity=RiskLevel.MEDIUM, findings=findings))


def test_suppresses_repeat_false_positive():
    st = _store()
    for i in range(3):
        st.record_finding(f"r{i}", "R", "security", "CWE-200", "f.py", "false_positive", "", "rev")
    rep = _report_with(["CWE-200", "CWE-89"])
    removed = apply_suppressions(rep, "R", st)
    assert removed == 1
    assert [f.cwe_id for f in rep.security.findings] == ["CWE-89"]
    assert rep.suppressed_count == 1 and rep.suppressed_notes


def test_does_not_suppress_when_marked_valid():
    st = _store()
    for i in range(3):
        st.record_finding(f"r{i}", "R", "security", "CWE-200", "f.py", "false_positive", "", "rev")
    st.record_finding("rv", "R", "security", "CWE-200", "f.py", "valid", "real", "rev")  # one valid
    rep = _report_with(["CWE-200"])
    assert apply_suppressions(rep, "R", st) == 0          # never hide a sometimes-valid check
    assert len(rep.security.findings) == 1


def test_below_threshold_not_suppressed():
    st = _store()
    for i in range(2):                                    # only 2 (< min_fp=3)
        st.record_finding(f"r{i}", "R", "security", "CWE-200", "f.py", "false_positive", "", "rev")
    rep = _report_with(["CWE-200"])
    assert apply_suppressions(rep, "R", st) == 0


def test_never_suppresses_high_or_critical_security():
    """A CWE dismissed as noise before can still be a REAL critical now —
    high/critical findings must NEVER be auto-suppressed by CWE."""
    st = _store()
    for i in range(5):                                    # plenty of FP marks
        st.record_finding(f"r{i}", "R", "security", "CWE-89", "test_x.py", "false_positive", "", "rev")
    # A real CRITICAL SQLi with that CWE arrives
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                         source_ref="a", target_ref="b",
                         security=SecurityResult(overall_severity=RiskLevel.CRITICAL, findings=[
                             SecurityFinding(file_path="app.py", line_range="1", severity=RiskLevel.CRITICAL,
                                             cwe_id="CWE-89", description="real SQLi", remediation="x")]))
    assert apply_suppressions(rep, "R", st) == 0          # kept, never hidden
    assert len(rep.security.findings) == 1


def test_low_severity_noise_still_suppressed():
    st = _store()
    for i in range(3):
        st.record_finding(f"r{i}", "R", "security", "CWE-200", "f.py", "false_positive", "", "rev")
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                         source_ref="a", target_ref="b",
                         security=SecurityResult(overall_severity=RiskLevel.LOW, findings=[
                             SecurityFinding(file_path="f.py", line_range="1", severity=RiskLevel.LOW,
                                             cwe_id="CWE-200", description="noise", remediation="x")]))
    assert apply_suppressions(rep, "R", st) == 1          # low/medium noise still quieted


# ── Diff-prompt edge cases (reliability) ──────────────────────────────────────

def test_empty_diff_is_safe():
    assert format_hunks_for_prompt([]) == "(no diff hunks)"


def test_large_pr_is_bounded_and_annotated():
    hunks = [DiffHunk(file_path=f"f{i}.py", language="python", additions=10, deletions=2,
                      content="x"*4000) for i in range(100)]
    out = format_hunks_for_prompt(hunks, max_total_chars=20000)
    assert len(out) < 40000                                # stayed bounded
    assert "of 100 reviewable files" in out                # tells the LLM scope was trimmed


def test_huge_single_file_is_truncated():
    hunks = [DiffHunk(file_path="big.py", language="python", additions=9000, deletions=0,
                      content="y"*500000)]
    out = format_hunks_for_prompt(hunks, max_chars_per_hunk=3000)
    assert "truncated" in out
    assert len(out) < 20000


def test_security_focus_ranks_sensitive_files_first():
    hunks = [
        DiffHunk(file_path="docs/readme.md", language="markdown", additions=200, deletions=0, content="a"*2000),
        DiffHunk(file_path="src/auth/login.py", language="python", additions=5, deletions=1, content="b"*2000),
    ]
    out = format_hunks_for_prompt(hunks, max_total_chars=2500, focus="security")
    # auth file outranks the larger docs change despite lower volume
    assert out.index("src/auth/login.py") < (out.index("docs/readme.md") if "docs/readme.md" in out else 10**9)


def test_json_repair_closes_truncated_output():
    repaired = _repair_truncated_json('{"findings": [{"severity": "high", "desc": "oops')
    import json
    obj = json.loads(repaired)                             # must be valid JSON now
    assert "findings" in obj


# ── Input guardrails ──────────────────────────────────────────────────────────

def test_low_signal_paths_detected():
    from ingestion.diff_parser import is_low_signal_path as low
    assert low("package-lock.json") and low("yarn.lock") and low("go.sum")
    assert low("assets/logo.png") and low("dist/app.min.js") and low("node_modules/x/a.js")
    assert not low("src/auth/login.py") and not low("README.md")


def test_low_signal_files_excluded_from_prompt():
    hunks = [
        DiffHunk(file_path="yarn.lock", language="text", additions=900, deletions=5, content="x"*5000),
        DiffHunk(file_path="src/app.py", language="python", additions=4, deletions=1, content="def f(): pass"),
    ]
    out = format_hunks_for_prompt(hunks)
    assert "yarn.lock" not in out and "src/app.py" in out
    assert "low-signal" in out


def test_binary_only_pr_does_not_blank_out():
    hunks = [DiffHunk(file_path="logo.png", language="binary", additions=1, deletions=0, content="...")]
    out = format_hunks_for_prompt(hunks)
    assert out and out != "(no diff hunks)"     # falls back rather than emptying


# ── Evidence guard (hallucination filter) ─────────────────────────────────────

def test_evidence_guard_flags_but_keeps_findings_not_in_diff():
    from governance.evidence import filter_unsubstantiated
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                         source_ref="a", target_ref="b",
                         security=SecurityResult(overall_severity=RiskLevel.CRITICAL, findings=[
                             SecurityFinding(file_path="src/real.py", line_range="1", severity=RiskLevel.HIGH,
                                             cwe_id="CWE-89", description="real", remediation="x"),
                             SecurityFinding(file_path="src/ghost.py", line_range="9", severity=RiskLevel.CRITICAL,
                                             cwe_id="CWE-79", description="hallucinated", remediation="y"),
                         ]))
    flagged = filter_unsubstantiated(rep, {"src/real.py"})
    assert flagged == 1
    # Nothing deleted — both findings remain visible
    assert len(rep.security.findings) == 2
    by_file = {f.file_path: f for f in rep.security.findings}
    assert by_file["src/real.py"].unverified is False
    assert by_file["src/ghost.py"].unverified is True
    # Gate-driving severity ignores the unverified critical → HIGH, not CRITICAL
    assert rep.security.overall_severity == RiskLevel.HIGH


def test_unverified_finding_does_not_block_gate():
    from governance.gate_policy import evaluate_policy
    from core.models import RiskResult, GateDecision
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                         source_ref="a", target_ref="b",
                         risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=GateDecision.APPROVE),
                         security=SecurityResult(overall_severity=RiskLevel.CRITICAL, findings=[
                             SecurityFinding(file_path="ghost.py", line_range="1", severity=RiskLevel.CRITICAL,
                                             cwe_id="CWE-79", description="hallucinated", remediation="y",
                                             unverified=True),
                         ]))
    # An unverified critical must NOT force a BLOCK
    assert evaluate_policy(rep).gate == GateDecision.APPROVE


def test_evidence_guard_keeps_pathless_finding_unflagged():
    from governance.evidence import filter_unsubstantiated
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="R",
                         source_ref="a", target_ref="b",
                         security=SecurityResult(overall_severity=RiskLevel.MEDIUM, findings=[
                             SecurityFinding(file_path="", line_range="", severity=RiskLevel.MEDIUM,
                                             cwe_id="CWE-1", description="repo-level note", remediation="x"),
                         ]))
    assert filter_unsubstantiated(rep, {"src/real.py"}) == 0     # no path → not disproven
    assert rep.security.findings[0].unverified is False
