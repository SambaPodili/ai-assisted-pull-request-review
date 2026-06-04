"""
api/routes/evaluate.py
-----------------------
REST endpoint for LLM-as-judge evaluation of a stored analysis report.

POST /api/v1/evaluate/{request_id}
  Body: EvaluateRequest
  Returns: EvaluationReport (JSON)

The endpoint pulls the stored AnalysisReport, runs the judge panel against
the provided diff text, and returns the consolidated verdicts.
"""
from __future__ import annotations
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["evaluation"])


# ── Request / Response schemas ─────────────────────────────────────────────────

class JudgeConfig(BaseModel):
    """Configuration for a single LLM judge."""
    provider:  str = "anthropic"
    model:     str = "claude-sonnet-4-6"
    api_key:   str = ""
    base_url:  str = ""


class EvaluateRequest(BaseModel):
    """
    Body for POST /api/v1/evaluate/{request_id}.

    diff_text    : the original code diff that was analysed (required)
    agent_names  : which agents to evaluate; omit to evaluate all non-None results
    judges       : judge configurations; if empty, defaults to 3 Anthropic judges
                   (claude-sonnet-4-6 × 2, claude-haiku-4-5-20251001 × 1)
    async_mode   : if True, start evaluation in background and return a job_id;
                   poll GET /api/v1/evaluation/{job_id} for results
    """
    diff_text:   str
    agent_names: list[str]        = []
    judges:      list[JudgeConfig] = []
    async_mode:  bool             = False


# ── In-memory evaluation result store ─────────────────────────────────────────

_eval_results: dict[str, Any] = {}


def _default_judges() -> list[dict]:
    """
    Three Anthropic judges using different models.
    Reads ANTHROPIC_API_KEY from settings; individual overrides can specify api_key.
    """
    from config.settings import get_settings
    key = get_settings().anthropic_api_key or ""
    return [
        {"provider": "anthropic", "model": "claude-sonnet-4-6",        "api_key": key},
        {"provider": "anthropic", "model": "claude-sonnet-4-6",        "api_key": key},
        {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "api_key": key},
    ]


def _fill_judge_keys(cfgs: list[dict]) -> None:
    """For any judge missing an api_key, inject the backend env key for its
    provider so the judge panel works without per-judge keys (the UI only sends
    a key when the judge's provider matches the chosen analysis model)."""
    from config.settings import get_settings
    cfg = get_settings()
    anth = cfg.anthropic_api_key or ""
    oai  = getattr(cfg, "openai_api_key", "") or ""
    for j in cfgs:
        if j.get("api_key"):
            continue
        p = (j.get("provider") or "anthropic").lower()
        if p == "anthropic":
            j["api_key"] = anth
        elif p in ("openai", "azure_openai"):
            j["api_key"] = oai
        # ollama / custom: leave blank (no key required)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/evaluate/{request_id}")
async def evaluate_report(
    request_id: str,
    payload:    EvaluateRequest,
    background: BackgroundTasks,
    request:    Request,
):
    """
    Run the LLM-as-judge panel against a stored analysis report.

    Synchronous by default (async_mode=False) — returns full EvaluationReport.
    Set async_mode=True to get a job_id immediately and poll later.
    """
    from api.app import get_report_store
    report = get_report_store().get(request_id)
    if not report:
        raise HTTPException(404, detail=f"Report '{request_id}' not found.")

    judge_cfgs = (
        [j.model_dump() for j in payload.judges]
        if payload.judges else
        _default_judges()
    )
    # A blank judge api_key falls back to the backend env key for that provider
    # (UI-supplied keys stay authoritative; env is only the fallback).
    _fill_judge_keys(judge_cfgs)

    from evaluation.panel import JudgePanel
    panel = JudgePanel(judges=judge_cfgs)

    agent_names = payload.agent_names or None

    if payload.async_mode:
        job_id = f"eval-{request_id}"
        _eval_results[job_id] = {"status": "running"}

        async def _run():
            try:
                result = panel.evaluate_report(report, payload.diff_text, agent_names)
                _eval_results[job_id] = {"status": "done", "result": result.model_dump()}
            except Exception as exc:
                log.error("[Eval %s] Failed: %s", job_id, exc, exc_info=True)
                _eval_results[job_id] = {"status": "error", "detail": str(exc)}

        background.add_task(_run)
        return {"job_id": job_id, "status": "running"}

    # Synchronous path
    try:
        result = panel.evaluate_report(report, payload.diff_text, agent_names)
        return result.model_dump()
    except Exception as exc:
        log.error("[Eval %s] Failed: %s", request_id, exc, exc_info=True)
        raise HTTPException(500, detail=f"Evaluation failed: {exc}")


@router.get("/evaluation/{job_id}")
def get_evaluation_result(job_id: str):
    """Poll for the result of an async evaluation."""
    entry = _eval_results.get(job_id)
    if entry is None:
        raise HTTPException(404, detail=f"Evaluation job '{job_id}' not found.")
    return entry
