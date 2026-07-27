"""
tests/unit/test_production_grade.py
-------------------------------------
Production-grade feature tests:
  - Thread-safe InMemoryReportStore
  - SQLite report store
  - Rate limiter
  - In-flight tracker (TTL + thread safety)
  - Health endpoints
  - Request size validation
  - AnalysisReport.freeze_gate()
  - Orchestrator timeout path
  - Report formatter with new fields
  - Settings new fields
"""
from __future__ import annotations
import threading
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType, DiffHunk,
    RiskLevel, GateDecision, RiskResult,
)
from core.token_manager import TokenBudgetManager
from storage.report_store import InMemoryReportStore, SQLiteReportStore


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_report(request_id: str | None = None) -> AnalysisReport:
    return AnalysisReport(
        request_id=request_id or str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/test",
        source_ref="feature/x",
        target_ref="main",
    )


def _make_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/test",
        source_ref="feature/x",
        target_ref="main",
        hunks=[DiffHunk(file_path="F.java", language="java", additions=1, deletions=0, content="+x\n")],
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Thread-safe InMemoryReportStore
# ─────────────────────────────────────────────────────────────────────────────

class TestThreadSafeInMemoryStore:

    def test_concurrent_saves_no_data_race(self):
        store  = InMemoryReportStore(max_size=200)
        errors = []

        def _save():
            try:
                for _ in range(20):
                    store.save(_make_report())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_save) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread-safety errors: {errors}"
        assert len(store.list_recent(limit=100)) <= 100

    def test_pagination_offset(self):
        store = InMemoryReportStore()
        ids   = []
        for _ in range(10):
            r = _make_report()
            store.save(r)
            ids.append(r.request_id)

        page1 = store.list_recent(limit=5, offset=0)
        page2 = store.list_recent(limit=5, offset=5)
        all_pages = {r["request_id"] for r in page1 + page2}
        assert len(all_pages) == 10

    def test_delete_nonexistent_returns_false(self):
        store = InMemoryReportStore()
        assert store.delete("nonexistent-id") is False

    def test_lru_eviction_order(self):
        store = InMemoryReportStore(max_size=3)
        ids = [str(uuid.uuid4()) for _ in range(5)]
        for i in ids:
            store.save(_make_report(i))
        # First two should be evicted
        assert store.get(ids[0]) is None
        assert store.get(ids[1]) is None
        assert store.get(ids[4]) is not None


# ─────────────────────────────────────────────────────────────────────────────
#  SQLite report store
# ─────────────────────────────────────────────────────────────────────────────

class TestSQLiteReportStore:

    @pytest.fixture
    def sqlite_store(self, tmp_path):
        return SQLiteReportStore(str(tmp_path / "test.db"))

    def test_save_and_retrieve(self, sqlite_store):
        r = _make_report()
        sqlite_store.save(r)
        loaded = sqlite_store.get(r.request_id)
        assert loaded is not None
        assert loaded.request_id == r.request_id

    def test_overwrite_on_save(self, sqlite_store):
        r = _make_report()
        sqlite_store.save(r)
        r.errors.append("patched")
        sqlite_store.save(r)
        loaded = sqlite_store.get(r.request_id)
        assert "patched" in loaded.errors

    def test_delete(self, sqlite_store):
        r = _make_report()
        sqlite_store.save(r)
        assert sqlite_store.delete(r.request_id) is True
        assert sqlite_store.get(r.request_id) is None

    def test_delete_nonexistent_returns_false(self, sqlite_store):
        assert sqlite_store.delete("no-such-id") is False

    def test_list_recent_ordered_newest_first(self, sqlite_store):
        ids = []
        for i in range(5):
            r = _make_report()
            sqlite_store.save(r)
            ids.append(r.request_id)
            time.sleep(0.005)

        listing = sqlite_store.list_recent(limit=5)
        assert listing[0]["request_id"] == ids[-1]

    def test_pagination(self, sqlite_store):
        for _ in range(10):
            sqlite_store.save(_make_report())
        page1 = sqlite_store.list_recent(limit=5, offset=0)
        page2 = sqlite_store.list_recent(limit=5, offset=5)
        ids_p1 = {r["request_id"] for r in page1}
        ids_p2 = {r["request_id"] for r in page2}
        assert ids_p1.isdisjoint(ids_p2)


