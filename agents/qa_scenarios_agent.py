"""
agents/qa_scenarios_agent.py
------------------------------
QA Scenarios Agent — generates a structured list of testing scenarios
that QA / testing teams should cover for a given code change.

Fallback (zero LLM tokens):
  Analyses the diff heuristically to produce scenarios per change category:
    • Security-related changes  → security test scenarios
    • API / endpoint changes    → API contract & regression scenarios
    • Database schema changes   → data integrity & rollback scenarios
    • Complex logic changes     → edge-case & boundary scenarios
    • New dependencies          → integration & compatibility scenarios
    • Config / env changes      → environment validation scenarios
    • UI / template changes     → functional & cross-browser scenarios
    • Test file changes         → test quality review scenarios

LLM path:
  Produces rich, context-aware scenarios with step-by-step procedures,
  expected results, and automation hints for each change in the diff.
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    QAScenariosResult, QAScenario, QAScenarioType, RiskLevel,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt
from ingestion.language_registry import lang_meta, test_framework_hint, linter_hint


# ── Heuristic classifiers ─────────────────────────────────────────────────────

_SECURITY_PATTERNS = re.compile(
    r'password|secret|token|api_?key|auth|credential|crypt|hash|jwt|oauth|'
    r'permission|privilege|role|acl|cors|csrf|xss|sql|inject|sanitize',
    re.I,
)
_API_PATTERNS = re.compile(
    r'@(get|post|put|delete|patch|router\.|app\.|route)|'
    r'openapi|swagger|endpoint|rest|graphql|grpc|\.proto|'
    r'status_?code|response_?model|request_?body',
    re.I,
)
_DB_PATTERNS = re.compile(
    r'migrate|migration|alter table|drop table|create table|'
    r'add column|drop column|schema|index|foreign key|constraint|'
    r'\.sql|alembic|flyway|liquibase|\.migration\.',
    re.I,
)
_CONFIG_PATTERNS = re.compile(
    r'\.env|settings\.|config\.|\.yaml|\.yml|\.json|\.toml|'
    r'environment|feature_flag|toggle|helm|dockerfile|docker-compose',
    re.I,
)
_PERF_PATTERNS = re.compile(
    r'cache|redis|memcache|async|await|concurrent|thread|pool|'
    r'batch|queue|kafka|pubsub|index|query|n\+1|pagination|limit',
    re.I,
)
_UI_PATTERNS = re.compile(
    r'\.html|\.jsx?|\.tsx?|\.vue|\.svelte|template|render|component|'
    r'style|css|scss|layout|form|input|button|modal',
    re.I,
)


def _categorise_hunk(file_path: str, content: str) -> set[QAScenarioType]:
    cats: set[QAScenarioType] = set()
    text = file_path + "\n" + content
    if _SECURITY_PATTERNS.search(text):
        cats.add(QAScenarioType.SECURITY)
    if _API_PATTERNS.search(text):
        cats.add(QAScenarioType.API)
    if _DB_PATTERNS.search(text):
        cats.add(QAScenarioType.DATA)
    if _CONFIG_PATTERNS.search(text):
        cats.add(QAScenarioType.INTEGRATION)
    if _PERF_PATTERNS.search(text):
        cats.add(QAScenarioType.PERFORMANCE)
    if _UI_PATTERNS.search(text):
        cats.add(QAScenarioType.FUNCTIONAL)
    # Always include regression for any changed file
    cats.add(QAScenarioType.REGRESSION)
    # Edge-case if many lines changed
    added = content.count("\n+")
    if added > 10:
        cats.add(QAScenarioType.EDGE_CASE)
    return cats


# ── Template library ──────────────────────────────────────────────────────────

# ── Production-grade scenario detail (preconditions / acceptance / skeleton) ────

def _lang_for(files: list[str]) -> str:
    f = (files[0] if files else "").lower()
    if f.endswith((".java", ".kt")): return "java"
    if f.endswith(".py"):            return "python"
    if f.endswith((".ts", ".tsx", ".js", ".jsx")): return "js"
    if f.endswith(".go"):            return "go"
    return ""


_PRECONDITIONS = {
    QAScenarioType.SECURITY:    ["A user/principal WITHOUT the required role/permission exists",
                                 "Auth is enforced (not disabled in the test profile)"],
    QAScenarioType.API:         ["The service is deployed with the new contract",
                                 "A client using the PREVIOUS contract version is available"],
    QAScenarioType.DATA:        ["A representative dataset (and one schema-mismatch record) is seeded",
                                 "A rollback/teardown path is available"],
    QAScenarioType.INTEGRATION: ["The external dependency is mocked/stubbed",
                                 "Failure modes (timeout, 5xx, malformed) can be simulated"],
    QAScenarioType.PERFORMANCE: ["A baseline latency/throughput measurement exists",
                                 "Load inputs at 1k / 10k / 100k are prepared"],
    QAScenarioType.REGRESSION:  ["The exact input that triggered the original defect is known"],
}
_DEFAULT_PRE = ["The unit under test is instantiated with valid dependencies (mocks where external)"]


def _acceptance(stype: QAScenarioType, expected: str) -> list[str]:
    base = [expected] if expected else ["The documented expected result is produced"]
    extra = {
        QAScenarioType.SECURITY:    ["Unauthorized access is rejected (401/403) and logged",
                                     "No injection payload alters behaviour or reaches a sink"],
        QAScenarioType.EDGE_CASE:   ["No unhandled exception on null/empty/boundary inputs"],
        QAScenarioType.API:         ["Previous-version clients still receive a valid response",
                                     "Required-field validation returns a clear 4xx error"],
        QAScenarioType.DATA:        ["Round-trip causes no data loss or precision change",
                                     "Rollback restores the prior state"],
        QAScenarioType.REGRESSION:  ["The test fails on the pre-fix code and passes on the fix"],
        QAScenarioType.PERFORMANCE: ["Latency/memory stays within the agreed budget vs baseline"],
    }.get(stype, [])
    return base + extra


def _skeleton(title: str, stype: QAScenarioType, files: list[str]) -> str:
    lang = _lang_for(files)
    safe = re.sub(r'[^a-zA-Z0-9]+', '_', title).strip('_').lower()[:60] or "scenario"
    if lang == "java":
        return ("@Test\n"
                f"void {safe}() {{\n"
                "    // Arrange — set up inputs, mocks, and preconditions\n"
                "    // Act — invoke the changed method\n"
                "    // Assert — verify the acceptance criteria\n"
                "    // assertThrows(...) for the error/security path\n"
                "}")
    if lang == "python":
        return (f"def test_{safe}():\n"
                "    # Arrange\n    ...\n"
                "    # Act\n    ...\n"
                "    # Assert — acceptance criteria\n    assert ...\n"
                "    # with pytest.raises(...):  # error/security path\n")
    if lang == "js":
        return (f"it('{title[:60]}', () => {{\n"
                "  // Arrange\n  // Act\n  // Assert\n"
                "  expect(result).toEqual(expected);\n"
                "  // expect(() => fn()).toThrow();  // error path\n"
                "});")
    if lang == "go":
        cap = "".join(w.capitalize() for w in safe.split("_"))[:60] or "Scenario"
        return (f"func Test{cap}(t *testing.T) {{\n"
                "    // Arrange / Act\n"
                "    // Assert\n"
                "    if got != want {\n        t.Fatalf(\"got %v want %v\", got, want)\n    }\n}")
    return ("// Arrange the unit under test and inputs\n"
            "// Act: invoke the change\n"
            "// Assert: verify each acceptance criterion (incl. the error path)")


def _make_scenario(idx: int, title: str, stype: QAScenarioType, priority: RiskLevel,
                   description: str, steps: list[str], expected: str,
                   files: list[str], hint: str = "") -> QAScenario:
    return QAScenario(
        id=f"QA-{idx:03d}",
        title=title,
        type=stype,
        priority=priority,
        description=description,
        steps=steps,
        expected_result=expected,
        affected_files=files,
        automation_hint=hint,
        preconditions=_PRECONDITIONS.get(stype, _DEFAULT_PRE),
        acceptance_criteria=_acceptance(stype, expected),
        test_skeleton=_skeleton(title, stype, files),
    )


def _build_fallback_scenarios(request: AnalysisRequest) -> list[QAScenario]:
    scenarios: list[QAScenario] = []
    idx = 1

    for hunk in request.hunks:
        fp      = hunk.file_path
        content = hunk.content
        cats    = _categorise_hunk(fp, content)
        meta    = lang_meta(hunk.language)
        fw_hint = test_framework_hint(hunk.language)
        lint_hint = linter_hint(hunk.language)

        # Build language-aware security hint
        sec_tools = []
        if fw_hint:
            sec_tools.append(fw_hint)
        if lint_hint:
            sec_tools.append(lint_hint)
        sec_tool_str = "; ".join(sec_tools) if sec_tools else "OWASP ZAP + static analysis"

        if QAScenarioType.SECURITY in cats:
            lang_risks = "; ".join(meta.security_notes[:3]) if meta.security_notes else "general OWASP top 10"
            scenarios.append(_make_scenario(
                idx, f"Security validation — {fp}",
                QAScenarioType.SECURITY, RiskLevel.CRITICAL,
                f"Verify that security controls in {fp} ({meta.display}) are correctly implemented. "
                f"Language-specific risks: {lang_risks}.",
                [
                    "Review the change for hardcoded secrets or credentials",
                    "Test authentication/authorization flows affected by the change",
                    "Verify input validation and sanitization are in place",
                    f"Test {meta.display}-specific attack vectors: {', '.join(meta.security_notes[:2]) if meta.security_notes else 'injection, XSS, path traversal'}",
                    "Check that sensitive data is not logged or exposed in error responses",
                ],
                "No credentials exposed; all auth checks pass; attack vectors rejected with "
                "appropriate HTTP status codes and safe error messages.",
                [fp],
                sec_tool_str,
            ))
            idx += 1

        if QAScenarioType.API in cats:
            scenarios.append(_make_scenario(
                idx, f"API contract test — {fp}",
                QAScenarioType.API, RiskLevel.HIGH,
                f"Verify that the API contract exposed by {fp} is correct, backwards-compatible, "
                "and handles all edge cases.",
                [
                    "Send valid requests and verify response shape and status codes",
                    "Send requests with missing required fields → expect 400/422",
                    "Send requests with out-of-range values → expect 400/422",
                    "Test authentication enforcement — unauthenticated request → 401",
                    "Test authorization — insufficient-role request → 403",
                    "Verify pagination / limit-offset parameters work correctly",
                    "Test concurrent requests for race conditions",
                ],
                "Correct HTTP status codes; response payloads match OpenAPI schema; "
                "no 500 errors under normal inputs.",
                [fp],
                fw_hint or "Schemathesis for property-based API testing",
            ))
            idx += 1

        if QAScenarioType.DATA in cats:
            db_hint = fw_hint or "Use a staging DB clone; run migration on a data snapshot"
            scenarios.append(_make_scenario(
                idx, f"Data integrity & migration test — {fp}",
                QAScenarioType.DATA, RiskLevel.CRITICAL,
                f"Verify that the database migration in {fp} ({meta.display}) runs cleanly "
                "in both directions and does not corrupt existing data.",
                [
                    "Apply migration on a copy of production data snapshot",
                    "Verify row counts before and after migration",
                    "Check that nullable/default constraints are correct for existing rows",
                    "Run application smoke tests after migration",
                    "Roll back the migration and verify data is restored correctly",
                    "Test migration under concurrent writes (if applicable)",
                ],
                "Migration completes without errors; row counts are consistent; rollback restores "
                "previous schema; application functions normally post-migration.",
                [fp],
                db_hint,
            ))
            idx += 1

        if QAScenarioType.PERFORMANCE in cats:
            perf_hint = fw_hint or "Locust / k6 for load testing; language-appropriate profiler"
            scenarios.append(_make_scenario(
                idx, f"Performance & load test — {fp}",
                QAScenarioType.PERFORMANCE, RiskLevel.MEDIUM,
                f"Ensure the change in {fp} ({meta.display}) does not introduce latency regressions "
                "or excessive resource consumption under load.",
                [
                    "Run baseline benchmark before the change (p50/p95/p99 latency)",
                    "Apply change and repeat benchmark under the same load profile",
                    "Test with 10× expected peak load to identify breaking points",
                    "Monitor memory, CPU, and DB query counts during the test",
                    "Check for N+1 query patterns with query logging enabled",
                ],
                "p99 latency within ±20% of baseline; no memory leaks; DB query count does not "
                "grow proportionally with input size.",
                [fp],
                perf_hint,
            ))
            idx += 1

        if QAScenarioType.FUNCTIONAL in cats:
            ui_hint = fw_hint or "Playwright or Cypress for E2E; axe-core for accessibility"
            scenarios.append(_make_scenario(
                idx, f"Functional UI test — {fp}",
                QAScenarioType.FUNCTIONAL, RiskLevel.MEDIUM,
                f"Verify that the UI change in {fp} ({meta.display}) renders correctly and all "
                "user-facing interactions work as expected.",
                [
                    "Test the happy path end-to-end in Chrome, Firefox, and Safari",
                    "Test with keyboard navigation only (accessibility)",
                    "Test at mobile viewport (375px) and desktop (1440px)",
                    "Submit forms with valid data → verify success",
                    "Submit forms with invalid data → verify inline validation messages",
                    "Test loading states and error states",
                ],
                "All interactions work correctly across browsers; no layout breakage at any "
                "tested viewport; accessibility score ≥ 90 (Lighthouse).",
                [fp],
                ui_hint,
            ))
            idx += 1

        if QAScenarioType.INTEGRATION in cats:
            int_hint = fw_hint or "Docker Compose test environment; integration test suite"
            scenarios.append(_make_scenario(
                idx, f"Integration & configuration test — {fp}",
                QAScenarioType.INTEGRATION, RiskLevel.HIGH,
                f"Verify that the configuration or dependency change in {fp} ({meta.display}) "
                "integrates correctly with all downstream services.",
                [
                    "Deploy to staging with the new configuration",
                    "Verify all dependent services can still communicate",
                    "Run end-to-end smoke test suite against staging",
                    "Check health endpoints of all services report 'ok'",
                    "Verify rollback procedure: revert config and confirm services recover",
                ],
                "All service health checks pass; no broken integrations; rollback restores "
                "normal operation within the defined SLA.",
                [fp],
                int_hint,
            ))
            idx += 1

        if QAScenarioType.EDGE_CASE in cats:
            edge_hint = fw_hint or "Property-based testing (e.g. Hypothesis, QuickCheck, PropEr)"
            scenarios.append(_make_scenario(
                idx, f"Edge-case & boundary test — {fp}",
                QAScenarioType.EDGE_CASE, RiskLevel.MEDIUM,
                f"The change in {fp} ({meta.display}) modifies substantial logic "
                f"({hunk.additions} lines added). Cover boundary conditions and unusual inputs.",
                [
                    "Test with empty inputs and null/None values",
                    "Test with maximum allowed values (int overflow, max string length)",
                    "Test with minimum allowed values (zero, empty list, negative numbers)",
                    "Test with Unicode / special characters in string fields",
                    "Test concurrency: two requests modifying the same record simultaneously",
                    "Simulate partial failures (network timeout mid-operation)",
                ],
                "No unhandled exceptions; appropriate error messages returned; "
                "data remains consistent after any failure.",
                [fp],
                edge_hint,
            ))
            idx += 1

        # Regression scenario for every changed file
        reg_hint = fw_hint or "Run existing test suite for this module"
        scenarios.append(_make_scenario(
            idx, f"Regression test — {fp}",
            QAScenarioType.REGRESSION, RiskLevel.MEDIUM,
            f"Confirm that the change in {fp} ({meta.display}) has not broken any existing "
            "functionality that was working before.",
            [
                "Run the full existing test suite and verify all tests pass",
                "Run tests specific to the module containing the changed file",
                "Execute any manual test cases that are not yet automated for this area",
                "Verify that the feature's normal use cases still behave identically",
            ],
            "All pre-existing tests pass; no regressions in related features.",
            [fp],
            reg_hint,
        ))
        idx += 1

    return scenarios


# ── Agent ──────────────────────────────────────────────────────────────────────

class QAScenariosAgent(BaseAgent[QAScenariosResult]):
    agent_name    = AgentName.QA_SCENARIOS
    output_model  = QAScenariosResult
    output_token_cap = 4000

    system_prompt = """You are a senior QA architect. Given a code diff, produce a JSON list of
