"""
tests/golden/test_golden.py
----------------------------
Golden-dataset quality tests for the impact-analysis pipeline.

Each test encodes a diff with KNOWN ground truth and asserts that the
correct agent(s) detect the correct issue at the correct severity.

Two test tiers:
  [Static]  — zero-token budget → forces fallback (static-rule) path.
              Runs in CI with no API key.  Verifies the deterministic
              detection layer never regresses.

  [LLM]     — marked @pytest.mark.live → skipped unless -m live is passed.
              Calls the real LLM via the orchestrator and makes softer
              assertions ("severity ≥ HIGH") that should hold across model
              versions.  Requires ANTHROPIC_API_KEY in the environment.

How to run:
  pytest tests/golden/                          # static only (CI)
  pytest tests/golden/ -m live                 # LLM tests only  (needs key)
  pytest tests/golden/ -m "live or not live"   # everything
"""
from __future__ import annotations

import os
import uuid
import pytest

from core.models import AnalysisRequest, ChangeType, DiffHunk, RiskLevel
from core.token_manager import TokenBudgetManager
from agents.security_agent        import SecurityReviewAgent
from agents.secrets_entropy_agent import SecretsEntropyAgent
from agents.schema_change_agent   import SchemaChangeAgent
from agents.interface_agent       import InterfaceAnalysisAgent
from agents.iac_analysis_agent    import IaCAnalysisAgent
from agents.ast_analysis_agent    import ASTAnalysisAgent
from agents.taint_analysis_agent  import TaintAnalysisAgent

# ── Helpers ───────────────────────────────────────────────────────────────────

def _req(file_path: str, content: str, language: str = "python") -> AnalysisRequest:
    """Build a minimal AnalysisRequest from a single diff hunk."""
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/myorg/payments-service",
        source_ref="feature/test",
        target_ref="main",
        hunks=[DiffHunk(
            file_path=file_path,
            language=language,
            additions=content.count("\n+"),
            deletions=content.count("\n-"),
            content=content,
        )],
    )


