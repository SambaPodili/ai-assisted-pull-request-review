"""
api/middleware/auth.py
-----------------------
API key authentication middleware.

Checks keys against both:
  1. The RBAC registry (rich key objects with roles/permissions)
  2. The raw api_keys list from settings (simple string keys)

This ensures backward compatibility with plain string keys in settings.json
while also supporting structured RBAC key objects.

IMPORTANT: OPTIONS requests (CORS preflight) must always be allowed through
without auth — the browser sends these before every cross-origin request and
they never carry credentials. Blocking them causes "Failed to fetch" errors.
"""
from __future__ import annotations
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


_OPEN_PATHS = {
    "/health", "/live", "/ready",
    "/docs", "/openapi.json", "/redoc",
    "/metrics",
}

# Static UI assets are public so the SPA shell can load WITHOUT a key (the UI then
# sends the X-API-Key on every /api call). Only the static shell is open; all API
# routes (/api/…, /admin, etc.) still require auth.
_STATIC_SUFFIXES = (
    ".js", ".css", ".svg", ".ico", ".png", ".jpg", ".jpeg", ".gif",
    ".woff", ".woff2", ".ttf", ".map", ".webmanifest", ".html", ".txt",
)


def _is_open_path(path: str) -> bool:
    if path in _OPEN_PATHS:
        return True
    if path == "/" or path.startswith("/assets/"):
        return True
    return path.endswith(_STATIC_SUFFIXES)


class APIKeyMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, api_keys: list[str], skip_auth: bool = False) -> None:
        super().__init__(app)
        # Normalise: extract raw key strings from dicts (RBAC key objects)
        self._keys: set[str] = set()
        for entry in api_keys:
            if isinstance(entry, str):
                self._keys.add(entry)
            elif isinstance(entry, dict):
                k = entry.get("key", "")
                if k:
                    self._keys.add(k)
        self._skip_auth = skip_auth
        if skip_auth:
            # Security-relevant config: surface loudly so it's never silently on in prod.
            log.warning("APIKeyMiddleware: SKIP_AUTH is ENABLED — all requests bypass "
                        "API-key authentication. Never use this in production.")
        else:
            log.info("APIKeyMiddleware: enabled with %d configured key(s)", len(self._keys))

    @staticmethod
    def _client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if self._skip_auth or _is_open_path(request.url.path):
            return await call_next(request)

        key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if not key:
            # Never log the key itself — only that a credential was missing, and where.
            log.warning("Auth rejected (401, no credential) %s %s from %s",
                        request.method, request.url.path, self._client(request))
            return JSONResponse(
                {"error": "Unauthorized — provide X-API-Key or Authorization: Bearer <key>"},
                status_code=401,
            )

        # Check RBAC registry first (preferred)
        from governance.rbac import get_registry
        if get_registry().resolve(key) is not None:
            return await call_next(request)

        # Fallback: check raw key set from settings
        if key in self._keys:
            return await call_next(request)

        # Invalid key — log the event (and a short fingerprint, never the key) so
        # brute-force / misconfig is visible without leaking the secret.
        log.warning("Auth rejected (401, invalid key …%s) %s %s from %s",
                    key[-4:] if len(key) >= 4 else "??",
                    request.method, request.url.path, self._client(request))
        return JSONResponse(
            {"error": "Unauthorized — invalid API key"},
            status_code=401,
        )
