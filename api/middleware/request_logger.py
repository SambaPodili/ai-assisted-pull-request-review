"""
api/middleware/request_logger.py
---------------------------------
Structured request/response logging middleware.

Emits one log line per request with:
  - method, path, status_code, duration_ms
  - client IP, request_id (from X-Request-ID header or auto-generated)
  - API key fingerprint (first 8 chars only)

Set LOG_FORMAT=json for structured JSON output (production log aggregators).
Set LOG_FORMAT=text for human-readable output (development default).
"""
from __future__ import annotations
import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = logging.getLogger("api.access")

_SKIP_PATHS = {"/metrics", "/live", "/ready"}


class RequestLoggingMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        request_id = (
            request.headers.get("X-Request-ID")
            or str(uuid.uuid4())
        )
        request.state.request_id = request_id

        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            key_hint = _key_hint(request)
            _log_request(
                method=request.method,
                path=request.url.path,
                status=status_code,
                duration_ms=duration_ms,
                request_id=request_id,
                client_ip=request.client.host if request.client else "-",
                key_hint=key_hint,
            )


def _key_hint(request: Request) -> str:
    key = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    return key[:8] + "…" if len(key) > 8 else key


def _log_request(
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    request_id: str,
    client_ip: str,
    key_hint: str,
) -> None:
    from config.settings import get_settings
    try:
        fmt = get_settings().log_format
    except Exception:
        fmt = "text"

    level = logging.WARNING if status >= 500 else logging.INFO

    if fmt == "json":
        log.log(level, json.dumps({
            "ts":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id":  request_id,
            "method":      method,
            "path":        path,
            "status":      status,
            "duration_ms": duration_ms,
            "client_ip":   client_ip,
            "key":         key_hint,
        }))
    else:
        log.log(
            level,
            "%s %s %d %.1fms rid=%s ip=%s key=%s",
            method, path, status, duration_ms,
            request_id[:8], client_ip, key_hint,
        )
