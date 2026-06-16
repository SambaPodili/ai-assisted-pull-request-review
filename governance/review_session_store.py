"""
governance/review_session_store.py
-----------------------------------
Persistent developer → reviewer → PR review workflow ("continuity").

A REVIEW SESSION is keyed by request_id and moves through three stages:

  developer  — the author triages each finding: valid / false_positive / wont_fix
               + a free-text comment. When done they SUBMIT for review.
  reviewer   — a user with the REVIEWER role sees the developer's triage and
               validates each finding: confirmed / rejected + a comment, then
               FINALIZES (final gate decision).
  done       — only findings the developer marked VALID *and* the reviewer
               CONFIRMED are the "real issues" — those get posted back to the PR.

Everything is persisted (SQLite) so the developer can key comments, hand off, and
a different reviewer can pick it up later — true continuity across people/sessions.

Backend: SQLite (single file). Degrades to a no-op store if SQLite is unavailable.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/review_sessions.db"

STAGES = ("developer", "reviewer", "done")
_DEV_VERDICTS = {"", "valid", "false_positive", "wont_fix"}
_REV_VERDICTS = {"", "confirmed", "rejected"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteReviewSessionStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS review_session (
                request_id    TEXT PRIMARY KEY,
                repo          TEXT DEFAULT '',
                pr_id         TEXT DEFAULT '',
                stage         TEXT DEFAULT 'developer',   -- developer | reviewer | done
                final_gate    TEXT DEFAULT '',
                posted_to_pr  INTEGER DEFAULT 0,
                submitted_by  TEXT DEFAULT '',
                finalized_by  TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS review_triage (
                request_id        TEXT NOT NULL,
                finding_key       TEXT NOT NULL,
                title             TEXT DEFAULT '',
                agent             TEXT DEFAULT '',
                file              TEXT DEFAULT '',
                line              TEXT DEFAULT '',
                severity          TEXT DEFAULT '',
                dev_verdict       TEXT DEFAULT '',
                dev_comment       TEXT DEFAULT '',
                dev_by            TEXT DEFAULT '',
                dev_ts            TEXT DEFAULT '',
                reviewer_verdict  TEXT DEFAULT '',
                reviewer_comment  TEXT DEFAULT '',
                reviewer_by       TEXT DEFAULT '',
                reviewer_ts       TEXT DEFAULT '',
                PRIMARY KEY (request_id, finding_key)
            );
            CREATE INDEX IF NOT EXISTS idx_rt_req ON review_triage(request_id);
        """)
        self._conn.commit()

    # ── Session ───────────────────────────────────────────────────────────────

    def ensure_session(self, request_id: str, repo: str = "", pr_id: str = "") -> dict:
        row = self._conn.execute("SELECT * FROM review_session WHERE request_id=?",
                                  (request_id,)).fetchone()
        if row is None:
            now = _now()
            self._conn.execute(
                """INSERT INTO review_session (request_id, repo, pr_id, stage, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (request_id, repo, pr_id, "developer", now, now),
            )
            self._conn.commit()
        elif (repo and not row["repo"]) or (pr_id and not row["pr_id"]):
            self._conn.execute(
                "UPDATE review_session SET repo=COALESCE(NULLIF(?,''),repo), pr_id=COALESCE(NULLIF(?,''),pr_id), updated_at=? WHERE request_id=?",
                (repo, pr_id, _now(), request_id))
            self._conn.commit()
        return self.get_session(request_id)

    def get_session(self, request_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM review_session WHERE request_id=?",
                                  (request_id,)).fetchone()
        if row is None:
            return None
        s = dict(row)
        s["posted_to_pr"] = bool(s.get("posted_to_pr"))
        s["triage"] = self.list_triage(request_id)
        return s

    def list_triage(self, request_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_triage WHERE request_id=? ORDER BY rowid", (request_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── Triage ────────────────────────────────────────────────────────────────

    def set_triage(self, request_id: str, finding_key: str, role: str, verdict: str,
                   comment: str, by: str, meta: dict | None = None) -> dict:
        """Upsert the developer OR reviewer columns of a finding's triage row."""
        if role not in ("developer", "reviewer"):
            raise ValueError("role must be developer or reviewer")
        if role == "developer" and verdict not in _DEV_VERDICTS:
            raise ValueError(f"dev verdict must be one of {_DEV_VERDICTS}")
        if role == "reviewer" and verdict not in _REV_VERDICTS:
            raise ValueError(f"reviewer verdict must be one of {_REV_VERDICTS}")

        self.ensure_session(request_id, (meta or {}).get("repo", ""), (meta or {}).get("pr_id", ""))
        m = meta or {}
        # create the row (with metadata) if absent
        self._conn.execute(
            """INSERT OR IGNORE INTO review_triage
               (request_id, finding_key, title, agent, file, line, severity)
               VALUES (?,?,?,?,?,?,?)""",
            (request_id, finding_key, m.get("title", ""), m.get("agent", ""),
             m.get("file", ""), str(m.get("line", "")), m.get("severity", "")),
        )
        now = _now()
        if role == "developer":
            self._conn.execute(
                "UPDATE review_triage SET dev_verdict=?, dev_comment=?, dev_by=?, dev_ts=? WHERE request_id=? AND finding_key=?",
                (verdict, comment, by, now, request_id, finding_key))
        else:
            self._conn.execute(
                "UPDATE review_triage SET reviewer_verdict=?, reviewer_comment=?, reviewer_by=?, reviewer_ts=? WHERE request_id=? AND finding_key=?",
                (verdict, comment, by, now, request_id, finding_key))
        self._touch(request_id)
        self._conn.commit()
        return dict(self._conn.execute(
            "SELECT * FROM review_triage WHERE request_id=? AND finding_key=?",
            (request_id, finding_key)).fetchone())

    # ── Stage transitions ─────────────────────────────────────────────────────

    def submit(self, request_id: str, by: str) -> dict:
        """Developer hands off → reviewer stage."""
        self.ensure_session(request_id)
        self._conn.execute(
            "UPDATE review_session SET stage='reviewer', submitted_by=?, updated_at=? WHERE request_id=?",
            (by, _now(), request_id))
        self._conn.commit()
        return self.get_session(request_id)

    def reopen(self, request_id: str, by: str) -> dict:
        """Reviewer sends it back to the developer."""
        self.ensure_session(request_id)
        self._conn.execute(
            "UPDATE review_session SET stage='developer', updated_at=? WHERE request_id=?",
            (_now(), request_id))
        self._conn.commit()
        return self.get_session(request_id)

    def finalize(self, request_id: str, gate: str, by: str, posted: bool = False) -> dict:
        """Reviewer finalizes → done stage, records the gate + whether issues posted."""
        self.ensure_session(request_id)
        self._conn.execute(
            "UPDATE review_session SET stage='done', final_gate=?, finalized_by=?, posted_to_pr=?, updated_at=? WHERE request_id=?",
            (gate, by, 1 if posted else 0, _now(), request_id))
        self._conn.commit()
        return self.get_session(request_id)

    # ── The "real issues" to post: dev VALID ∩ reviewer CONFIRMED ──────────────

    def validated_findings(self, request_id: str) -> list[dict]:
        rows = self.list_triage(request_id)
        return [r for r in rows
                if r.get("dev_verdict") == "valid" and r.get("reviewer_verdict") == "confirmed"]

    def _touch(self, request_id: str) -> None:
        self._conn.execute("UPDATE review_session SET updated_at=? WHERE request_id=?",
                           (_now(), request_id))


class _NullReviewSessionStore:
    def ensure_session(self, *a, **k): return None
    def get_session(self, *a, **k): return None
    def list_triage(self, *a, **k): return []
    def set_triage(self, *a, **k): return {}
    def submit(self, *a, **k): return None
    def reopen(self, *a, **k): return None
    def finalize(self, *a, **k): return None
    def validated_findings(self, *a, **k): return []


_store = None


def get_review_store():
    global _store
    if _store is None:
        try:
            from config.settings import get_settings
            path = getattr(get_settings(), "review_session_db_path", "") or DEFAULT_DB_PATH
            _store = SQLiteReviewSessionStore(path)
            log.info("Review-session store: SQLite (%s)", path)
        except Exception as exc:
            log.warning("Review-session store unavailable (%s) — using no-op", exc)
            _store = _NullReviewSessionStore()
    return _store
