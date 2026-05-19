"""
governance/diff_cache.py
-------------------------
SHA-256 content-addressed cache for analysis results.

If the same diff content is submitted twice (e.g. repeated PR webhook,
rebase that produces identical changes), the second request returns the
cached report immediately without spending any tokens.

TTL: 24 hours by default. Backed by Redis when available, in-memory otherwise.
"""
from __future__ import annotations
import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import AnalysisReport, AnalysisRequest

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60 * 60 * 24   # 24 hours


def _diff_fingerprint(request: "AnalysisRequest") -> str:
    """
    Stable cache key from the diff content + analysis phase.
    Ignores request_id and timestamp so identical diffs hit the same key.
    """
    content = json.dumps(
        {
            "repo":    request.repo_url,
            "src":     request.source_ref,
            "dst":     request.target_ref,
            "hunks":   [(h.file_path, h.additions, h.deletions, hash(h.content)) for h in request.hunks],
        },
        sort_keys=True,
    )
    return hashlib.sha256(content.encode()).hexdigest()


class DiffCache:
    """Content-addressed cache for AnalysisReport objects."""

    def __init__(self, redis_url: str = "", ttl: int = CACHE_TTL_SECONDS) -> None:
        self._ttl   = ttl
        self._redis = None
        self._local: dict[str, str] = {}   # fingerprint → JSON

        if redis_url:
            try:
                import redis
                self._redis = redis.from_url(redis_url, decode_responses=True,
                                             socket_connect_timeout=2,
                                             socket_timeout=2)
                self._redis.ping()
                log.info("DiffCache: using Redis at %s", redis_url)
            except Exception as e:
                self._redis = None   # ← KEY FIX: was missing; kept broken client
                log.warning("DiffCache: Redis unavailable (%s) — using in-memory cache", e)

    def get(self, request: "AnalysisRequest") -> "AnalysisReport | None":
        from core.models import AnalysisReport
        key = _diff_fingerprint(request)
        try:
            data = self._redis.get(key) if self._redis else self._local.get(key)
        except Exception as e:
            log.warning("[DiffCache] Redis GET failed (%s) — skipping cache", e)
            self._redis = None   # stop trying Redis for this session
            return None
        if not data:
            return None
        try:
            report = AnalysisReport.model_validate_json(data)
            log.info("[DiffCache] HIT for %s → %s", request.request_id, key[:12])
            return report
        except Exception as e:
            log.warning("[DiffCache] Deserialise error: %s", e)
            return None

    def set(self, request: "AnalysisRequest", report: "AnalysisReport") -> None:
        key  = _diff_fingerprint(request)
        data = report.model_dump_json()
        try:
            if self._redis:
                self._redis.setex(key, self._ttl, data)
            else:
                self._local[key] = data
                if len(self._local) > 1000:
                    oldest = next(iter(self._local))
                    del self._local[oldest]
            log.debug("[DiffCache] SET %s", key[:12])
        except Exception as e:
            log.warning("[DiffCache] Redis write failed (%s) — storing in memory", e)
            self._redis = None
            self._local[key] = data

    def invalidate(self, request: "AnalysisRequest") -> None:
        key = _diff_fingerprint(request)
        try:
            if self._redis:
                self._redis.delete(key)
            else:
                self._local.pop(key, None)
        except Exception as e:
            log.warning("[DiffCache] Invalidate failed: %s", e)
            self._local.pop(key, None)


# ── Module-level singleton ────────────────────────────────────────────────────

_cache: DiffCache | None = None


def get_diff_cache() -> DiffCache:
    global _cache
    if _cache is None:
        from config.settings import get_settings
        _cache = DiffCache(redis_url=get_settings().redis_url)
    return _cache
