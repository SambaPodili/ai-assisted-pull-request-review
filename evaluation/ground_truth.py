"""
evaluation/ground_truth.py
---------------------------
Ground-truth case definitions for Level 2 recall/precision tracking.

Each GroundTruthCase pairs a diff with the findings every correct
implementation MUST produce.  The diff texts are the canonical source;
tests/golden/test_golden.py imports them from here so there is a single
copy of each fixture.

Match hints
-----------
A finding "covers" a GroundTruthFinding when ALL of its match_hints
appear (case-insensitive) in the JSON-serialised text of the finding.
Keep hints specific enough to not match unrelated findings, but don't
over-specify (e.g. don't include line numbers that may drift).
"""
from __future__ import annotations
from pydantic import BaseModel


# ── Models ─────────────────────────────────────────────────────────────────────

class GroundTruthFinding(BaseModel):
    """One expected finding for a specific agent in a given diff scenario."""
    agent:       str        # agent key, e.g. "security", "secrets_entropy"
    category:    str        # human label, e.g. "sql_injection"
    match_hints: list[str]  # ALL must appear (case-insensitive) in serialised finding


class GroundTruthCase(BaseModel):
    """A diff with fully-known expected findings — the unit of Level 2 evaluation."""
    case_id:           str
    description:       str
    diff_text:         str
    expected_findings: list[GroundTruthFinding] = []
    # True → diff is intentionally clean; any finding returned is a false positive
    expected_clean:    bool = False


# ── Diff fixtures (shared with tests/golden/test_golden.py) ───────────────────

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

DROP_TABLE_DIFF = """\
--- /dev/null
+++ b/db/migrations/V20240601__remove_legacy_accounts.sql
@@ -0,0 +1,4 @@
+-- Remove legacy account table no longer used after migration
+DROP TABLE legacy_accounts;
+DROP TABLE legacy_account_history;
+COMMIT;
"""

SQL_INJECTION_DIFF = """\
--- a/src/payments/repository.py
+++ b/src/payments/repository.py
@@ -12,6 +12,8 @@
 class PaymentRepository:
+    def find_by_account(self, account_id):
+        return self.db.execute("SELECT * FROM payments WHERE account_id = '" + account_id + "'")
"""

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

TAINT_FLOW_DIFF = """\
--- a/src/api/search.py
+++ b/src/api/search.py
@@ -8,6 +8,10 @@
 from flask import request

+def search_transactions():
+    account_id = request.args.get('account_id')
+    return db.execute("SELECT * FROM transactions WHERE account_id = " + account_id).fetchall()
"""

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


# ── Canonical ground-truth cases ──────────────────────────────────────────────

JAVA_SQLI_DIFF = """\
--- a/src/main/java/dao/AccountDao.java
+++ b/src/main/java/dao/AccountDao.java
@@ -10,4 +10,8 @@
+    public List<Account> findByName(String name) {
+        String sql = "SELECT * FROM accounts WHERE name = '" + name + "'";
+        return jdbcTemplate.query(sql, rowMapper);
+    }
"""

JAVA_N_PLUS_1_DIFF = """\
--- a/src/main/java/svc/OrderService.java
+++ b/src/main/java/svc/OrderService.java
@@ -20,4 +20,10 @@
+    public List<OrderDto> getOrders(List<Long> ids) {
+        List<OrderDto> result = new ArrayList<>();
+        for (Long id : ids) {
+            Order o = orderRepository.findById(id);
+            result.add(map(o));
+        }
+        return result;
+    }
"""

JAVA_TAINT_DIFF = """\
--- a/src/main/java/api/SearchController.java
+++ b/src/main/java/api/SearchController.java
@@ -15,4 +15,9 @@
+    @GetMapping("/search")
+    public List<Row> search(@RequestParam String q) {
+        String sql = "SELECT * FROM t WHERE c LIKE '%" + q + "%'";
+        return jdbc.query(sql, mapper);
+    }
"""

