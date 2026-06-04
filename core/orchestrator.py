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
from agents.qa_scenarios_agent      import QAScenariosAgent
from agents.reference_impact_agent    import ReferenceImpactAgent
from agents.performance_impact_agent  import PerformanceImpactAgent
from agents.data_privacy_agent        import DataPrivacyAgent
from agents.maintainability_agent     import MaintainabilityAgent
from agents.license_compliance_agent  import LicenseComplianceAgent
from agents.observability_agent       import ObservabilityAgent
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

        # Auto-load service graph if not explicitly provided
        if graph_store is None:
            try:
                from ingestion.service_graph_builder import load_service_graph
                graph_store = load_service_graph()
            except Exception as exc:
                log.debug("Service graph auto-load skipped: %s", exc)

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
        self._qa       = QAScenariosAgent(api_key)
        self._ref      = ReferenceImpactAgent(api_key)
        self._perf     = PerformanceImpactAgent(api_key)
        self._privacy  = DataPrivacyAgent(api_key)
        self._maint    = MaintainabilityAgent(api_key)
        self._license  = LicenseComplianceAgent(api_key)
        self._obs      = ObservabilityAgent(api_key)

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
        # Default to the parallel threaded pipeline (fast). LangGraph runs fan-out
        # nodes sequentially — opt in explicitly via USE_LANGGRAPH=true.
        use_lg = HAS_LANGGRAPH
        try:
            from config.settings import get_settings
            use_lg = HAS_LANGGRAPH and getattr(get_settings(), "use_langgraph", False)
        except Exception:
            use_lg = False
        if use_lg:
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
        loop = asyncio.get_running_loop()   # get_event_loop() is deprecated in 3.10+, raises on 3.12+
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

        # ── Phase 1: code_analysis + security ─────────────────────────────────
        from config.settings import get_settings as _gs
        _cfg = _gs()
        if getattr(request, "deep_scan", False) and len(request.hunks) >= getattr(_cfg, "deep_scan_min_files", 8):
            # Full-coverage: run code + security over ALL files in batches.
            from core.deep_scan import run_batched, merge_code, merge_security
            mc = getattr(_cfg, "deep_scan_batch_chars", 12000)
            mb = getattr(_cfg, "deep_scan_max_batches", 10)
            log.info("[%s] Deep-scan enabled — %d changed files", request.request_id, len(request.hunks))
            report.code_analysis = run_batched(self._code, request, ctx.get(AgentName.CODE_ANALYSIS, {}),
                                               merge_code, self._budgets, mc, mb)
            report.security = run_batched(self._sec, request, ctx.get(AgentName.SECURITY, {}),
                                          merge_security, self._budgets, mc, mb)
        else:
            p1 = self._run_parallel({
                AgentName.CODE_ANALYSIS: (self._code, ctx.get(AgentName.CODE_ANALYSIS, {})),
                AgentName.SECURITY:      (self._sec,  ctx.get(AgentName.SECURITY, {})),
            }, request, budget)
            report.code_analysis = p1.get(AgentName.CODE_ANALYSIS)
            report.security      = p1.get(AgentName.SECURITY)
            self._record_usage(report, p1, AgentName.CODE_ANALYSIS, AgentName.SECURITY)

        # ── Evidence guard: drop security findings citing files not in the diff
        try:
            from governance.evidence import filter_unsubstantiated
            changed = {h.file_path for h in request.hunks}
            filter_unsubstantiated(report, changed)
        except Exception as exc:
            log.debug("[%s] Evidence guard skipped: %s", request.request_id, exc)

        # ── Phase 1b: Advanced detection (parallel, always runs) ──────────────
        p1b = self._run_parallel_advanced(request, budget, ctx)
        report.ast_analysis    = p1b.get("ast")
        report.secrets_entropy = p1b.get("entropy")
        report.taint_analysis  = p1b.get("taint")
        report.iac_analysis    = p1b.get("iac")
        report.temporal_risk   = p1b.get("temporal")
        report.schema_change   = p1b.get("schema")
        report.qa_scenarios    = p1b.get("qa")
        report.reference_impact = p1b.get("ref")
        report.performance_impact = p1b.get("perf")
        report.data_privacy       = p1b.get("privacy")
        report.maintainability    = p1b.get("maint")
        report.license_compliance = p1b.get("license")
        report.observability      = p1b.get("obs")
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
            res_qa:        Optional[Any]
            res_ref:       Optional[Any]
            res_perf:      Optional[Any]
            res_privacy:   Optional[Any]
            res_maint:     Optional[Any]
            res_license:   Optional[Any]
            res_obs:       Optional[Any]
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

        def node_qa(state: PipelineState) -> dict:
            return {"res_qa": orch._qa.run(state["request"], state["budget"], {})}

        def node_ref(state: PipelineState) -> dict:
            return {"res_ref": orch._ref.run(state["request"], state["budget"], {})}

        def node_perf(state: PipelineState) -> dict:
            return {"res_perf": orch._perf.run(state["request"], state["budget"], {})}

        def node_privacy(state: PipelineState) -> dict:
            return {"res_privacy": orch._privacy.run(state["request"], state["budget"], {})}

        def node_maint(state: PipelineState) -> dict:
            return {"res_maint": orch._maint.run(state["request"], state["budget"], {})}

        def node_license(state: PipelineState) -> dict:
            return {"res_license": orch._license.run(state["request"], state["budget"], {})}

        def node_obs(state: PipelineState) -> dict:
            return {"res_obs": orch._obs.run(state["request"], state["budget"], {})}

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
                qa_scenarios=state.get("res_qa"),
                reference_impact=state.get("res_ref"),
                performance_impact=state.get("res_perf"),
                data_privacy=state.get("res_privacy"),
                maintainability=state.get("res_maint"),
                license_compliance=state.get("res_license"),
                observability=state.get("res_obs"),
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
                qa_scenarios=state.get("res_qa"),
                reference_impact=state.get("res_ref"),
                performance_impact=state.get("res_perf"),
                data_privacy=state.get("res_privacy"),
                maintainability=state.get("res_maint"),
                license_compliance=state.get("res_license"),
                observability=state.get("res_obs"),
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
        builder.add_node("qa",       node_qa)
        builder.add_node("ref",      node_ref)
        builder.add_node("perf",     node_perf)
        builder.add_node("privacy",  node_privacy)
        builder.add_node("maint",    node_maint)
        builder.add_node("license",  node_license)
        builder.add_node("obs",      node_obs)

        _advanced = (
            "security", "ast", "entropy", "taint", "iac", "temporal",
            "schema", "qa", "ref", "perf", "privacy", "maint", "license", "obs",
        )

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
            "res_qa": None, "res_ref": None,
            "res_perf": None, "res_privacy": None, "res_maint": None,
            "res_license": None, "res_obs": None,
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
            qa_scenarios=final.get("res_qa"),
            reference_impact=final.get("res_ref"),
            performance_impact=final.get("res_perf"),
            data_privacy=final.get("res_privacy"),
            maintainability=final.get("res_maint"),
            license_compliance=final.get("res_license"),
            observability=final.get("res_obs"),
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
            (AgentName.QA_SCENARIOS,    "res_qa"),
            (AgentName.REFERENCE_IMPACT,   "res_ref"),
            (AgentName.PERFORMANCE_IMPACT, "res_perf"),
            (AgentName.DATA_PRIVACY,       "res_privacy"),
            (AgentName.MAINTAINABILITY,    "res_maint"),
            (AgentName.LICENSE_COMPLIANCE, "res_license"),
            (AgentName.OBSERVABILITY,      "res_obs"),
            (AgentName.DEPENDENCY,         "res_dep"),
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
            "qa":       self._qa,
            "ref":      self._ref,
            "perf":     self._perf,
            "privacy":  self._privacy,
            "maint":    self._maint,
            "license":  self._license,
            "obs":      self._obs,
        }
        with ThreadPoolExecutor(max_workers=13) as pool:
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

        # ── Auto-suppress known false positives (reviewer feedback loop) ─────
        # Runs BEFORE the gate so a repeatedly-dismissed check can't block merges.
        try:
            from governance.feedback_store import get_feedback_store
            from governance.suppression import apply_suppressions
            apply_suppressions(report, report.repo_url, get_feedback_store())
        except Exception as exc:
            log.debug("[%s] Suppression skipped: %s", report.request_id, exc)

        # ── Deterministic gate enforcement (overrides the LLM proposal) ──────
        try:
            from governance.gate_policy import evaluate_policy
            policy = evaluate_policy(report)
            report.ai_proposed_gate          = policy.llm_gate.value
            report.gate_policy_reasons       = policy.reasons
            report.gate_overridden_by_policy = policy.overrode_llm
            # Force the final gate to the policy-enforced value
            from core.models import GateDecision
            object.__setattr__(report, "_gate_decision", policy.gate)
            if policy.overrode_llm:
                log.warning("[%s] Gate policy raised %s → %s: %s",
                            report.request_id, policy.llm_gate.value, policy.gate.value,
                            "; ".join(policy.reasons[:2]))
        except Exception as exc:
            log.error("[%s] Gate policy evaluation failed (using AI gate): %s",
                      report.request_id, exc)

        # ── Business-capability mapping ──────────────────────────────────────
        try:
            from governance.capability_map import capabilities_for_report
            report.capabilities_affected = capabilities_for_report(report)
        except Exception as exc:
            log.debug("[%s] Capability mapping skipped: %s", report.request_id, exc)

        # ── Downstream consumer-impact tracing ───────────────────────────────
        try:
            from governance.consumer_impact import trace_consumer_impacts
            report.consumer_impacts = trace_consumer_impacts(report)
        except Exception as exc:
            log.debug("[%s] Consumer-impact tracing skipped: %s", report.request_id, exc)

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
            "qa":      AgentName.QA_SCENARIOS,
            "ref":     AgentName.REFERENCE_IMPACT,
            "perf":    AgentName.PERFORMANCE_IMPACT,
            "privacy": AgentName.DATA_PRIVACY,
            "maint":   AgentName.MAINTAINABILITY,
            "license": AgentName.LICENSE_COMPLIANCE,
            "obs":     AgentName.OBSERVABILITY,
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
