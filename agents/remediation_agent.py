"""
agents/remediation_agent.py
-----------------------------
Phase 3 agent: produces actionable fixes, deployment strategy, and executive summary.

Always runs LAST after RiskAssessmentAgent completes.
Model: Sonnet — executive communication needs quality reasoning.
Fallback: generic banking-safe defaults.
"""
from __future__ import annotations
import json
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, AnalysisReport,
    RemediationResult, DeploymentStrategy, RiskLevel, GateDecision,
)
from agents.base_agent import BaseAgent


class RemediationAgent(BaseAgent[RemediationResult]):

    agent_name   = AgentName.REMEDIATION
    output_model = RemediationResult

    system_prompt = (
        "You are a principal engineer and release coach at an enterprise bank.\n"
        "Given the complete analysis report, produce:\n"
        "  • fix_suggestions: list of SPECIFIC, actionable fixes (max 8, one per finding)\n"
        "    — e.g. 'Replace String concatenation on line 42 of PaymentService.java with PreparedStatement'\n"
        "  • validation_checklist: ordered QA/testing steps before deploy (max 10)\n"
        "    — e.g. 'Run PaymentServiceIntegrationTest against staging DB'\n"
        "  • deployment_strategy: standard | canary | blue_green | phased | feature_flag\n"
        "  • executive_summary: 3-4 sentences for non-technical audience (CTO / Risk Committee)\n\n"
        "Be concrete. Avoid generic advice. Reference specific files/methods where possible.\n"
        "Output ONLY compact JSON. No prose."
    )

    def run(self, request: AnalysisRequest, budget, context: dict[str, Any] | None = None) -> RemediationResult:
        result = super().run(request, budget, context)
        # Attach concrete, high-confidence before/after fixes (deterministic).
        try:
            from agents.fix_generator import generate_fixes
            result.code_fixes = generate_fixes(request)
        except Exception:
            pass
        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        report = context.get("full_report", {})

        # Build minimal prompt — token budget may be low after other agents
        gate       = _safe_get(report, "risk", "gate_decision", default="HOLD")
        risk       = _safe_get(report, "risk", "overall_risk",  default="high")
        sec_sev    = _safe_get(report, "security", "overall_severity", default="unknown")
        rationale  = _safe_get(report, "risk", "rationale", default="")
        blast      = _safe_get(report, "dependency", "blast_radius_score", default=0)
        strategy   = _infer_strategy(report)

        top_findings = []
        for f in _safe_list(report, "security", "findings")[:3]:
            top_findings.append(f"[SEC:{f.get('cwe_id','')}] {f.get('description','')} — {f.get('file_path','')}")
        for b in _safe_list(report, "interface", "breaking_changes")[:2]:
            top_findings.append(f"[API] {b.get('break_type','')} at {b.get('path','')}")
        for g in _safe_list(report, "test_coverage", "uncovered_paths")[:2]:
            top_findings.append(f"[TEST] No tests for {g.get('uncovered_path','')} in {g.get('file_path','')}")

        context_str = (
            f"Change: {request.source_ref} → {request.target_ref} ({request.repo_url})\n"
            f"Gate: {gate} | Risk: {risk} | Security: {sec_sev} | Blast radius: {blast}\n"
            f"Risk rationale: {rationale}\n"
            f"Suggested deployment: {strategy}\n"
            f"Top findings:\n" + "\n".join(f"  - {x}" for x in top_findings)
        )
        return context_str

    def fallback_result(self, request: AnalysisRequest) -> RemediationResult:
        return RemediationResult(
            fix_suggestions=[
                "Review and remediate all critical and high security findings before merging.",
                "Ensure unit and integration test coverage exceeds 80% for all changed modules.",
                "Validate API contract changes with all consuming teams before deployment.",
                "Perform a manual walkthrough of changed transaction logic with a domain expert.",
            ],
            validation_checklist=[
                "Run full regression test suite on staging environment.",
                "Perform manual smoke test on all affected API endpoints.",
                "Validate rollback procedure with the DBA team (especially for schema changes).",
                "Confirm no secrets are committed (scan with detect-secrets or gitleaks).",
                "Obtain sign-off from the security team for any critical findings.",
            ],
            deployment_strategy=DeploymentStrategy.CANARY,
            executive_summary=(
                "This change has been flagged for manual review before production deployment. "
                "Automated analysis identified issues that require engineering team attention. "
                "A canary deployment is recommended once all findings are resolved. "
                "The security and QA teams should provide explicit sign-off."
            ),
        )


def build_full_report_context(report: AnalysisReport) -> dict:
    """Serialise AnalysisReport to a compact dict for the remediation agent prompt."""
    return {"full_report": report.model_dump(exclude={"token_usage", "errors", "completed_at"})}


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


def _infer_strategy(report: dict) -> str:
    blast    = _safe_get(report, "dependency", "blast_radius_score", default=0)
    breaking = len(_safe_list(report, "interface", "breaking_changes"))
    risk     = _safe_get(report, "risk", "overall_risk", default="low")

    if isinstance(blast, (int, float)) and blast > 60:
        return DeploymentStrategy.PHASED.value
    if breaking > 0:
        return DeploymentStrategy.BLUE_GREEN.value
    if risk in ("high", "critical"):
        return DeploymentStrategy.CANARY.value
    return DeploymentStrategy.STANDARD.value
