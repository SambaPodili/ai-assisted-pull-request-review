"""
tests/test_correlation_falsepositives.py
-----------------------------------------
Regression tests built directly from a real "Top Issues" screenshot the user
flagged as full of false positives. Three concrete defects are pinned here:

  1. Findings on the same line that are about DIFFERENT things were faking a
     "2 agents agree" corroboration (e.g. "code moved" + "no distributed
     tracing"). They must NOT merge / must NOT claim agreement.
  2. A genuine duplicate reported a few lines apart by different agents
     ("table name changed" at xml:388 and xml:394) was NOT merged. It must
     collapse into ONE issue.
  3. Speculative "Potential …" findings were ranked as top issues. They must be
     marked low-confidence and rank below confirmed findings.
"""
from core.models import (
    AnalysisReport, ChangeType, RiskLevel,
    SecurityResult, SecurityFinding,
    CodeAnalysisResult, CodeFinding,
    ObservabilityResult, ObservabilityFinding,
    PerformanceImpactResult, PerformanceFinding,
    DataPrivacyResult, PIIFinding,
    TaintAnalysisResult, TaintPath, TaintSource, TaintSink,
)
from governance.correlation import correlate_findings


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def test_unrelated_findings_on_same_line_do_not_fake_agreement():
    # Screenshot #1: code-analysis "code moved" + observability "no tracing",
    # both ~line 583 of the same file. They share no subject → no corroboration.
    rep = _rep(
        code_analysis=CodeAnalysisResult(summary="x", change_type="modify", findings=[CodeFinding(
            file_path="src/KycStatusSaveServiceImpl.java", line_range="583",
            severity=RiskLevel.MEDIUM, category="design",
            description="The call to saveRpCreationDetails is moved and the related party "
                        "details update logic is modified", suggestion="")]),
        observability=ObservabilityResult(findings=[ObservabilityFinding(
            kind="missing_tracing", severity=RiskLevel.MEDIUM,
            description="No evidence of distributed tracing in the service implementation",
            file_path="src/KycStatusSaveServiceImpl.java", line=584, suggestion="")]),
    )
    issues = correlate_findings(rep)
    # Two SEPARATE issues, neither claiming multi-agent agreement.
    assert len(issues) == 2
    for i in issues:
        assert len(i.agents) == 1, f"unrelated finding falsely corroborated: {i.agents}"


def test_true_duplicate_across_line_buckets_merges():
    # Screenshot #2 vs #7: the SAME "table name changed" finding at xml:388
    # (code) and xml:394 (perf) — different 3-line buckets, must still merge.
    rep = _rep(
        code_analysis=CodeAnalysisResult(summary="x", change_type="modify", findings=[CodeFinding(
            file_path="src/db/RelatedPartyDetailsTemplate.xml", line_range="394",
            severity=RiskLevel.MEDIUM, category="data",
            description="The table name in the INSERT statement has been changed from "
                        "tvrm_rif_details to tvrm_batch_ewf_reports", suggestion="")]),
        performance_impact=PerformanceImpactResult(findings=[PerformanceFinding(
            category="query", severity=RiskLevel.MEDIUM,
            description="The table name in the SQL query has been changed from "
                        "tvrm_rif_details to tvrm_batch_ewf_reports",
            file_path="src/db/RelatedPartyDetailsTemplate.xml", line=388, suggestion="")]),
    )
    issues = correlate_findings(rep)
    assert len(issues) == 1, "identical finding a few lines apart must merge into one"
    assert set(issues[0].agents) == {"code_analysis", "performance_impact"}


def test_speculative_potential_findings_are_low_confidence_and_outranked():
    # Screenshot #5/#9/#10 ("Potential …") vs a confirmed deterministic finding.
    rep = _rep(
        # confirmed finding from a deterministic agent (taint analysis is exact)
        taint_analysis=TaintAnalysisResult(taint_paths=[TaintPath(
            source=TaintSource(file_path="src/Dao.java", line=40, variable="q", source="request_param"),
            sink=TaintSink(file_path="src/Dao.java", line=42, variable="sql", sink="sql_query"),
            cwe="CWE-89", severity=RiskLevel.MEDIUM,
            description="SQL built via string concatenation reaches the query sink")]),
        # speculative LLM security finding — opens with "Potential", no concrete evidence
        security=SecurityResult(
            findings=[SecurityFinding(
                file_path="src/db/Template.xml", line_range="21",
                severity=RiskLevel.MEDIUM, cwe_id="CWE-89", owasp_cat="A03",
                description="Potential SQL injection risk due to dynamic table "
                            "name in INSERT statement",
                remediation="review")],
            overall_severity=RiskLevel.MEDIUM),       # LLM path (fallback_used=False)
        data_privacy=DataPrivacyResult(pii_findings=[PIIFinding(
            pii_type="error_message", risk_level=RiskLevel.MEDIUM,
            description="Potential PII exposure through ERROR_MESSAGE column in tvrm_batch_ewf_reports table",
            file_path="db/pdn_vrm_ddl.sql", line=9)]),
    )
    issues = correlate_findings(rep)
    confirmed = next(i for i in issues if "concatenation" in i.title.lower())
    spec_sql  = next(i for i in issues if "potential sql" in i.title.lower())
    spec_pii  = next(i for i in issues if "potential pii" in i.title.lower())

    assert spec_sql.confidence == "low" and spec_pii.confidence == "low"
    assert confirmed.score > spec_sql.score
    assert confirmed.score > spec_pii.score
