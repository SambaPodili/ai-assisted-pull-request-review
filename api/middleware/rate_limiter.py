"""
api/middleware/rate_limiter.py
-------------------------------
Token-bucket rate limiter — one bucket per API key (or per client IP when
skip_auth=True).

Algorithm: sliding-window counter.
  - A key is allowed `rpm` requests per 60-second window.
  - Excess requests receive HTTP 429 with a Retry-After header.

The store is in-process memory (no Redis required).  For multi-process
deployments, replace _BUCKETS with a shared Redis counter.
"""
from __future__ import annotations
import logging
import time
from collections import deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

_OPEN_PATHS = {"/health", "/live", "/ready", "/docs", "/openapi.json", "/redoc", "/metrics"}
_WINDOW_S   = 60.0    # sliding window length in seconds


class _SlidingWindow:
    __slots__ = ("_dq", "_lock", "last_seen")

    def __init__(self) -> None:
        self._dq:   deque[float] = deque()
        self._lock: Lock         = Lock()
        self.last_seen: float    = time.monotonic()

    def allow(self, rpm: int) -> bool:
        now = time.monotonic()
        self.last_seen = now
        cutoff = now - _WINDOW_S
        with self._lock:
            while self._dq and self._dq[0] < cutoff:
                self._dq.popleft()
            if len(self._dq) >= rpm:
                return False
            self._dq.append(now)
            return True

    def retry_after(self) -> int:
        """Seconds until the oldest request falls out of the window."""
        if not self._dq:
            return 0
        return max(1, int(_WINDOW_S - (time.monotonic() - self._dq[0])) + 1)


_BUCKETS: dict[str, _SlidingWindow] = {}
_BUCKETS_LOCK = Lock()

# Idle-bucket eviction: without this, every distinct API key / client IP leaves a
# permanent entry in _BUCKETS — an unbounded memory leak and a cheap DoS vector
# (spoofed IPs / random keys). We sweep idle buckets periodically.
_SWEEP_INTERVAL_S = 300.0    # at most one sweep every 5 min
_IDLE_TTL_S       = 600.0    # drop buckets unused for 10 min (state is recreatable)
_MAX_BUCKETS      = 50_000   # hard ceiling — force a sweep if exceeded
_last_sweep       = time.monotonic()


def _maybe_sweep(now: float) -> None:
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_S and len(_BUCKETS) < _MAX_BUCKETS:
        return
    with _BUCKETS_LOCK:
        if now - _last_sweep < _SWEEP_INTERVAL_S and len(_BUCKETS) < _MAX_BUCKETS:
            return
        _last_sweep = now
        stale = [k for k, b in _BUCKETS.items() if now - b.last_seen > _IDLE_TTL_S]
        for k in stale:
            _BUCKETS.pop(k, None)
        if stale:
            log.debug("Rate-limiter swept %d idle buckets (%d remain)", len(stale), len(_BUCKETS))


def _get_bucket(key: str) -> _SlidingWindow:
    _maybe_sweep(time.monotonic())
    with _BUCKETS_LOCK:
        if key not in _BUCKETS:
            _BUCKETS[key] = _SlidingWindow()
        return _BUCKETS[key]


class RateLimitMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, rpm: int = 60, skip_auth: bool = False) -> None:
        super().__init__(app)
        self._rpm       = rpm
        self._skip_auth = skip_auth

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in _OPEN_PATHS:
            return await call_next(request)

        if self._skip_auth:
            return await call_next(request)

        # Identify client by API key or fallback to IP
        key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.client.host if request.client else "unknown"
        )

        bucket = _get_bucket(key)
        if not bucket.allow(self._rpm):
            retry = bucket.retry_after()
            log.warning("Rate limit exceeded for key=%.8s… retry_after=%ds", key, retry)
            return JSONResponse(
                {"error": "Rate limit exceeded", "retry_after_s": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        return await call_next(request)
