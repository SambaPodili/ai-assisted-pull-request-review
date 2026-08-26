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
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, AnalysisReport, CodeFix, MermaidDiagram,
    RemediationResult, DeploymentStrategy, RiskLevel, GateDecision,
)
from agents.base_agent import BaseAgent, format_user_priorities, format_hunks_for_prompt
from ingestion.diff_parser import iter_added_lines

_LINE_RE = re.compile(r'@@ line (\d+) @@')


class RemediationAgent(BaseAgent[RemediationResult]):

    agent_name   = AgentName.REMEDIATION
    output_model = RemediationResult

    system_prompt = (
        "You are a principal engineer and release coach at an enterprise bank.\n"
        "Given the complete analysis report, produce:\n"
        "  • fix_suggestions: list of SPECIFIC, actionable fixes (max 8, one per finding)\n"
        "    — e.g. 'Replace String concatenation on line 42 of PaymentService.java with PreparedStatement'\n"
        "  • code_fixes: for findings tied to one of the lines listed under 'Actual changed lines'\n"
        "    below, a concrete SINGLE-LINE before/after patch — one CodeFix object per fix:\n"
        "    {title, file_path, category, severity, before, after, diff, explanation, confidence}.\n"
        "    - `before` MUST be copied EXACTLY, character-for-character, from that ONE line —\n"
        "      never paraphrase, reformat, or fix whitespace/quoting. A fix is discarded downstream\n"
        "      if `before` doesn't match the real line exactly, so precision matters more than style.\n"
        "    - `after` replaces that single line and MAY span multiple lines if the fix genuinely\n"
        "      needs to (e.g. wrapping a call in try/except) — write real, correct, complete code,\n"
        "      not a placeholder or TODO.\n"
        "    - `diff` = \"--- a/{file_path}\\n+++ b/{file_path}\\n@@ line {line} @@\\n-{before}\\n\" then\n"
        "      each line of `after` on its own line prefixed with \"+\", using the line number shown\n"
        "      for that line below.\n"
        "    - Always set confidence to \"low\" (marks it as AI-suggested for reviewer verification,\n"
        "      distinct from the deterministic high-confidence fixes already in the pipeline).\n"
        "    - Only emit a code_fix when confident the one-line change is correct and complete;\n"
        "      omit it rather than guess.\n"
        "  • validation_checklist: ordered QA/testing steps before deploy (max 10)\n"
        "    — e.g. 'Run PaymentServiceIntegrationTest against staging DB'\n"
        "  • deployment_strategy: standard | canary | blue_green | phased | feature_flag\n"
        "  • executive_summary: 3-4 sentences for non-technical audience (CTO / Risk Committee)\n\n"
        "Be concrete. Avoid generic advice. Reference specific files/methods where possible.\n"
        "Output ONLY compact JSON. No prose."
    )

    def run(self, request: AnalysisRequest, budget, context: dict[str, Any] | None = None) -> RemediationResult:
        result = super().run(request, budget, context)
        llm_fixes = result.code_fixes or []

        # Deterministic, high-confidence before/after fixes always take priority.
        try:
            from agents.fix_generator import generate_fixes
            deterministic = generate_fixes(request)
        except Exception:
            deterministic = []

        # Hallucination guard: only keep an LLM-proposed fix if `before` matches
        # a REAL added line at that exact file/line in the diff. The LLM only
        # ever saw a text excerpt, not the file — without this check a wrong
        # line number or a subtly reworded `before` would silently corrupt the
        # file when applied (the apply step's own staleness check compares
        # against the live file, not against what the LLM claimed).
        seen = {(f.file_path, _fix_line(f)) for f in deterministic}
        verified_llm_fixes: list[CodeFix] = []
        for fix in llm_fixes:
            line = _fix_line(fix)
            if line is None or not fix.file_path or (fix.file_path, line) in seen:
                continue
            if _line_context(request, fix.file_path, line) != fix.before:
                continue
            fix.confidence = "low"  # enforce — never trust the LLM's own claim here
            seen.add((fix.file_path, line))
            verified_llm_fixes.append(fix)

        result.code_fixes = deterministic + verified_llm_fixes

        # Best-effort narrative diagram — never let a failure here break the
        # main remediation result (fix_suggestions/code_fixes/checklist).
        try:
            diagram = self._maybe_generate_diagram(request, context or {})
            if diagram:
                result.diagrams = [diagram]
        except Exception:
            pass

        return result

    def _maybe_generate_diagram(self, request: AnalysisRequest, context: dict[str, Any]) -> MermaidDiagram | None:
        """Narrative (LLM-generated, not call-graph-verified) sequence diagram
        for complex changes — see MermaidDiagram's docstring for the
        confidence-labeling rationale. Only attempted for diffs already
        flagged as non-trivial (real reference_impact data AND medium+ risk)
        to avoid spending an extra LLM call on every PR."""
        report = context.get("full_report", {})
        references = _safe_list(report, "reference_impact", "references")
        overall_risk = str(_safe_get(report, "risk", "overall_risk", default="")).lower()
        if not references or overall_risk not in ("medium", "high", "critical"):
            return None

        from agents.llm_client import make_llm_client
        model_cfg = self._resolve_model_config(context, self.agent_name.value)
        client = make_llm_client(model_cfg)

        hunks_text = format_hunks_for_prompt(request.hunks, max_chars_per_hunk=1500)
        ref_lines = "\n".join(
            f"- `{r.get('symbol', '')}` called from {r.get('file_path', '')}:{r.get('line', '')}"
            for r in references[:10]
        )
        system = (
            "You are a principal engineer producing a control-flow sequence diagram for a code "
            "review. Output ONLY valid Mermaid syntax starting with 'sequenceDiagram' — no prose, "
            "no markdown code fences, no explanation before or after. Base it on the actual diff "
            "and the real caller references given below; keep it focused on the changed control "
            "flow (roughly 5-15 lines is usually enough — don't pad it). If the change genuinely "
            "has no meaningful sequential flow to diagram, output exactly: NONE"
        )
        user = (
            f"Changed code:\n{hunks_text}\n\n"
            f"Known real callers of changed symbols (from static analysis, not guessed):\n"
            f"{ref_lines or '(none found)'}\n"
        )
        response = client.create(system=system, user=user, max_tokens=600)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0].strip()
        if not text or text.upper() == "NONE":
            return None
        # Cheap syntax sanity check — the honest limit of what's verifiable
        # without ground truth to check the diagram's claims against.
        if not text.startswith(("sequenceDiagram", "graph ", "graph\n", "flowchart ")):
            return None
        return MermaidDiagram(mermaid_source=text)

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        report = context.get("full_report", {})

        # Build minimal prompt — token budget may be low after other agents
        gate       = _safe_get(report, "risk", "gate_decision", default="HOLD")
        risk       = _safe_get(report, "risk", "overall_risk",  default="high")
        sec_sev    = _safe_get(report, "security", "overall_severity", default="unknown")
        rationale  = _safe_get(report, "risk", "rationale", default="")
        blast      = _safe_get(report, "dependency", "blast_radius_score", default=0)
        strategy   = _infer_strategy(report)

        # Sourced from top_issues (already deduped/ranked by governance/correlation.py)
        # rather than raw per-agent findings, so this matches exactly what the
        # reviewer sees in the report, and gives us a resolved file:line to look
        # up the real changed-line text for code_fixes.
        top_issues = _safe_list(report, "top_issues")[:6]
        finding_lines: list[str] = []
        code_lines: list[str] = []
        for it in top_issues:
            fp, ln = it.get("file_path", ""), it.get("line", 0)
            loc = f" — {fp}:{ln}" if fp else ""
            finding_lines.append(f"[{(it.get('severity') or '').upper()}] {it.get('title','')}{loc}")
            code = _line_context(request, fp, ln) if fp and ln else None
            if code is not None:
                code_lines.append(f"{fp}:{ln}: {code}")

        context_str = (
            f"Change: {request.source_ref} → {request.target_ref} ({request.repo_url})\n"
            f"Gate: {gate} | Risk: {risk} | Security: {sec_sev} | Blast radius: {blast}\n"
            f"Risk rationale: {rationale}\n"
            f"Suggested deployment: {strategy}\n"
            f"Top findings:\n" + "\n".join(f"  - {x}" for x in finding_lines)
        )
        if code_lines:
            context_str += (
                "\n\nActual changed lines (use these EXACT strings as `before` in code_fixes):\n"
                + "\n".join(f"  {x}" for x in code_lines)
            )
        return context_str + format_user_priorities(request.user_instructions)

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

def _fix_line(fix: CodeFix) -> int | None:
    """Line number a CodeFix targets, parsed from its `diff`'s `@@ line N @@`
    marker (CodeFix has no direct line field) — mirrors agents/fix_generator.py
    and vscode-extension/src/codeActions.ts's identical parsing."""
    m = _LINE_RE.search(fix.diff)
    return int(m.group(1)) if m else None


def _line_context(request: AnalysisRequest, file_path: str, line: int) -> str | None:
    """The real added-line text at `file_path:line` in the diff, or None if no
    such added line exists — the ground truth an LLM-proposed fix's `before`
    must match exactly to be trusted."""
    for hunk in request.hunks:
        if hunk.file_path != file_path:
            continue
        for line_no, raw in iter_added_lines(hunk.content):
            if line_no == line:
                return raw.rstrip()
    return None


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
