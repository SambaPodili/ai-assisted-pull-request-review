"""
tests/unit/test_phase4.py
--------------------------
Unit tests for Phase 4 components:
  - RBAC (roles, permissions, gate overrides)
  - Schema change agent (DDL detection fallback)
  - Observability (in-memory metrics)
  - CI/CD gate logic
"""
from __future__ import annotations
import uuid
import pytest

from core.models import AnalysisRequest, ChangeType, DiffHunk, GateDecision


# ═══════════════════════════════════════════════════════════════════════════════
#  RBAC Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRBAC:

    def test_role_permissions_coverage(self):
        from governance.rbac import Role, Permission, _ROLE_PERMISSIONS
        # Admin must have all permissions
        admin_perms = _ROLE_PERMISSIONS[Role.ADMIN]
        assert admin_perms == set(Permission)

    def test_ci_system_cannot_admin(self):
        from governance.rbac import Role, Permission, _ROLE_PERMISSIONS
        ci_perms = _ROLE_PERMISSIONS[Role.CI_SYSTEM]
        assert Permission.ADMIN_CONFIG not in ci_perms
        assert Permission.GATE_OVERRIDE not in ci_perms

    def test_auditor_read_only(self):
        from governance.rbac import Role, Permission, _ROLE_PERMISSIONS
        auditor_perms = _ROLE_PERMISSIONS[Role.AUDITOR]
        assert Permission.ANALYSIS_SUBMIT not in auditor_perms
        assert Permission.AUDIT_READ      in  auditor_perms

    def test_subject_has_permission(self):
        from governance.rbac import Subject, Role, Permission
        subject = Subject(key_id="k1", roles=[Role.ANALYST])
        assert subject.has_permission(Permission.GATE_OVERRIDE)
        assert not subject.has_permission(Permission.ADMIN_CONFIG)

    def test_subject_require_raises_on_missing(self):
        from governance.rbac import Subject, Role, Permission
        from fastapi import HTTPException
        subject = Subject(key_id="k1", roles=[Role.CI_SYSTEM])
        with pytest.raises(HTTPException) as exc_info:
            subject.require(Permission.GATE_OVERRIDE)
        assert exc_info.value.status_code == 403

    def test_registry_resolves_key(self):
        from governance.rbac import APIKeyRegistry, Subject, Role
        registry = APIKeyRegistry()
        subject  = Subject(key_id="test-key-1", roles=[Role.ANALYST], name="Alice")
        registry.add_key("test-key-1", subject)
        resolved = registry.resolve("test-key-1")
        assert resolved is not None
        assert resolved.name == "Alice"

    def test_registry_unknown_key_returns_none(self):
        from governance.rbac import APIKeyRegistry
        registry = APIKeyRegistry()
        assert registry.resolve("nonexistent") is None

    def test_gate_override_store(self):
        from governance.rbac import GateOverrideStore, GateOverride
        store = GateOverrideStore()
        override = GateOverride(
            request_id="req-1",
            original_gate="BLOCK",
            override_to="APPROVE",
            reason="Emergency hotfix approved by risk committee",
            override_by="alice@bank.com",
            override_team="Release",
        )
        store.record(override)
        fetched = store.get("req-1")
        assert fetched is not None
        assert fetched.override_to == "APPROVE"

    def test_gate_override_list(self):
        from governance.rbac import GateOverrideStore, GateOverride
        store = GateOverrideStore()
        for i in range(3):
            store.record(GateOverride(
                request_id=f"req-{i}", original_gate="HOLD", override_to="APPROVE",
                reason="Approved by risk", override_by="bob", override_team="Ops",
            ))
        assert len(store.list_all()) == 3