JAVA_EMPTY_CATCH_DIFF = """\
--- a/src/main/java/svc/FeeService.java
+++ b/src/main/java/svc/FeeService.java
@@ -30,4 +30,9 @@
+    public int calc(int amount) {
+        try {
+            return compute(amount);
+        } catch (Exception e) {
+        }
+        return -1;
+    }
"""


GOLDEN_CASES: list[GroundTruthCase] = [

    GroundTruthCase(
        case_id="aws_secret",
        description="Hardcoded AWS credentials committed to source",
        diff_text=AWS_SECRET_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="secrets_entropy",
                category="hardcoded_aws_key",
                # kind="known_prefix" and value starts with "AKIAZ3"
                match_hints=["known_prefix"],
            ),
            GroundTruthFinding(
                agent="secrets_entropy",
                category="hardcoded_aws_secret",
                # the AWS *secret access key* one line below — caught by entropy;
                # variable name 'aws_secret_access_key' appears in the finding
                match_hints=["high_entropy", "aws_secret"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="drop_table",
        description="Destructive DROP TABLE in a SQL migration",
        diff_text=DROP_TABLE_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="schema_change",
                category="irreversible_ddl",
                match_hints=["drop_table"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="sql_injection",
        description="SQL injection via string concatenation in execute()",
        diff_text=SQL_INJECTION_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="security",
                category="sql_injection",
                match_hints=["cwe-89"],   # CWE-89 appears in SecurityFinding.cwe_id
            ),
        ],
    ),

    GroundTruthCase(
        case_id="removed_endpoint",
        description="Breaking removal of a REST endpoint",
        diff_text=REMOVED_ENDPOINT_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="interface",
                category="removed_rest_endpoint",
                # ContractBreak serialises with break_type and path containing "payments"
                match_hints=["removed", "payments"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="open_ingress",
        description="Terraform security group rule open to 0.0.0.0/0",
        diff_text=OPEN_INGRESS_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="iac_analysis",
                category="open_ingress",
                match_hints=["open_ingress"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="high_complexity",
        description="Highly complex function (cyclomatic complexity spike)",
        diff_text=HIGH_COMPLEXITY_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="ast_analysis",
                category="complexity_spike",
                match_hints=["complexity_spike"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="taint_flow",
        description="User-controlled request param flows into SQL execution",
        diff_text=TAINT_FLOW_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="taint_analysis",
                category="sql_taint",
                # TaintPath.sink.sink = "sql_query" serialised inside nested JSON
                match_hints=["sql_query"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="clean_refactor",
        description="Pure refactor with no security or structural risk",
        diff_text=CLEAN_REFACTOR_DIFF,
        expected_findings=[],
        expected_clean=True,
    ),

    # ── Java cases (the primary enterprise stack) ──────────────────────────────
    GroundTruthCase(
        case_id="java_sql_injection",
        description="Java: SQL built via string concat, executed through JdbcTemplate",
        diff_text=JAVA_SQLI_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="security",
                category="sql_injection_concat",
                match_hints=["cwe-89", "concat"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="java_n_plus_1",
        description="Java: repository.findById inside a for-loop (N+1 query)",
        diff_text=JAVA_N_PLUS_1_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="performance_impact",
                category="n_plus_1_query",
                match_hints=["n_plus_1"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="java_taint_flow",
        description="Java: @RequestParam flows into jdbc.query via concatenated SQL",
        diff_text=JAVA_TAINT_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="taint_analysis",
                category="request_to_sql",
                match_hints=["request_param", "sql_query"],
            ),
        ],
    ),

    GroundTruthCase(
        case_id="java_swallowed_exception",
        description="Java: empty catch block silently swallows the exception",
        diff_text=JAVA_EMPTY_CATCH_DIFF,
        expected_findings=[
            GroundTruthFinding(
                agent="maintainability",
                category="swallowed_exception",
                match_hints=["swallowed_exception"],
            ),
        ],
    ),
]


def get_case(case_id: str) -> GroundTruthCase | None:
    """Look up a case by its case_id."""
    return next((c for c in GOLDEN_CASES if c.case_id == case_id), None)
