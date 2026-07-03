"""
storage/report_store.py
------------------------
Stores and retrieves AnalysisReport objects.

Three backends (selected by factory):
  • InMemoryReportStore  — testing / no external deps (thread-safe, LRU eviction)
  • SQLiteReportStore    — production without Redis (persistent, single-file DB)
  • RedisReportStore     — production at scale (persistent, TTL-based, multi-process)
"""
from __future__ import annotations
import json
import logging
import threading
from datetime import datetime
from typing import Protocol, runtime_checkable

from core.models import AnalysisReport

log = logging.getLogger(__name__)
DEFAULT_TTL = 60 * 60 * 24 * 7   # 7 days

try:
    import redis as redis_client
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


@runtime_checkable
class ReportStore(Protocol):
    def save(self, report: AnalysisReport) -> None: ...
    def get(self, request_id: str) -> AnalysisReport | None: ...
    def delete(self, request_id: str) -> bool: ...
    def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict]: ...


# ── In-memory backend ─────────────────────────────────────────────────────────

class InMemoryReportStore:
    """
    Thread-safe LRU-evicting in-memory store.
    All mutations hold a lock to prevent data races under concurrent analysis.
    """

    def __init__(self, max_size: int = 500) -> None:
        self._store:    dict[str, AnalysisReport] = {}
        self._order:    list[str] = []
        self._max_size  = max_size
        self._lock      = threading.Lock()

    def save(self, report: AnalysisReport) -> None:
        with self._lock:
            if report.request_id in self._store:
                self._store[report.request_id] = report
                return
            if len(self._order) >= self._max_size:
                oldest = self._order.pop(0)
                self._store.pop(oldest, None)
            self._store[report.request_id] = report
            self._order.append(report.request_id)

    def get(self, request_id: str) -> AnalysisReport | None:
        with self._lock:
            return self._store.get(request_id)

    def delete(self, request_id: str) -> bool:
        with self._lock:
            if request_id not in self._store:
                return False
            self._store.pop(request_id)
            try:
                self._order.remove(request_id)
            except ValueError:
                pass
            return True

    def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        with self._lock:
            recent = list(reversed(self._order))
            page   = recent[offset: offset + limit]
            return [
                {
                    "request_id":   r,
                    "gate":         self._store[r].gate_decision.value,
                    "risk":         self._store[r].final_risk.value,
                    "repo":         self._store[r].repo_url,
                    "source_ref":   self._store[r].source_ref,
                    "target_ref":   self._store[r].target_ref,
                    "completed_at": self._store[r].completed_at.isoformat(),
                }
                for r in page if r in self._store
            ]


# ── SQLite backend ────────────────────────────────────────────────────────────