testing scenarios that a QA / testing team must execute before this change can be released.

For each scenario provide:
  - id: "QA-001", "QA-002", ... (sequential)
  - title: concise (≤12 words)
  - type: one of functional | security | regression | edge_case | integration | performance | api | data
  - priority: one of CRITICAL | HIGH | MEDIUM | LOW
  - description: why this scenario exists and what risk it covers (2-3 sentences)
  - steps: numbered list of concrete test steps (3-8 steps)
  - expected_result: what a PASS looks like
  - affected_files: list of file paths from the diff this scenario targets
  - automation_hint: which framework / tool to use for automation (1 sentence, optional)

Rules:
  - Security changes and DB migrations → at least one CRITICAL scenario each
  - API surface changes → at least one HIGH API contract scenario
  - Cover the happy path AND at least one negative/failure path per scenario
  - Do NOT repeat the same scenario for different files — merge them
  - Aim for 5-12 scenarios total; quality over quantity
  - coverage_areas: list of high-level areas covered (e.g. ["auth", "payments API", "user data"])
  - summary: 1-2 sentences summarising the QA scope

Respond ONLY with valid JSON matching the QAScenariosResult schema."""

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        languages = sorted({lang_meta(h.language).display for h in request.hunks})
        parts = [
            f"Repo: {request.repo_url}",
            f"Branch: {request.source_ref} → {request.target_ref}",
            f"Languages: {', '.join(languages)}",
            "",
        ]
        parts.append(format_hunks_for_prompt(request.hunks, max_chars_per_hunk=2000))
        return "\n".join(parts)

    def fallback_result(self, request: AnalysisRequest) -> QAScenariosResult:
        scenarios = _build_fallback_scenarios(request)
        critical  = sum(1 for s in scenarios if s.priority == RiskLevel.CRITICAL)
        high      = sum(1 for s in scenarios if s.priority == RiskLevel.HIGH)
        areas     = sorted({s.type.value for s in scenarios})
        return QAScenariosResult(
            scenarios=scenarios,
            total_scenarios=len(scenarios),
            critical_count=critical,
            high_count=high,
            coverage_areas=areas,
            summary=(
                f"{len(scenarios)} test scenarios generated covering "
                f"{len(areas)} area(s): {', '.join(areas)}."
            ),
        )
