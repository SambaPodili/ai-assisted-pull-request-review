"""
tests/unit/test_all.py
-----------------------
Unit tests for all core modules.
No real Anthropic API calls — uses mocks and fallback paths.
"""
from __future__ import annotations
import uuid
import pytest
from unittest.mock import MagicMock, patch

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk, RiskLevel, GateDecision,
    CodeAnalysisResult, SecurityResult, SecurityFinding,
    DependencyResult, InterfaceResult, RiskResult,
)
from core.token_manager import (
    TokenBudgetManager, chunk_diff, select_hottest_chunk, trim_diff_for_budget, estimate_tokens,
)
from ingestion.diff_parser import parse_diff, detect_language, summarise_diff
from ingestion.webhook_parser import parse_bitbucket_webhook, parse_github_webhook
from agents.security_agent import SecurityReviewAgent, _derive_flags
from agents.code_analysis_agent import CodeAnalysisAgent
from agents.dependency_agent import DependencyMappingAgent, build_service_graph, _extract_changed_packages
from agents.test_coverage_agent import TestCoverageAgent, _is_test_file, _expected_test_path
from agents.interface_agent import InterfaceAnalysisAgent, _parse_openapi_diff, _parse_proto_diff
from agents.risk_agent import RiskAssessmentAgent
from agents.remediation_agent import RemediationAgent
from governance.compliance_checker import ComplianceChecker
from storage.report_store import InMemoryReportStore


# ═══════════════════════════════════════════════════════════════════════════════
#  Token Budget Manager
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenBudgetManager:

    def test_reserve_and_record(self):
        bm = TokenBudgetManager("t1", {"security": 5000, "_reserve": 1000})
        assert bm.check_and_reserve("security", 1000) is True
        bm.record_usage("security", 800, "claude-haiku-4-5-20251001")
        assert bm._agents["security"].used == 1000   # remains at reserved amount

    def test_exhausted_returns_false(self):
        bm = TokenBudgetManager("t2", {"security": 100, "_reserve": 0})
        assert bm.check_and_reserve("security", 500) is False

    def test_donate_transfers_budget(self):
        bm = TokenBudgetManager("t3", {"dependency": 3000, "risk": 1000, "_reserve": 0})
        bm._agents["dependency"].used = 200   # only used 200 of 3000
        bm.donate_unused("dependency", "risk")
        assert bm._agents["risk"].allocated > 1000

    def test_summary_structure(self):
        bm = TokenBudgetManager("t4", {"code_analysis": 2000, "_reserve": 500})
        summary = bm.summary()
        assert "total_allocated" in summary
        assert "per_agent"       in summary

    def test_model_selection(self):
        bm = TokenBudgetManager("t5")
        assert "sonnet" in bm.select_model("security").lower()
        assert "haiku"  in bm.select_model("code_analysis").lower()


# ═══════════════════════════════════════════════════════════════════════════════
#  Diff utilities
# ═══════════════════════════════════════════════════════════════════════════════

class TestDiffUtilities:

    # Need @@ markers so chunk_diff knows where to split
    LARGE_DIFF = "\n".join(
        "\n".join([f"@@ -{i*10},10 +{i*10},10 @@"] + [f"+line {j}" for j in range(i*10, i*10+10)])
        for i in range(50)
    )

    def test_chunk_diff_respects_max_lines(self):
        chunks = chunk_diff(self.LARGE_DIFF, max_lines=100)
        assert len(chunks) >= 2   # large diff must be split into multiple chunks

    def test_select_hottest_chunk(self):
        chunks = ["+a\n+b\n", "+x\n+y\n+z\n-a\n-b\n-c\n"]
        hot = select_hottest_chunk(chunks)
        assert hot == chunks[1]

    def test_trim_diff_returns_content(self):
        long_diff = "+" * 10000
        result    = trim_diff_for_budget(long_diff, max_tokens_approx=100)
        assert len(result) <= 100 * 4 + 50   # small slack for chunk boundaries

    def test_estimate_tokens(self):
        assert estimate_tokens("a" * 400) == 100


# ═══════════════════════════════════════════════════════════════════════════════
#  Diff Parser
# ═══════════════════════════════════════════════════════════════════════════════

