"""
tests/test_report_merge.py
------------------------------
Unit tests for governance/report_merge.py::merge_reports — the core of "true
incremental re-review": merging a PR's prior full analysis with a partial
analysis of just new commits. The per-agent-result-type list attribute is
NOT uniform (confirmed against governance/correlation.py::_collect) — covers
at least 3 non-".findings" types plus one uniform type.
"""
from __future__ import annotations
from datetime import datetime, timezone

from core.models import (
    AnalysisReport, ChangeType, SecurityResult, SecurityFinding,
    TaintAnalysisResult, TaintPath, TaintSource, TaintSink,
    SchemaChangeResult, SchemaChange,
    InterfaceResult, ContractBreak,
    RemediationResult, CodeFix, RiskResult, RiskLevel, GateDecision,
    DependencyResult, CveFinding, LicenseComplianceResult, LicenseFinding,
    FunctionalValidationResult, TestCoverageResult,
)
from governance.report_merge import merge_reports


def make_report(request_id: str, **kw) -> AnalysisReport:
    defaults = dict(
        request_id=request_id, change_type=ChangeType.PR,
        repo_url="https://github.com/org/repo", source_ref="feature", target_ref="main",
        completed_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    return AnalysisReport(**defaults)


def test_merge_uniform_findings_attribute():
    old = make_report("old", security=SecurityResult(findings=[
        SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                         description="old finding", remediation="fix it"),
    ]))
    new = make_report("new", security=SecurityResult(findings=[
        SecurityFinding(file_path="b.py", line_range="2", severity=RiskLevel.HIGH,
                         description="new finding", remediation="fix it"),
    ]))
    merged = merge_reports(old, new)
    assert len(merged.security.findings) == 2
    assert {f.description for f in merged.security.findings} == {"old finding", "new finding"}


def test_merge_taint_analysis_uses_taint_paths_not_findings():
    source = TaintSource(file_path="a.py", line=1, variable="input", source="request_param")
    sink = TaintSink(file_path="a.py", line=5, variable="query", sink="sql_query")
    old = make_report("old", taint_analysis=TaintAnalysisResult(taint_paths=[
        TaintPath(source=source, sink=sink, severity=RiskLevel.HIGH, cwe="CWE-89", description="old taint"),
    ]))
    new = make_report("new", taint_analysis=TaintAnalysisResult(taint_paths=[
        TaintPath(source=source, sink=sink, severity=RiskLevel.HIGH, cwe="CWE-89", description="new taint"),
    ]))
    merged = merge_reports(old, new)
    assert len(merged.taint_analysis.taint_paths) == 2


def test_merge_schema_change_uses_changes_not_findings():
    old = make_report("old", schema_change=SchemaChangeResult(changes=[
        SchemaChange(file_path="m1.sql", change_type="drop_column", severity=RiskLevel.HIGH,
                     description="old schema change", reversible=False),
    ]))
    new = make_report("new", schema_change=SchemaChangeResult(changes=[
        SchemaChange(file_path="m2.sql", change_type="drop_table", severity=RiskLevel.CRITICAL,
                     description="new schema change", reversible=False),
    ]))
    merged = merge_reports(old, new)
    assert len(merged.schema_change.changes) == 2


def test_merge_interface_uses_breaking_changes_not_findings():
    old = make_report("old", interface=InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="/api/v1/x", break_type="removed", severity=RiskLevel.HIGH),
    ]))
    new = make_report("new", interface=InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="/api/v1/y", break_type="removed", severity=RiskLevel.HIGH),
    ]))
    merged = merge_reports(old, new)
    assert len(merged.interface.breaking_changes) == 2


def test_merge_when_new_partial_has_no_result_for_a_type():
    """An agent that found nothing new in the incremental slice — old findings
    for that type must be preserved unchanged, not dropped."""
    old = make_report("old", security=SecurityResult(findings=[
        SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                         description="old finding", remediation="fix it"),
    ]))
    new = make_report("new")  # security is None on the partial report
    merged = merge_reports(old, new)
    assert len(merged.security.findings) == 1
    assert merged.security.findings[0].description == "old finding"


def test_merge_remediation_code_fixes_are_concatenated():
    old = make_report("old", remediation=RemediationResult(
        code_fixes=[CodeFix(file_path="a.py", before="x", after="y", confidence="high")],
        pr_walkthrough="old walkthrough",
    ))
    new = make_report("new", remediation=RemediationResult(
        code_fixes=[CodeFix(file_path="b.py", before="p", after="q", confidence="high")],
        pr_walkthrough="new walkthrough",
    ))
    merged = merge_reports(old, new)
    assert len(merged.remediation.code_fixes) == 2
    # Narrative fields reflect the latest state, not the old one.
    assert merged.remediation.pr_walkthrough == "new walkthrough"


def test_merge_risk_reflects_latest_push():
    old = make_report("old", risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10,
                                              gate_decision=GateDecision.APPROVE))
    new = make_report("new", risk=RiskResult(overall_risk=RiskLevel.HIGH, risk_score=80,
                                              gate_decision=GateDecision.BLOCK))
    merged = merge_reports(old, new)
    assert merged.risk.risk_score == 80


