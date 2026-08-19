"""
agents/code_analysis_agent.py
------------------------------
Phase 1 agent: classifies the diff and identifies code quality issues.

Model: Haiku (fast, cheap) — code classification does not need deep reasoning.
Fallback: heuristic line-count analysis.
"""
from __future__ import annotations
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    CodeAnalysisResult, CodeFinding, RiskLevel,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt, format_user_priorities
from ingestion.language_registry import lang_meta


class CodeAnalysisAgent(BaseAgent[CodeAnalysisResult]):

    agent_name   = AgentName.CODE_ANALYSIS
    output_model = CodeAnalysisResult

    system_prompt = (
        "You are an expert software engineer specialising in code review for enterprise banking systems.\n"
        "Analyse the unified diff and return:\n"
        "  • summary: one-sentence description of what this change does\n"
        "  • change_type: refactor | feature | bugfix | config | mixed\n"
        "  • complexity_delta: integer (-5 to +5); positive means increased complexity\n"
        "  • findings: list of code quality issues (smells, anti-patterns, dead code, logic flaws)\n\n"
        "Banking context: pay special attention to transaction logic, idempotency, error handling, "
        "and financial calculation correctness.\n"
        "Output ONLY compact JSON. No markdown, no preamble."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        languages = sorted({lang_meta(h.language).display for h in request.hunks})
        diff_block = format_hunks_for_prompt(request.hunks, max_chars_per_hunk=2000, focus="general")
        fn_ctx = context.get("function_context", "")
        return (
            f"Repository: {request.repo_url}\n"
            f"Comparison: {request.source_ref} → {request.target_ref}\n"
            f"Languages: {', '.join(languages)}\n\n"
            f"DIFF:\n{diff_block}"
            + (f"\n\n{fn_ctx}" if fn_ctx else "")
            + format_user_priorities(request.user_instructions)
        )

    def fallback_result(self, request: AnalysisRequest) -> CodeAnalysisResult:
        """Heuristic fallback: flag high-churn files without LLM."""
        findings = []
        for h in request.hunks:
            if h.churn > 200:
                findings.append(CodeFinding(
                    file_path=h.file_path,
                    line_range="*",
                    severity=RiskLevel.HIGH,
                    category="volume",
                    description=f"Large change: {h.churn} lines modified — manual review needed.",
                    suggestion="Consider breaking into smaller, reviewable commits.",
                ))
            elif h.churn > 50:
                findings.append(CodeFinding(
                    file_path=h.file_path,
                    line_range="*",
                    severity=RiskLevel.MEDIUM,
                    category="volume",
                    description=f"Moderate change: {h.churn} lines modified.",
                ))

        total = request.total_churn
        return CodeAnalysisResult(
            summary=(
                f"[Fallback] {total} lines changed across {len(request.hunks)} file(s). "
                "LLM review unavailable for this run (token budget or model error) — "
                "heuristic churn analysis only. Enable deep-scan / re-run to get full LLM review."
            ),
            change_type="mixed",
            complexity_delta=0,
            findings=findings,
        )
