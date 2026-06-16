"""tests/test_finding_quality.py
Line anchoring + speculation flagging for LLM findings (the two biggest
real-world complaints: wrong line numbers and speculative false positives).
"""
from core.models import (AnalysisReport, ChangeType, RiskLevel,
                         SecurityResult, SecurityFinding,
                         CodeAnalysisResult, CodeFinding)
from governance.finding_quality import correct_findings, _HEDGE


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def test_line_anchored_to_nearest_changed_line():
    # LLM said line 21-25, but the real change is at line 393.
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.MEDIUM, findings=[
        SecurityFinding(file_path="db/Template.xml", line_range="21-25", severity=RiskLevel.MEDIUM,
                        cwe_id="CWE-89", owasp_cat="A03", description="SQL injection risk", remediation="x")]))
    anchored, _ = correct_findings(rep, {"db/Template.xml": {393, 394, 395}})
    assert anchored == 1
    assert rep.security.findings[0].line_range == "393"


def test_correct_line_left_untouched():
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.MEDIUM, findings=[
        SecurityFinding(file_path="A.java", line_range="42", severity=RiskLevel.MEDIUM,
                        cwe_id="CWE-89", owasp_cat="A03", description="confirmed injection", remediation="x")]))
    anchored, _ = correct_findings(rep, {"A.java": {40, 41, 42, 43}})
    assert anchored == 0 and rep.security.findings[0].line_range == "42"


def test_speculative_finding_flagged_and_dropped_from_gate():
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="svc/Impl.java", line_range="610", severity=RiskLevel.HIGH,
                        cwe_id="CWE-89", owasp_cat="A03",
                        description="Potential SQL injection risk if updateRelatedParty is not using parameterized queries",
                        remediation="x")]))
    _, flagged = correct_findings(rep, {"svc/Impl.java": {610, 611, 612}})
    assert flagged == 1
    assert rep.security.findings[0].unverified is True
    # gate-driving severity recomputed down (the only finding is now unverified)
    assert rep.security.overall_severity == RiskLevel.LOW


def test_confirmed_finding_not_flagged():
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="dao/X.java", line_range="10", severity=RiskLevel.HIGH,
                        cwe_id="CWE-89", owasp_cat="A03",
                        description="SQL query built by concatenating request input into execute()",
                        remediation="x")]))
    _, flagged = correct_findings(rep, {"dao/X.java": {10}})
    assert flagged == 0 and rep.security.findings[0].unverified is False


def test_hedge_regex_matches_real_phrasings():
    assert _HEDGE.search("Potential SQL injection risk if X is not using parameterized queries")
    assert _HEDGE.search("Ensure the input is validated before use")
    assert _HEDGE.search("This may lead to data exposure")
    assert not _HEDGE.search("Hardcoded AWS access key committed in source")


def test_deterministic_findings_not_flagged():
    # security in fallback (regex SAST) mode = deterministic — never speculation-flagged
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.HIGH, fallback_used=True, findings=[
        SecurityFinding(file_path="X.java", line_range="5", severity=RiskLevel.HIGH, cwe_id="CWE-89",
                        owasp_cat="A03", description="may be vulnerable to injection", remediation="x")]))
    _, flagged = correct_findings(rep, {"X.java": {5}})
    assert flagged == 0