class SQLiteReportStore:
    """
    Persistent single-file SQLite store.
    Thread-safe: each call opens a short-lived connection (WAL mode).
    Suitable for single-node production when Redis is not available.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS reports (
        request_id   TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL,
        gate         TEXT NOT NULL,
        risk         TEXT NOT NULL,
        repo_url     TEXT NOT NULL,
        source_ref   TEXT NOT NULL,
        target_ref   TEXT NOT NULL,
        payload      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_completed ON reports(completed_at DESC);
    """

    def __init__(self, db_path: str) -> None:
        import sqlite3
        self._path = db_path
        with sqlite3.connect(self._path) as conn:
            conn.executescript(self._SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
        log.info("SQLite report store initialised at %s", db_path)

    def save(self, report: AnalysisReport) -> None:
        import sqlite3
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO reports (request_id, completed_at, gate, risk, repo_url, source_ref, target_ref, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    completed_at = excluded.completed_at,
                    gate         = excluded.gate,
                    risk         = excluded.risk,
                    payload      = excluded.payload
                """,
                (
                    report.request_id,
                    report.completed_at.isoformat(),
                    report.gate_decision.value,
                    report.final_risk.value,
                    report.repo_url,
                    report.source_ref,
                    report.target_ref,
                    report.model_dump_json(),
                ),
            )

    def get(self, request_id: str) -> AnalysisReport | None:
        import sqlite3
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT payload, gate, risk FROM reports WHERE request_id = ?", (request_id,)
            ).fetchone()
        if not row:
            return None
        try:
            report = AnalysisReport.model_validate_json(row[0])
            # Re-inject the policy-ENFORCED gate/risk. They live in PrivateAttrs
            # (_gate_decision/_final_risk) which model_dump_json() drops, so without
            # this a reloaded report reverts to the LLM's pre-policy gate (e.g. a
            # policy HOLD on a critical finding would show as APPROVE).
            from core.models import GateDecision, RiskLevel
            if row[1]:
                object.__setattr__(report, "_gate_decision", GateDecision(row[1]))
            if row[2]:
                object.__setattr__(report, "_final_risk", RiskLevel(row[2]))
            return report
        except Exception as e:
            log.error("Failed to deserialise report %s: %s", request_id, e)
            return None

    def delete(self, request_id: str) -> bool:
        import sqlite3
        with sqlite3.connect(self._path) as conn:
            cur = conn.execute("DELETE FROM reports WHERE request_id = ?", (request_id,))
        return cur.rowcount > 0

    def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        import sqlite3
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                """
                SELECT request_id, gate, risk, repo_url, source_ref, target_ref, completed_at
                FROM reports
                ORDER BY completed_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [
            {
                "request_id":   r[0],
                "gate":         r[1],
                "risk":         r[2],
                "repo":         r[3],
                "source_ref":   r[4],
                "target_ref":   r[5],
                "completed_at": r[6],
            }
            for r in rows
        ]


# ── Redis backend ─────────────────────────────────────────────────────────────

class RedisReportStore:

    _KEY_PREFIX  = "impact_report:"
    _INDEX_KEY   = "impact_report_index"

    def __init__(self, redis_url: str, ttl: int = DEFAULT_TTL) -> None:
        if not HAS_REDIS:
            raise ImportError("pip install redis to enable Redis report store")
        self._r   = redis_client.from_url(redis_url, decode_responses=True)
        self._ttl = ttl

    def save(self, report: AnalysisReport) -> None:
        key  = f"{self._KEY_PREFIX}{report.request_id}"
        data = report.model_dump_json()
        pipe = self._r.pipeline()
        pipe.set(key, data, ex=self._ttl)
        pipe.zadd(self._INDEX_KEY, {report.request_id: report.completed_at.timestamp()})
        pipe.execute()

    def get(self, request_id: str) -> AnalysisReport | None:
        data = self._r.get(f"{self._KEY_PREFIX}{request_id}")
        if not data:
            return None
        try:
            return AnalysisReport.model_validate_json(data)
        except Exception as e:
            log.error("Failed to deserialise report %s: %s", request_id, e)
            return None

    def delete(self, request_id: str) -> bool:
        pipe = self._r.pipeline()
        pipe.delete(f"{self._KEY_PREFIX}{request_id}")
        pipe.zrem(self._INDEX_KEY, request_id)
        results = pipe.execute()
        return bool(results[0])

    def list_recent(self, limit: int = 20, offset: int = 0) -> list[dict]:
        ids = self._r.zrevrange(self._INDEX_KEY, offset, offset + limit - 1)
        summaries = []
        for rid in ids:
            report = self.get(rid)
            if report:
                summaries.append({
                    "request_id":   rid,
                    "gate":         report.gate_decision.value,
                    "risk":         report.final_risk.value,
                    "repo":         report.repo_url,
                    "source_ref":   report.source_ref,
                    "target_ref":   report.target_ref,
                    "completed_at": report.completed_at.isoformat(),
                })
        return summaries


# ── Factory ────────────────────────────────────────────────────────────────────

_DEFAULT_SQLITE_PATH = "data/reports.db"


def make_report_store(settings=None) -> ReportStore:
    from config.settings import get_settings
    cfg = settings or get_settings()

    # Redis (highest priority — multi-process safe, explicitly configured)
    if cfg.redis_url and HAS_REDIS:
        try:
            store = RedisReportStore(cfg.redis_url)
            store._r.ping()
            log.info("Report store: Redis (%s)", cfg.redis_url)
            return store
        except Exception as e:
            log.warning(
                "Redis configured but unavailable (%s) — falling back to SQLite", e
            )

    # SQLite — explicit path takes priority, then DATA_DIR, then a safe default.
    # getattr-guard data_path so a partial deploy (new report_store.py but old
    # settings.py without data_path) still uses SQLite instead of crashing into
    # the in-memory store (which silently loses reports → 404 on Post-to-PR).
    import os
    if getattr(cfg, "sqlite_path", ""):
        sqlite_path = cfg.sqlite_path
    elif hasattr(cfg, "data_path"):
        sqlite_path = cfg.data_path("reports.db")
    else:
        sqlite_path = os.path.join(getattr(cfg, "data_dir", "data") or "data", "reports.db")
    try:
        os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
        store = SQLiteReportStore(sqlite_path)
        log.info("Report store: SQLite (%s)", sqlite_path)
        return store
    except Exception as e:
        log.warning("SQLite store unavailable (%s) — using in-memory (reports lost on restart)", e)

    return InMemoryReportStore()
