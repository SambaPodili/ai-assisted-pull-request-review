"""
storage/temporal_store.py
--------------------------
Persistent store for historical analysis results, indexed by file path and repo.

Used by the TemporalRiskAgent to detect cross-PR patterns:
  • Files changed too frequently (change fatigue → high defect rate)
  • Gradual security erosion (permissions removed incrementally across PRs)
  • Files historically correlated with production incidents
  • Risk score trends over time (improving vs degrading)

Storage backends:
  • SQLite (default) — file-based, no external server, persists across restarts
  • In-memory dict — testing and environments where SQLite is unavailable

Schema:
  file_changes table:
    repo_url, file_path, request_id, risk_score, gate_decision,
    security_severity, has_secrets, changed_at (ISO timestamp)

  repo_risk_trend table:
    repo_url, week (YYYY-WW), avg_risk_score, block_count, hold_count, approve_count
"""
from __future__ import annotations
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/temporal.db"


@dataclass
class FileChangeRecord:
    repo_url:           str
    file_path:          str
    request_id:         str
    risk_score:         int
    gate_decision:      str
    security_severity:  str
    has_secrets:        bool
    changed_at:         str  # ISO 8601


@dataclass
class FileHistory:
    file_path:         str
    change_count:      int
    avg_risk_score:    float
    max_risk_score:    int
    last_changed:      str
    gates:             list[str] = field(default_factory=list)
    security_severities: list[str] = field(default_factory=list)
    had_secrets:       bool = False


@dataclass
class RepoRiskTrend:
    repo_url:       str
    weeks:          list[str]        # ["2025-W01", "2025-W02", ...]
    avg_scores:     list[float]      # per week
    block_counts:   list[int]
    trend:          str = "stable"   # improving | stable | degrading | critical


# ── SQLite backend ────────────────────────────────────────────────────────────

class SQLiteTemporalStore:

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS file_changes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_url         TEXT NOT NULL,
                file_path        TEXT NOT NULL,
                request_id       TEXT NOT NULL,
                risk_score       INTEGER DEFAULT 0,
                gate_decision    TEXT DEFAULT 'HOLD',
                security_severity TEXT DEFAULT 'low',
                has_secrets      INTEGER DEFAULT 0,
                changed_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_file_changes_repo_file
                ON file_changes(repo_url, file_path);
            CREATE INDEX IF NOT EXISTS idx_file_changes_repo_date
                ON file_changes(repo_url, changed_at);

            CREATE TABLE IF NOT EXISTS incident_files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_url   TEXT NOT NULL,
                file_path  TEXT NOT NULL,
                incident_date TEXT NOT NULL,
                description   TEXT DEFAULT ''
            );
        """)
        self._conn.commit()

    def record_change(self, record: FileChangeRecord) -> None:
        try:
            self._conn.execute(
                """INSERT INTO file_changes
                   (repo_url, file_path, request_id, risk_score, gate_decision,
                    security_severity, has_secrets, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.repo_url, record.file_path, record.request_id,
                 record.risk_score, record.gate_decision, record.security_severity,
                 1 if record.has_secrets else 0, record.changed_at),
            )
            self._conn.commit()
        except Exception as e:
            log.error("TemporalStore.record_change failed: %s", e)

    def get_file_history(
        self,
        repo_url:  str,
        file_path: str,
        days:      int = 30,
    ) -> FileHistory | None:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            rows = self._conn.execute(
                """SELECT risk_score, gate_decision, security_severity, has_secrets, changed_at
                   FROM file_changes
                   WHERE repo_url = ? AND file_path = ? AND changed_at >= ?
                   ORDER BY changed_at DESC""",
                (repo_url, file_path, since),
            ).fetchall()
        except Exception:
            return None

        if not rows:
            return None

        scores = [r["risk_score"] for r in rows]
        return FileHistory(
            file_path=file_path,
            change_count=len(rows),
            avg_risk_score=round(sum(scores) / len(scores), 1),
            max_risk_score=max(scores),
            last_changed=rows[0]["changed_at"],
            gates=[r["gate_decision"] for r in rows],
            security_severities=[r["security_severity"] for r in rows],
            had_secrets=any(r["has_secrets"] for r in rows),
        )

    def get_hot_files(
        self,
        repo_url:          str,
        days:              int = 30,
        min_changes:       int = 3,
    ) -> list[FileHistory]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            rows = self._conn.execute(
                """SELECT file_path,
                          COUNT(*)              AS change_count,
                          AVG(risk_score)       AS avg_risk,
                          MAX(risk_score)       AS max_risk,
                          MAX(changed_at)       AS last_changed,
                          SUM(has_secrets)      AS secret_count
                   FROM file_changes
                   WHERE repo_url = ? AND changed_at >= ?
                   GROUP BY file_path
                   HAVING change_count >= ?
                   ORDER BY change_count DESC, avg_risk DESC
                   LIMIT 20""",
                (repo_url, since, min_changes),
            ).fetchall()
        except Exception:
            return []

        return [
            FileHistory(
                file_path=r["file_path"],
                change_count=r["change_count"],
                avg_risk_score=round(r["avg_risk"], 1),
                max_risk_score=r["max_risk"],
                last_changed=r["last_changed"],
                had_secrets=r["secret_count"] > 0,
            )
            for r in rows
        ]

    def get_risk_trend(self, repo_url: str, weeks: int = 8) -> RepoRiskTrend:
        since = (datetime.utcnow() - timedelta(weeks=weeks)).isoformat()
        try:
            rows = self._conn.execute(
                """SELECT strftime('%Y-W%W', changed_at) AS week,
                          AVG(risk_score) AS avg_score,
                          SUM(CASE WHEN gate_decision='BLOCK' THEN 1 ELSE 0 END) AS blocks,
                          SUM(CASE WHEN gate_decision='HOLD'  THEN 1 ELSE 0 END) AS holds,
                          SUM(CASE WHEN gate_decision='APPROVE' THEN 1 ELSE 0 END) AS approves
                   FROM file_changes
                   WHERE repo_url = ? AND changed_at >= ?
                   GROUP BY week
                   ORDER BY week ASC""",
                (repo_url, since),
            ).fetchall()
        except Exception:
            return RepoRiskTrend(repo_url=repo_url, weeks=[], avg_scores=[], block_counts=[])

        week_labels = [r["week"] for r in rows]
        scores      = [round(r["avg_score"], 1) for r in rows]
        blocks      = [r["blocks"] for r in rows]

        trend = _compute_trend(scores)
        return RepoRiskTrend(
            repo_url=repo_url,
            weeks=week_labels,
            avg_scores=scores,
            block_counts=blocks,
            trend=trend,
        )

    def get_incident_correlated_files(self, repo_url: str) -> list[str]:
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT file_path FROM incident_files WHERE repo_url = ?",
                (repo_url,),
            ).fetchall()
            return [r["file_path"] for r in rows]
        except Exception:
            return []

    def record_incident(self, repo_url: str, file_paths: list[str], description: str = "") -> None:
        now = datetime.utcnow().isoformat()
        try:
            for fp in file_paths:
                self._conn.execute(
                    "INSERT INTO incident_files (repo_url, file_path, incident_date, description) VALUES (?, ?, ?, ?)",
                    (repo_url, fp, now, description),
                )
            self._conn.commit()
        except Exception as e:
            log.error("TemporalStore.record_incident failed: %s", e)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── In-memory backend (testing) ───────────────────────────────────────────────

