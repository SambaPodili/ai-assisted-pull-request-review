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
    • Build/infra/config files, and files that are already tests themselves,
      are excluded — see _is_scenario_worthy() and is_test_file()

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
from agents.base_agent import BaseAgent, format_hunks_for_prompt, format_user_priorities
from ingestion.language_registry import lang_meta, test_framework_hint, linter_hint, detect_language, LANGUAGES
from ingestion.test_detect import is_test_file


def _is_scenario_worthy(language: str) -> bool:
    """True only for a language that's real, testable source — not a log/data
    file (detect_language falls back to the literal string "unknown"), and
    not an "orphan" language id like "xml" that EXT_TO_LANG maps to but that
    has no LangMeta entry (lang_meta() silently degrades those to the same
    unknown-fallback metadata, so checking `language == "unknown"` alone
    misses them — pom.xml, web.xml, etc. would otherwise pass through and get
    a nonsensical "write a unit test for this build file" scenario), and not
    a real, recognized infra/config language (yaml/json/toml/dockerfile/hcl —
    LangMeta.is_infra) either, since those aren't unit-tested the same way."""
    if language == "unknown" or language not in LANGUAGES:
        return False
    return not lang_meta(language).is_infra


# ── Heuristic classifiers ─────────────────────────────────────────────────────

