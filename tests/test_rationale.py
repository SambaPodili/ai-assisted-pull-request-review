"""
tests/test_rationale.py
------------------------
The displayed gate rationale must be built from the FINAL report values so it
never contradicts the headline metric tiles (the LLM risk rationale, written
mid-pipeline, claimed "blast radius is 0" while the tile showed 14, and cited the
now-removed coverage delta).
"""
from core.models import (
    AnalysisReport, ChangeType, RiskLevel,
    SecurityResult, SecurityFinding, DependencyResult, InterfaceResult,
    TestCoverageResult, TestGap, CodeAnalysisResult,
)
from governance.rationale import build_rationale


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def test_rationale_reports_final_blast_radius_not_zero():
    rep = _rep(dependency=DependencyResult(blast_radius_score=14, affected_services=[]))
    r = build_rationale(rep)
    assert "blast radius 14/100" in r
    assert "blast radius 0" not in r
    assert "no declared downstream services" in r


def test_rationale_has_no_coverage_delta_wording():
    rep = _rep(test_coverage=TestCoverageResult(
        uncovered_paths=[TestGap(file_path="x.py", uncovered_path="m", suggested_test="t")]))
    r = build_rationale(rep).lower()
    assert "coverage delta" not in r and "%" not in r
    assert "1 changed file(s) without tests" in build_rationale(rep)


def test_rationale_reflects_real_signals():
    rep = _rep(
        security=SecurityResult(findings=[SecurityFinding(
            file_path="a.py", line_range="1", severity=RiskLevel.HIGH, cwe_id="CWE-89",
            description="SQLi", remediation="x")], secrets_detected=True, overall_severity=RiskLevel.HIGH),
        dependency=DependencyResult(blast_radius_score=60, affected_services=["svc-a", "svc-b"], cve_hits=["CVE-1"]),
        interface=InterfaceResult(breaking_changes=[]),
    )
    r = build_rationale(rep)
    assert "1 security finding(s)" in r
    assert "hardcoded secrets present" in r
    assert "blast radius 60/100 across 2 downstream service(s)" in r
    assert "1 known CVE(s)" in r


def test_rationale_clean_change():
    r = build_rationale(_rep(dependency=DependencyResult(blast_radius_score=0)))
    assert "no security findings" in r and "no secrets" in r and "no breaking changes" in r
    assert "blast radius 0/100" in r


def test_unverified_security_finding_not_counted():
    rep = _rep(security=SecurityResult(findings=[SecurityFinding(
        file_path="a.py", line_range="1", severity=RiskLevel.HIGH, cwe_id="CWE-89",
        description="maybe", remediation="x", unverified=True)], overall_severity=RiskLevel.HIGH))
    assert "no security findings" in build_rationale(rep)