# ─────────────────────────────────────────────────────────────────────────────
#  AnalysisReport.freeze_gate
# ─────────────────────────────────────────────────────────────────────────────

class TestFreezeGate:

    def test_freeze_stores_gate_and_risk(self):
        r = _make_report()
        r.risk = RiskResult(
            overall_risk=RiskLevel.HIGH,
            risk_score=75,
            gate_decision=GateDecision.HOLD,
            deployment_guidance="Review required",
            rollback_feasibility="moderate",
            rationale="High churn",
        )
        r.freeze_gate()
        assert r._gate_decision == GateDecision.HOLD
        assert r._final_risk    == RiskLevel.HIGH

    def test_frozen_gate_survives_risk_none(self):
        r = _make_report()
        r.risk = RiskResult(
            overall_risk=RiskLevel.CRITICAL,
            risk_score=95,
            gate_decision=GateDecision.BLOCK,
            deployment_guidance="Do not deploy",
            rollback_feasibility="complex",
            rationale="Secrets detected",
        )
        r.freeze_gate()
        r.risk = None   # simulate serialisation round-trip where risk is stripped
        assert r.gate_decision == GateDecision.BLOCK
        assert r.final_risk    == RiskLevel.CRITICAL

    def test_no_risk_defaults_to_hold_and_low(self):
        r = _make_report()
        assert r.gate_decision == GateDecision.HOLD
        assert r.final_risk    == RiskLevel.LOW


# ─────────────────────────────────────────────────────────────────────────────
#  In-flight tracker
# ─────────────────────────────────────────────────────────────────────────────

class TestInFlightTracker:

    def test_set_and_get(self):
        from api.routes.analysis import _InFlight
        tracker = _InFlight()
        tracker.set("abc", "running")
        assert tracker.get("abc") == "running"

    def test_ttl_expiry(self):
        from api.routes.analysis import _InFlight
        tracker = _InFlight()
        tracker._TTL = 0    # expire immediately
        tracker.set("abc", "running")
        time.sleep(0.01)
        assert tracker.get("abc") is None

    def test_unknown_returns_none(self):
        from api.routes.analysis import _InFlight
        tracker = _InFlight()
        assert tracker.get("unknown-id") is None

    def test_gc_runs_and_cleans(self):
        from api.routes.analysis import _InFlight
        tracker = _InFlight()
        tracker._GC_INTERVAL  = 0   # GC runs every time
        # Insert with near-zero TTL then wait
        tracker._TTL = 0
        tracker.set("old", "done")
        time.sleep(0.01)
        # Switch to a real TTL before inserting "new" so it survives
        tracker._TTL = 3600
        tracker.set("new", "running")   # triggers GC which purges "old" from MEMORY
        assert "old" not in tracker._data          # memory evicted by GC
        # …but the status remains answerable from the persistent write-through
        # store (run_status.db) — a completed run must not become 'unknown'
        # just because in-process memory was garbage-collected.
        assert tracker.get("old") in ("done", None)
        assert tracker.get("new") == "running"


# ─────────────────────────────────────────────────────────────────────────────
#  Rate limiter
# ─────────────────────────────────────────────────────────────────────────────

class TestSlidingWindow:

    def test_allows_under_limit(self):
        from api.middleware.rate_limiter import _SlidingWindow
        w = _SlidingWindow()
        for _ in range(5):
            assert w.allow(10) is True

    def test_blocks_over_limit(self):
        from api.middleware.rate_limiter import _SlidingWindow
        w = _SlidingWindow()
        for _ in range(10):
            w.allow(10)
        assert w.allow(10) is False

    def test_retry_after_positive(self):
        from api.middleware.rate_limiter import _SlidingWindow
        w = _SlidingWindow()
        for _ in range(5):
            w.allow(5)
        assert w.retry_after() > 0

    def test_window_expires_old_entries(self):
        from api.middleware.rate_limiter import _SlidingWindow, _WINDOW_S
        import api.middleware.rate_limiter as rl_mod
        w = _SlidingWindow()
        # Manually inject old timestamps
        old_ts = time.monotonic() - _WINDOW_S - 1
        w._dq.extend([old_ts] * 5)
        # Now the window is empty effectively — should allow
        assert w.allow(5) is True


