"""
tests/test_coverage_delta_and_speculation.py
---------------------------------------------
Two real-screenshot regressions:

  A. Coverage delta was a fabricated constant (always -5%) because the LLM guesses
     it with no baseline. It must now be DETERMINISTIC — derived from real test
     gaps in the diff, varying with the change, and 0 for doc/config-only changes.

  B. Speculative "Potential SQL injection …" security findings (no concrete
     evidence) leaked into the Compliance OWASP-A03 roll-up. They must be marked
     unverified (excluded from compliance + gate), while a CRITICAL stays.
"""
from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType, DiffHunk, RiskLevel,
    SecurityResult, SecurityFinding,
)
from agents.test_coverage_agent import _untested_changed_files
from governance.finding_quality import correct_findings
from governance.compliance import assess


def _req(files):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b",
        hunks=[DiffHunk(file_path=f, language="java", additions=1, deletions=0, content="+x")
               for f in files],
    )


# ── A. Coverage delta ─────────────────────────────────────────────────────────

def test_coverage_gap_varies_with_changed_source_files():
    assert _untested_changed_files(_req(["A.java", "B.java"])) == 2
    assert _untested_changed_files(_req(["A.java", "src/test/java/ATest.java"])) == 1


def test_coverage_gap_zero_for_non_code_changes():
    # docs / sql / config must not count as a coverage gap → no fabricated drop
    assert _untested_changed_files(_req(["README.md", "db/migrate.sql", "config/app.yaml"])) == 0


def test_coverage_gap_zero_when_nothing_changed():
    assert _untested_changed_files(_req([])) == 0


def _req_meta(files, existing):
    r = _req(files)
    r.metadata = {"existing_tests": existing}
    return r


def test_existing_repo_test_suppresses_gap():
    # A.java has no test IN the PR, but its test already exists in the repo
    # (pre-fetched into metadata.existing_tests) → must NOT count as a gap.
    files = ["src/main/java/A.java", "src/main/java/B.java"]
    assert _untested_changed_files(_req(files)) == 2                    # no repo tests known
    existing = [{"path": "src/test/java/ATest.java", "text": "class ATest {}"}]
    assert _untested_changed_files(_req_meta(files, existing)) == 1     # A covered by repo, B still a gap


# ── B. Speculation → unverified → out of compliance ───────────────────────────

def _rep(findings):
    return AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        security=SecurityResult(findings=findings, overall_severity=RiskLevel.MEDIUM),
    )


def test_leading_potential_finding_excluded_from_compliance():
    rep = _rep([
        SecurityFinding(file_path="Template.xml", line_range="21", severity=RiskLevel.MEDIUM,
                        cwe_id="CWE-89", owasp_cat="A03",
                        description="Potential SQL injection risk due to dynamic table name in INSERT statement",
                        remediation="x"),
        SecurityFinding(file_path="Svc.java", line_range="38", severity=RiskLevel.MEDIUM,
                        cwe_id="CWE-89", owasp_cat="A03",
                        description="Potential SQL injection risk if dao.update is not using parameterized queries",
                        remediation="x"),
    ])
    _anchored, flagged = correct_findings(rep, {"template.xml": {21}, "svc.java": {38}})
    assert flagged == 2 and all(f.unverified for f in rep.security.findings)
    a03 = next(it for s in assess(rep)["standards"] if "OWASP" in s["name"]
               for it in s["items"] if it["id"] == "A03")
    assert a03["status"] == "pass"   # speculative FPs no longer fail A03


def test_critical_speculative_finding_is_kept():
    # a CRITICAL must never be auto-suppressed on a hedge — it still holds the gate
    rep = _rep([SecurityFinding(
        file_path="Svc.java", line_range="38", severity=RiskLevel.CRITICAL, cwe_id="CWE-89",
        owasp_cat="A03", description="Potential SQL injection allowing full DB read", remediation="x")])
    _a, flagged = correct_findings(rep, {"svc.java": {38}})
    assert flagged == 0 and rep.security.findings[0].unverified is False
    a03 = next(it for s in assess(rep)["standards"] if "OWASP" in s["name"]
               for it in s["items"] if it["id"] == "A03")
    assert a03["status"] == "fail"


def test_confirmed_concrete_finding_still_counts():
    rep = _rep([SecurityFinding(
        file_path="Svc.java", line_range="38", severity=RiskLevel.HIGH, cwe_id="CWE-89",
        owasp_cat="A03", description="SQL built by concatenating request input into execute()",
        remediation="x")])
    _a, flagged = correct_findings(rep, {"svc.java": {38}})
    assert flagged == 0 and rep.security.findings[0].unverified is False