class TestDiffParser:

    SAMPLE = """\
--- a/src/PaymentService.java
+++ b/src/PaymentService.java
@@ -1,3 +1,4 @@
+import java.sql.PreparedStatement;
 public class PaymentService {
-    // old comment
+    // new comment
 }
"""

    def test_parses_file_path(self):
        hunks = parse_diff(self.SAMPLE)
        assert len(hunks) == 1
        assert hunks[0].file_path == "src/PaymentService.java"

    def test_counts_additions_deletions(self):
        hunks = parse_diff(self.SAMPLE)
        assert hunks[0].additions == 2
        assert hunks[0].deletions == 1

    def test_detect_language(self):
        assert detect_language("foo/Bar.java") == "java"
        assert detect_language("service.py")   == "python"
        assert detect_language("api.yaml")     == "yaml"
        assert detect_language("msg.proto")    == "protobuf"
        assert detect_language("unknown.xyz")  == "unknown"

    def test_summarise_diff(self):
        hunks   = parse_diff(self.SAMPLE)
        summary = summarise_diff(hunks)
        assert summary["total_files"]     == 1
        assert "java" in summary["languages"]


# ═══════════════════════════════════════════════════════════════════════════════
#  Webhook Parser
# ═══════════════════════════════════════════════════════════════════════════════

class TestWebhookParser:

    def test_bitbucket_pr_created(self):
        payload = {
            "pullrequest": {
                "source":      {"branch": {"name": "feature/abc"}},
                "destination": {"branch": {"name": "develop"}},
                "id": 42, "title": "Fix payment",
                "author": {"display_name": "Dev"},
            },
            "repository": {"links": {"html": {"href": "https://bitbucket.org/bank/payments"}}},
        }
        req = parse_bitbucket_webhook("pullrequest:created", payload)
        assert req is not None
        assert req.source_ref == "feature/abc"
        assert req.target_ref == "develop"
        assert req.change_type == ChangeType.PR

    def test_github_pr_opened(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "head": {"ref": "feature/xyz", "sha": "abc123"},
                "base": {"ref": "main",        "sha": "def456"},
                "number": 7, "title": "Add feature",
                "user": {"login": "dev"},
            },
            "repository": {"html_url": "https://github.com/bank/api"},
        }
        req = parse_github_webhook("pull_request", payload)
        assert req is not None
        assert req.source_ref == "feature/xyz"
        assert req.target_ref == "main"

    def test_unsupported_event_returns_none(self):
        assert parse_bitbucket_webhook("repo:fork", {}) is None
        assert parse_github_webhook("ping", {})          is None

    def test_github_push_deletion_skipped(self):
        payload = {
            "before": "abc", "after": "0000000000000000000000000000000000000000",
            "ref": "refs/heads/feature/old",
            "repository": {"html_url": "https://github.com/bank/api"},
        }
        assert parse_github_webhook("push", payload) is None


# ═══════════════════════════════════════════════════════════════════════════════
#  Security Agent (fallback SAST)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecurityAgentFallback:

    def _make_req(self, diff_content: str) -> AnalysisRequest:
        return AnalysisRequest(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/test",
            source_ref="feature", target_ref="main",
            hunks=[DiffHunk(file_path="Service.java", language="java", additions=2, deletions=0, content=diff_content)],
        )

    def test_detects_sql_injection(self, zero_budget):
        agent  = SecurityReviewAgent(api_key="test")
        req    = self._make_req('+    rs = conn.executeQuery("SELECT * FROM accounts WHERE id = " + accountId);')
        result = agent.run(req, zero_budget)
        cwes   = [f.cwe_id for f in result.findings]
        assert "CWE-89" in cwes

    def test_detects_hardcoded_password(self, zero_budget):
        agent  = SecurityReviewAgent(api_key="test")
        req    = self._make_req('+    String password = "SuperSecret123";')
        result = agent.run(req, zero_budget)
        cwes   = [f.cwe_id for f in result.findings]
        assert "CWE-798" in cwes
        assert result.secrets_detected is True

    def test_detects_weak_crypto(self, zero_budget):
        agent  = SecurityReviewAgent(api_key="test")
        req    = self._make_req("+    MessageDigest md = MessageDigest.getInstance(MD5.class);")
        result = agent.run(req, zero_budget)
        cwes   = [f.cwe_id for f in result.findings]
        assert "CWE-327" in cwes

    def test_clean_code_no_findings(self, zero_budget):
        agent  = SecurityReviewAgent(api_key="test")
        req    = self._make_req("+    PreparedStatement ps = conn.prepareStatement(query);")
        result = agent.run(req, zero_budget)
        assert result.overall_severity == RiskLevel.LOW

    def test_compliance_flags_derived(self):
        findings = [SecurityFinding(
            file_path="A.java", line_range="1", severity=RiskLevel.CRITICAL,
            cwe_id="CWE-798", owasp_cat="A07", description="secret", remediation="fix"
        )]
        flags = _derive_flags(findings)
        assert any("PCI-DSS" in f for f in flags)
        assert any("MAS TRM" in f for f in flags)