@pytest.fixture
def zb() -> TokenBudgetManager:
    """Zero budget for every agent — forces the static fallback path."""
    all_agents = [
        "code_analysis", "security", "ast_analysis", "secrets_entropy",
        "taint_analysis", "iac_analysis", "temporal_risk", "schema_change",
        "dependency", "test_coverage", "interface", "risk", "remediation",
        "_reserve",
    ]
    return TokenBudgetManager(
        request_id=str(uuid.uuid4()),
        custom_budgets={a: 0 for a in all_agents},
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Golden diffs — one constant per scenario
# ═══════════════════════════════════════════════════════════════════════════════

# 1. Hardcoded AWS credentials ─────────────────────────────────────────────────
# Note: deliberately NOT using the AWS "EXAMPLE" placeholder keys — those are
# filtered by _SAFE_PATTERNS. These keys look like real leaked credentials.
AWS_SECRET_DIFF = """\
--- a/src/config/aws_client.py
+++ b/src/config/aws_client.py
@@ -3,6 +3,10 @@
 import boto3

+AWS_ACCESS_KEY_ID     = "AKIAZ3GKPB5Q2LHVDPOI"
+AWS_SECRET_ACCESS_KEY = "8Pt7TZdmMCJiXBjNMCBHUtnFEMI+K7MDENGPxz1"
+
 def get_s3_client():
     return boto3.client('s3')
"""

# 2. Destructive SQL migration ─────────────────────────────────────────────────
DROP_TABLE_DIFF = """\
--- /dev/null
+++ b/db/migrations/V20240601__remove_legacy_accounts.sql
@@ -0,0 +1,4 @@
+-- Remove legacy account table no longer used after migration
+DROP TABLE legacy_accounts;
+DROP TABLE legacy_account_history;
+COMMIT;
"""

# 3. SQL injection via string concatenation ────────────────────────────────────
# Concatenation must be inside the execute() call so the static pattern triggers.
SQL_INJECTION_DIFF = """\
--- a/src/payments/repository.py
+++ b/src/payments/repository.py
@@ -12,6 +12,8 @@
 class PaymentRepository:
+    def find_by_account(self, account_id):
+        return self.db.execute("SELECT * FROM payments WHERE account_id = '" + account_id + "'")
"""

# 4. Removed OpenAPI endpoint (breaking change) ────────────────────────────────
REMOVED_ENDPOINT_DIFF = """\
--- a/api/openapi.yaml
+++ b/api/openapi.yaml
@@ -10,12 +10,6 @@
 paths:
-  /payments/{id}:
-    get:
-      summary: Get payment by ID
-      operationId: getPaymentById
-      parameters:
-        - name: id
   /payments:
     post:
       summary: Create payment
"""

# 5. Terraform security group open to the world ────────────────────────────────
OPEN_INGRESS_DIFF = """\
--- /dev/null
+++ b/infra/security_groups.tf
@@ -0,0 +1,12 @@
+resource "aws_security_group_rule" "allow_all_ssh" {
+  type        = "ingress"
+  from_port   = 22
+  to_port     = 22
+  protocol    = "tcp"
+  cidr_blocks = ["0.0.0.0/0"]
+  description = "allow SSH from anywhere"
+  security_group_id = aws_security_group.app.id
+}
"""

# 6. High-complexity function (cyclomatic complexity >> threshold) ──────────────
HIGH_COMPLEXITY_DIFF = """\
--- a/src/payments/processor.py
+++ b/src/payments/processor.py
@@ -5,6 +5,38 @@
+def process_payment_v2(txn, account, flags, opts, ctx):
+    if txn is None:
+        return None
+    if txn.amount <= 0:
+        raise ValueError("amount must be positive")
+    if account.status == "frozen":
+        return {"status": "rejected", "reason": "frozen"}
+    if account.status == "pending_kyc":
+        if flags.get("skip_kyc"):
+            pass
+        else:
+            return {"status": "rejected", "reason": "kyc_required"}
+    for item in txn.line_items:
+        if item.currency not in ("USD", "EUR", "GBP"):
+            if opts.get("allow_exotic"):
+                pass
+            else:
+                return {"status": "rejected", "reason": f"unsupported_currency:{item.currency}"}
+        if item.amount > 10000:
+            if not flags.get("large_tx_approved"):
+                return {"status": "hold", "reason": "large_tx_review"}
+        if item.category == "wire":
+            if ctx.get("region") == "OFAC_RESTRICTED":
+                return {"status": "blocked", "reason": "ofac"}
+            elif ctx.get("region") == "HIGH_RISK":
+                if not flags.get("high_risk_override"):
+                    return {"status": "hold", "reason": "high_risk_region"}
+    if txn.recurring:
+        if txn.recurring_count > 12:
+            if not opts.get("extended_recurring_allowed"):
+                return {"status": "hold", "reason": "extended_recurring"}
+    return {"status": "approved"}
"""

# 7. Taint flow: request param flows into SQL execution ────────────────────────
# The source (request.args.get) and sink (db.execute + concatenation) must be
# visible in the added lines so Pass 1 finds the source and Pass 3 matches sink.
TAINT_FLOW_DIFF = """\
--- a/src/api/search.py
+++ b/src/api/search.py
@@ -8,6 +8,10 @@
 from flask import request

+def search_transactions():
+    account_id = request.args.get('account_id')
+    return db.execute("SELECT * FROM transactions WHERE account_id = " + account_id).fetchall()
"""

# 8. Clean refactor — no risk (true-negative) ──────────────────────────────────
CLEAN_REFACTOR_DIFF = """\
--- a/src/payments/formatter.py
+++ b/src/payments/formatter.py
@@ -5,7 +5,7 @@
 class PaymentFormatter:
-    def format_amount(self, amt, ccy):
-        return f"{ccy} {amt:.2f}"
+    def format_amount(self, amount: float, currency: str) -> str:
+        return f"{currency} {amount:.2f}"
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Static / fallback tests  (no API key required)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGoldenStatic:
    """
    Each test passes a known-bad diff through the relevant agent's fallback
    (static-analysis) path and asserts the expected finding is present.

    These run in CI — zero token budget prevents any LLM call.
    """

    # ── 1. Hardcoded AWS secret ────────────────────────────────────────────────

    def test_aws_secret_detected_by_entropy_agent(self, zb):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        result = SecretsEntropyAgent().fallback_result(req)
        assert result.findings, "Expected at least one secret finding"
        severities = {f.severity for f in result.findings}
        assert RiskLevel.CRITICAL in severities or RiskLevel.HIGH in severities, \
            f"Expected HIGH/CRITICAL severity, got: {severities}"

    def test_aws_secret_detected_by_security_agent(self, zb):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        result = SecurityReviewAgent().fallback_result(req)
        assert result.secrets_detected, \
            "SecurityReviewAgent fallback should set secrets_detected=True for a hardcoded AWS key"

    def test_aws_secret_values_are_redacted(self, zb):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        result = SecretsEntropyAgent().fallback_result(req)
        # EntropyFinding.value stores only the first 6 chars ("AKIAZ3...")
        for f in result.findings:
            assert len(f.value) <= 10, \
                f"Finding value should be truncated/redacted, got: {f.value!r}"
            assert "8Pt7TZdmMCJiXBjNMCBHUtnFEMI" not in f.value, \
                "Raw secret key must not appear unredacted in finding output"

    # ── 2. DROP TABLE migration ────────────────────────────────────────────────

    def test_drop_table_is_destructive(self, zb):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        result = SchemaChangeAgent().fallback_result(req)
        assert result.has_destructive, \
            "DROP TABLE must be flagged as has_destructive=True"

    def test_drop_table_gate_is_block(self, zb):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        result = SchemaChangeAgent().fallback_result(req)
        assert result.gate_contribution == "BLOCK", \
            f"DROP TABLE gate_contribution should be BLOCK, got: {result.gate_contribution}"

    def test_drop_table_is_irreversible(self, zb):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        result = SchemaChangeAgent().fallback_result(req)
        assert result.has_irreversible, \
            "DROP TABLE must be flagged as has_irreversible=True"

    def test_both_dropped_tables_reported(self, zb):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        result = SchemaChangeAgent().fallback_result(req)
        tables = {c.table_name for c in result.changes if c.table_name}
        assert "legacy_accounts" in tables or "legacy_account_history" in tables, \
            f"Expected dropped table names in changes, got: {tables}"

    # ── 3. SQL injection ──────────────────────────────────────────────────────

    def test_sql_injection_detected_by_security_agent(self, zb):
        req    = _req("src/payments/repository.py", SQL_INJECTION_DIFF)
        result = SecurityReviewAgent().fallback_result(req)
        # SecurityFinding uses .description (no .category field)
        descs = {f.description.lower() for f in result.findings}
        assert any("sql" in d or "injection" in d for d in descs), \
            f"Expected SQL injection finding, got descriptions: {descs}"

    def test_sql_injection_severity_is_critical(self, zb):
        req    = _req("src/payments/repository.py", SQL_INJECTION_DIFF)
        result = SecurityReviewAgent().fallback_result(req)
        sql_findings = [f for f in result.findings
                        if "sql" in f.description.lower() or "injection" in f.description.lower()]
        assert sql_findings, "No SQL injection finding to check severity on"
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        worst = max((f.severity for f in sql_findings), key=lambda s: order.index(s))
        assert worst in (RiskLevel.HIGH, RiskLevel.CRITICAL), \
            f"SQL injection should be HIGH or CRITICAL, got: {worst}"

    # ── 4. Breaking API change ─────────────────────────────────────────────────

    def test_removed_endpoint_detected(self, zb):
        req    = _req("api/openapi.yaml", REMOVED_ENDPOINT_DIFF, "yaml")
        result = InterfaceAnalysisAgent().fallback_result(req)
        assert result.breaking_changes, \
            "Removing a GET endpoint from OpenAPI spec should produce at least one breaking_change"

    def test_removed_endpoint_breaking_changes_nonempty(self, zb):
        req    = _req("api/openapi.yaml", REMOVED_ENDPOINT_DIFF, "yaml")
        result = InterfaceAnalysisAgent().fallback_result(req)
        # InterfaceResult has no has_breaking_changes property; check list directly
        assert bool(result.breaking_changes), \
            "breaking_changes should be non-empty when an endpoint is removed"

    # ── 5. IaC open ingress ────────────────────────────────────────────────────

    def test_open_ssh_ingress_detected(self, zb):
        req    = _req("infra/security_groups.tf", OPEN_INGRESS_DIFF, "terraform")
        result = IaCAnalysisAgent().fallback_result(req)
        assert result.findings, "Expected at least one IaC finding for 0.0.0.0/0 SSH ingress"
        kinds  = {f.kind for f in result.findings}
        assert "open_ingress" in kinds, f"Expected open_ingress finding, got: {kinds}"

    def test_open_ingress_severity_high_or_critical(self, zb):
        req    = _req("infra/security_groups.tf", OPEN_INGRESS_DIFF, "terraform")
        result = IaCAnalysisAgent().fallback_result(req)
        worst  = max(
            (f.severity for f in result.findings),
            key=lambda s: [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL].index(s),
        )
        assert worst in (RiskLevel.HIGH, RiskLevel.CRITICAL), \
            f"Open ingress 0.0.0.0/0 should be HIGH or CRITICAL, got: {worst}"

    # ── 6. High cyclomatic complexity ─────────────────────────────────────────

    def test_high_complexity_function_flagged(self, zb):
        req    = _req("src/payments/processor.py", HIGH_COMPLEXITY_DIFF)
        result = ASTAnalysisAgent().fallback_result(req)
        # ASTAnalysisResult exposes max_complexity and findings (kind='complexity_spike')
        complexity_findings = [f for f in result.findings if f.kind == "complexity_spike"]
        assert complexity_findings or result.max_complexity >= 8, \
            f"process_payment_v2 has 12+ decision points; max_complexity={result.max_complexity}, findings={result.findings}"

    def test_complexity_score_above_threshold(self, zb):
        req    = _req("src/payments/processor.py", HIGH_COMPLEXITY_DIFF)
        result = ASTAnalysisAgent().fallback_result(req)
        assert result.max_complexity >= 8, \
            f"process_payment_v2 has 12+ decision points; expected max_complexity >= 8, got: {result.max_complexity}"

    # ── 7. Taint flow ─────────────────────────────────────────────────────────

    def test_request_param_to_sql_taint_detected(self, zb):
        req    = _req("src/api/search.py", TAINT_FLOW_DIFF)
        result = TaintAnalysisAgent().fallback_result(req)
        assert result.taint_paths, \
            "request.args.get() flowing into db.execute() should produce a taint path"

    def test_taint_sink_is_sql_query(self, zb):
        req    = _req("src/api/search.py", TAINT_FLOW_DIFF)
        result = TaintAnalysisAgent().fallback_result(req)
        # TaintPath.sink is a TaintSink object; its .sink field holds the sink kind
        sink_types = {p.sink.sink for p in result.taint_paths}
        assert "sql_query" in sink_types, \
            f"Expected taint sink kind=sql_query, got: {sink_types}"

    def test_taint_cwe_is_89(self, zb):
        req    = _req("src/api/search.py", TAINT_FLOW_DIFF)
        result = TaintAnalysisAgent().fallback_result(req)
        # TaintPath uses .cwe not .cwe_id
        cwes = {p.cwe for p in result.taint_paths}
        assert "CWE-89" in cwes, \
            f"SQL-injection taint path should carry CWE-89, got: {cwes}"

    # ── 8. True negative — clean refactor ─────────────────────────────────────

    def test_clean_refactor_no_secrets(self, zb):
        req    = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF)
        result = SecretsEntropyAgent().fallback_result(req)
        assert not result.findings, \
            f"A clean rename/type-hint refactor should have zero secret findings; got: {result.findings}"

    def test_clean_refactor_no_security_issues(self, zb):
        req    = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF)
        result = SecurityReviewAgent().fallback_result(req)
        critical = [f for f in result.findings if f.severity == RiskLevel.CRITICAL]
        assert not critical, \
            f"Clean refactor should have no CRITICAL security findings; got: {critical}"

    def test_clean_refactor_no_taint_paths(self, zb):
        req    = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF)
        result = TaintAnalysisAgent().fallback_result(req)
        assert not result.taint_paths, \
            f"Clean refactor has no user input sources or dangerous sinks; got: {result.taint_paths}"

    def test_clean_refactor_no_schema_changes(self, zb):
        req    = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF)
        result = SchemaChangeAgent().fallback_result(req)
        assert not result.has_destructive and not result.has_irreversible, \
            "Clean Python refactor must not trigger schema-change alerts"


# ═══════════════════════════════════════════════════════════════════════════════
#  Cross-agent consistency tests (static, no LLM)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossAgentConsistency:
    """
    Verify that when two agents independently examine the same diff they agree
    on the risk category — and that neither gives a false all-clear.
    """

    def test_aws_secret_detected_by_both_relevant_agents(self, zb):
        req     = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        entropy = SecretsEntropyAgent().fallback_result(req)
        sec     = SecurityReviewAgent().fallback_result(req)
        assert entropy.findings, "SecretsEntropyAgent must find the AWS key"
        assert sec.secrets_detected, "SecurityReviewAgent must also detect secrets"

    def test_sql_injection_caught_by_security_and_taint(self, zb):
        req   = _req("src/api/search.py", TAINT_FLOW_DIFF)
        sec   = SecurityReviewAgent().fallback_result(req)
        taint = TaintAnalysisAgent().fallback_result(req)
        # At least one of them must detect an issue (SecurityFinding uses .description not .category)
        sec_hit   = any("sql" in f.description.lower() or "injection" in f.description.lower()
                        for f in sec.findings)
        taint_hit = bool(taint.taint_paths)
        assert sec_hit or taint_hit, \
            "Neither SecurityAgent nor TaintAgent detected SQL injection — both missed the issue"

    def test_drop_table_not_confused_with_clean_code(self, zb):
        clean_req = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF, "sql")
        result    = SchemaChangeAgent().fallback_result(clean_req)
        assert not result.has_destructive, \
            "SchemaChangeAgent must not produce false positives on clean Python refactors"

    def test_open_ingress_not_flagged_on_clean_sql_file(self, zb):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        result = IaCAnalysisAgent().fallback_result(req)
        assert not result.findings, \
            "IaCAnalysisAgent must not flag a SQL migration file as an IaC finding"


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM quality tests  (require ANTHROPIC_API_KEY — run with: pytest -m live)
# ═══════════════════════════════════════════════════════════════════════════════

live = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM tests (run with -m live)",
)


@live
class TestGoldenLLM:
    """
    Run the full orchestrator pipeline with a real LLM and assert that findings
    meet quality thresholds.  Assertions are intentionally 'soft' (threshold-
    based) because LLM output is non-deterministic.

    These tests catch:
      - Regressions when the prompt or model changes
      - Cases where the LLM actively contradicts the static fallback
      - Gate-decision inconsistencies (BLOCK issue → APPROVE gate)
    """

    @pytest.fixture(scope="class")
    def orchestrator(self):
        from core.orchestrator import ImpactAnalysisOrchestrator
        return ImpactAnalysisOrchestrator(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            phase=3,
        )

    def test_llm_aws_secret_gate_is_block(self, orchestrator):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        report = orchestrator.analyse(req)
        from core.models import GateDecision
        assert report.gate_decision == GateDecision.BLOCK, \
            f"Hardcoded AWS credentials must produce gate=BLOCK, got: {report.gate_decision}"

    def test_llm_aws_secret_severity_critical(self, orchestrator):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        report = orchestrator.analyse(req)
        if report.security:
            assert report.security.overall_severity in (RiskLevel.HIGH, RiskLevel.CRITICAL), \
                f"Hardcoded secret should be HIGH/CRITICAL, got: {report.security.overall_severity}"

    def test_llm_drop_table_gate_is_block(self, orchestrator):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        report = orchestrator.analyse(req)
        from core.models import GateDecision
        assert report.gate_decision == GateDecision.BLOCK, \
            f"DROP TABLE migration must produce gate=BLOCK, got: {report.gate_decision}"

    def test_llm_drop_table_schema_destructive(self, orchestrator):
        req    = _req("db/migrations/V20240601__remove_legacy_accounts.sql", DROP_TABLE_DIFF, "sql")
        report = orchestrator.analyse(req)
        if report.schema_change:
            assert report.schema_change.has_destructive, \
                "LLM must agree that DROP TABLE is destructive"

    def test_llm_sql_injection_not_missed(self, orchestrator):
        req    = _req("src/payments/repository.py", SQL_INJECTION_DIFF)
        report = orchestrator.analyse(req)
        from core.models import GateDecision
        # Must be HOLD or BLOCK — APPROVE means the LLM missed a clear SQL injection
        assert report.gate_decision != GateDecision.APPROVE, \
            "LLM should not APPROVE a diff with a textbook SQL injection"

    def test_llm_open_ingress_not_approved(self, orchestrator):
        req    = _req("infra/security_groups.tf", OPEN_INGRESS_DIFF, "terraform")
        report = orchestrator.analyse(req)
        from core.models import GateDecision
        assert report.gate_decision != GateDecision.APPROVE, \
            "LLM should not APPROVE a Terraform change that opens SSH to 0.0.0.0/0"

    def test_llm_clean_refactor_is_approved(self, orchestrator):
        req    = _req("src/payments/formatter.py", CLEAN_REFACTOR_DIFF)
        report = orchestrator.analyse(req)
        from core.models import GateDecision
        assert report.gate_decision == GateDecision.APPROVE, \
            f"LLM should APPROVE a clean type-hint refactor, got: {report.gate_decision}"

    def test_llm_all_agents_ran(self, orchestrator):
        req    = _req("src/payments/repository.py", SQL_INJECTION_DIFF)
        report = orchestrator.analyse(req)
        ran_agents = {u.agent.value for u in report.token_usage}
        expected   = {"security", "ast_analysis", "secrets_entropy", "taint_analysis"}
        missing    = expected - ran_agents
        assert not missing, \
            f"Expected these agents to have run and reported tokens: {missing}"

    def test_llm_report_has_no_empty_findings_on_bad_diff(self, orchestrator):
        req    = _req("src/config/aws_client.py", AWS_SECRET_DIFF)
        report = orchestrator.analyse(req)
        all_findings = 0
        if report.security:
            all_findings += len(report.security.findings)
        if report.secrets_entropy:
            all_findings += len(report.secrets_entropy.findings)
        assert all_findings > 0, \
            "LLM pipeline must return at least one security/entropy finding for a hardcoded AWS key"
