"""
tests/integration/test_pipeline.py
------------------------------------
End-to-end pipeline tests with mocked Anthropic API.
Tests all three phases and verifies the full orchestration contract.
"""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import MagicMock, patch

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk,
    CodeAnalysisResult, SecurityResult, SecurityFinding,
    DependencyResult, TestCoverageResult, InterfaceResult,
    RiskResult, RemediationResult, RiskLevel, GateDecision, DeploymentStrategy,
)
from core.orchestrator import ImpactAnalysisOrchestrator


# ── Helpers ────────────────────────────────────────────────────────────────────

def _req(n_hunks: int = 1) -> AnalysisRequest:
    hunks = [
        DiffHunk(
            file_path=f"src/Service{i}.java",
            language="java",
            additions=20,
            deletions=5,
            content=f"+// changed line {i}\n-// old line {i}\n",
        )
        for i in range(n_hunks)
    ]
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/payments",
        source_ref="feature/JIRA-9999",
        target_ref="main",
        hunks=hunks,
    )


def _mock_client(responses: list[str]):
    """
    Build a mock Anthropic client that returns the given JSON strings in order.
    Each call to messages.create() pops from the front.
    """
    client        = MagicMock()
    call_count    = [0]

    def _create(**kwargs):
        idx   = min(call_count[0], len(responses) - 1)
        resp  = MagicMock()
        resp.content = [MagicMock(text=responses[idx])]
        resp.usage   = MagicMock(input_tokens=100, output_tokens=50)
        call_count[0] += 1
        return resp

    client.messages.create.side_effect = _create
    return client


# ── Phase 1 integration ────────────────────────────────────────────────────────

class TestPhase1Pipeline:

    def test_phase1_complete_with_mocked_llm(self):
        """Both Phase 1 agents complete successfully with mocked API responses."""
        code_json = CodeAnalysisResult(
            summary="Refactored payment validation logic",
            change_type="refactor", complexity_delta=-1,
        ).model_dump_json()

        sec_json = SecurityResult(
            findings=[], secrets_detected=False, overall_severity=RiskLevel.LOW
        ).model_dump_json()

        orch     = ImpactAnalysisOrchestrator(api_key="sk-test", phase=1)
        mock_cli = _mock_client([code_json, sec_json])
        orch._code._client = mock_cli
        orch._sec._client  = mock_cli

        report = orch.analyse(_req())
        assert report.code_analysis is not None
        assert report.security      is not None
        assert report.dependency    is None   # Phase 2 not run
        assert report.risk          is None

    def test_phase1_fallback_when_budget_zero(self):
        """Phase 1 agents use deterministic fallback when budget is exhausted."""
        from core.token_manager import TokenBudgetManager
        budgets = {k: 0 for k in ["code_analysis", "security", "dependency",
                                    "test_coverage", "interface", "risk", "remediation", "_reserve"]}
        orch    = ImpactAnalysisOrchestrator(api_key="sk-test", phase=1, token_budgets=budgets)
        report  = orch.analyse(_req())
        assert report.code_analysis.fallback_used is True
        assert report.security.fallback_used      is True


# ── Phase 2 integration ────────────────────────────────────────────────────────

