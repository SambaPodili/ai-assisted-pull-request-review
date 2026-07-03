"""
governance/feedback_store.py
-----------------------------
Reviewer feedback loop — persistent record of:

  1. Gate decisions: what the AI proposed, what policy enforced, what the human
     finally decided. Lets us measure how often humans override the system and
     in which direction (too strict vs too lenient).

  2. Per-finding verdicts: reviewers mark a finding false_positive / valid /
     fixed. Aggregated, this tells you which checks are noisy on *your* codebase
     so you can tune or suppress them.

This is what turns a static analyser into one that improves over time: the
Insights "Feedback" view surfaces per-agent false-positive rates and gate
override rates per repo, and the findings UI warns when a check has been a
repeat false-positive here before.

Backend: SQLite (single file, no server). Degrades to a no-op in-memory store
if SQLite is unavailable.
"""
from __future__ import annotations
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/feedback.db"

_VALID_VERDICTS = {"false_positive", "valid", "fixed", "wont_fix"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteFeedbackStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS gate_feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id    TEXT NOT NULL,
                repo          TEXT DEFAULT '',
                ai_gate       TEXT DEFAULT '',
                policy_gate   TEXT DEFAULT '',
                human_gate    TEXT DEFAULT '',
                direction     TEXT DEFAULT '',   -- stricter | looser | same
                reason        TEXT DEFAULT '',
                reviewer      TEXT DEFAULT '',
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS finding_feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id    TEXT NOT NULL,
                repo          TEXT DEFAULT '',
                agent         TEXT NOT NULL,     -- e.g. security, performance_impact
                category      TEXT DEFAULT '',   -- finding kind/cwe/category
                file_path     TEXT DEFAULT '',
                verdict       TEXT NOT NULL,     -- false_positive | valid | fixed | wont_fix
                note          TEXT DEFAULT '',
                reviewer      TEXT DEFAULT '',
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ff_agent ON finding_feedback(agent, repo);
            CREATE INDEX IF NOT EXISTS idx_gf_repo  ON gate_feedback(repo);
        """)
        self._conn.commit()

    # ── Recording ───────────────────────────────────────────────────────────

    def record_gate(self, request_id: str, repo: str, ai_gate: str, policy_gate: str,
                    human_gate: str, reason: str, reviewer: str) -> None:
        order = {"APPROVE": 0, "HOLD": 1, "BLOCK": 2}
        a, h = order.get(policy_gate or ai_gate, 1), order.get(human_gate, 1)
        direction = "stricter" if h > a else "looser" if h < a else "same"
        self._conn.execute(
            """INSERT INTO gate_feedback
               (request_id, repo, ai_gate, policy_gate, human_gate, direction, reason, reviewer, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (request_id, repo, ai_gate, policy_gate, human_gate, direction, reason, reviewer, _now()),
        )
        self._conn.commit()

    def record_finding(self, request_id: str, repo: str, agent: str, category: str,
                       file_path: str, verdict: str, note: str, reviewer: str) -> None:
        if verdict not in _VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {_VALID_VERDICTS}")
        self._conn.execute(
            """INSERT INTO finding_feedback
               (request_id, repo, agent, category, file_path, verdict, note, reviewer, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (request_id, repo, agent, category, file_path, verdict, note, reviewer, _now()),
        )
        self._conn.commit()

    # ── Aggregation ───────────────────────────────────────────────────────────

    def noisy_checks(self, repo: str = "", limit: int = 20) -> list[dict]:
        """Per agent+category: total marks, false positives, FP rate. Ranked by FP count."""
        where = "WHERE repo = ?" if repo else ""
        args = (repo,) if repo else ()
        rows = self._conn.execute(f"""
            SELECT agent, category,
                   COUNT(*) AS total,
                   SUM(CASE WHEN verdict='false_positive' THEN 1 ELSE 0 END) AS fp,
                   SUM(CASE WHEN verdict='valid' THEN 1 ELSE 0 END) AS valid,
                   SUM(CASE WHEN verdict='fixed' THEN 1 ELSE 0 END) AS fixed
            FROM finding_feedback {where}
            GROUP BY agent, category
            ORDER BY fp DESC, total DESC
            LIMIT ?
        """, (*args, limit)).fetchall()
        out = []
        for r in rows:
            total = r["total"] or 1
            out.append({
                "agent": r["agent"], "category": r["category"],
                "total": r["total"], "false_positives": r["fp"],
                "valid": r["valid"], "fixed": r["fixed"],
                "fp_rate": round((r["fp"] or 0) / total, 3),
            })
        return out

    def fp_count(self, agent: str, category: str = "", repo: str = "") -> int:
        """How many times this exact check was marked a false positive (for inline warnings)."""
        q = "SELECT COUNT(*) FROM finding_feedback WHERE agent=? AND verdict='false_positive'"
        args: list = [agent]
        if category:
            q += " AND category=?"; args.append(category)
        if repo:
            q += " AND repo=?"; args.append(repo)
        return self._conn.execute(q, tuple(args)).fetchone()[0]

    def suppressed_categories(self, repo: str = "", min_fp: int = 3) -> set[tuple[str, str]]:
        """(agent, category) pairs that reviewers have repeatedly marked as false
        positives with NO 'valid' verdicts — safe to auto-suppress on new runs.

        Conservative on purpose: requires ≥ min_fp false positives and zero
        'valid' marks, so a check that is ever genuinely useful is never hidden.
        """
        q = """
            SELECT agent, category,
                   SUM(CASE WHEN verdict='false_positive' THEN 1 ELSE 0 END) AS fp,
                   SUM(CASE WHEN verdict='valid'          THEN 1 ELSE 0 END) AS valid
            FROM finding_feedback
            {where}
            GROUP BY agent, category
            HAVING fp >= ? AND valid = 0 AND category != ''
        """.format(where="WHERE repo = ?" if repo else "")
        args = ([repo, min_fp] if repo else [min_fp])
        rows = self._conn.execute(q, tuple(args)).fetchall()
        return {(r["agent"], r["category"]) for r in rows}

    def gate_stats(self, repo: str = "") -> dict:
        where = "WHERE repo = ?" if repo else ""
        args = (repo,) if repo else ()
        row = self._conn.execute(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN direction='stricter' THEN 1 ELSE 0 END) AS stricter,
                   SUM(CASE WHEN direction='looser'  THEN 1 ELSE 0 END) AS looser,
                   SUM(CASE WHEN direction='same'    THEN 1 ELSE 0 END) AS same
            FROM gate_feedback {where}
        """, args).fetchone()
        total = row["total"] or 0
        return {
            "total_overrides": total,
            "stricter": row["stricter"] or 0,
            "looser":   row["looser"] or 0,
            "same":     row["same"] or 0,
            "override_rate_note": "high 'looser' counts suggest the gate is too strict for this repo",
        }

    def recent_finding_feedback(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM finding_feedback ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


class _NullFeedbackStore:
    def record_gate(self, *a, **k): pass
    def record_finding(self, *a, **k): pass
    def noisy_checks(self, *a, **k): return []
    def fp_count(self, *a, **k): return 0
    def suppressed_categories(self, *a, **k): return set()
    def gate_stats(self, *a, **k): return {"total_overrides": 0, "stricter": 0, "looser": 0, "same": 0}
    def recent_finding_feedback(self, *a, **k): return []


_store = None


def get_feedback_store():
    global _store
    if _store is None:
        try:
            from config.settings import get_settings
            _cfg = get_settings()
            path = getattr(_cfg, "feedback_db_path", "") or _cfg.data_path("feedback.db")
            _store = SQLiteFeedbackStore(path)
            log.info("Feedback store: SQLite (%s)", path)
        except Exception as exc:
            log.warning("Feedback store unavailable (%s) — using no-op", exc)
            _store = _NullFeedbackStore()
    return _store
