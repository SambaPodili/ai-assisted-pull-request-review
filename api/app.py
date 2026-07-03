"""
api/app.py
-----------
FastAPI application factory — production-grade.
Wires: orchestrator, report store, audit logger, RBAC, observability, all routes.
"""
from __future__ import annotations
import logging

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from core.orchestrator import ImpactAnalysisOrchestrator
from storage.report_store import make_report_store, ReportStore
from governance.audit_logger import make_audit_logger, NullAuditLogger
from governance.observability import init_tracing, prometheus_metrics_response
from governance.rbac import get_registry
from api.middleware.auth import APIKeyMiddleware
from api.middleware.rate_limiter import RateLimitMiddleware
from api.middleware.request_logger import RequestLoggingMiddleware
from api.routes.analysis  import router as analysis_router
from api.routes.webhooks  import router as webhook_router
from api.routes.admin     import router as admin_router
from api.routes.gate_override import router as gate_router
from api.routes.git_proxy import router as git_proxy_router
from api.routes.health    import router as health_router
from api.routes.evaluate  import router as evaluate_router
from api.routes.quality   import router as quality_router
from api.routes.insights  import router as insights_router
from api.routes.review_session import router as review_router

log = logging.getLogger(__name__)

_orchestrator: ImpactAnalysisOrchestrator | None = None
_report_store: ReportStore                | None = None
_audit_logger                                    = None


def get_orchestrator() -> ImpactAnalysisOrchestrator:
    if _orchestrator is None:
        raise RuntimeError("App not initialised — call create_app() first")
    return _orchestrator


def get_report_store() -> ReportStore:
    if _report_store is None:
        raise RuntimeError("App not initialised — call create_app() first")
    return _report_store


def get_audit_logger():
    return _audit_logger or NullAuditLogger()


