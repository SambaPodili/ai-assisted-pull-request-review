"""
core/orchestrator.py
---------------------
Central coordinator: wires all agents together for all three phases.

Execution order:
  Phase 1:  code_analysis + security (parallel)
  Phase 1b: advanced detection — AST, entropy, taint, IaC, temporal (parallel)
  Phase 2:  dependency + test_coverage + interface (parallel) → risk
  Phase 3:  + remediation (sequential, always last)

Two execution backends:
  1. LangGraph  — preferred if installed (typed StateGraph, async-ready)
  2. ThreadPoolExecutor — synchronous fallback (no extra deps)
"""
from __future__ import annotations
import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from core.models import (
    AnalysisRequest, AnalysisReport, ChangeType,
    AgentName, AgentTokenUsage,
)
from core.token_manager import TokenBudgetManager
from agents.code_analysis_agent import CodeAnalysisAgent
from agents.security_agent      import SecurityReviewAgent
from agents.dependency_agent    import DependencyMappingAgent
from agents.test_coverage_agent import TestCoverageAgent
from agents.interface_agent     import InterfaceAnalysisAgent
from agents.risk_agent          import RiskAssessmentAgent, build_partial_report_context
from agents.remediation_agent   import RemediationAgent, build_full_report_context
from agents.ast_analysis_agent     import ASTAnalysisAgent
from agents.secrets_entropy_agent  import SecretsEntropyAgent
from agents.taint_analysis_agent   import TaintAnalysisAgent
from agents.iac_analysis_agent     import IaCAnalysisAgent
from agents.temporal_risk_agent    import TemporalRiskAgent
from agents.schema_change_agent    import SchemaChangeAgent
from governance.audit_logger    import make_audit_logger, NullAuditLogger
from governance.circuit_breaker import get_breaker_registry
from governance.diff_cache      import get_diff_cache
from governance.observability   import (
    record_analysis_complete, record_agent_tokens, record_agent_fallback,
    analysis_span,
)
from context.context_engine     import ContextEngine

log = logging.getLogger(__name__)

try:
    from langgraph.graph import StateGraph, END
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False
    log.info("langgraph not installed — using ThreadPoolExecutor pipeline")