# ═══════════════════════════════════════════════════════════════════════════════
#  Schema Change Agent Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaChangeAgent:

    def _make_req(self, diff_content: str, filename: str = "db/V1__changes.sql") -> AnalysisRequest:
        return AnalysisRequest(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/test",
            source_ref="feature", target_ref="main",
            hunks=[DiffHunk(file_path=filename, language="sql", additions=2, deletions=0,
                            content=diff_content)],
        )

    def test_detects_drop_table(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        agent  = SchemaChangeAgent(api_key="test")
        req    = self._make_req("+DROP TABLE payment_transactions;")
        result = agent.run(req, zero_budget)
        assert result.has_destructive   is True
        assert result.has_irreversible  is True
        assert result.gate_contribution == "BLOCK"
        assert any(c.change_type == "drop_table" for c in result.changes)

    def test_detects_drop_column(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        agent  = SchemaChangeAgent(api_key="test")
        req    = self._make_req("+ALTER TABLE accounts DROP COLUMN legacy_field;")
        result = agent.run(req, zero_budget)
        assert result.has_destructive is True
        assert any(c.change_type == "drop_column" for c in result.changes)

    def test_detects_create_table_as_low_risk(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        from core.models import RiskLevel
        agent  = SchemaChangeAgent(api_key="test")
        req    = self._make_req("+CREATE TABLE new_audit_log (id BIGINT PRIMARY KEY, event TEXT);")
        result = agent.run(req, zero_budget)
        assert any(c.change_type == "add_table" for c in result.changes)
        assert not result.has_destructive
        assert all(c.severity in (RiskLevel.LOW, RiskLevel.MEDIUM) for c in result.changes)

    def test_detects_alter_not_null(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        from core.models import RiskLevel
        agent  = SchemaChangeAgent(api_key="test")
        req    = self._make_req("+ALTER TABLE accounts ADD COLUMN status VARCHAR(20) NOT NULL;")
        result = agent.run(req, zero_budget)
        assert any(c.severity == RiskLevel.HIGH for c in result.changes)

    def test_no_schema_changes_returns_approve(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        agent  = SchemaChangeAgent(api_key="test")
        req    = AnalysisRequest(
            request_id=str(uuid.uuid4()), change_type=ChangeType.PR,
            repo_url="https://github.com/bank/test", source_ref="a", target_ref="b",
            hunks=[DiffHunk(file_path="Service.java", language="java",
                            additions=1, deletions=0, content="+// comment\n")],
        )
        result = agent.run(req, zero_budget)
        assert result.gate_contribution == "APPROVE"
        assert not result.changes

    def test_truncate_detected_as_destructive(self, zero_budget):
        from agents.schema_change_agent import SchemaChangeAgent
        agent  = SchemaChangeAgent(api_key="test")
        req    = self._make_req("+TRUNCATE TABLE temp_batch_data;")
        result = agent.run(req, zero_budget)
        assert result.has_destructive is True


# ═══════════════════════════════════════════════════════════════════════════════
#  Observability Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservability:

    def test_in_memory_metrics_track_analysis(self):
        from governance.observability import InMemoryMetrics
        m = InMemoryMetrics()
        m.record_complete("APPROVE", "low", tokens=5000, duration=12.3)
        m.record_complete("HOLD",    "high", tokens=8000, duration=25.1)
        assert m.analysis_count        == 2
        assert m.gate_counts["APPROVE"] == 1
        assert m.gate_counts["HOLD"]    == 1
        assert m.total_tokens          == 13000

    def test_in_memory_metrics_record_tokens(self):
        from governance.observability import InMemoryMetrics
        m = InMemoryMetrics()
        m.record_tokens("security",  3000)
        m.record_tokens("risk",      1500)
        m.record_tokens("security",  2000)
        assert m.agent_tokens["security"] == 5000
        assert m.agent_tokens["risk"]     == 1500

    def test_in_memory_metrics_fallbacks(self):
        from governance.observability import InMemoryMetrics
        m = InMemoryMetrics()
        m.record_fallback("code_analysis")
        m.record_fallback("code_analysis")
        m.record_fallback("dependency")
        assert m.fallback_counts["code_analysis"] == 2
        assert m.fallback_counts["dependency"]     == 1

    def test_summary_structure(self):
        from governance.observability import InMemoryMetrics
        m = InMemoryMetrics()
        m.record_complete("APPROVE", "low", 1000, 5.0)
        summary = m.summary()
        assert "analysis_count"    in summary
        assert "gate_distribution" in summary
        assert "agent_token_usage" in summary
        assert "avg_duration_s"    in summary

    def test_prometheus_response_no_crash(self):
        from governance.observability import prometheus_metrics_response
        body, ct = prometheus_metrics_response()
        assert isinstance(body, bytes)
        assert "text" in ct


# ═══════════════════════════════════════════════════════════════════════════════
#  CI/CD Gate Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCICDGate:

    def test_gate_result_constants(self):
        from cicd.pipeline_gate import GateResult
        assert GateResult.APPROVE == 0
        assert GateResult.HOLD    == 1
        assert GateResult.BLOCK   == 2
        assert GateResult.ERROR   == 3

    def test_handle_approve_result(self):
        from cicd.pipeline_gate import _handle_result
        report = {
            "gate": "APPROVE", "risk": "low", "risk_score": 10,
            "repo": "bank/payments", "comparison": "feature → main",
            "total_tokens": 5000, "request_id": "abc123",
        }
        code = _handle_result(report, output_file="")
        assert code == 0

    def test_handle_block_result(self):
        from cicd.pipeline_gate import _handle_result, GateResult
        report = {
            "gate": "BLOCK", "risk": "critical", "risk_score": 95,
            "repo": "bank/payments", "comparison": "feature → main",
            "total_tokens": 8000, "request_id": "def456",
        }
        code = _handle_result(report, output_file="")
        assert code == GateResult.BLOCK

    def test_handle_hold_result(self):
        from cicd.pipeline_gate import _handle_result, GateResult
        report = {
            "gate": "HOLD", "risk": "high", "risk_score": 65,
            "repo": "bank/api", "comparison": "feature → main",
            "total_tokens": 6000, "request_id": "ghi789",
        }
        code = _handle_result(report, output_file="")
        assert code == GateResult.HOLD


# ═══════════════════════════════════════════════════════════════════════════════
#  Schema file detection helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchemaHelpers:

    def test_migration_file_detection(self):
        from agents.schema_change_agent import _is_migration_file
        assert _is_migration_file("db/migrations/V001__add_account_status.sql")
        assert _is_migration_file("flyway/V2__alter_payments.sql")
        assert _is_migration_file("src/main/resources/db/changelog/changes.sql")
        assert _is_migration_file("config/schema.sql")
        assert not _is_migration_file("src/PaymentService.java")

    def test_orm_file_detection(self):
        from agents.schema_change_agent import _is_orm_file
        assert _is_orm_file("src/main/java/com/bank/domain/Account.java")
        assert _is_orm_file("models.py")
        assert _is_orm_file("entities/Payment.java")
        assert not _is_orm_file("controllers/PaymentController.java")

    def test_has_ddl_detection(self):
        from agents.schema_change_agent import _has_ddl
        assert _has_ddl("ALTER TABLE accounts ADD COLUMN status VARCHAR(20);")
        assert _has_ddl("CREATE TABLE new_table (id BIGINT);")
        assert _has_ddl("DROP TABLE old_table;")
        assert not _has_ddl("SELECT * FROM accounts WHERE id = 1;")
        assert not _has_ddl("public class AccountService {")
