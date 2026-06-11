"""tests/test_quality_hardening.py
Framework-wide quality: shared grounding/severity rubric on every agent prompt,
and the generalised hallucination guard across all agent findings.
"""
from core.models import (AnalysisReport, ChangeType, RiskLevel,
                         CodeAnalysisResult, CodeFinding,
                         ASTAnalysisResult, ASTFinding,
                         DataPrivacyResult, PIIFinding)
from governance.evidence import filter_all_unsubstantiated


def test_quality_directive_appended_to_every_agent():
    from agents import base_agent as ba
    d = ba._QUALITY_DIRECTIVE
    assert "GROUNDING" in d and "CRITICAL" in d and "empty findings list" in d
    # It is concatenated onto the agent system prompt inside _call_llm — verify the
    # call path builds `system = self.system_prompt + _QUALITY_DIRECTIVE`.
    import inspect
    src = inspect.getsource(ba.BaseAgent._call_llm)
    assert "_QUALITY_DIRECTIVE" in src and "system=system" in src


def _report(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def test_generalised_guard_flags_offdiff_findings():
    rep = _report(
        code_analysis=CodeAnalysisResult(summary="", change_type="feature", findings=[
            CodeFinding(file_path="src/Changed.java", line_range="10", severity=RiskLevel.HIGH, category="logic", description="in diff"),
            CodeFinding(file_path="src/Ghost.java", line_range="5", severity=RiskLevel.HIGH, category="logic", description="NOT in diff"),
        ]),
        ast_analysis=ASTAnalysisResult(findings=[
            ASTFinding(file_path="src/Ghost.java", line=3, function="f", kind="null_risk", severity=RiskLevel.MEDIUM, description="hallucinated"),
        ]),
        data_privacy=DataPrivacyResult(pii_findings=[
            PIIFinding(pii_type="email", file_path="src/Changed.java", line=4, description="ok"),
        ]),
    )
    flagged = filter_all_unsubstantiated(rep, {"src/Changed.java"})
    assert flagged == 2  # Ghost.java in code + ast
    cf = {f.file_path: f.unverified for f in rep.code_analysis.findings}
    assert cf["src/Changed.java"] is False and cf["src/Ghost.java"] is True
    assert rep.ast_analysis.findings[0].unverified is True
    assert rep.data_privacy.pii_findings[0].unverified is False


def test_guard_keeps_pathless_findings_and_noops_without_diff():
    rep = _report(code_analysis=CodeAnalysisResult(summary="", change_type="feature", findings=[
        CodeFinding(file_path="", line_range="", severity=RiskLevel.LOW, category="smell", description="repo-level")]))
    assert filter_all_unsubstantiated(rep, set()) == 0          # no diff → no-op
    filter_all_unsubstantiated(rep, {"src/X.java"})
    assert rep.code_analysis.findings[0].unverified is False    # no path → kept verified
