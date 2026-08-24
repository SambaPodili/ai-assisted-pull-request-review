"""
governance/user_store.py
-------------------------
UI-managed users with HASHED API keys (SQLite).

Created via the admin screen instead of hand-editing keys.json. The raw API key
is generated server-side, only its SHA-256 hash is stored, and the plaintext is
returned exactly ONCE on creation (copy-and-store; never recoverable after).

The RBAC registry resolves a request against both the file-based keys (bootstrap
/ CI) and these managed users, so the two coexist.

Backend: SQLite. Degrades to a no-op store if SQLite is unavailable.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from governance.rbac import Subject, Role

log = logging.getLogger(__name__)
DEFAULT_DB_PATH = "data/users.db"
_KEY_PREFIX = "gto_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()


def _gen_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


class SQLiteUserStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                key_hash    TEXT UNIQUE NOT NULL,
                key_prefix  TEXT DEFAULT '',
                user_id     TEXT DEFAULT '',            -- external identity (e.g. Bitbucket slug)
                name        TEXT DEFAULT '',            -- display name (e.g. Bitbucket displayName)
                team        TEXT DEFAULT '',
                roles       TEXT DEFAULT 'developer',   -- csv of role values
                created_by  TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                active      INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_users_hash ON users(key_hash);
            CREATE TABLE IF NOT EXISTS user_audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                action     TEXT NOT NULL,      -- created | revoked | updated
                actor      TEXT DEFAULT '',
                target_id  TEXT DEFAULT '',
                target     TEXT DEFAULT '',    -- target user name
                roles      TEXT DEFAULT '',
                detail     TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_user_audit_ts ON user_audit(ts);
        """)
        # Migration: add user_id to pre-existing DBs that were created before it.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()}
        if "user_id" not in cols:
            self._conn.execute("ALTER TABLE users ADD COLUMN user_id TEXT DEFAULT ''")
        self._conn.commit()

    # ── Audit trail ───────────────────────────────────────────────────────────

    def record_event(self, action: str, actor: str, target_id: str = "",
                     target: str = "", roles: list[Role] | None = None,
                     detail: str = "") -> None:
        self._conn.execute(
            """INSERT INTO user_audit (ts, action, actor, target_id, target, roles, detail)
               VALUES (?,?,?,?,?,?,?)""",
            (_now(), action, actor, target_id, target,
             ",".join(r.value for r in (roles or [])), detail),
        )
        self._conn.commit()

    def list_audit(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM user_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r["ts"], "action": r["action"], "actor": r["actor"],
                 "target_id": r["target_id"], "target": r["target"],
                 "roles": [x for x in (r["roles"] or "").split(",") if x],
                 "detail": r["detail"]} for r in rows]

    # ── Resolution (auth) ─────────────────────────────────────────────────────

    def resolve(self, key: str) -> Subject | None:
        if not key:
            return None
        row = self._conn.execute(
            "SELECT * FROM users WHERE key_hash=? AND active=1", (_hash(key),)).fetchone()
        if row is None:
            return None
        return Subject(key_id=row["id"], roles=_parse_roles(row["roles"]),
                       name=row["name"], team=row["team"],
                       user_id=(row["user_id"] if "user_id" in row.keys() else ""))

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create_user(self, name: str, team: str, roles: list[Role],
                    created_by: str, user_id: str = "") -> tuple[str, dict]:
        """Create a user; returns (plaintext_key, record). Key shown ONCE.
        `user_id` is the external identity (e.g. Bitbucket slug); `name` is the
        display name (e.g. Bitbucket displayName)."""
        key = _gen_key()
        uid = uuid.uuid4().hex[:16]
        prefix = key[:12] + "…"
        self._conn.execute(
            """INSERT INTO users (id, key_hash, key_prefix, user_id, name, team, roles, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (uid, _hash(key), prefix, user_id, name, team,
             ",".join(r.value for r in roles), created_by, _now()),
        )
        self._conn.commit()
        log.warning("User created: %s (%s) roles=%s by %s",
                    uid, name, [r.value for r in roles], created_by)
        return key, self.get(uid)

    def get(self, uid: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return _redact(row) if row else None

    def list_users(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM users WHERE active=1 ORDER BY created_at DESC").fetchall()
        return [_redact(r) for r in rows]

    def roles_of(self, uid: str) -> list[Role]:
        row = self._conn.execute("SELECT roles FROM users WHERE id=?", (uid,)).fetchone()
        return _parse_roles(row["roles"]) if row else []

    def update(self, uid: str, name: str | None = None, team: str | None = None,
               roles: list[Role] | None = None, user_id: str | None = None) -> dict | None:
        sets, args = [], []
        if name is not None: sets.append("name=?");  args.append(name)
        if team is not None: sets.append("team=?");  args.append(team)
        if user_id is not None: sets.append("user_id=?"); args.append(user_id)
        if roles is not None: sets.append("roles=?"); args.append(",".join(r.value for r in roles))
        if not sets:
            return self.get(uid)
        args.append(uid)
        self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
        self._conn.commit()
        return self.get(uid)

    def revoke(self, uid: str) -> bool:
        cur = self._conn.execute("UPDATE users SET active=0 WHERE id=? AND active=1", (uid,))
        self._conn.commit()
        return cur.rowcount > 0


def _parse_roles(csv: str) -> list[Role]:
    out: list[Role] = []
    for r in (csv or "").split(","):
        r = r.strip()
        if not r:
            continue
        try:
            out.append(Role(r))
        except ValueError:
            pass
    return out or [Role.DEVELOPER]


def _redact(row) -> dict:
    keys = row.keys()
    return {
        "id":         row["id"],
        "key_prefix": row["key_prefix"],
        "user_id":    row["user_id"] if "user_id" in keys else "",
        "name":       row["name"],
        "team":       row["team"],
        "roles":      [r.value for r in _parse_roles(row["roles"])],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


class _NullUserStore:
    def resolve(self, *a, **k): return None
    def create_user(self, *a, **k): raise RuntimeError("user store unavailable")
    def get(self, *a, **k): return None
    def list_users(self, *a, **k): return []
    def roles_of(self, *a, **k): return []
    def update(self, *a, **k): return None
    def revoke(self, *a, **k): return False
    def record_event(self, *a, **k): return None
    def list_audit(self, *a, **k): return []


_store = None


def get_user_store():
    global _store
    if _store is None:
        try:
            from config.settings import get_settings
            _cfg = get_settings()
            path = getattr(_cfg, "user_db_path", "") or _cfg.data_path("users.db")
            _store = SQLiteUserStore(path)
        except Exception as exc:
            log.warning("User store unavailable (%s) — using no-op", exc)
            _store = _NullUserStore()
    return _store
