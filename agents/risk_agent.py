"""
agents/risk_agent.py
---------------------
Phase 3 agent: synthesises all prior findings into a composite risk score and gate decision.

Always runs sequentially AFTER Phase 1 and 2 agents complete.
Model: Sonnet — risk reasoning requires holistic understanding of all findings.
Fallback: heuristic scoring from total churn + blast radius + security severity.
"""
from __future__ import annotations
import json
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, AnalysisReport,
    RiskResult, RiskLevel, GateDecision,
)
from agents.base_agent import BaseAgent


class RiskAssessmentAgent(BaseAgent[RiskResult]):

    agent_name   = AgentName.RISK
    output_model = RiskResult

    system_prompt = (
        "You are a senior release risk officer at an enterprise bank.\n"
        "You receive a structured JSON summary of all code analysis findings and must produce:\n"
        "  • overall_risk: low | medium | high | critical\n"
        "  • risk_score: integer 0-100\n"
        "  • deployment_guidance: 1-2 sentences on when/how to deploy\n"
        "  • rollback_feasibility: easy | moderate | complex | infeasible\n"
        "  • gate_decision: APPROVE | HOLD | BLOCK\n"
        "  • rationale: 2-3 sentences explaining the decision\n\n"
        "BLOCK rules (any one sufficient):\n"
        "  - Critical security finding (secrets, SQL injection, CWE-798, CWE-89)\n"
        "  - secrets_detected = true\n"
        "  - breaking API changes without migration path\n"
        "  - blast_radius_score > 70 with no feature flag\n"
        "HOLD rules:\n"
        "  - High-severity security findings\n"
        "  - Coverage dropped > 5%\n"
        "  - DB schema migrations (complex rollback)\n"
        "  - Any breaking API changes (even with consumers identified)\n"
        "APPROVE: all else passing.\n"
        "Output ONLY compact JSON. No prose."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        report = context.get("partial_report", {})
        summary = {
            "repo":              request.repo_url,
            "comparison":        f"{request.source_ref} → {request.target_ref}",
            "total_churn":       request.total_churn,
            "files_changed":     len(request.hunks),
            "security_severity": _safe_get(report, "security", "overall_severity"),
            "secrets_detected":  _safe_get(report, "security", "secrets_detected"),
            "sec_findings_count": len(_safe_list(report, "security", "findings")),
            "compliance_flags":  _safe_list(report, "security", "compliance_flags"),
            "blast_radius":      _safe_get(report, "dependency", "blast_radius_score"),
            "affected_services": _safe_list(report, "dependency", "affected_services"),
            "cve_hits":          _safe_list(report, "dependency", "cve_hits"),
            "breaking_changes":  len(_safe_list(report, "interface", "breaking_changes")),
            "coverage_delta":    _safe_get(report, "test_coverage", "coverage_delta"),
            "regression_risk":   _safe_get(report, "test_coverage", "regression_risk"),
            "complexity_delta":  _safe_get(report, "code_analysis", "complexity_delta"),
        }
        return f"Analysis summary:\n{json.dumps(summary, indent=2)}"

    def fallback_result(self, request: AnalysisRequest) -> RiskResult:
        """Heuristic scoring without LLM."""
        churn = request.total_churn
        score = min(100, churn // 10)

        risk = (
            RiskLevel.CRITICAL if score > 80 else
            RiskLevel.HIGH     if score > 50 else
            RiskLevel.MEDIUM   if score > 20 else
            RiskLevel.LOW
        )
        decision = (
            GateDecision.BLOCK if risk == RiskLevel.CRITICAL else
            GateDecision.HOLD  if risk == RiskLevel.HIGH     else
            GateDecision.APPROVE
        )

        return RiskResult(
            overall_risk=risk,
            risk_score=score,
            deployment_guidance="Heuristic assessment only — LLM unavailable. Manual review required.",
            rollback_feasibility="unknown",
            gate_decision=decision,
            rationale=f"Score derived from {churn} changed lines across {len(request.hunks)} files.",
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def _safe_list(d: dict, *keys) -> list:
    val = _safe_get(d, *keys, default=[])
    return val if isinstance(val, list) else []


def build_partial_report_context(report: AnalysisReport) -> dict:
    """
    Serialise an AnalysisReport to a compact dict for the risk agent prompt.
    Called by the orchestrator before invoking RiskAssessmentAgent.
    """
    return {"partial_report": report.model_dump(exclude={"token_usage", "errors", "completed_at"})}