def create_app(settings=None) -> FastAPI:
    global _orchestrator, _report_store, _audit_logger

    from config.settings import get_settings
    cfg = settings or get_settings()

    # Configure ROOT logging here (not only in main.py): with uvicorn --reload or
    # --workers, the worker imports api.app directly and main.py's basicConfig
    # never runs — leaving app INFO logs invisible (only WARNING+ via Python's
    # last-resort handler). Without this, diagnostics like "[id] Analyse
    # submitted — diff=N bytes → M hunks" silently vanish.
    import logging as _logging
    if not _logging.getLogger().handlers:
        _logging.basicConfig(
            level=getattr(_logging, str(getattr(cfg, "log_level", "INFO")).upper(), _logging.INFO),
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        )

    if getattr(cfg, "otlp_endpoint", ""):
        init_tracing(cfg.otlp_endpoint)

    _orchestrator = ImpactAnalysisOrchestrator(
        api_key=cfg.anthropic_api_key or None,
        phase=cfg.analysis_phase,
    )
    _report_store = make_report_store(cfg)
    _audit_logger = make_audit_logger(cfg)

    get_registry().load_from_settings(cfg)

    app = FastAPI(
        title="Code Analysis & Review",
        description=(
            "Enterprise AI multi-agent code impact and security review framework.\n\n"
            "Supports GitHub, Bitbucket, GitLab webhooks, direct diff submission, "
            "CI/CD pipeline gating, and compliance checking (MAS TRM, PCI-DSS 4.0, OWASP ASVS)."
        ),
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {"name": "analysis",   "description": "Submit diffs and retrieve reports"},
            {"name": "webhooks",   "description": "Git provider webhook endpoints"},
            {"name": "health",     "description": "Liveness, readiness and health checks"},
            {"name": "admin",      "description": "Administration and circuit-breaker controls"},
            {"name": "gate",       "description": "Manual gate override (analyst+ role required)"},
        ],
    )

    # ── Middleware (outermost first — last applied to request) ────────────────
    cors_origins = getattr(cfg, "cors_origins", ["*"])
    # A wildcard origin with credentials is rejected by browsers and forbidden by
    # the CORS spec. Auth uses the X-API-Key header (not cookies), so credentials
    # are only enabled when explicit origins are configured.
    wildcard = "*" in cors_origins
    if wildcard:
        cors_origins = ["*"]
        log.warning("CORS allow_origins is '*' — set CORS_ORIGINS to your UI origin(s) for production.")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=not wildcard,
    )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        rpm=getattr(cfg, "rate_limit_rpm", 60),
        skip_auth=getattr(cfg, "skip_auth", False),
    )
    app.add_middleware(
        APIKeyMiddleware,
        api_keys=cfg.api_keys,
        skip_auth=cfg.skip_auth,
    )

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(health_router)          # /health, /ready, /live — no auth
    app.include_router(analysis_router)
    app.include_router(evaluate_router)
    app.include_router(quality_router)
    app.include_router(webhook_router)
    app.include_router(admin_router)
    app.include_router(gate_router)
    app.include_router(git_proxy_router)
    app.include_router(insights_router)
    app.include_router(review_router)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        body, ct = prometheus_metrics_response()
        return Response(content=body, media_type=ct)

    # ── Global error handling ─────────────────────────────────────────────────
    # Any unhandled exception is logged with the request-id and returned as a
    # sanitized JSON envelope — never a stack trace to the client.
    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: _Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("Unhandled error rid=%s %s %s: %s",
                      rid, request.method, request.url.path, exc)
        return _JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "request_id": rid},
        )

    # ── Static UI (optional, served from the SAME service) ────────────────────
    # When the frontend has been built (frontend/dist), serve it from this service
    # so a no-Docker / single-systemd deploy needs no separate web server. Assets
    # live under /assets (a specific mount — never shadows API routes); the SPA
    # shell is served via a 404 FALLBACK so any unmatched GET returns index.html
    # WITHOUT shadowing real routes. Static paths are auth-exempt (see auth.py).
    from pathlib import Path as _Path
    _dist = _Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if (_dist / "index.html").exists():
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse as _FileResponse
        from starlette.exceptions import HTTPException as _StarletteHTTPException
        if (_dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")
        _API_PREFIXES = ("/api", "/admin", "/health", "/live", "/ready",
                         "/docs", "/redoc", "/openapi", "/metrics", "/webhook")

        @app.exception_handler(_StarletteHTTPException)
        async def _spa_or_404(request, exc):
            # Genuine API 404s keep their JSON shape; unmatched GETs for non-API
            # paths serve the SPA (a real top-level file if present, else index).
            if (exc.status_code == 404 and request.method == "GET"
                    and not request.url.path.startswith(_API_PREFIXES)):
                cand = (_dist / request.url.path.lstrip("/")).resolve()
                if cand.is_file() and str(cand).startswith(str(_dist.resolve())):
                    return _FileResponse(str(cand))
                return _FileResponse(str(_dist / "index.html"))
            return _JSONResponse(status_code=exc.status_code,
                                 content={"detail": getattr(exc, "detail", "Not Found")})
        log.info("Serving frontend UI from %s (assets at /assets, SPA fallback)", _dist)
    else:
        log.info("No built frontend at %s — UI not served. Build it with "
                 "`cd frontend && npm run build` (API still runs).", _dist)

    # ── Daily email digest scheduler (optional) ───────────────────────────────
    if getattr(cfg, "digest_enabled", False):
        _start_digest_scheduler(cfg)

    log.info(
        "App ready. Phase=%d Provider=%s CORS=%s OTLP=%s",
        cfg.analysis_phase,
        cfg.git_provider,
        cors_origins,
        bool(getattr(cfg, "otlp_endpoint", "")),
    )
    return app


def _start_digest_scheduler(cfg) -> None:
    """
    Fire the daily digest once every 24h on a daemon thread.
    Zero external dependencies — uses threading.Timer-style loop.
    The DIGEST_SEND_HOUR env (0-23 UTC) controls the target hour; default 8.
    """
    import threading, time as _time
    from datetime import datetime as _dt, timezone as _tz

    send_hour = getattr(cfg, "digest_send_hour", 8)

    def _loop():
        # Send once shortly after startup if we're already at/after send_hour today,
        # otherwise wait until the next send_hour.
        while True:
            now = _dt.now(_tz.utc)
            target = now.replace(hour=send_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                from datetime import timedelta as _td
                target = target + _td(days=1)
            sleep_s = max(60, (target - now).total_seconds())
            _time.sleep(sleep_s)
            try:
                from output.digest import send_digest
                result = send_digest(days=1)
                log.info("Scheduled digest: %s", result)
            except Exception as exc:
                log.error("Scheduled digest failed: %s", exc)

    t = threading.Thread(target=_loop, name="digest-scheduler", daemon=True)
    t.start()
    log.info("Digest scheduler started (daily at %02d:00 UTC)", send_hour)


app = create_app()
