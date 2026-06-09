"""
agents/test_coverage_agent.py
------------------------------
Phase 2 agent: identifies test gaps and generates test stubs.

Model: Haiku — test stub generation is structured and does not need deep reasoning.
Fallback: heuristic file-pairing analysis.
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    TestCoverageResult, TestGap, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent


class TestCoverageAgent(BaseAgent[TestCoverageResult]):

    agent_name        = AgentName.TEST_COVERAGE
    output_model      = TestCoverageResult
    output_token_cap  = 5000   # stubs are verbose; allow more output than the default 4000

    system_prompt = (
        "You are a QA architect specialising in enterprise banking test strategy.\n"
        "Analyse the code diff against the existing test file list and produce:\n"
        "  • coverage_delta: estimated % change (negative = dropped coverage)\n"
        "  • uncovered_paths: new code paths without corresponding tests\n"
        "    Each entry: file_path, uncovered_path (method or branch), suggested_test (1 sentence)\n"
        "  • regression_risk: low | medium | high | critical\n"
        "  • generated_stubs: 1-2 SHORT test skeletons (method signature + 3 comment lines max)\n"
        "    Keep each stub under 8 lines. No imports, no class wrapper.\n\n"
        "Focus on: transaction logic, error paths, boundary conditions, idempotency, rollback.\n"
        "Output ONLY compact JSON. No prose."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        test_files   = context.get("test_files", [])
        coverage_pct = context.get("current_coverage_pct", "unknown")
        diff         = "\n\n".join(h.content for h in request.hunks)
        trimmed      = trim_diff_for_budget(diff, max_tokens_approx=2200)

        return (
            f"Repository: {request.repo_url}\n"
            f"Current test coverage: {coverage_pct}%\n"
            f"Existing test files (sample, max 20): {test_files[:20]}\n\n"
            f"CODE DIFF:\n{trimmed}"
        )

    def fallback_result(self, request: AnalysisRequest) -> TestCoverageResult:
        """Heuristic: flag source files with no paired test file."""
        gaps:      list[TestGap] = []
        test_paths = {h.file_path for h in request.hunks if _is_test_file(h.file_path)}

        for hunk in request.hunks:
            if _is_test_file(hunk.file_path):
                continue
            expected = _expected_test_path(hunk.file_path)
            if expected not in test_paths:
                gaps.append(TestGap(
                    file_path=hunk.file_path,
                    uncovered_path="All changed methods",
                    suggested_test=f"Create {expected} covering changed logic and error paths.",
                ))

        # Extract new method signatures from diff for stub hints
        new_methods = _extract_new_methods(request)
        stubs = [_stub_for_method(m) for m in new_methods[:3]]

        # Method-level unit-test scenario validation (deterministic).
        method_cov, scenario_summary, hollow = [], "", []
        try:
            from ingestion.unit_test_validation import validate as _validate_units, hollow_tests as _hollow
            method_cov, scenario_summary = _validate_units(request)
            hollow = _hollow(request)
        except Exception:   # pragma: no cover - never let it break coverage analysis
            pass

        # Risk also rises when many recommended scenarios are uncovered.
        missing_total = sum(len(m.missing_scenarios) for m in method_cov)
        risk = (
            RiskLevel.CRITICAL if len(gaps) > 5 else
            RiskLevel.HIGH     if len(gaps) > 2 or missing_total > 8 else
            RiskLevel.MEDIUM   if gaps or missing_total > 0 else
            RiskLevel.LOW
        )

        return TestCoverageResult(
            coverage_delta=-float(len(gaps) * 2),
            uncovered_paths=gaps,
            regression_risk=risk,
            generated_stubs=stubs,
            method_coverage=method_cov,
            scenario_summary=scenario_summary,
            hollow_tests=hollow,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

from ingestion.test_detect import is_test_file as _is_test_file


def _expected_test_path(src: str) -> str:
    if src.endswith(".java"):
        return src.replace("/main/", "/test/").replace(".java", "Test.java")
    if src.endswith(".py"):
        parts = src.rsplit("/", 1)
        return f"{parts[0]}/test_{parts[1]}" if len(parts) == 2 else f"test_{src}"
    if src.endswith(".ts") or src.endswith(".js"):
        return src.replace(".ts", ".spec.ts").replace(".js", ".spec.js")
    if src.endswith(".go"):
        return src.replace(".go", "_test.go")
    return f"test_{src}"


def _extract_new_methods(request: AnalysisRequest) -> list[str]:
    methods = []
    for hunk in request.hunks:
        for line in hunk.content.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            # Java: public/private/protected <type> methodName(
            m = re.search(r'(?:public|private|protected)\s+\w+\s+(\w+)\s*\(', line)
            if m:
                methods.append(m.group(1))
                continue
            # Python: def method_name(
            m = re.search(r'def\s+([a-z_]\w+)\s*\(', line)
            if m:
                methods.append(m.group(1))
    return list(dict.fromkeys(methods))   # deduplicate


def _stub_for_method(method_name: str) -> str:
    return (
        f"@Test\n"
        f"void test_{method_name}_shouldSucceedForValidInput() {{\n"
        f"    // Arrange\n"
        f"    // Act\n"
        f"    // Assert\n"
        f"}}"
    )
