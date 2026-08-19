"""
tests/test_qa_scenarios.py
--------------------------
Regression tests for the QA-scenario fallback classifier.

Driven by real user feedback: a plain CREATE TABLE / DDL file was being
classified as a SECURITY concern (purely because the word "sql" appeared) and
the agent then emitted nonsensical "test authentication flows -> 401 / injection
attack vectors" steps for a file that has no HTTP layer at all.

A DDL file must be a DATA concern only; genuine injection / auth / token code
must still be flagged SECURITY.
"""
from __future__ import annotations

import uuid

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk, QAScenarioType as T,
    QAScenariosResult, QAScenario, RiskLevel,
)
from core.token_manager import TokenBudgetManager
from agents.qa_scenarios_agent import (
    _categorise_hunk, _build_fallback_scenarios, _skeleton, QAScenariosAgent,
)


# ── classifier ────────────────────────────────────────────────────────────────

def test_plain_ddl_is_data_not_security():
    ddl = "CREATE TABLE PDN_VRM_TBL ( id NUMBER PRIMARY KEY, name VARCHAR2(100) );"
    cats = _categorise_hunk("db/PDN_VRM_DDL_TBL_CREATION_008.sql", ddl)
    assert T.DATA in cats, "DDL should be a DATA / migration concern"
    assert T.SECURITY not in cats, "a plain DDL must NOT be flagged SECURITY"


def test_sql_injection_code_is_security():
    code = 'String q = "SELECT * FROM u WHERE n=" + name;  // sql injection risk'
    cats = _categorise_hunk("src/Dao.java", code)
    assert T.SECURITY in cats


def test_camelcase_security_identifiers_still_match():
    # word boundaries would miss these; substring matching must keep them
    assert T.SECURITY in _categorise_hunk("src/Login.java",
                                          "if(!user.hasPermission()) throw new AuthException();")
    assert T.SECURITY in _categorise_hunk("src/Token.java", "String t = getUserToken();")


# ── generated scenarios ─────────────────────────────────────────────────────────

def _ddl_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://bitbucket.org/mybank/db-scripts",
        source_ref="feature/new-table",
        target_ref="main",
        hunks=[DiffHunk(
            file_path="db/PDN_VRM_DDL_TBL_CREATION_008.sql",
            language="sql",
            additions=12,
            deletions=0,
            content="CREATE TABLE PDN_VRM_TBL ( id NUMBER PRIMARY KEY, name VARCHAR2(100) );",
        )],
    )


def test_ddl_fallback_has_no_http_auth_steps():
    scenarios = _build_fallback_scenarios(_ddl_request())
    types = {s.type for s in scenarios}
    assert T.DATA in types, "DDL change must yield a data-integrity/migration scenario"
    assert T.SECURITY not in types, "DDL change must not yield a security scenario"

    # belt-and-braces: no scenario should reference HTTP-auth status codes
    joined = " ".join(
        " ".join(s.steps) + " " + s.expected_result for s in scenarios
    ).lower()
    assert "401" not in joined and "403" not in joined, \
        "DDL scenarios must not mention HTTP auth status codes"


# ── test_skeleton generation (runnable test stubs) ──────────────────────────────

_SKELETON_CASES = [
    # (language, file_path, hunk_content, expected symbol, expected filename)
    ("java", "src/main/java/com/bank/PaymentService.java",
     "+public class PaymentService {\n+    public void processPayment(String token) {\n+    }\n+}\n",
     "processPayment", "ProcessPaymentTest.java"),
    ("python", "payments/service.py",
     "+def process_payment(token):\n+    return True\n",
     "process_payment", "test_service.py"),
    ("javascript", "src/payment.js",
     "+function processPayment(token) {\n+  return true\n+}\n",
     "processPayment", "payment.test.js"),
    ("typescript", "src/payment.ts",
     "+export function processPayment(token: string): boolean {\n+  return true\n+}\n",
     "processPayment", "payment.test.ts"),
    ("go", "internal/payment/service.go",
     "+func ProcessPayment(token string) bool {\n+\treturn true\n+}\n",
     "ProcessPayment", "service_test.go"),
    ("csharp", "Payments/PaymentService.cs",
     "+public bool ProcessPayment(string token) {\n+    return true;\n+}\n",
     "ProcessPayment", "ProcessPaymentTests.cs"),
    ("kotlin", "src/main/kotlin/PaymentService.kt",
     "+fun processPayment(token: String): Boolean {\n+    return true\n+}\n",
     "processPayment", "ProcessPaymentTest.kt"),
]