# NOTE: deliberately does NOT match a bare "sql" — a plain CREATE TABLE / DDL
# file is a DATA concern (handled by _DB_PATTERNS), not a security one. Real
# SQL-injection risk is still caught via "inject"/"sanitize"/"sql injection".
# Substring (not word-bounded) so camelCase identifiers like hasPermission /
# getUserToken / AuthException are still recognised.
_SECURITY_PATTERNS = re.compile(
    r'password|secret|token|api_?key|auth|credential|crypt|hash|jwt|oauth|'
    r'permission|privilege|role|acl|cors|csrf|xss|inject|sanitize|'
    r'sql\s*injection|dynamic\s*sql',
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
    """Crude extension-based fallback — only used when no DiffHunk is available
    (e.g. the requirement-verification scenario, which has no source file)."""
    f = (files[0] if files else "").lower()
    if f.endswith(".java"): return "java"
    if f.endswith(".kt"):   return "kotlin"
    if f.endswith(".py"):   return "python"
    if f.endswith((".ts", ".tsx")): return "typescript"
    if f.endswith((".js", ".jsx")): return "javascript"
    if f.endswith(".go"):   return "go"
    if f.endswith(".cs"):   return "csharp"
    return ""


def _best_symbol(hunk):
    """Best-match changed function/method/class name for this hunk, or None.
    Prefers an added (not removed) function/method over a class, since that's
    almost always what the scenario actually needs to exercise."""
    if hunk is None:
        return None
    try:
        from ingestion.symbol_extractor import extract_changed_symbols
        syms = extract_changed_symbols(hunk)
    except Exception:
        return None
    pool = [s for s in syms if s.is_added] or syms
    for kind in ("method", "function", "class"):
        for s in pool:
            if s.kind == kind:
                return s
    return pool[0] if pool else None


def _file_stem(files: list[str]) -> str:
    f = (files[0] if files else "").rsplit("/", 1)[-1]
    return f.rsplit(".", 1)[0] if "." in f else (f or "scenario")


def _pascal(name: str) -> str:
    """PascalCase a symbol/title for a class name. Preserves existing internal
    casing for already-camelCase symbols (processPayment -> ProcessPayment)
    instead of lowercasing them — only snake_case/space/dash-separated input
    gets each word capitalized."""
    cleaned = re.sub(r'[^a-zA-Z0-9_]+', '_', name)
    parts = [w for w in cleaned.split('_') if w]
    if not parts:
        return "Scenario"
    return "".join(w[:1].upper() + w[1:] for w in parts)[:60] or "Scenario"


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


def _skeleton(title: str, stype: QAScenarioType, files: list[str], hunk=None) -> tuple[str, str]:
    """
    Return (code, filename) for a ready-to-run test stub. Real templates
    (imports, class/package wrapper, naming convention) for the highest-value
    tier of languages for an enterprise shop: Java, Python, JavaScript,
    TypeScript, Go, C#, Kotlin. Everything else falls back to a generic,
    honestly-labelled comment-only skeleton — same as before this upgrade.

    Uses the real changed symbol name (via ingestion.symbol_extractor) when
    one can be found in the hunk, instead of a name derived from the scenario
    title. Package/namespace/import paths are inherently best-effort from a
    diff hunk alone, so they're always flagged with an explicit TODO comment
    rather than silently guessed.
    """
    lang   = hunk.language if hunk is not None else _lang_for(files)
    symbol = _best_symbol(hunk)
    safe   = re.sub(r'[^a-zA-Z0-9]+', '_', title).strip('_').lower()[:60] or "scenario"
    name   = symbol.name if symbol else safe
    stem   = _file_stem(files)
    pascal = _pascal(name)

    if lang == "java":
        cls = f"{pascal}Test"
        return (
            "// TODO: adjust package to match the class under test\n"
            "import org.junit.jupiter.api.Test;\n"
            "import static org.junit.jupiter.api.Assertions.*;\n\n"
            f"class {cls} {{\n"
            "    @Test\n"
            f"    void {safe}() {{\n"
            "        // Arrange — set up inputs, mocks, and preconditions\n"
            f"        // Act — invoke {name}(...)\n"
            "        // Assert — verify the acceptance criteria\n"
            "        // assertThrows(...) for the error/security path\n"
            "    }\n"
            "}",
            f"{cls}.java",
        )

    if lang == "kotlin":
        cls = f"{pascal}Test"
        return (
            "// TODO: adjust package to match the class under test\n"
            "import org.junit.jupiter.api.Test\n\n"
            f"class {cls} {{\n"
            "    @Test\n"
            f"    fun `{title[:60]}`() {{\n"
            "        // Arrange\n"
            f"        // Act — invoke {name}(...)\n"
            "        // Assert\n"
            "    }\n"
            "}",
            f"{cls}.kt",
        )

    if lang == "python":
        import_line = f"from {stem} import {name}  # TODO: adjust import path\n\n" if symbol else ""
        return (
            "import pytest\n"
            f"{import_line}"
            f"def test_{safe}():\n"
            "    # Arrange\n    ...\n"
            f"    # Act — invoke {name}(...)\n    ...\n"
            "    # Assert — acceptance criteria\n    assert ...\n"
            "    # with pytest.raises(...):  # error/security path\n",
            f"test_{stem}.py",
        )

    if lang == "javascript":
        import_line = f"// const {{ {name} }} = require('./{stem}')  // TODO: adjust import path\n" if symbol else ""
        return (
            f"{import_line}"
            f"it('{title[:60]}', () => {{\n"
            "  // Arrange\n  // Act\n  // Assert\n"
            "  expect(result).toEqual(expected);\n"
            "  // expect(() => fn()).toThrow();  // error path\n"
            "});",
            f"{stem}.test.js",
        )

    if lang == "typescript":
        import_line = f"import {{ {name} }} from './{stem}'  // TODO: adjust import path\n" if symbol else ""
        return (
            f"{import_line}"
            f"it('{title[:60]}', () => {{\n"
            "  // Arrange\n  // Act\n  // Assert\n"
            "  expect(result).toEqual(expected)\n"
            "  // expect(() => fn()).toThrow()  // error path\n"
            "})",
            f"{stem}.test.ts",
        )

    if lang == "go":
        return (
            "package your_package // TODO: match the package of the file under test\n\n"
            "import \"testing\"\n\n"
            f"func Test{pascal}(t *testing.T) {{\n"
            f"    // Arrange / Act — invoke {name}(...)\n"
            "    // Assert\n"
            "    if got != want {\n        t.Fatalf(\"got %v want %v\", got, want)\n    }\n}",
            f"{stem}_test.go",
        )

    if lang == "csharp":
        cls = f"{pascal}Tests"
        return (
            "// TODO: adjust namespace to match the class under test\n"
            "using Xunit;\n\n"
            f"public class {cls} {{\n"
            "    [Fact]\n"
            f"    public void {pascal}() {{\n"
            "        // Arrange\n"
            f"        // Act — invoke {name}(...)\n"
            "        // Assert\n"
            "    }\n"
            "}",
            f"{cls}.cs",
        )

    return (
        "// Arrange the unit under test and inputs\n"
        "// Act: invoke the change\n"
        "// Assert: verify each acceptance criterion (incl. the error path)",
        "test_scenario.txt",
    )


def _make_scenario(idx: int, title: str, stype: QAScenarioType, priority: RiskLevel,
                   description: str, steps: list[str], expected: str,
                   files: list[str], hint: str = "", hunk=None) -> QAScenario:
    skeleton, skeleton_filename = _skeleton(title, stype, files, hunk)
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
        test_skeleton=skeleton,
        test_skeleton_filename=skeleton_filename,
    )


