"""
tests/test_gate_and_capability.py
-----------------------------------
Deterministic gate enforcement + business-capability mapping.
Both are pure functions over a report — no LLM, fully deterministic.
"""
from __future__ import annotations
import pytest

from core.models import (
    AnalysisReport, ChangeType, GateDecision, RiskLevel,
    RiskResult, SecurityResult, SecurityFinding, DependencyResult,
    InterfaceResult, ContractBreak, TestCoverageResult, ReferenceImpactResult, SymbolReference,
    CodeAnalysisResult, CodeFinding,
)
from governance.gate_policy import evaluate_policy
from governance.capability_map import map_paths, capabilities_for_report


def _report(**kw) -> AnalysisReport:
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def _risk(gate: GateDecision) -> RiskResult:
    return RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=gate)


# ── Gate enforcement ──────────────────────────────────────────────────────────

def test_secrets_force_block_even_if_ai_approves():
    r = _report(
        risk=_risk(GateDecision.APPROVE),
        security=SecurityResult(overall_severity=RiskLevel.LOW, secrets_detected=True, findings=[]),
    )
    res = evaluate_policy(r)
    assert res.gate == GateDecision.BLOCK
    assert res.overrode_llm is True
    assert any("secret" in reason.lower() for reason in res.reasons)


def test_critical_security_forces_block():
    r = _report(
        risk=_risk(GateDecision.HOLD),
        security=SecurityResult(
            overall_severity=RiskLevel.CRITICAL, secrets_detected=False,
            findings=[SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.CRITICAL,
                                      cwe_id="CWE-89", description="SQLi", remediation="parametrise")],
        ),
    )
    assert evaluate_policy(r).gate == GateDecision.BLOCK


def _code(**kw):
    base = dict(file_path="agents/code_agent.py", line_range="184", severity=RiskLevel.CRITICAL,
                category="broken_reference",
                description="_ext_to_lang(fp) called but its module-level def was deleted -> NameError",
                suggestion="restore the helper")
    base.update(kw)
    return CodeFinding(**base)


def test_confirmed_critical_code_finding_holds_even_if_ai_approves():
    # A real broken-reference bug must not silently APPROVE — it holds for review,
    # so the gate agrees with the Review Plan / Top Issues.
    r = _report(risk=_risk(GateDecision.APPROVE),
                code_analysis=CodeAnalysisResult(summary="x", change_type="refactor", findings=[_code()]))
    res = evaluate_policy(r)
    assert res.gate == GateDecision.HOLD
    assert res.overrode_llm is True
    assert any("critical code finding" in reason.lower() for reason in res.reasons)


def test_unverified_critical_code_finding_does_not_hold():
    # A hallucinated/unverified critical code finding must NOT drive the gate.
    r = _report(risk=_risk(GateDecision.APPROVE),
                code_analysis=CodeAnalysisResult(summary="x", change_type="refactor",
                                                 findings=[_code(unverified=True)]))
    res = evaluate_policy(r)
    assert res.gate == GateDecision.APPROVE
    assert not any("critical code finding" in reason.lower() for reason in res.reasons)


def test_non_critical_code_finding_does_not_hold():
    r = _report(risk=_risk(GateDecision.APPROVE),
                code_analysis=CodeAnalysisResult(summary="x", change_type="refactor",
                                                 findings=[_code(severity=RiskLevel.MEDIUM)]))
    assert evaluate_policy(r).gate == GateDecision.APPROVE


def test_breaking_api_forces_at_least_hold():
    r = _report(
        risk=_risk(GateDecision.APPROVE),
        interface=InterfaceResult(breaking_changes=[
            ContractBreak(interface_type="REST", path="/v1/x", break_type="removed")]),
    )
    res = evaluate_policy(r)
    assert res.gate == GateDecision.HOLD
    assert res.overrode_llm is True


def test_coverage_drop_thresholds():
    # -6% → HOLD
    r = _report(risk=_risk(GateDecision.APPROVE),
                test_coverage=TestCoverageResult(coverage_delta=-6.0, regression_risk=RiskLevel.MEDIUM))
    assert evaluate_policy(r).gate == GateDecision.HOLD
    # -20% → BLOCK
    r2 = _report(risk=_risk(GateDecision.APPROVE),
                 test_coverage=TestCoverageResult(coverage_delta=-20.0, regression_risk=RiskLevel.HIGH))
    assert evaluate_policy(r2).gate == GateDecision.BLOCK


