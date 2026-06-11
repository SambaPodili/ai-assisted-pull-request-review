"""tests/test_correlation.py
Cross-agent correlation: dedupe overlapping findings, boost corroborated ones,
penalise unverified locations, rank by impact.
"""
from core.models import (AnalysisReport, ChangeType, RiskLevel,
                         SecurityResult, SecurityFinding,
                         ASTAnalysisResult, ASTFinding,
                         TaintAnalysisResult, TaintPath, TaintSource, TaintSink,
                         MaintainabilityResult, MaintainabilityIssue)
from governance.correlation import correlate_findings


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def _sec(file, line, sev=RiskLevel.HIGH, desc="SQL injection via concatenation", unv=False):
    return SecurityFinding(file_path=file, line_range=str(line), severity=sev,
                           cwe_id="CWE-89", owasp_cat="A03", description=desc,
                           remediation="use prepared statements", unverified=unv)


def test_same_location_findings_merge_and_corroborate():
    rep = _rep(
        security=SecurityResult(findings=[_sec("src/Dao.java", 42)], overall_severity=RiskLevel.HIGH),
        taint_analysis=TaintAnalysisResult(taint_paths=[TaintPath(
            source=TaintSource(file_path="src/Dao.java", line=40, variable="q", source="request_param"),
            sink=TaintSink(file_path="src/Dao.java", line=42, variable="sql", sink="sql_query"),
            cwe="CWE-89", severity=RiskLevel.CRITICAL, description="Tainted q reaches sql_query")]),
        ast_analysis=ASTAnalysisResult(findings=[ASTFinding(
            file_path="src/Other.java", line=5, function="f", kind="null_risk",
            severity=RiskLevel.LOW, description="possible null deref")]),
    )
    issues = correlate_findings(rep)
    # security + taint on Dao.java:42 merge into ONE corroborated issue
    top = issues[0]
    assert set(top.agents) == {"security", "taint_analysis"}
    assert top.severity == "critical"            # worst of the merged pair
    assert top.confidence == "high"              # corroborated
    assert top.file_path.endswith("dao.java")
    # the lone low-severity AST finding ranks below
    assert len(issues) == 2 and issues[1].agents == ["ast_analysis"]
    assert top.score > issues[1].score


def test_corroborated_outranks_same_severity_single_source():
    rep = _rep(
        security=SecurityResult(findings=[
            _sec("src/A.java", 10, RiskLevel.HIGH, "issue in A"),
            _sec("src/B.java", 20, RiskLevel.HIGH, "issue in B")],
            overall_severity=RiskLevel.HIGH),
        maintainability=MaintainabilityResult(issues=[MaintainabilityIssue(
            kind="swallowed_exception", severity="high", description="issue in A (maint view)",
            file_path="src/A.java", line=11)]),
    )
    issues = correlate_findings(rep)
    a = next(i for i in issues if i.file_path.endswith("a.java"))
    b = next(i for i in issues if i.file_path.endswith("b.java"))
    assert a.score > b.score                     # corroboration bonus
    assert len(a.agents) == 2 and len(b.agents) == 1


def test_unverified_only_issue_is_low_confidence_and_penalised():
    rep = _rep(security=SecurityResult(findings=[
        _sec("src/Ghost.java", 5, RiskLevel.CRITICAL, "hallucinated location", unv=True),
        _sec("src/Real.java", 9, RiskLevel.HIGH, "verified issue")],
        overall_severity=RiskLevel.CRITICAL))
    issues = correlate_findings(rep)
    ghost = next(i for i in issues if "ghost" in i.file_path)
    real  = next(i for i in issues if "real" in i.file_path)
    assert ghost.unverified and ghost.confidence == "low"
    # CRITICAL-but-unverified must NOT outrank verified HIGH
    assert real.score > ghost.score


def test_empty_report_returns_empty():
    assert correlate_findings(_rep()) == []


def test_max_issues_cap():
    findings = [_sec(f"src/F{i}.java", i * 10, RiskLevel.MEDIUM, f"issue {i}") for i in range(15)]
    rep = _rep(security=SecurityResult(findings=findings, overall_severity=RiskLevel.MEDIUM))
    assert len(correlate_findings(rep, max_issues=10)) == 10


def test_top_issues_markdown_renders_ranked_list():
    from governance.correlation import top_issues_markdown, correlate_findings
    rep = _rep(security=SecurityResult(findings=[
        _sec("src/Dao.java", 42, RiskLevel.CRITICAL, "SQL injection via concatenation")],
        overall_severity=RiskLevel.CRITICAL))
    rep.top_issues = correlate_findings(rep)
    md = top_issues_markdown(rep)
    assert "Top issues to review" in md
    assert "1. 🚨 **CRITICAL**" in md
    assert "src/dao.java:42" in md.lower()


def test_top_issues_markdown_empty_when_no_issues():
    from governance.correlation import top_issues_markdown
    assert top_issues_markdown(_rep()) == ""


def test_pr_comment_leads_with_top_issues():
    from output.pr_commenter import PRCommenter
    from governance.correlation import correlate_findings
    from core.models import GateDecision, RiskLevel as RL, RiskResult
    rep = _rep(
        security=SecurityResult(findings=[_sec("src/Dao.java", 42, RiskLevel.CRITICAL,
                                               "SQL injection via concatenation")],
                                overall_severity=RiskLevel.CRITICAL),
        risk=RiskResult(overall_risk=RL.HIGH, risk_score=80, gate_decision=GateDecision.HOLD),
    )
    rep.top_issues = correlate_findings(rep)
    body = PRCommenter(token="t", provider="github")._render(rep)
    # Top Issues section appears BEFORE the metric table
    assert "Top issues to review" in body
    assert body.index("Top issues to review") < body.index("| Metric | Value |")


def test_full_markdown_report_includes_top_issues():
    from output.report_formatter import to_markdown
    from governance.correlation import correlate_findings
    rep = _rep(security=SecurityResult(findings=[_sec("src/Dao.java", 42)],
                                       overall_severity=RiskLevel.HIGH))
    rep.top_issues = correlate_findings(rep)
    md = to_markdown(rep)
    assert "Top issues to review" in md