def test_skeleton_uses_real_symbol_and_correct_filename():
    for lang, path, content, expected_symbol, expected_filename in _SKELETON_CASES:
        hunk = DiffHunk(file_path=path, language=lang, additions=3, deletions=0, content=content)
        code, filename = _skeleton("Some scenario title", T.FUNCTIONAL, [path], hunk)
        assert expected_symbol in code, (
            f"{lang}: expected real symbol {expected_symbol!r} in skeleton, got:\n{code}"
        )
        assert filename == expected_filename, f"{lang}: expected filename {expected_filename!r}, got {filename!r}"


def test_skeleton_falls_back_to_generic_for_unsupported_language():
    hunk = DiffHunk(file_path="script.pl", language="perl", additions=2, deletions=0,
                     content="+sub process_payment {\n+}\n")
    code, filename = _skeleton("Some scenario", T.FUNCTIONAL, ["script.pl"], hunk)
    assert filename == "test_scenario.txt"
    assert "Arrange" in code and "Assert" in code


def test_skeleton_with_no_hunk_uses_extension_fallback_and_title():
    code, filename = _skeleton("Verify widget renders", T.FUNCTIONAL, ["src/widget.py"], None)
    assert filename == "test_widget.py"
    assert "def test_verify_widget_renders" in code


# ── QAScenariosAgent.run() LLM-path backfill ────────────────────────────────────

def test_llm_path_backfills_empty_test_skeleton(monkeypatch):
    """The LLM never produces test_skeleton (not asked for in the prompt) —
    QAScenariosAgent.run() must backfill it from the real diff, same as the
    fallback path."""
    req = AnalysisRequest(
        request_id=str(uuid.uuid4()), change_type=ChangeType.PR,
        repo_url="https://github.com/bank/payments", source_ref="f", target_ref="main",
        hunks=[DiffHunk(file_path="payments/service.py", language="python", additions=2, deletions=0,
                         content="+def process_payment(token):\n+    return True\n")],
    )
    llm_result = QAScenariosResult(
        scenarios=[QAScenario(id="QA-001", title="Verify payment processing", type=T.FUNCTIONAL,
                               priority=RiskLevel.HIGH, description="d",
                               affected_files=["payments/service.py"])],
        total_scenarios=1,
    )

    import agents.qa_scenarios_agent as mod
    monkeypatch.setattr(mod.BaseAgent, "run", lambda self, request, budget, context=None: llm_result)

    agent  = QAScenariosAgent(api_key="sk-test")
    budget = TokenBudgetManager(req.request_id, {"qa_scenarios": 999999, "_reserve": 0})
    result = agent.run(req, budget)

    s = result.scenarios[0]
    assert s.test_skeleton, "test_skeleton must be backfilled on the LLM path"
    assert s.test_skeleton_filename == "test_service.py"
    assert "process_payment" in s.test_skeleton


def test_llm_path_does_not_overwrite_existing_skeleton(monkeypatch):
    """If the LLM (or a future prompt change) DOES return a skeleton, the
    backfill must not clobber it."""
    req = AnalysisRequest(
        request_id=str(uuid.uuid4()), change_type=ChangeType.PR,
        repo_url="https://github.com/bank/payments", source_ref="f", target_ref="main",
        hunks=[DiffHunk(file_path="payments/service.py", language="python", additions=2, deletions=0,
                         content="+def process_payment(token):\n+    return True\n")],
    )
    llm_result = QAScenariosResult(
        scenarios=[QAScenario(id="QA-001", title="t", type=T.FUNCTIONAL, priority=RiskLevel.HIGH,
                               description="d", affected_files=["payments/service.py"],
                               test_skeleton="# already provided by the LLM")],
        total_scenarios=1,
    )

    import agents.qa_scenarios_agent as mod
    monkeypatch.setattr(mod.BaseAgent, "run", lambda self, request, budget, context=None: llm_result)

    agent  = QAScenariosAgent(api_key="sk-test")
    budget = TokenBudgetManager(req.request_id, {"qa_scenarios": 999999, "_reserve": 0})
    result = agent.run(req, budget)

    assert result.scenarios[0].test_skeleton == "# already provided by the LLM"
