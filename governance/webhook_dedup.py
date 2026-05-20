"""
governance/webhook_dedup.py
----------------------------
Idempotency guard for incoming webhooks.

Two backends:
  • In-memory  — TTL dict; single-process, resets on restart (default)
  • Redis      — cross-process, survives restarts (used when cfg.redis_url is set)

Key format: "{provider}:{event_key}"
where event_key is the most specific available identifier:
  GitHub  → X-GitHub-Delivery header (unique per delivery)
  Bitbucket → pr_id + head_sha from payload  (no delivery id in BB)

Usage:
    store = make_dedup_store()
    if store.is_duplicate("github", "abc123-delivery-id"):
        return {"status": "duplicate", "request_id": None}
    store.mark_seen("github", "abc123-delivery-id")
"""
from __future__ import annotations
import logging
import threading
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class WebhookDedupStore(ABC):
    @abstractmethod
    def is_duplicate(self, provider: str, event_key: str) -> bool: ...

    @abstractmethod
    def mark_seen(self, provider: str, event_key: str) -> None: ...

    def key(self, provider: str, event_key: str) -> str:
        return f"wh_dedup:{provider}:{event_key}"


# ── In-memory backend ─────────────────────────────────────────────────────────

class InMemoryDedupStore(WebhookDedupStore):
    """Thread-safe TTL dict. Expired entries are swept lazily on mark_seen."""

    def __init__(self, ttl_s: int = 300) -> None:
        self._ttl   = ttl_s
        self._store: dict[str, float] = {}   # key → expiry timestamp
        self._lock  = threading.Lock()

    def is_duplicate(self, provider: str, event_key: str) -> bool:
        k = self.key(provider, event_key)
        with self._lock:
            expiry = self._store.get(k, 0.0)
            return time.time() < expiry

    def mark_seen(self, provider: str, event_key: str) -> None:
        k   = self.key(provider, event_key)
        now = time.time()
        with self._lock:
            self._store[k] = now + self._ttl
            # Lazy GC: sweep expired keys on every write
            expired = [key for key, exp in self._store.items() if exp <= now]
            for key in expired:
                del self._store[key]


# ── Redis backend ─────────────────────────────────────────────────────────────

class RedisDedupStore(WebhookDedupStore):
    """Uses Redis SETNX + EXPIRE for cross-process deduplication."""

    def __init__(self, redis_url: str, ttl_s: int = 300) -> None:
        import redis
        self._r   = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_s

    def is_duplicate(self, provider: str, event_key: str) -> bool:
        return bool(self._r.exists(self.key(provider, event_key)))

    def mark_seen(self, provider: str, event_key: str) -> None:
        self._r.set(self.key(provider, event_key), "1", ex=self._ttl)


# ── Factory ───────────────────────────────────────────────────────────────────

_store: WebhookDedupStore | None = None
_store_lock = threading.Lock()


def get_dedup_store() -> WebhookDedupStore:
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        from config.settings import get_settings
        cfg = get_settings()
        ttl = cfg.webhook_dedup_ttl_s
        if cfg.redis_url:
            try:
                _store = RedisDedupStore(cfg.redis_url, ttl_s=ttl)
                log.info("Webhook dedup: Redis backend (ttl=%ds)", ttl)
                return _store
            except Exception as exc:
                log.warning("Webhook dedup: Redis unavailable (%s) — falling back to in-memory", exc)
        _store = InMemoryDedupStore(ttl_s=ttl)
        log.info("Webhook dedup: in-memory backend (ttl=%ds)", ttl)
        return _store
