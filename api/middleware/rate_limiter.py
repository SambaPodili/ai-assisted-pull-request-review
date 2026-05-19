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
    __slots__ = ("_dq", "_lock")

    def __init__(self) -> None:
        self._dq:   deque[float] = deque()
        self._lock: Lock         = Lock()

    def allow(self, rpm: int) -> bool:
        now = time.monotonic()
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


def _get_bucket(key: str) -> _SlidingWindow:
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
