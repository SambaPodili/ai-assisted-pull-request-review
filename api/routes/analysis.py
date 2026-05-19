"""
api/routes/analysis.py
-----------------------
REST endpoints for direct analysis submission and report retrieval.

Production changes vs. initial implementation:
  - Thread-safe in-flight tracker with TTL cleanup (no memory leak)
  - Request size limit (MAX_DIFF_BYTES) to prevent unbounded diff uploads
  - Per-analysis timeout (ANALYSIS_TIMEOUT_S) to prevent pipeline hangs
  - Request-ID propagation from middleware
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType,
)
from ingestion.diff_parser import parse_diff
from output.report_formatter import to_summary_json, to_markdown

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["analysis"])

# ── Thread-safe in-flight tracker ────────────────────────────────────────────

class _InFlight:
    """
    Thread-safe dict with TTL-based eviction so completed entries don't
    accumulate indefinitely in long-running processes.
    """
    _TTL = 3600      # keep entries for 1 hour after completion
    _GC_INTERVAL = 300  # run GC every 5 minutes

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}  # id → (status, expires_at)
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def set(self, request_id: str, status: str) -> None:
        expires = time.monotonic() + self._TTL
        with self._lock:
            self._data[request_id] = (status, expires)
            self._maybe_gc()

    def get(self, request_id: str) -> str | None:
        with self._lock:
            entry = self._data.get(request_id)
            if entry is None:
                return None
            status, expires = entry
            if time.monotonic() > expires:
                del self._data[request_id]
                return None
            return status

    def _maybe_gc(self) -> None:
        now = time.monotonic()
        if now - self._last_gc < self._GC_INTERVAL:
            return
        self._last_gc = now
        expired = [k for k, (_, exp) in self._data.items() if now > exp]
        for k in expired:
            del self._data[k]


_in_flight = _InFlight()


# ── Request / Response models ─────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    repo_url:     str
    source_ref:   str
    target_ref:   str
    change_type:  ChangeType = ChangeType.BRANCH
    diff_text:    str = ""
    metadata:     dict[str, Any] = {}
    llm_config:   dict[str, Any] = {}
    model_config_override: dict[str, Any] = {}

    @field_validator("diff_text")
    @classmethod
    def _check_diff_size(cls, v: str) -> str:
        from config.settings import get_settings
        max_bytes = get_settings().max_diff_bytes
        if len(v.encode()) > max_bytes:
            raise ValueError(
                f"diff_text exceeds maximum allowed size ({max_bytes // 1024} KB). "
                "Split large diffs or submit per-file."
            )
        return v


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/analyse", status_code=202)
async def submit_analysis(
    payload: AnalyseRequest,
    background: BackgroundTasks,
    request: Request,
):
    """
    Submit a code diff for asynchronous analysis.
    Returns immediately with a request_id; poll /report/{id} for results.
    """
    from api.app import get_orchestrator, get_report_store
    from config.settings import get_settings
    orch    = get_orchestrator()
    store   = get_report_store()
    cfg     = get_settings()

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    hunks      = parse_diff(payload.diff_text) if payload.diff_text else []
    llm_cfg    = payload.llm_config or payload.model_config_override or {}

    req = AnalysisRequest(
        request_id=request_id,
        change_type=payload.change_type,
        repo_url=payload.repo_url,
        source_ref=payload.source_ref,
        target_ref=payload.target_ref,
        hunks=hunks,
        metadata=payload.metadata,
        model_config_=llm_cfg,
    )

    _in_flight.set(request_id, "running")

    async def _run():
        timeout = getattr(cfg, "analysis_timeout_s", 300)
        try:
            report = await asyncio.wait_for(
                orch.analyse_async(req),
                timeout=timeout,
            )
            store.save(report)
            _in_flight.set(request_id, "done")
            log.info("[%s] Analysis complete — gate=%s", request_id, report.gate_decision.value)
        except asyncio.TimeoutError:
            log.error("[%s] Analysis timed out after %ds", request_id, timeout)
            _in_flight.set(request_id, f"error: timed out after {timeout}s")
            _save_error(store, req, f"Analysis timed out after {timeout}s")
        except Exception as exc:
            log.error("[%s] Analysis failed: %s", request_id, exc, exc_info=True)
            _in_flight.set(request_id, f"error: {exc}")
            _save_error(store, req, str(exc))

    background.add_task(_run)
    return {"request_id": request_id, "status": "queued"}


@router.get("/progress/{request_id}")
def get_agent_progress(request_id: str):
    """
    Per-agent live status while analysis is running.
    Returns the status, elapsed time, and tokens for every agent seen so far.
    Poll this every 1-2 seconds from the UI for a live progress display.
    """
    from core.progress import get_progress_store
    run = get_progress_store().get(request_id)
    if not run:
        return {"request_id": request_id, "agents": []}
    return {"request_id": request_id, "agents": run.snapshot()}


@router.get("/status/{request_id}")
def get_status(request_id: str):
    """Quick status check without retrieving the full report."""
    status = _in_flight.get(request_id)
    if status is None:
        # May be done and evicted from in-flight — check the store
        from api.app import get_report_store
        if get_report_store().get(request_id):
            return {"request_id": request_id, "status": "done"}
        return {"request_id": request_id, "status": "unknown"}
    return {"request_id": request_id, "status": status}


@router.get("/report/{request_id}")
def get_report(request_id: str, fmt: str = "json"):
    """
    Retrieve a completed analysis report.

    fmt=json  → compact summary JSON (default)
    fmt=full  → full JSON with all agent results
    fmt=md    → Markdown report
    Returns 202 while still processing.
    """
    from api.app import get_report_store
    store  = get_report_store()

    status = _in_flight.get(request_id)
    if status == "running":
        raise HTTPException(202, detail="Analysis still running — please poll again shortly.")

    report = store.get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")

    if fmt == "md":
        return {"markdown": to_markdown(report)}
    if fmt == "full":
        return report.model_dump()
    return to_summary_json(report)


@router.get("/reports")
def list_reports(limit: int = 20, offset: int = 0):
    """List recent analysis reports (newest first)."""
    from api.app import get_report_store
    limit = min(limit, 100)
    return get_report_store().list_recent(limit=limit, offset=offset)


@router.delete("/report/{request_id}", status_code=204)
def delete_report(request_id: str):
    from api.app import get_report_store
    if not get_report_store().delete(request_id):
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_error(store, req: AnalysisRequest, message: str) -> None:
    try:
        error_report = AnalysisReport(
            request_id=req.request_id,
            change_type=req.change_type,
            repo_url=req.repo_url,
            source_ref=req.source_ref,
            target_ref=req.target_ref,
            errors=[message],
        )
        store.save(error_report)
    except Exception as exc:
        log.error("[%s] Could not save error report: %s", req.request_id, exc)