class ImpactAnalysisOrchestrator:
    """
    Top-level coordinator for the AI impact analysis framework.

    Parameters
    ----------
    api_key        : Anthropic API key (or set ANTHROPIC_API_KEY env var)
    phase          : 1 = code+security only
                     2 = + dependency+tests+interface+risk
                     3 = + remediation
    token_budgets  : Override default per-agent allocations
    context_engine : Pre-configured ContextEngine; built from settings if None
    graph_store    : Pre-built dependency graph for DependencyMappingAgent
    """

    def __init__(
        self,
        api_key:        str | None      = None,
        phase:          int             = 2,
        token_budgets:  dict[str, int]  | None = None,
        context_engine: ContextEngine   | None = None,
        graph_store:    Any             | None = None,
    ) -> None:
        self.phase         = phase
        self._budgets      = token_budgets
        self._ctx_engine   = context_engine
        self._audit        = make_audit_logger()

        self._code  = CodeAnalysisAgent(api_key)
        self._sec   = SecurityReviewAgent(api_key)
        self._dep   = DependencyMappingAgent(api_key, service_graph=graph_store)
        self._test  = TestCoverageAgent(api_key)
        self._iface = InterfaceAnalysisAgent(api_key)
        self._risk  = RiskAssessmentAgent(api_key)
        self._rem   = RemediationAgent(api_key)
        self._ast      = ASTAnalysisAgent(api_key)
        self._entropy  = SecretsEntropyAgent(api_key)
        self._taint    = TaintAnalysisAgent(api_key)
        self._iac      = IaCAnalysisAgent(api_key)
        self._temporal = TemporalRiskAgent(api_key)
        self._schema   = SchemaChangeAgent(api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    def analyse(self, request: AnalysisRequest) -> AnalysisReport:
        """Synchronous entry point. Returns completed AnalysisReport."""
        try:
            cache = get_diff_cache()
            cached = cache.get(request)
            if cached:
                log.info("[%s] Cache HIT — returning cached report", request.request_id)
                return cached
        except Exception as e:
            log.warning("[%s] Cache lookup failed (%s) — running fresh analysis", request.request_id, e)
            cache = None

        start = time.monotonic()
        if HAS_LANGGRAPH:
            report = self._langgraph_pipeline(request)
        else:
            report = self._threaded_pipeline(request)

        # Record observability metrics
        duration = time.monotonic() - start
        report.duration_s = round(duration, 2)
        record_analysis_complete(self.phase, report.gate_decision.value, report.final_risk.value)
        for usage in report.token_usage:
            record_agent_tokens(usage.agent.value, usage.model, usage.tokens_used)

        try:
            if cache:
                cache.set(request, report)
        except Exception as e:
            log.warning("[%s] Cache write failed (%s) — continuing", request.request_id, e)

        return report

    async def analyse_async(self, request: AnalysisRequest) -> AnalysisReport:
        """
        Async wrapper for FastAPI / asyncio usage.
        Uses a dedicated thread pool so the analysis never blocks the event loop
        or contends with uvicorn's default executor.
        """
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis") as executor:
            return await loop.run_in_executor(executor, self.analyse, request)

    # ═════════════════════════════════════════════════════════════════════════
    #  Threaded pipeline (fallback, no LangGraph dependency)
    # ═════════════════════════════════════════════════════════════════════════

    def _threaded_pipeline(self, request: AnalysisRequest) -> AnalysisReport:
        budget = TokenBudgetManager(request.request_id, self._budgets)
        ctx    = self._build_all_context(request)

        self._audit.log_analysis_started(
            request.request_id, request.repo_url, request.source_ref, request.target_ref
        )
        log.info("[%s] Phase %d analysis started: %s → %s",
                 request.request_id, self.phase, request.source_ref, request.target_ref)

        report = AnalysisReport(
            request_id=request.request_id,
            change_type=request.change_type,
            repo_url=request.repo_url,
            source_ref=request.source_ref,
            target_ref=request.target_ref,
            pr=request.pr,
            phase_run=self.phase,
        )

        # ── Phase 1: code_analysis + security (parallel) ──────────────────────
        p1 = self._run_parallel({
            AgentName.CODE_ANALYSIS: (self._code, ctx.get(AgentName.CODE_ANALYSIS, {})),
            AgentName.SECURITY:      (self._sec,  ctx.get(AgentName.SECURITY, {})),
        }, request, budget)

        report.code_analysis = p1.get(AgentName.CODE_ANALYSIS)
        report.security      = p1.get(AgentName.SECURITY)
        self._record_usage(report, p1, AgentName.CODE_ANALYSIS, AgentName.SECURITY)

        # ── Phase 1b: Advanced detection (parallel, always runs) ──────────────
        p1b = self._run_parallel_advanced(request, budget, ctx)
        report.ast_analysis    = p1b.get("ast")
        report.secrets_entropy = p1b.get("entropy")
        report.taint_analysis  = p1b.get("taint")
        report.iac_analysis    = p1b.get("iac")
        report.temporal_risk   = p1b.get("temporal")
        report.schema_change   = p1b.get("schema")
        self._record_advanced_usage(report, p1b)

        if self.phase < 2:
            return self._finalize(report, budget)

        # ── Phase 2: dependency + test_coverage + interface (parallel) ────────
        p2 = self._run_parallel({
            AgentName.DEPENDENCY:    (self._dep,   ctx.get(AgentName.DEPENDENCY, {})),
            AgentName.TEST_COVERAGE: (self._test,  ctx.get(AgentName.TEST_COVERAGE, {})),
            AgentName.INTERFACE:     (self._iface, ctx.get(AgentName.INTERFACE, {})),
        }, request, budget)

        report.dependency    = p2.get(AgentName.DEPENDENCY)
        report.test_coverage = p2.get(AgentName.TEST_COVERAGE)
        report.interface     = p2.get(AgentName.INTERFACE)
        self._record_usage(report, p2, AgentName.DEPENDENCY, AgentName.TEST_COVERAGE, AgentName.INTERFACE)

        budget.donate_unused("dependency", "risk")

        # ── Risk (sequential — needs all previous results) ────────────────────
        risk_ctx = build_partial_report_context(report)
        report.risk = self._risk.run(request, budget, risk_ctx)
        self._record_single(report, report.risk, AgentName.RISK)

        if self.phase < 3:
            return self._finalize(report, budget)

        # ── Phase 3: remediation (always last) ───────────────────────────────
        rem_ctx = build_full_report_context(report)
        report.remediation = self._rem.run(request, budget, rem_ctx)
        self._record_single(report, report.remediation, AgentName.REMEDIATION)

        return self._finalize(report, budget)

    # ═════════════════════════════════════════════════════════════════════════
    #  LangGraph pipeline — complete implementation with all agents
    # ═════════════════════════════════════════════════════════════════════════

    def _langgraph_pipeline(self, request: AnalysisRequest) -> AnalysisReport:
        try:
            return self._langgraph_pipeline_inner(request)
        except Exception as exc:
            log.warning(
                "[%s] LangGraph pipeline failed (%s) — falling back to threaded pipeline",
                request.request_id, exc,
            )
            return self._threaded_pipeline(request)

    def _langgraph_pipeline_inner(self, request: AnalysisRequest) -> AnalysisReport:
        from typing import TypedDict

        class PipelineState(TypedDict):
            request:       AnalysisRequest
            budget:        TokenBudgetManager
            context:       dict
            res_code:      Optional[Any]
            res_security:  Optional[Any]
            # Advanced detection (Phase 1b)
            res_ast:       Optional[Any]
            res_entropy:   Optional[Any]
            res_taint:     Optional[Any]
            res_iac:       Optional[Any]
            res_temporal:  Optional[Any]
            res_schema:    Optional[Any]
            # Phase 2
            res_dep:       Optional[Any]
            res_test:      Optional[Any]
            res_iface:     Optional[Any]
            res_risk:      Optional[Any]
            res_rem:       Optional[Any]

        orch = self

        def node_code(state: PipelineState) -> dict:
            ctx = state["context"].get(AgentName.CODE_ANALYSIS, {})
            return {"res_code": orch._code.run(state["request"], state["budget"], ctx)}

        def node_security(state: PipelineState) -> dict:
            ctx = state["context"].get(AgentName.SECURITY, {})
            return {"res_security": orch._sec.run(state["request"], state["budget"], ctx)}

        def node_ast(state: PipelineState) -> dict:
            return {"res_ast": orch._ast.run(state["request"], state["budget"], {})}

        def node_entropy(state: PipelineState) -> dict:
            return {"res_entropy": orch._entropy.run(state["request"], state["budget"], {})}

        def node_taint(state: PipelineState) -> dict:
            return {"res_taint": orch._taint.run(state["request"], state["budget"], {})}

        def node_iac(state: PipelineState) -> dict:
            return {"res_iac": orch._iac.run(state["request"], state["budget"], {})}

        def node_temporal(state: PipelineState) -> dict:
            return {"res_temporal": orch._temporal.run(state["request"], state["budget"], {})}

        def node_schema(state: PipelineState) -> dict:
            return {"res_schema": orch._schema.run(state["request"], state["budget"], {})}

        def node_dependency(state: PipelineState) -> dict:
            ctx = state["context"].get(AgentName.DEPENDENCY, {})
            return {"res_dep": orch._dep.run(state["request"], state["budget"], ctx)}

        def node_test(state: PipelineState) -> dict:
            ctx = state["context"].get(AgentName.TEST_COVERAGE, {})
            return {"res_test": orch._test.run(state["request"], state["budget"], ctx)}

        def node_interface(state: PipelineState) -> dict:
            ctx = state["context"].get(AgentName.INTERFACE, {})
            return {"res_iface": orch._iface.run(state["request"], state["budget"], ctx)}

        def node_risk(state: PipelineState) -> dict:
            partial = AnalysisReport(
                request_id=state["request"].request_id,
                change_type=state["request"].change_type,
                repo_url=state["request"].repo_url,
                source_ref=state["request"].source_ref,
                target_ref=state["request"].target_ref,
                code_analysis=state.get("res_code"),
                security=state.get("res_security"),
                dependency=state.get("res_dep"),
                test_coverage=state.get("res_test"),
                interface=state.get("res_iface"),
                ast_analysis=state.get("res_ast"),
                secrets_entropy=state.get("res_entropy"),
                taint_analysis=state.get("res_taint"),
                iac_analysis=state.get("res_iac"),
                temporal_risk=state.get("res_temporal"),
                schema_change=state.get("res_schema"),
            )
            state["budget"].donate_unused("dependency", "risk")
            ctx = build_partial_report_context(partial)
            return {"res_risk": orch._risk.run(state["request"], state["budget"], ctx)}

        def node_remediation(state: PipelineState) -> dict:
            full = AnalysisReport(
                request_id=state["request"].request_id,
                change_type=state["request"].change_type,
                repo_url=state["request"].repo_url,
                source_ref=state["request"].source_ref,
                target_ref=state["request"].target_ref,
                code_analysis=state.get("res_code"),
                security=state.get("res_security"),
                dependency=state.get("res_dep"),
                test_coverage=state.get("res_test"),
                interface=state.get("res_iface"),
                ast_analysis=state.get("res_ast"),
                secrets_entropy=state.get("res_entropy"),
                taint_analysis=state.get("res_taint"),
                iac_analysis=state.get("res_iac"),
                temporal_risk=state.get("res_temporal"),
                schema_change=state.get("res_schema"),
                risk=state.get("res_risk"),
            )
            ctx = build_full_report_context(full)
            return {"res_rem": orch._rem.run(state["request"], state["budget"], ctx)}

        # ── Build graph ───────────────────────────────────────────────────────
        builder = StateGraph(PipelineState)
        builder.add_node("code",     node_code)
        builder.add_node("security", node_security)
        builder.add_node("ast",      node_ast)
        builder.add_node("entropy",  node_entropy)
        builder.add_node("taint",    node_taint)
        builder.add_node("iac",      node_iac)
        builder.add_node("temporal", node_temporal)
        builder.add_node("schema",   node_schema)

        _advanced = ("security", "ast", "entropy", "taint", "iac", "temporal", "schema")

        # Fan-out from code: security + all advanced detection run in parallel
        builder.set_entry_point("code")
        for node in _advanced:
            builder.add_edge("code", node)

        if self.phase >= 2:
            builder.add_node("dependency", node_dependency)
            builder.add_node("test",       node_test)
            builder.add_node("interface",  node_interface)
            builder.add_node("risk",       node_risk)
            # Fan-in: phase-2 agents wait for security + advanced to complete
            for src in _advanced:
                for dst in ("dependency", "test", "interface"):
                    builder.add_edge(src, dst)
            for src in ("dependency", "test", "interface"):
                builder.add_edge(src, "risk")

            if self.phase >= 3:
                builder.add_node("remediation", node_remediation)
                builder.add_edge("risk", "remediation")
                builder.add_edge("remediation", END)
            else:
                builder.add_edge("risk", END)
        else:
            # Phase 1 only: all 8 nodes fan-out from code, no further steps
            for node in _advanced:
                builder.add_edge(node, END)

        graph  = builder.compile()
        budget = TokenBudgetManager(request.request_id, self._budgets)
        ctx    = self._build_all_context(request)

        self._audit.log_analysis_started(
            request.request_id, request.repo_url, request.source_ref, request.target_ref
        )

        init_state = {
            "request": request, "budget": budget, "context": ctx,
            "res_code": None, "res_security": None,
            "res_ast": None, "res_entropy": None, "res_taint": None,
            "res_iac": None, "res_temporal": None, "res_schema": None,
            "res_dep": None, "res_test": None, "res_iface": None,
            "res_risk": None, "res_rem": None,
        }
        final = graph.invoke(init_state)

        report = AnalysisReport(
            request_id=request.request_id,
            change_type=request.change_type,
            repo_url=request.repo_url,
            source_ref=request.source_ref,
            target_ref=request.target_ref,
            pr=request.pr,
            phase_run=self.phase,
            code_analysis=final.get("res_code"),
            security=final.get("res_security"),
            ast_analysis=final.get("res_ast"),
            secrets_entropy=final.get("res_entropy"),
            taint_analysis=final.get("res_taint"),
            iac_analysis=final.get("res_iac"),
            temporal_risk=final.get("res_temporal"),
            schema_change=final.get("res_schema"),
            dependency=final.get("res_dep"),
            test_coverage=final.get("res_test"),
            interface=final.get("res_iface"),
            risk=final.get("res_risk"),
            remediation=final.get("res_rem"),
        )

        slot_names = [
            (AgentName.CODE_ANALYSIS,   "res_code"),
            (AgentName.SECURITY,        "res_security"),
            (AgentName.AST_ANALYSIS,    "res_ast"),
            (AgentName.SECRETS_ENTROPY, "res_entropy"),
            (AgentName.TAINT_ANALYSIS,  "res_taint"),
            (AgentName.IAC_ANALYSIS,    "res_iac"),
            (AgentName.TEMPORAL_RISK,   "res_temporal"),
            (AgentName.SCHEMA_CHANGE,   "res_schema"),
            (AgentName.DEPENDENCY,      "res_dep"),
            (AgentName.TEST_COVERAGE,   "res_test"),
            (AgentName.INTERFACE,       "res_iface"),
            (AgentName.RISK,            "res_risk"),
            (AgentName.REMEDIATION,     "res_rem"),
        ]
        for name, slot in slot_names:
            result = final.get(slot)
            if result and getattr(result, "token_usage", 0):
                report.token_usage.append(AgentTokenUsage(
                    agent=name, tokens_used=result.token_usage, model=result.model_used,
                    duration_s=getattr(result, "duration_s", 0.0),
                ))

        return self._finalize(report, budget)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _run_parallel_advanced(
        self,
        request: AnalysisRequest,
        budget:  TokenBudgetManager,
        ctx:     dict,
    ) -> dict:
        results: dict[str, Any] = {}
        agents_map = {
            "ast":      self._ast,
            "entropy":  self._entropy,
            "taint":    self._taint,
            "iac":      self._iac,
            "temporal": self._temporal,
            "schema":   self._schema,
        }
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(agent.run, request, budget, ctx): name
                for name, agent in agents_map.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    log.warning("Advanced agent %s raised: %s", name, exc)
        return results

    def _build_all_context(self, request: AnalysisRequest) -> dict[AgentName, dict]:
        base = {}
        if request.model_config_:
            base["model_config"] = request.model_config_

        if self._ctx_engine:
            per_agent = self._ctx_engine.build_all(request)
            if base:
                for k in per_agent:
                    per_agent[k].update(base)
            return per_agent

        if base:
            from core.models import AgentName as AN
            return {agent: dict(base) for agent in AN}
        return {}

    def _run_parallel(
        self,
        agents:  dict[AgentName, tuple],
        request: AnalysisRequest,
        budget:  TokenBudgetManager,
    ) -> dict[AgentName, Any]:
        results: dict[AgentName, Any] = {}
        breakers = get_breaker_registry()

        with ThreadPoolExecutor(max_workers=len(agents)) as pool:
            futures = {}
            for name, (agent, ctx) in agents.items():
                breaker = breakers.get(name.value)
                if not breaker.allow_request():
                    log.warning("[%s] %s circuit breaker OPEN — using fallback", request.request_id, name.value)
                    fb = agent.fallback_result(request)
                    fb.fallback_used = True
                    results[name] = fb
                    record_agent_fallback(name.value)
                    continue
                futures[pool.submit(agent.run, request, budget, ctx)] = (name, agent, breaker)

            for future in as_completed(futures):
                name, agent, breaker = futures[future]
                try:
                    result = future.result()
                    breaker.record_success()
                    if getattr(result, "fallback_used", False):
                        record_agent_fallback(name.value)
                    results[name] = result
                except Exception as exc:
                    breaker.record_failure()
                    log.error("[%s] Agent %s raised: %s", request.request_id, name, exc)
                    self._audit.log_agent_error(request.request_id, name.value, str(exc))
                    fb = agent.fallback_result(request)
                    fb.fallback_used = True
                    record_agent_fallback(name.value)
                    results[name] = fb
        return results

    def _finalize(self, report: AnalysisReport, budget: TokenBudgetManager) -> AnalysisReport:
        summary = budget.summary()
        report.token_budget = summary["total_allocated"]
        report.freeze_gate()   # snapshot computed gate/risk so storage never re-derives
        self._audit.log_analysis_completed(
            report.request_id,
            report.gate_decision.value,
            report.final_risk.value,
            report.total_tokens,
        )
        log.info(
            "[%s] Complete. Gate=%s Risk=%s Tokens=%d/%d",
            report.request_id,
            report.gate_decision.value,
            report.final_risk.value,
            report.total_tokens,
            report.token_budget,
        )
        return report

    @staticmethod
    def _record_usage(report: AnalysisReport, results: dict, *names: AgentName) -> None:
        for name in names:
            r = results.get(name)
            if r and r.token_usage:
                report.token_usage.append(AgentTokenUsage(
                    agent=name, tokens_used=r.token_usage, model=r.model_used,
                    duration_s=getattr(r, "duration_s", 0.0),
                ))

    @staticmethod
    def _record_single(report: AnalysisReport, result: Any, name: AgentName) -> None:
        if result and result.token_usage:
            report.token_usage.append(AgentTokenUsage(
                agent=name, tokens_used=result.token_usage, model=result.model_used,
                duration_s=getattr(result, "duration_s", 0.0),
            ))

    @staticmethod
    def _record_advanced_usage(report: AnalysisReport, results: dict) -> None:
        advanced_map = {
            "ast":      AgentName.AST_ANALYSIS,
            "entropy":  AgentName.SECRETS_ENTROPY,
            "taint":    AgentName.TAINT_ANALYSIS,
            "iac":      AgentName.IAC_ANALYSIS,
            "temporal": AgentName.TEMPORAL_RISK,
            "schema":   AgentName.SCHEMA_CHANGE,
        }
        for key, agent_name in advanced_map.items():
            result = results.get(key)
            if result and getattr(result, "token_usage", 0):
                report.token_usage.append(AgentTokenUsage(
                    agent=agent_name,
                    tokens_used=result.token_usage,
                    model=result.model_used,
                    duration_s=getattr(result, "duration_s", 0.0),
                ))