class TestPhase2Pipeline:

    def _all_mock_responses(self) -> list[str]:
        return [
            CodeAnalysisResult(summary="feature", change_type="feature", complexity_delta=2).model_dump_json(),
            SecurityResult(findings=[], secrets_detected=False, overall_severity=RiskLevel.LOW).model_dump_json(),
            DependencyResult(blast_radius_score=30, affected_services=["accounts"]).model_dump_json(),
            TestCoverageResult(coverage_delta=-2.0, regression_risk=RiskLevel.MEDIUM).model_dump_json(),
            InterfaceResult(breaking_changes=[], schema_diffs=[], affected_consumers=[]).model_dump_json(),
            RiskResult(overall_risk=RiskLevel.MEDIUM, risk_score=40, gate_decision=GateDecision.HOLD,
                       rollback_feasibility="easy", rationale="Medium risk.").model_dump_json(),
        ]

    def test_phase2_all_agents_run(self):
        orch    = ImpactAnalysisOrchestrator(api_key="sk-test", phase=2)
        mock_cli = _mock_client(self._all_mock_responses())
        for agent in [orch._code, orch._sec, orch._dep, orch._test, orch._iface, orch._risk]:
            agent._client = mock_cli

        report = orch.analyse(_req())
        assert report.code_analysis is not None
        assert report.security      is not None
        assert report.dependency    is not None
        assert report.test_coverage is not None
        assert report.interface     is not None
        assert report.risk          is not None
        assert report.remediation   is None    # Phase 3 not run

    def test_gate_decision_propagated(self):
        orch     = ImpactAnalysisOrchestrator(api_key="sk-test", phase=2)
        risk_j   = RiskResult(
            overall_risk=RiskLevel.CRITICAL, risk_score=95, gate_decision=GateDecision.BLOCK,
            rollback_feasibility="infeasible", rationale="Critical secret found."
        ).model_dump_json()
        mock_cli = _mock_client([
            CodeAnalysisResult(summary="x", change_type="feature").model_dump_json(),
            SecurityResult(findings=[], secrets_detected=True, overall_severity=RiskLevel.CRITICAL).model_dump_json(),
            DependencyResult().model_dump_json(),
            TestCoverageResult().model_dump_json(),
            InterfaceResult().model_dump_json(),
            risk_j,
        ])
        for agent in [orch._code, orch._sec, orch._dep, orch._test, orch._iface, orch._risk]:
            agent._client = mock_cli

        report = orch.analyse(_req())
        assert report.gate_decision == GateDecision.BLOCK


# ── Phase 3 integration ────────────────────────────────────────────────────────

class TestPhase3Pipeline:

    def test_phase3_remediation_runs(self):
        orch = ImpactAnalysisOrchestrator(api_key="sk-test", phase=3)
        rem_j = RemediationResult(
            fix_suggestions=["Fix SQL injection on line 42"],
            validation_checklist=["Run full test suite"],
            deployment_strategy=DeploymentStrategy.CANARY,
            executive_summary="Medium-risk change. Deploy via canary.",
        ).model_dump_json()

        responses = [
            CodeAnalysisResult(summary="feature", change_type="feature").model_dump_json(),
            SecurityResult(findings=[], secrets_detected=False, overall_severity=RiskLevel.LOW).model_dump_json(),
            DependencyResult(blast_radius_score=10, affected_services=[]).model_dump_json(),
            TestCoverageResult(coverage_delta=2.0, regression_risk=RiskLevel.LOW).model_dump_json(),
            InterfaceResult().model_dump_json(),
            RiskResult(overall_risk=RiskLevel.LOW, risk_score=15, gate_decision=GateDecision.APPROVE,
                       rollback_feasibility="easy", rationale="Low risk.").model_dump_json(),
            rem_j,
        ]
        mock_cli = _mock_client(responses)
        for agent in [orch._code, orch._sec, orch._dep, orch._test, orch._iface, orch._risk, orch._rem]:
            agent._client = mock_cli

        report = orch.analyse(_req())
        assert report.remediation is not None
        assert len(report.remediation.fix_suggestions) >= 1
        assert report.remediation.executive_summary != ""
        assert report.gate_decision == GateDecision.APPROVE
        assert report.total_tokens  > 0


# ── Report formatter ───────────────────────────────────────────────────────────

class TestReportFormatter:

    def test_markdown_render_non_empty(self):
        from output.report_formatter import to_markdown, to_summary_json
        from core.models import AnalysisReport

        report = AnalysisReport(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/test",
            source_ref="feature", target_ref="main",
            phase_run=2,
            security=SecurityResult(findings=[], secrets_detected=False, overall_severity=RiskLevel.LOW),
            risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=GateDecision.APPROVE,
                            rollback_feasibility="easy", rationale="Low risk."),
        )
        md   = to_markdown(report)
        summ = to_summary_json(report)
        assert "APPROVE" in md
        assert summ["gate"] == "APPROVE"