class InMemoryTemporalStore:
    """Lightweight in-memory store for testing. Data lost on restart."""

    def __init__(self) -> None:
        self._records:  list[FileChangeRecord] = []
        self._incidents: list[dict] = []

    def record_change(self, record: FileChangeRecord) -> None:
        self._records.append(record)

    def get_file_history(self, repo_url: str, file_path: str, days: int = 30) -> FileHistory | None:
        since = datetime.utcnow() - timedelta(days=days)
        matching = [
            r for r in self._records
            if r.repo_url == repo_url and r.file_path == file_path
            and datetime.fromisoformat(r.changed_at) >= since
        ]
        if not matching:
            return None
        scores = [r.risk_score for r in matching]
        return FileHistory(
            file_path=file_path,
            change_count=len(matching),
            avg_risk_score=round(sum(scores)/len(scores), 1),
            max_risk_score=max(scores),
            last_changed=max(r.changed_at for r in matching),
            gates=[r.gate_decision for r in matching],
            had_secrets=any(r.has_secrets for r in matching),
        )

    def get_hot_files(self, repo_url: str, days: int = 30, min_changes: int = 3) -> list[FileHistory]:
        since = datetime.utcnow() - timedelta(days=days)
        matching = [
            r for r in self._records
            if r.repo_url == repo_url
            and datetime.fromisoformat(r.changed_at) >= since
        ]
        from collections import Counter
        counts = Counter(r.file_path for r in matching)
        return [
            self.get_file_history(repo_url, fp, days)
            for fp, cnt in counts.most_common(20)
            if cnt >= min_changes and self.get_file_history(repo_url, fp, days)
        ]

    def get_risk_trend(self, repo_url: str, weeks: int = 8) -> RepoRiskTrend:
        return RepoRiskTrend(repo_url=repo_url, weeks=[], avg_scores=[], block_counts=[])

    def get_incident_correlated_files(self, repo_url: str) -> list[str]:
        return [i["file_path"] for i in self._incidents if i["repo_url"] == repo_url]

    def record_incident(self, repo_url: str, file_paths: list[str], description: str = "") -> None:
        for fp in file_paths:
            self._incidents.append({"repo_url": repo_url, "file_path": fp, "description": description})


# ── Trend computation ─────────────────────────────────────────────────────────

def _compute_trend(scores: list[float]) -> str:
    if len(scores) < 3:
        return "stable"
    recent = sum(scores[-2:]) / 2
    earlier = sum(scores[:2]) / 2
    delta = recent - earlier
    if delta > 20:
        return "critical"
    if delta > 8:
        return "degrading"
    if delta < -8:
        return "improving"
    return "stable"


# ── Factory ────────────────────────────────────────────────────────────────────

_store: SQLiteTemporalStore | InMemoryTemporalStore | None = None


def get_temporal_store(db_path: str = DEFAULT_DB_PATH) -> SQLiteTemporalStore | InMemoryTemporalStore:
    global _store
    if _store is None:
        try:
            _store = SQLiteTemporalStore(db_path)
            log.info("Temporal store: SQLite at %s", db_path)
        except Exception as e:
            log.warning("SQLite temporal store unavailable (%s) — using in-memory", e)
            _store = InMemoryTemporalStore()
    return _store