def test_merge_never_mutates_inputs():
    old = make_report("old", security=SecurityResult(findings=[
        SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                         description="old finding", remediation="fix it"),
    ]))
    new = make_report("new", security=SecurityResult(findings=[
        SecurityFinding(file_path="b.py", line_range="2", severity=RiskLevel.HIGH,
                         description="new finding", remediation="fix it"),
    ]))
    merge_reports(old, new)
    assert len(old.security.findings) == 1
    assert len(new.security.findings) == 1


def test_merge_ors_secrets_detected_flag():
    """gate_policy.py's hardcoded-secret BLOCK rule reads security.
    secrets_detected directly, not derived from the findings list — if the
    OLD report had no secret but the NEW partial does, the merged flag must
    be True (OR), not silently keep the old (False) value."""
    old = make_report("old", security=SecurityResult(findings=[], secrets_detected=False))
    new = make_report("new", security=SecurityResult(findings=[], secrets_detected=True))
    merged = merge_reports(old, new)
    assert merged.security.secrets_detected is True


def test_merge_ors_taint_boolean_signals():
    old = make_report("old", taint_analysis=TaintAnalysisResult(has_injection=False, has_ssrf=True))
    new = make_report("new", taint_analysis=TaintAnalysisResult(has_injection=True, has_ssrf=False))
    merged = merge_reports(old, new)
    assert merged.taint_analysis.has_injection is True   # from new
    assert merged.taint_analysis.has_ssrf is True         # from old — OR, not overwrite


def test_merge_ors_schema_destructive_irreversible():
    old = make_report("old", schema_change=SchemaChangeResult(has_destructive=True, has_irreversible=False))
    new = make_report("new", schema_change=SchemaChangeResult(has_destructive=False, has_irreversible=True))
    merged = merge_reports(old, new)
    assert merged.schema_change.has_destructive is True
    assert merged.schema_change.has_irreversible is True


def test_merge_dependency_cve_hits_and_blast_radius_takes_worse():
    old = make_report("old", dependency=DependencyResult(cve_hits=["CVE-2021-1"], blast_radius_score=30))
    new = make_report("new", dependency=DependencyResult(cve_hits=["CVE-2023-2"], blast_radius_score=80))
    merged = merge_reports(old, new)
    assert set(merged.dependency.cve_hits) == {"CVE-2021-1", "CVE-2023-2"}
    assert merged.dependency.blast_radius_score == 80  # worse of the two, never lowered


def test_merge_dependency_cve_findings_dedup_by_package_and_id():
    old = make_report("old", dependency=DependencyResult(cve_findings=[
        CveFinding(package="lodash", cve_id="CVE-2021-1", severity="HIGH")]))
    new = make_report("new", dependency=DependencyResult(cve_findings=[
        CveFinding(package="lodash", cve_id="CVE-2021-1", severity="HIGH"),   # duplicate
        CveFinding(package="requests", cve_id="CVE-2023-2", severity="CRITICAL", fixed_version="2.31.0")]))
    merged = merge_reports(old, new)
    ids = sorted((c.package, c.cve_id) for c in merged.dependency.cve_findings)
    assert ids == [("lodash", "CVE-2021-1"), ("requests", "CVE-2023-2")]


def test_merge_license_compliance_findings_and_copyleft_flag():
    old = make_report("old", license_compliance=LicenseComplianceResult(
        findings=[LicenseFinding(package="a", detected_license="MIT")], has_copyleft=False))
    new = make_report("new", license_compliance=LicenseComplianceResult(
        findings=[LicenseFinding(package="b", detected_license="GPL-3.0")], has_copyleft=True))
    merged = merge_reports(old, new)
    assert len(merged.license_compliance.findings) == 2
    assert merged.license_compliance.has_copyleft is True


def test_merge_functional_validation_contradiction_flag():
    old = make_report("old", functional_validation=FunctionalValidationResult(has_contradiction=False))
    new = make_report("new", functional_validation=FunctionalValidationResult(has_contradiction=True))
    merged = merge_reports(old, new)
    assert merged.functional_validation.has_contradiction is True


def test_merge_test_coverage_delta_takes_worse_not_average():
    """coverage_delta is negative-is-bad — the merged value must be the more
    negative (worse) of the two, not silently overwritten or averaged."""
    old = make_report("old", test_coverage=TestCoverageResult(coverage_delta=-5.0))
    new = make_report("new", test_coverage=TestCoverageResult(coverage_delta=-20.0))
    merged = merge_reports(old, new)
    assert merged.test_coverage.coverage_delta == -20.0


def test_merge_identity_and_target_ref():
    old = make_report("old", target_ref="main")
    new = make_report("new", source_ref="feature-v2", target_ref="main")
    merged = merge_reports(old, new)
    assert merged.request_id == "new"
    assert merged.target_ref == "main"
    assert merged.source_ref == "feature-v2"