# ═══════════════════════════════════════════════════════════════════════════════
#  Code Analysis Agent fallback
# ═══════════════════════════════════════════════════════════════════════════════

class TestCodeAnalysisAgentFallback:

    def test_large_churn_flagged_as_high(self, zero_budget):
        hunks = [DiffHunk(
            file_path=f"File{i}.java", language="java",
            additions=120, deletions=120, content="+x\n" * 120 + "-x\n" * 120,
        ) for i in range(3)]
        req    = AnalysisRequest(request_id=str(uuid.uuid4()), change_type=ChangeType.PR,
                                  repo_url="http://x", source_ref="a", target_ref="b", hunks=hunks)
        agent  = CodeAnalysisAgent(api_key="test")
        result = agent.run(req, zero_budget)
        assert result.fallback_used is True
        severities = [f.severity for f in result.findings]
        assert RiskLevel.HIGH in severities


# ═══════════════════════════════════════════════════════════════════════════════
#  Dependency Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestDependencyAgent:

    def test_graph_traversal_blast_radius(self):
        graph   = build_service_graph({
            "payments-service":  ["auth-lib", "core-utils"],
            "accounts-service":  ["auth-lib"],
            "reporting-service": ["auth-lib"],
        })
        agent  = DependencyMappingAgent(api_key="test", service_graph=graph)
        result = agent.analyse_with_graph(["auth-lib"])
        assert "payments-service"  in result.affected_services
        assert "accounts-service"  in result.affected_services
        assert "reporting-service" in result.affected_services
        assert result.blast_radius_score > 0

    def test_extract_maven_packages(self):
        hunk = DiffHunk(
            file_path="pom.xml", language="xml", additions=1, deletions=0,
            content="+    <artifactId>spring-core</artifactId>\n",
        )
        req  = AnalysisRequest(request_id="x", change_type=ChangeType.PR,
                                repo_url="x", source_ref="a", target_ref="b", hunks=[hunk])
        pkgs = _extract_changed_packages(req)
        assert "spring-core" in pkgs

    def test_no_manifest_returns_empty(self, zero_budget):
        hunk = DiffHunk(file_path="Service.java", language="java", additions=1, deletions=0, content="+x\n")
        req  = AnalysisRequest(request_id="x", change_type=ChangeType.PR,
                                repo_url="x", source_ref="a", target_ref="b", hunks=[hunk])
        agent  = DependencyMappingAgent(api_key="test")
        result = agent.run(req, zero_budget)
        assert result.affected_services == []


# ═══════════════════════════════════════════════════════════════════════════════
#  Interface Analysis Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestInterfaceAgent:

    def test_openapi_path_removal_detected(self):
        breaks = _parse_openapi_diff(
            "--- a/api.yaml\n+++ b/api.yaml\n-/payments/{id}:\n+  post:\n",
            "api/openapi.yaml"
        )
        assert any(b.break_type == "removed" for b in breaks)

    def test_proto_field_removal_detected(self):
        breaks = _parse_proto_diff(
            "--- a/p.proto\n+++ b/p.proto\n-  string payment_id = 1;\n",
            "proto/payment.proto"
        )
        assert len(breaks) >= 1
        assert breaks[0].interface_type == "gRPC"

    def test_no_interface_files_returns_empty(self, zero_budget):
        hunk   = DiffHunk(file_path="Service.java", language="java", additions=1, deletions=0, content="+x\n")
        req    = AnalysisRequest(request_id="x", change_type=ChangeType.PR,
                                  repo_url="x", source_ref="a", target_ref="b", hunks=[hunk])
        agent  = InterfaceAnalysisAgent(api_key="test")
        result = agent.run(req, zero_budget)
        assert result.breaking_changes == []

    def test_fallback_uses_structural_scan(self, zero_budget):
        hunk   = DiffHunk(
            file_path="api/openapi.yaml", language="yaml", additions=0, deletions=2,
            content="--- a/api/openapi.yaml\n+++ b/api/openapi.yaml\n-/accounts:\n-  get:\n",
        )
        req    = AnalysisRequest(request_id="x", change_type=ChangeType.PR,
                                  repo_url="x", source_ref="a", target_ref="b", hunks=[hunk])
        agent  = InterfaceAnalysisAgent(api_key="test")
        result = agent.fallback_result(req)
        assert len(result.breaking_changes) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
