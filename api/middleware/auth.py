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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


_OPEN_PATHS = {
    "/health", "/live", "/ready",
    "/docs", "/openapi.json", "/redoc",
    "/metrics",
}


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

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if self._skip_auth or request.url.path in _OPEN_PATHS:
            return await call_next(request)

        key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if not key:
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

        return JSONResponse(
            {"error": "Unauthorized — invalid API key"},
            status_code=401,
        )
