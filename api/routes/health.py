"""
api/routes/health.py
---------------------
Liveness, readiness and full health-check endpoints.

  GET /live   — liveness probe: is the process running?
  GET /ready  — readiness probe: are all dependencies healthy?
  GET /health — full health summary with dependency status

All three endpoints bypass authentication so Kubernetes/load-balancer probes
work without credentials.
"""
from __future__ import annotations
import logging
import time
from typing import Any

from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_START_TIME = time.time()


@router.get("/live", include_in_schema=True)
def liveness():
    """Liveness probe — always returns 200 while the process is running."""
    return {"status": "ok", "uptime_s": round(time.time() - _START_TIME, 1)}


@router.get("/ready", include_in_schema=True)
def readiness():
    """
    Readiness probe — returns 200 when the orchestrator and store are initialised.
    Returns 503 if either is not yet ready (e.g. startup still in progress).
    """
    from fastapi import HTTPException
    try:
        from api.app import get_orchestrator, get_report_store
        get_orchestrator()
        get_report_store()
        return {"status": "ready"}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Not ready: {exc}")


@router.get("/health", include_in_schema=True)
def health():
    """Full health check — checks all optional dependencies (Redis, Neo4j)."""
    checks: dict[str, Any] = {
        "uptime_s":    round(time.time() - _START_TIME, 1),
        "status":      "ok",
        "components":  {},
    }

    # Orchestrator
    try:
        from api.app import get_orchestrator
        get_orchestrator()
        checks["components"]["orchestrator"] = {"status": "ok"}
    except Exception as exc:
        checks["components"]["orchestrator"] = {"status": "error", "detail": str(exc)}
        checks["status"] = "degraded"

    # Report store
    try:
        from api.app import get_report_store
        store = get_report_store()
        checks["components"]["report_store"] = {
            "status":  "ok",
            "backend": type(store).__name__,
        }
    except Exception as exc:
        checks["components"]["report_store"] = {"status": "error", "detail": str(exc)}
        checks["status"] = "degraded"

    # Redis (optional)
    try:
        from config.settings import get_settings
        cfg = get_settings()
        if cfg.redis_url:
            import redis as redis_client
            r = redis_client.from_url(cfg.redis_url, socket_connect_timeout=2)
            r.ping()
            checks["components"]["redis"] = {"status": "ok"}
    except ImportError:
        pass
    except Exception as exc:
        checks["components"]["redis"] = {"status": "error", "detail": str(exc)}
        checks["status"] = "degraded"

    # Circuit breakers
    try:
        from governance.circuit_breaker import get_breaker_registry
        breakers = get_breaker_registry().all_metrics()
        open_count = sum(1 for b in breakers if b["state"] == "open")
        checks["components"]["circuit_breakers"] = {
            "status":      "degraded" if open_count else "ok",
            "total":       len(breakers),
            "open":        open_count,
        }
        if open_count:
            checks["status"] = "degraded"
    except Exception as exc:
        log.debug("Circuit breaker health check failed: %s", exc)

    # LLM provider key + optional reachability
    try:
        from config.settings import get_settings
        cfg = get_settings()
        provider = getattr(cfg, "llm_provider", "anthropic")
        has_key  = bool(
            cfg.anthropic_api_key if provider == "anthropic"
            else getattr(cfg, "openai_api_key", "")
        )
        llm_check: dict[str, Any] = {
            "status":   "ok" if has_key else "degraded",
            "provider": provider,
            "key_set":  has_key,
        }
        if not has_key:
            llm_check["detail"] = "No API key configured"
            checks["status"] = "degraded"
        checks["components"]["llm"] = llm_check
    except Exception as exc:
        checks["components"]["llm"] = {"status": "error", "detail": str(exc)}
        checks["status"] = "degraded"

    return checks
