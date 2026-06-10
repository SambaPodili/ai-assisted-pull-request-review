"""tests/test_advanced_model_config.py
Regression: the per-request LLM override (custom endpoint/key) must reach the
parallel deep-scan agents, not just code/security. Previously _run_parallel_advanced
handed each agent the whole {AgentName: ctx} map, so context.get('model_config')
was None and those agents silently fell back to env settings.
"""
from core.orchestrator import ImpactAnalysisOrchestrator
from core.models import AnalysisRequest, ChangeType


def _orch():
    return ImpactAnalysisOrchestrator(api_key=None, phase=2)


def test_all_advanced_agents_have_agent_name():
    o = _orch()
    advanced = [o._ast, o._entropy, o._taint, o._iac, o._temporal, o._schema,
                o._qa, o._ref, o._perf, o._privacy, o._maint, o._license, o._obs]
    assert all(getattr(a, "agent_name", None) is not None for a in advanced)


def test_override_reaches_every_advanced_agent(monkeypatch):
    o = _orch()
    req = AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                          source_ref="a", target_ref="b", hunks=[],
                          model_config_={"provider": "custom", "model": "m", "base_url": "https://x/v1"})
    ctx = o._build_all_context(req)

    # Capture the context each agent actually receives.
    seen = {}
    def fake_run(self, request, budget, context=None):
        seen[self.agent_name] = context
        return self.fallback_result(request)
    import agents.base_agent as ba
    monkeypatch.setattr(ba.BaseAgent, "run", fake_run, raising=False)

    o._run_parallel_advanced(req, budget=None, ctx=ctx)
    assert seen, "no advanced agents ran"
    # Every advanced agent must have gotten the model_config override.
    for name, c in seen.items():
        assert (c or {}).get("model_config", {}).get("base_url") == "https://x/v1", name