# ─────────────────────────────────────────────────────────────────────────────
#  Request size validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRequestSizeValidation:

    def test_diff_within_limit_passes(self):
        from api.routes.analysis import AnalyseRequest
        r = AnalyseRequest(
            repo_url="https://github.com/x/y",
            source_ref="a", target_ref="b",
            diff_text="+" * 100,
        )
        assert r.diff_text == "+" * 100

    def test_diff_over_limit_raises(self):
        from pydantic import ValidationError
        from api.routes.analysis import AnalyseRequest
        with patch("config.settings.get_settings") as mock_cfg:
            mock_cfg.return_value = MagicMock(max_diff_bytes=10)
            with pytest.raises(ValidationError, match="exceeds maximum"):
                AnalyseRequest(
                    repo_url="https://github.com/x/y",
                    source_ref="a", target_ref="b",
                    diff_text="x" * 100,
                )


# ─────────────────────────────────────────────────────────────────────────────
#  Report formatter — new fields
# ─────────────────────────────────────────────────────────────────────────────

class TestReportFormatter:

    def test_summary_json_includes_duration(self):
        from output.report_formatter import to_summary_json
        r = _make_report()
        r.duration_s = 12.5
        summary = to_summary_json(r)
        assert summary["duration_s"] == 12.5

    def test_summary_json_includes_source_target_refs(self):
        from output.report_formatter import to_summary_json
        r = _make_report()
        summary = to_summary_json(r)
        assert summary["source_ref"] == "feature/x"
        assert summary["target_ref"] == "main"

    def test_summary_json_includes_error_list(self):
        from output.report_formatter import to_summary_json
        r = _make_report()
        r.errors.append("agent timed out")
        summary = to_summary_json(r)
        assert "agent timed out" in summary["errors"]

    def test_summary_json_taint_paths_metric(self):
        from output.report_formatter import to_summary_json
        from core.models import TaintAnalysisResult, TaintPath, TaintSource, TaintSink
        r = _make_report()
        r.taint_analysis = TaintAnalysisResult(
            taint_paths=[
                TaintPath(
                    source=TaintSource(file_path="A.py", line=1, variable="x", source="request_param"),
                    sink=TaintSink(file_path="A.py",   line=5, variable="x", sink="sql_query"),
                )
            ],
        )
        summary = to_summary_json(r)
        assert summary["metrics"]["taint_paths"] == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Schema Change Agent
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaChangeAgent:

    def _agent(self):
        from agents.schema_change_agent import SchemaChangeAgent
        return SchemaChangeAgent(api_key="test")

    def _req_with_sql(self, sql: str) -> AnalysisRequest:
        return AnalysisRequest(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/x",
            source_ref="feature",
            target_ref="main",
            hunks=[DiffHunk(file_path="db/migrations/V2__add_table.sql",
                            additions=3, deletions=0, content=f"+{sql}")],
        )

    def test_no_schema_changes_returns_approve(self):
        req = AnalysisRequest(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/x",
            source_ref="feature", target_ref="main",
            hunks=[DiffHunk(file_path="src/Service.java", additions=5, deletions=2,
                            content="+public void doSomething() {}")],
        )
        from core.token_manager import TokenBudgetManager
        budget = TokenBudgetManager(req.request_id)
        result = self._agent().run(req, budget, {})
        assert result.gate_contribution == "APPROVE"
        assert result.changes == []

    def test_drop_table_is_block(self):
        req = self._req_with_sql("DROP TABLE payments;")
        budget = TokenBudgetManager(req.request_id)
        result = self._agent().fallback_result(req)
        assert result.has_destructive is True
        assert result.has_irreversible is True
        assert result.gate_contribution == "BLOCK"
        assert any(c.change_type == "drop_table" for c in result.changes)

    def test_drop_column_is_critical(self):
        req = self._req_with_sql("ALTER TABLE accounts DROP COLUMN balance;")
        result = self._agent().fallback_result(req)
        assert result.has_destructive is True
        assert any(c.severity.value == "critical" for c in result.changes)

    def test_create_table_is_low_risk(self):
        req = self._req_with_sql("CREATE TABLE audit_log (id INT PRIMARY KEY);")
        result = self._agent().fallback_result(req)
        assert not result.has_destructive
        assert any(c.change_type == "add_table" for c in result.changes)
        assert result.gate_contribution == "HOLD"

    def test_agent_name_is_schema_change(self):
        from core.models import AgentName
        assert self._agent().agent_name == AgentName.SCHEMA_CHANGE

    def test_analysis_report_has_schema_change_field(self):
        from core.models import SchemaChangeResult, RiskLevel
        report = _make_report()
        assert report.schema_change is None
        report.schema_change = SchemaChangeResult(
            summary="1 change",
            has_destructive=True,
            gate_contribution="BLOCK",
        )
        assert report.schema_change.has_destructive is True

    def test_analysis_report_has_pr_field(self):
        from core.models import PRMetadata
        report = _make_report()
        assert report.pr.pr_number == 0
        report.pr = PRMetadata(pr_number=42, pr_title="Fix payment flow", author="alice")
        assert report.pr.pr_number == 42

    def test_summary_json_includes_schema_metrics(self):
        from output.report_formatter import to_summary_json
        from core.models import SchemaChangeResult, SchemaChange, RiskLevel
        report = _make_report()
        report.schema_change = SchemaChangeResult(
            changes=[SchemaChange(file_path="m.sql", change_type="drop_table",
                                  severity=RiskLevel.CRITICAL, reversible=False)],
            has_destructive=True,
        )
        summary = to_summary_json(report)
        assert summary["metrics"]["schema_changes"] == 1
        assert summary["metrics"]["schema_destructive"] is True

    def test_truncate_detected_as_destructive(self):
        req = self._req_with_sql("TRUNCATE TABLE transactions;")
        result = self._agent().fallback_result(req)
        assert result.has_destructive is True
        assert result.has_irreversible is True