#  Test Coverage Agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestTestCoverageAgent:

    def test_missing_test_file_flagged(self, zero_budget):
        hunk  = DiffHunk(file_path="src/main/java/com/bank/PaymentService.java",
                          language="java", additions=10, deletions=0, content="+x\n" * 10)
        req   = AnalysisRequest(request_id="x", change_type=ChangeType.PR,
                                 repo_url="x", source_ref="a", target_ref="b", hunks=[hunk])
        agent = TestCoverageAgent(api_key="test")
        result = agent.run(req, zero_budget)
        assert len(result.uncovered_paths) >= 1

    def test_test_file_detection(self):
        assert _is_test_file("src/test/PaymentServiceTest.java") is True
        assert _is_test_file("test_payment_service.py")          is True
        assert _is_test_file("src/main/PaymentService.java")     is False

    def test_expected_test_path_java(self):
        path = _expected_test_path("src/main/java/com/bank/Service.java")
        assert "test" in path
        assert path.endswith("Test.java")


# ═══════════════════════════════════════════════════════════════════════════════
#  Compliance Checker
# ═══════════════════════════════════════════════════════════════════════════════

class TestComplianceChecker:

    def test_secret_triggers_pci_block(self):
        sec    = SecurityResult(
            findings=[SecurityFinding(
                file_path="Service.java", line_range="10", severity=RiskLevel.CRITICAL,
                cwe_id="CWE-798", owasp_cat="A07", description="secret", remediation="fix"
            )],
            secrets_detected=True,
            overall_severity=RiskLevel.CRITICAL,
        )
        checker    = ComplianceChecker(["MAS TRM", "PCI-DSS 4.0"])
        violations = checker.check(sec)
        assert checker.must_block(violations) is True
        assert checker.derive_gate(violations) == GateDecision.BLOCK

    def test_no_findings_approve(self):
        sec        = SecurityResult(findings=[], secrets_detected=False, overall_severity=RiskLevel.LOW)
        checker    = ComplianceChecker(["MAS TRM", "PCI-DSS 4.0"])
        violations = checker.check(sec)
        assert not checker.must_block(violations)
        assert checker.derive_gate(violations) == GateDecision.APPROVE


# ═══════════════════════════════════════════════════════════════════════════════
#  Report Store
# ═══════════════════════════════════════════════════════════════════════════════

class TestInMemoryReportStore:

    def _make_report(self):
        from core.models import AnalysisReport
        return AnalysisReport(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/test",
            source_ref="feature", target_ref="main",
        )

    def test_save_and_get(self, report_store):
        r = self._make_report()
        report_store.save(r)
        assert report_store.get(r.request_id) is not None

    def test_delete(self, report_store):
        r = self._make_report()
        report_store.save(r)
        assert report_store.delete(r.request_id) is True
        assert report_store.get(r.request_id) is None

    def test_list_recent(self, report_store):
        for _ in range(5):
            report_store.save(self._make_report())
        listing = report_store.list_recent(limit=10)
        assert len(listing) == 5

    def test_max_size_eviction(self):
        store = InMemoryReportStore(max_size=3)
        ids   = []
        for _ in range(5):
            r = self._make_report()
            store.save(r)
            ids.append(r.request_id)
        # First two should be evicted
        assert store.get(ids[0]) is None
        assert store.get(ids[-1]) is not None