def test_cve_forces_block():
    r = _report(risk=_risk(GateDecision.APPROVE),
                dependency=DependencyResult(cve_hits=["CVE-2023-32681"]))
    assert evaluate_policy(r).gate == GateDecision.BLOCK


def test_taint_injection_forces_block():
    """Data-flow-proven injection/SSRF/path-traversal must reach the gate."""
    from core.models import TaintAnalysisResult
    r = _report(risk=_risk(GateDecision.APPROVE),
                taint_analysis=TaintAnalysisResult(has_injection=True, taint_paths=[]))
    res = evaluate_policy(r)
    assert res.gate == GateDecision.BLOCK
    assert any("injection" in reason.lower() for reason in res.reasons)


def test_critical_iac_forces_block_high_holds():
    from core.models import IaCAnalysisResult, IaCFinding
    crit = _report(risk=_risk(GateDecision.APPROVE),
                   iac_analysis=IaCAnalysisResult(findings=[
                       IaCFinding(file_path="main.tf", line=1, resource="aws_s3_bucket.x",
                                  kind="public_bucket", severity=RiskLevel.CRITICAL, description="public")]))
    assert evaluate_policy(crit).gate == GateDecision.BLOCK

    high = _report(risk=_risk(GateDecision.APPROVE),
                   iac_analysis=IaCAnalysisResult(findings=[
                       IaCFinding(file_path="main.tf", line=2, resource="aws_iam", kind="wildcard_iam",
                                  severity=RiskLevel.HIGH, description="wildcard")]))
    assert evaluate_policy(high).gate == GateDecision.HOLD


def test_policy_never_weakens_ai_gate():
    # AI says BLOCK, policy finds nothing → final stays BLOCK (most restrictive)
    r = _report(risk=_risk(GateDecision.BLOCK))
    res = evaluate_policy(r)
    assert res.gate == GateDecision.BLOCK
    assert res.overrode_llm is False


def test_clean_change_approves():
    r = _report(risk=_risk(GateDecision.APPROVE))
    res = evaluate_policy(r)
    assert res.gate == GateDecision.APPROVE
    assert res.reasons == []


# ── Capability mapping ──────────────────────────────────────────────────────────

def test_map_paths_tags_payments_and_auth():
    caps = map_paths(["services/payment/refund.py", "src/auth/login.py", "README.md"])
    names = {c["name"] for c in caps}
    assert "Payments / Refunds" in names
    assert "Authentication & Access" in names
    # README maps to nothing
    pay = next(c for c in caps if c["name"] == "Payments / Refunds")
    assert "services/payment/refund.py" in pay["files"]
    assert pay["criticality"] == "critical"


def test_map_paths_sorted_by_criticality():
    caps = map_paths(["templates/welcome.html", "ledger/accounts.py"])
    # Core Ledger (critical) should come before Notifications (low)
    assert caps[0]["criticality"] == "critical"


def test_capabilities_for_report_from_findings():
    r = _report(
        risk=_risk(GateDecision.HOLD),
        reference_impact=ReferenceImpactResult(
            changed_symbols=["refund"], references=[
                SymbolReference(symbol="refund", file_path="services/payment/refund.py", line=10)
            ], total_references=1, high_impact_files=["services/payment/refund.py"],
            intra_project_risk=RiskLevel.MEDIUM, search_backend="local_grep",
        ),
    )
    caps = capabilities_for_report(r)
    assert any(c["name"] == "Payments / Refunds" for c in caps)


def test_empty_report_no_capabilities():
    assert capabilities_for_report(_report(risk=_risk(GateDecision.APPROVE))) == []


def test_phantom_finding_does_not_block():
    """A finding with severity but no content must NOT trigger BLOCK/HOLD."""
    r = _report(
        risk=_risk(GateDecision.APPROVE),
        security=SecurityResult(overall_severity=RiskLevel.CRITICAL, secrets_detected=False,
            findings=[SecurityFinding(file_path="", line_range="", severity=RiskLevel.CRITICAL,
                                      cwe_id="", description="", remediation="")]),
    )
    res = evaluate_policy(r)
    assert res.gate == GateDecision.APPROVE
