"""tests/test_context_and_confidence_gate.py
Function-context expansion (enclosing function fed to LLM agents) and the
opt-in confidence-weighted gate (GATE_REQUIRE_CONFIRMED_HIGHS).
"""
import textwrap
from core.models import (AnalysisRequest, AnalysisReport, ChangeType, DiffHunk,
                         RiskLevel, RiskResult, GateDecision,
                         SecurityResult, SecurityFinding, CorrelatedIssue)


# ── Function-context expansion ────────────────────────────────────────────────

JAVA_FILE = textwrap.dedent("""\
    package svc;

    public class FeeService {

        public int unrelated() {
            return 1;
        }

        public int calc(int amount, String tier) {
            validate(tier);
            int fee = amount * rate(tier);
            return fee;
        }
    }
""")


def test_expander_returns_enclosing_function(tmp_path):
    from ingestion.context_expander import expand_function_context
    f = tmp_path / "src" / "FeeService.java"
    f.parent.mkdir(parents=True)
    f.write_text(JAVA_FILE)
    hunk = DiffHunk(file_path="src/FeeService.java", language="java", additions=1, deletions=0,
                    content="+        int fee = amount * rate(tier);")
    ctx = expand_function_context([hunk], str(tmp_path))
    assert "src/FeeService.java" in ctx
    snip = ctx["src/FeeService.java"]
    assert "public int calc(int amount, String tier)" in snip   # enclosing signature
    assert "int fee = amount * rate(tier);" in snip


def test_expander_noops_without_repo_path():
    from ingestion.context_expander import expand_function_context
    hunk = DiffHunk(file_path="a.java", language="java", additions=1, deletions=0, content="+x")
    assert expand_function_context([hunk], "") == {}
    assert expand_function_context([hunk], "/nonexistent/dir/xyz") == {}


def test_security_prompt_includes_function_context():
    from agents.security_agent import SecurityReviewAgent
    req = AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                          source_ref="a", target_ref="b",
                          hunks=[DiffHunk(file_path="A.java", language="java",
                                          additions=1, deletions=0, content="+int x = 1;")])
    prompt = SecurityReviewAgent(api_key=None).build_user_prompt(
        req, {"function_context": "SURROUNDING CODE:\n--- A.java ---\npublic int calc()"})
    assert "SURROUNDING CODE" in prompt and "public int calc()" in prompt


# ── Confidence-weighted gate ──────────────────────────────────────────────────

def _report_with_high(confirmed: bool):
    rep = AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=GateDecision.APPROVE),
        security=SecurityResult(findings=[SecurityFinding(
            file_path="src/Dao.java", line_range="42", severity=RiskLevel.HIGH,
            cwe_id="CWE-89", owasp_cat="A03", description="possible injection",
            remediation="fix")], overall_severity=RiskLevel.HIGH),
    )
    rep.top_issues = [CorrelatedIssue(
        title="possible injection", file_path="src/dao.java", line=42,
        severity="high", confidence="high" if confirmed else "medium",
        score=70, agents=["security", "taint_analysis"] if confirmed else ["security"])]
    return rep


def test_default_mode_any_verified_high_holds():
    from governance.gate_policy import evaluate_policy
    from config.settings import Settings
    cfg = Settings(skip_auth=True)   # default: confidence weighting OFF
    assert evaluate_policy(_report_with_high(confirmed=False), settings=cfg).gate == GateDecision.HOLD


def test_confidence_mode_unconfirmed_high_does_not_hold():
    from governance.gate_policy import evaluate_policy
    from config.settings import Settings
    cfg = Settings(skip_auth=True, gate_require_confirmed_highs=True)
    res = evaluate_policy(_report_with_high(confirmed=False), settings=cfg)
    assert res.gate == GateDecision.APPROVE     # single-source LLM high → visible but no hold


def test_confidence_mode_confirmed_high_still_holds():
    from governance.gate_policy import evaluate_policy
    from config.settings import Settings
    cfg = Settings(skip_auth=True, gate_require_confirmed_highs=True)
    res = evaluate_policy(_report_with_high(confirmed=True), settings=cfg)
    assert res.gate == GateDecision.HOLD
    assert any("confirmed" in r for r in res.reasons)
