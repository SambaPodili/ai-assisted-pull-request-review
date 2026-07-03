"""
ingestion/sca_cache.py
----------------------
Last-known-good cache for SCA scan results (Layer-3 fallback).

Every successful scan is persisted (keyed by manifest content + source). When
the vulnerability database is unreachable — after retries and any configured
source fallback — the last successful result is served STALE, clearly labelled
with its age, instead of an empty error page. Never mistaken for fresh data:
the caller must surface `stale`/`stale_from` prominently.

Storage: SQLite in <DATA_DIR>/sca_cache.db (survives restarts/deploys).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time

log = logging.getLogger(__name__)


def _db_path() -> str:
    from config.settings import get_settings
    cfg = get_settings()
    if hasattr(cfg, "data_path"):
        return cfg.data_path("sca_cache.db")
    import os
    return os.path.join(getattr(cfg, "data_dir", "data") or "data", "sca_cache.db")


def _conn():
    c = sqlite3.connect(_db_path(), timeout=2)
    c.execute("CREATE TABLE IF NOT EXISTS sca_results ("
              "key TEXT PRIMARY KEY, result TEXT, saved_at REAL)")
    return c


def cache_key(manifest_text: str, ecosystem: str, source: str) -> str:
    h = hashlib.sha256()
    h.update(ecosystem.encode())
    h.update((source or "osv").encode())
    h.update((manifest_text or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def save(key: str, result: dict) -> None:
    """Persist a SUCCESSFUL scan result. Best-effort — never breaks the scan."""
    try:
        with _conn() as c:
            c.execute("INSERT INTO sca_results (key, result, saved_at) VALUES (?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET result=excluded.result, saved_at=excluded.saved_at",
                      (key, json.dumps(result), time.time()))
    except Exception as exc:
        log.debug("sca_cache save failed: %s", exc)


def load(key: str) -> tuple[dict | None, float]:
    """Return (result, saved_at_epoch) for the last successful scan, or (None, 0)."""
    try:
        with _conn() as c:
            row = c.execute("SELECT result, saved_at FROM sca_results WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row[0]), float(row[1])
    except Exception as exc:
        log.debug("sca_cache load failed: %s", exc)
    return None, 0.0


def age_label(saved_at: float) -> str:
    hours = max(0.0, (time.time() - saved_at) / 3600)
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"{int(hours)} hour(s) ago"
    return f"{int(hours // 24)} day(s) ago"
