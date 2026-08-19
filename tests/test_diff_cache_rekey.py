"""
tests/test_diff_cache_rekey.py
------------------------------
Re-running the SAME diff hits the DiffCache. The cached report must be
(1) RE-KEYED to the new request_id — returning it with the ORIGINAL id made the
    API save it under the old id while the client polls the new one → 404
    forever ("2nd analysis never completes, agents don't start"), and
(2) returned with the POLICY-ENFORCED gate — the enforced gate lives in
    PrivateAttrs which the cache's JSON round-trip drops (HOLD reverted to the
    LLM's APPROVE).
"""
from __future__ import annotations

from core.models import (AnalysisReport, AnalysisRequest, ChangeType,
                         GateDecision, RiskLevel, RiskResult)
from governance.diff_cache import DiffCache, _diff_fingerprint


def _req(rid: str) -> AnalysisRequest:
    return AnalysisRequest(request_id=rid, change_type=ChangeType.PR,
                           repo_url="r/x", source_ref="f", target_ref="main",
                           hunks=[])


def _report(rid: str) -> AnalysisReport:
    rep = AnalysisReport(request_id=rid, change_type=ChangeType.PR, repo_url="r/x",
                         source_ref="f", target_ref="main",
                         risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=12,
                                         gate_decision=GateDecision.APPROVE))
    # Policy raised the gate to HOLD (e.g. confirmed critical finding)
    object.__setattr__(rep, "_gate_decision", GateDecision.HOLD)
    return rep


def test_cache_roundtrip_preserves_enforced_gate():
    cache = DiffCache(redis_url="")            # in-memory
    cache.set(_req("run-1"), _report("run-1"))
    back = cache.get(_req("run-2"))            # same diff fingerprint
    assert back is not None
    assert back.gate_decision == GateDecision.HOLD          # NOT the LLM's APPROVE
    assert back.risk.gate_decision == GateDecision.APPROVE  # LLM proposal intact


def test_orchestrator_cache_hit_rekeys_to_new_request_id(monkeypatch):
    import governance.diff_cache as dc
    from core.orchestrator import ImpactAnalysisOrchestrator

    cache = DiffCache(redis_url="")
    cache.set(_req("run-1"), _report("run-1"))
    monkeypatch.setattr(dc, "_cache", cache)   # make get_diff_cache() return ours

    orch = ImpactAnalysisOrchestrator(api_key=None, phase=1)
    result = orch.analyse(_req("run-2"))       # same diff → cache HIT

    assert result.request_id == "run-2"                     # re-keyed — store/polling id match
    assert result.gate_decision == GateDecision.HOLD        # enforced gate survives


# ── selected_agents must be part of the cache key ───────────────────────────────
# Otherwise a "Fast" (2-agent) run and a "Thorough" (full) run on the identical
# diff collide on the same cache entry and silently return each other's
# wrong-scope report.

def test_fingerprint_differs_by_selected_agents():
    base = _req("run-1")
    fast = base.model_copy(update={"selected_agents": ["code_analysis", "security"]})
    thorough_explicit = base.model_copy(update={"selected_agents": ["code_analysis", "security", "risk"]})
    no_filter = base.model_copy(update={"selected_agents": None})

    assert _diff_fingerprint(fast) != _diff_fingerprint(thorough_explicit)
    assert _diff_fingerprint(fast) != _diff_fingerprint(no_filter)
    # order-independent — same set, different list order, same key
    reordered = base.model_copy(update={"selected_agents": ["security", "code_analysis"]})
    assert _diff_fingerprint(fast) == _diff_fingerprint(reordered)


def test_fast_run_cache_entry_not_served_to_thorough_request():
    cache = DiffCache(redis_url="")
    fast_req = _req("run-1").model_copy(update={"selected_agents": ["code_analysis", "security"]})
    cache.set(fast_req, _report("run-1"))

    thorough_req = _req("run-2").model_copy(update={"selected_agents": None})
    assert cache.get(thorough_req) is None, (
        "a Fast-run cache entry must not be returned for a Thorough request on the same diff"
    )