def _build_fallback_scenarios(request: AnalysisRequest) -> list[QAScenario]:
    scenarios: list[QAScenario] = []
    idx = 1

    # Requirement-aware scenarios from uploaded functional documents.
    fdocs = (request.metadata or {}).get("functional_docs") or []
    for doc in fdocs[:5]:
        name = (doc.get("name") or "spec") if isinstance(doc, dict) else "spec"
        scenarios.append(_make_scenario(
            idx, f"Requirement verification — {name}", QAScenarioType.FUNCTIONAL, RiskLevel.HIGH,
            f"Verify the change satisfies the documented functional requirements in '{name}' "
            "and does not contradict any stated acceptance criteria.",
            ["Map each changed behaviour to a requirement / acceptance criterion in the document",
             "Write a test per acceptance criterion that the change touches",
             "Confirm no documented requirement is broken or left unimplemented",
             "Flag any change that has NO corresponding requirement (scope creep)"],
            "Every affected acceptance criterion has a passing test; no documented requirement regresses.",
            [name], hint="Trace tests to requirement IDs (e.g. @Tag/@Requirement annotations).",
        ))
        idx += 1

    for hunk in request.hunks:
        fp      = hunk.file_path
        content = hunk.content
        if not _is_scenario_worthy(hunk.language):
            # Not recognized as testable source at all (logs, .csv, .md, .txt,
            # ...), or a build/infra/config file (pom.xml, yaml/json/toml,
            # Dockerfile, ...) — there's no code or schema here to write a
            # unit test against, and keyword regexes below would otherwise
            # false-positive on log/prose content that merely mentions "auth"
            # or "migration". See _is_scenario_worthy for the full reasoning.
            continue
        if is_test_file(fp):
            # Don't suggest "write a unit test for this" for a file that's
            # already a test — the test_coverage agent is what reports on
            # *other* changed files lacking a corresponding test; recommending
            # a test for a test is meaningless.
            continue
        cats    = _categorise_hunk(fp, content)
        meta    = lang_meta(hunk.language)
        fw_hint = test_framework_hint(hunk.language)
        lint_hint = linter_hint(hunk.language)
        # Bind `hunk` for every _make_scenario() call in this iteration, so each
        # scenario's test_skeleton is generated from the real changed symbol.
        _make = lambda *a, **kw: _make_scenario(*a, hunk=hunk, **kw)

        # Build language-aware security hint
        sec_tools = []
        if fw_hint:
            sec_tools.append(fw_hint)
        if lint_hint:
            sec_tools.append(lint_hint)
        sec_tool_str = "; ".join(sec_tools) if sec_tools else "OWASP ZAP + static analysis"

        if QAScenarioType.SECURITY in cats:
            lang_risks = "; ".join(meta.security_notes[:3]) if meta.security_notes else "general OWASP top 10"
            is_api = QAScenarioType.API in cats
            attack_vectors = ', '.join(meta.security_notes[:2]) if meta.security_notes else 'injection, XSS, path traversal'
            sec_steps = ["Review the change for hardcoded secrets or credentials"]
            if is_api:
                sec_steps.append("Test authentication/authorization flows affected by the change")
            sec_steps += [
                "Verify input validation and sanitization are in place",
                f"Test {meta.display}-specific attack vectors: {attack_vectors}",
                "Check that sensitive data is not logged or exposed in error responses",
            ]
            sec_expected = (
                "No credentials exposed; all auth checks pass; attack vectors rejected with "
                "appropriate HTTP status codes and safe error messages."
                if is_api else
                "No credentials exposed; inputs are validated/sanitised; attack vectors rejected "
                "and sensitive data is never logged or leaked in errors."
            )
            scenarios.append(_make(
                idx, f"Security validation — {fp}",
                QAScenarioType.SECURITY, RiskLevel.CRITICAL,
                f"Verify that security controls in {fp} ({meta.display}) are correctly implemented. "
                f"Language-specific risks: {lang_risks}.",
                sec_steps,
                sec_expected,
                [fp],
                sec_tool_str,
            ))
            idx += 1

        if QAScenarioType.API in cats:
            scenarios.append(_make(
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
            scenarios.append(_make(
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
            scenarios.append(_make(
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
            scenarios.append(_make(
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
            scenarios.append(_make(
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
            scenarios.append(_make(
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
        scenarios.append(_make(
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
        # Include uploaded functional/requirement docs so scenarios trace to them.
        fdocs = (request.metadata or {}).get("functional_docs") or []
        if fdocs:
            parts.append("\n--- FUNCTIONAL REQUIREMENTS (verify the change against these) ---")
            for doc in fdocs[:5]:
                if isinstance(doc, dict):
                    parts.append(f"\n# {doc.get('name','spec')}\n{(doc.get('text') or '')[:6000]}")
            parts.append("\nGenerate scenarios that verify each affected requirement and flag any "
                         "requirement left unimplemented or contradicted.")
        parts.append(format_user_priorities(request.user_instructions))
        return "\n".join(parts)

    def run(self, request: AnalysisRequest, budget, context: dict[str, Any] | None = None) -> QAScenariosResult:
        result = super().run(request, budget, context)
        # The LLM sees raw diff content in the prompt and sometimes proposes a
        # scenario for a file that isn't real testable source at all — a log
        # or data file whose CONTENT merely contains words like "auth" or
        # "migration" (no code there to write a real test against), a
        # build/infra/config file like pom.xml (see _is_scenario_worthy), or a
        # file that's already a test itself. Drop any scenario whose every
        # affected_file fails all three checks — the same gates the fallback
        # path applies.
        try:
            for s in result.scenarios:
                # A scenario that named both a test file and a real file would
                # otherwise still display "…crsEnquiryResponseProcessorTest.java".
                non_test = [f for f in (s.affected_files or []) if not is_test_file(f)]
                if non_test:
                    s.affected_files = non_test
            result.scenarios = [
                s for s in result.scenarios
                if not s.affected_files
                or any(_is_scenario_worthy(detect_language(f)) and not is_test_file(f) for f in s.affected_files)
            ]
            result.total_scenarios = len(result.scenarios)
            priorities = [getattr(s.priority, "value", s.priority) for s in result.scenarios]
            result.critical_count  = sum(1 for p in priorities if p == "critical")
            result.high_count      = sum(1 for p in priorities if p == "high")
        except Exception:
            pass
        # The LLM path never asks for test_skeleton (see system_prompt above) — it
        # comes back empty on every LLM-produced scenario. Backfill it here with
        # the same per-language template used by the fallback path, so a real
        # (non-fallback) analysis still gets ready-to-run stubs.
        try:
            hunks_by_file = {h.file_path: h for h in request.hunks}
            for s in result.scenarios:
                if s.test_skeleton:
                    continue
                hunk = next((hunks_by_file[f] for f in s.affected_files if f in hunks_by_file), None)
                s.test_skeleton, s.test_skeleton_filename = _skeleton(s.title, s.type, s.affected_files, hunk)
        except Exception:
            pass
        return result

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