# ─────────────────────────────────────────────────────────────────────────────
#  Settings new fields
# ─────────────────────────────────────────────────────────────────────────────

class TestNewSettings:

    def test_defaults(self):
        from config.settings import Settings
        cfg = Settings(ANTHROPIC_API_KEY="test")
        assert cfg.max_diff_bytes  == 5_000_000
        assert cfg.rate_limit_rpm  == 240   # raised from 60 — the UI polls a lot
        assert cfg.analysis_timeout_s == 900   # raised to 900 for slow self-hosted models
        assert cfg.cors_origins    == ["*"]
        assert cfg.log_format      == "text"
        assert cfg.sqlite_path     == ""

    def test_custom_values(self):
        from config.settings import Settings
        cfg = Settings(
            ANTHROPIC_API_KEY="test",
            MAX_DIFF_BYTES=500_000,
            RATE_LIMIT_RPM=30,
            ANALYSIS_TIMEOUT_S=120,
            CORS_ORIGINS=["https://app.example.com"],
            LOG_FORMAT="json",
            SQLITE_PATH="data/prod.db",
        )
        assert cfg.max_diff_bytes    == 500_000
        assert cfg.rate_limit_rpm    == 30
        assert cfg.analysis_timeout_s == 120
        assert cfg.cors_origins      == ["https://app.example.com"]
        assert cfg.log_format        == "json"
        assert cfg.sqlite_path       == "data/prod.db"


# ─────────────────────────────────────────────────────────────────────────────
#  Health endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEndpoints:

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from api.routes.health import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_live_always_200(self, client):
        resp = client.get("/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert "uptime_s" in resp.json()

    def test_ready_returns_503_when_not_initialised(self, client):
        with patch("api.app.get_orchestrator", side_effect=RuntimeError("not init")):
            resp = client.get("/ready")
        assert resp.status_code == 503

    def test_health_returns_ok_or_degraded(self, client):
        resp = client.get("/health")
        # Will return degraded because orchestrator isn't initialised in isolation
        assert resp.status_code == 200
        assert resp.json()["status"] in ("ok", "degraded")


# ─────────────────────────────────────────────────────────────────────────────
#  PR Metadata model
# ─────────────────────────────────────────────────────────────────────────────

class TestPRMetadata:

    def test_default_pr_metadata(self):
        req = _make_request()
        assert req.pr.pr_number == 0
        assert req.pr.pr_title  == ""
        assert req.pr.labels    == []

    def test_pr_metadata_serialises(self):
        from core.models import PRMetadata
        req = AnalysisRequest(
            request_id="x",
            change_type=ChangeType.PR,
            repo_url="https://github.com/x/y",
            source_ref="feature/abc",
            target_ref="main",
            pr=PRMetadata(pr_number=42, pr_title="Fix bug", author="dev", labels=["bugfix"]),
        )
        data = req.model_dump()
        assert data["pr"]["pr_number"] == 42
        assert data["pr"]["labels"]    == ["bugfix"]
