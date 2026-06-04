"""
tests/conftest.py
------------------
Shared pytest fixtures available to all unit and integration tests.
"""
from __future__ import annotations
import os
import uuid
import pytest
from unittest.mock import MagicMock, patch

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk,
    SecurityResult, SecurityFinding, CodeAnalysisResult,
    RiskLevel, GateDecision,
)
from core.token_manager import TokenBudgetManager
from storage.report_store import InMemoryReportStore


# ── Database isolation (autouse, session-wide) ─────────────────────────────────
# Critical: without this, any test that calls create_app() / the report or
# feedback stores writes into the REAL data/*.db files and pollutes the live
# Insights dashboard with test artifacts. Redirect all on-disk stores to a
# throwaway temp dir before any test runs.
@pytest.fixture(scope="session", autouse=True)
def _isolate_databases(tmp_path_factory):
    d = tmp_path_factory.mktemp("ciaa_test_dbs")

    # Reports: default sqlite_path is "" so make_report_store falls back to this.
    import storage.report_store as rs
    rs._DEFAULT_SQLITE_PATH = str(d / "reports.db")

    # Feedback: its default path is non-empty, so pre-seed the singleton at temp.
    import governance.feedback_store as fb
    from governance.feedback_store import SQLiteFeedbackStore
    fb._store = SQLiteFeedbackStore(str(d / "feedback.db"))

    # Temporal store.
    import storage.temporal_store as ts
    ts._store = None
    ts.DEFAULT_DB_PATH = str(d / "temporal.db")

    # Drop cached settings + app globals so the temp stores take effect.
    from config.settings import get_settings
    get_settings.cache_clear()
    import api.app as app_mod
    app_mod._report_store = None
    app_mod._orchestrator = None
    app_mod._audit_logger = None
    os.environ.setdefault("SKIP_AUTH", "true")
    yield


# ── Synthetic diff fixtures ────────────────────────────────────────────────────

SQL_INJECTION_DIFF = """\
--- a/src/main/java/com/bank/payments/PaymentService.java
+++ b/src/main/java/com/bank/payments/PaymentService.java
@@ -42,6 +42,8 @@
 public class PaymentService {
+    String query = "SELECT * FROM accounts WHERE id = " + accountId;
+    String apiKey = "sk-prod-abc123secretkeyXYZ";
     validateAmount(amount);
"""

OPENAPI_BREAKING_DIFF = """\
--- a/api/openapi.yaml
+++ b/api/openapi.yaml
@@ -10,7 +10,6 @@
   /payments/{id}:
-    get:
-      summary: Get payment
     post:
       summary: Create payment
-  /accounts:
"""

PROTO_BREAKING_DIFF = """\
--- a/proto/payment.proto
+++ b/proto/payment.proto
@@ -5,7 +5,6 @@
 message Payment {
-  string payment_id = 1;
   double amount = 2;
   string currency = 3;
"""


@pytest.fixture
def sample_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://bitbucket.org/mybank/payments-service",
        source_ref="feature/JIRA-1234-fix",
        target_ref="main",
        hunks=[
            DiffHunk(
                file_path="src/main/java/com/bank/payments/PaymentService.java",
                language="java",
                additions=2,
                deletions=0,
                content=SQL_INJECTION_DIFF,
            ),
        ],
    )


@pytest.fixture
def openapi_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/mybank/api-gateway",
        source_ref="feature/api-change",
        target_ref="main",
        hunks=[
            DiffHunk(
                file_path="api/openapi.yaml",
                language="yaml",
                additions=0,
                deletions=3,
                content=OPENAPI_BREAKING_DIFF,
            ),
        ],
    )


@pytest.fixture
def proto_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.BRANCH,
        repo_url="https://github.com/mybank/payments-grpc",
        source_ref="feature/proto-change",
        target_ref="main",
        hunks=[
            DiffHunk(
                file_path="proto/payment.proto",
                language="protobuf",
                additions=0,
                deletions=1,
                content=PROTO_BREAKING_DIFF,
            ),
        ],
    )


@pytest.fixture
def zero_budget() -> TokenBudgetManager:
    """Budget manager with all agents set to 0 tokens — forces fallback path."""
    budgets = {agent: 0 for agent in [
        "code_analysis", "security", "dependency", "test_coverage",
        "interface", "risk", "remediation", "_reserve",
    ]}
    return TokenBudgetManager(request_id=str(uuid.uuid4()), custom_budgets=budgets)


@pytest.fixture
def full_budget() -> TokenBudgetManager:
    return TokenBudgetManager(request_id=str(uuid.uuid4()))


@pytest.fixture
def report_store() -> InMemoryReportStore:
    return InMemoryReportStore()


@pytest.fixture
def mock_anthropic_response():
    """Factory for mocking Anthropic API responses."""
    def _make(result_model):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=result_model.model_dump_json())]
        mock_resp.usage   = MagicMock(input_tokens=100, output_tokens=80)
        return mock_resp
    return _make
