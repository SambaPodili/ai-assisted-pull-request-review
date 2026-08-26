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
    AgentName, AgentTokenUsage, PathReviewSummary,
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
from agents.functional_validation_agent import FunctionalValidationAgent
from agents.cross_repo_impact_agent     import CrossRepoImpactAgent
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


# All selectable agent keys, as used in AnalysisRequest.selected_agents — the
# AgentName enum values (matches frontend AGENT_ORDER), minus ORCHESTRATOR
# which isn't a runnable agent.
_ALL_AGENT_KEYS: set[str] = {a.value for a in AgentName if a != AgentName.ORCHESTRATOR}

# Phase-1b "advanced detection" agents are wired via short internal codes
# (see _run_parallel_advanced's agents_map) that don't match the canonical
# AgentName.value strings used in selected_agents. This is the single mapping
# between the two — reused by both _run_parallel_advanced (selection filter)
# and _record_advanced_usage (token accounting), so they can never drift apart.
_P1B_SHORT_TO_AGENT: dict[str, AgentName] = {
    "ast":        AgentName.AST_ANALYSIS,
    "entropy":    AgentName.SECRETS_ENTROPY,
    "taint":      AgentName.TAINT_ANALYSIS,
    "iac":        AgentName.IAC_ANALYSIS,
    "temporal":   AgentName.TEMPORAL_RISK,
    "schema":     AgentName.SCHEMA_CHANGE,
    "qa":         AgentName.QA_SCENARIOS,
    "ref":        AgentName.REFERENCE_IMPACT,
    "perf":       AgentName.PERFORMANCE_IMPACT,
    "privacy":    AgentName.DATA_PRIVACY,
    "maint":      AgentName.MAINTAINABILITY,
    "license":    AgentName.LICENSE_COMPLIANCE,
    "obs":        AgentName.OBSERVABILITY,
    "functional": AgentName.FUNCTIONAL_VALIDATION,
    "xrepo":      AgentName.CROSS_REPO_IMPACT,
}


def _want(selected: set[str] | None, key: str) -> bool:
    """True if `key` should run — selected=None means no filtering (run everything)."""
    return selected is None or key in selected


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
        self._func     = FunctionalValidationAgent(api_key)
        self._xrepo    = CrossRepoImpactAgent(api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    def analyse(self, request: AnalysisRequest) -> AnalysisReport:
        """Synchronous entry point. Returns completed AnalysisReport."""
        try:
            cache = get_diff_cache()
            # force_reanalyse (UI "Re-analyse fresh") bypasses the cache lookup;
            # the fresh result still refreshes the cache entry afterwards.
            _force = bool((request.metadata or {}).get("force_reanalyse"))
            if _force:
                log.info("[%s] force_reanalyse — bypassing diff cache", request.request_id)
            cached = None if _force else cache.get(request)
            if cached:
                # RE-KEY the cached report to THIS request's id. The cached copy
                # carries the ORIGINAL run's request_id — returning it as-is made
                # the API save it under the OLD id while the client polls the NEW
                # one → 404 forever ("2nd run of the same diff never completes").
                # Also re-freeze the enforced gate: the cached payload loses the
                # PrivateAttr gate on serialisation, silently reverting HOLD→APPROVE.
                enforced_gate, enforced_risk = cached.gate_decision, cached.final_risk
                cached = cached.model_copy(deep=True, update={"request_id": request.request_id,
                                                              "from_cache": True})
                object.__setattr__(cached, "_gate_decision", enforced_gate)
                object.__setattr__(cached, "_final_risk", enforced_risk)
                log.info("[%s] Cache HIT — returning cached report re-keyed to this run (gate=%s)",
                         request.request_id, enforced_gate.value)
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
            if request.selected_agents is not None:
                log.warning(
                    "[%s] selected_agents set but USE_LANGGRAPH=true — per-agent "
                    "selection is only honoured on the threaded pipeline; running "
                    "the full LangGraph graph.", request.request_id,
                )
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

        # ── Resolve agent selection (narrows execution only; None = run everything,
        #    identical to today's behaviour) ────────────────────────────────────
        selected: set[str] | None = None
        if request.selected_agents is not None:
            selected = set(request.selected_agents) & _ALL_AGENT_KEYS
            dropped = set(request.selected_agents) - _ALL_AGENT_KEYS
            if dropped:
                log.warning("[%s] Unknown agent key(s) in selected_agents ignored: %s",
                            request.request_id, sorted(dropped))
            if "remediation" in selected and "risk" not in selected:
                log.info("[%s] remediation selected — auto-including risk (hard dependency: "
                         "remediation reads risk.gate_decision/overall_risk)", request.request_id)
                selected.add("risk")
            if not selected:
                log.warning("[%s] selected_agents resolved to an empty set — running full pipeline",
                            request.request_id)
                selected = None

        # ── Path-scoped review config (.gto.yaml) — narrows agent selection
        #    further (never widens) and adds steering text. Never touches the
        #    report or gate directly; see AnalysisRequest.path_review_config
        #    and ingestion/path_review_config.py. ──────────────────────────
        if request.path_review_config and request.path_review_config.paths:
            from ingestion.path_review_config import agent_fully_excluded, collect_path_scoped_instructions
            base = selected if selected is not None else set(_ALL_AGENT_KEYS)
            path_excluded = {k for k in base if agent_fully_excluded(request.path_review_config, k, request.hunks)}
            if path_excluded:
                selected = base - path_excluded
                log.info("[%s] .gto.yaml excludes agent(s) for this diff: %s",
                         request.request_id, sorted(path_excluded))
            extra_instructions = collect_path_scoped_instructions(request.path_review_config, request.hunks)
            if extra_instructions:
                request.user_instructions = (
                    f"{request.user_instructions}\n{extra_instructions}".strip()
                    if request.user_instructions else extra_instructions
                )
            if path_excluded or extra_instructions:
                report.path_review_summary = PathReviewSummary(
                    agents_excluded=sorted(path_excluded),
                    steering_applied=bool(extra_instructions),
                )

        # ── Phase 1: code_analysis + security ─────────────────────────────────
        from config.settings import get_settings as _gs
        _cfg = _gs()
        # Decide deep-scan (full-coverage batched LLM review of EVERY file):
        #  1. explicit request → always honour it, regardless of file count
        #     (a few very large files can blow the budget even when count is low);
        #  2. else auto-promote when the prioritised single-prompt pass would skip
        #     signal files (so nothing silently falls back to churn heuristics).
        _explicit_ds = bool(getattr(request, "deep_scan", False))
        _deep_scan_used = _explicit_ds and bool(request.hunks)
        _ds_reason = "requested" if _deep_scan_used else ""
        if not _deep_scan_used and getattr(_cfg, "deep_scan_auto", True) and request.hunks:
            try:
                from agents.base_agent import count_files_in_llm_budget
                # Use the code agent's per-file cap (2000) so big-diff truncation
                # is detected the same way the real prompt would clip it.
                _cov = count_files_in_llm_budget(request.hunks, max_chars_per_hunk=2000)
                _skipped = _cov.get("signal", 0) - _cov.get("reviewed", 0)
                _trunc = _cov.get("truncated", 0)
                if _skipped > 0 or _trunc > 0:
                    _deep_scan_used = True
                    bits = []
                    if _skipped: bits.append(f"{_skipped} file(s) would be skipped")
                    if _trunc:   bits.append(f"{_trunc} large file(s) would be truncated")
                    _ds_reason = "auto — " + ", ".join(bits)
            except Exception as exc:
                log.debug("[%s] deep-scan auto-check skipped: %s", request.request_id, exc)
        if _deep_scan_used:
            # Full-coverage: run code + security over ALL files in batches.
            from core.deep_scan import run_batched, merge_code, merge_security
            mc = getattr(_cfg, "deep_scan_batch_chars", 12000)
            mb = getattr(_cfg, "deep_scan_max_batches", 10)
            log.info("[%s] Deep-scan enabled (%s) — %d changed files reviewed in batches",
                     request.request_id, _ds_reason, len(request.hunks))
            if _want(selected, "code_analysis"):
                report.code_analysis = run_batched(self._code, request, ctx.get(AgentName.CODE_ANALYSIS, {}),
                                                   merge_code, self._budgets, mc, mb)
            if _want(selected, "security"):
                report.security = run_batched(self._sec, request, ctx.get(AgentName.SECURITY, {}),
                                              merge_security, self._budgets, mc, mb)
        else:
            p1_agents = {}
            if _want(selected, "code_analysis"):
                p1_agents[AgentName.CODE_ANALYSIS] = (self._code, ctx.get(AgentName.CODE_ANALYSIS, {}))
            if _want(selected, "security"):
                p1_agents[AgentName.SECURITY] = (self._sec, ctx.get(AgentName.SECURITY, {}))
            p1 = self._run_parallel(p1_agents, request, budget)
            report.code_analysis = p1.get(AgentName.CODE_ANALYSIS)
            report.security      = p1.get(AgentName.SECURITY)
            self._record_usage(report, p1, AgentName.CODE_ANALYSIS, AgentName.SECURITY)

        report.llm_coverage = self._llm_coverage(request, _deep_scan_used)

        # ── Evidence guard: drop security findings citing files not in the diff
        try:
            from governance.evidence import filter_unsubstantiated
            changed = {h.file_path for h in request.hunks}
            filter_unsubstantiated(report, changed)
        except Exception as exc:
            log.debug("[%s] Evidence guard skipped: %s", request.request_id, exc)

        # ── Phase 1b: Advanced detection (parallel) ────────────────────────────
        p1b = self._run_parallel_advanced(request, budget, ctx, selected)
        report.ast_analysis    = p1b.get("ast")
        report.secrets_entropy = p1b.get("entropy")
        report.taint_analysis  = p1b.get("taint")
        report.iac_analysis    = p1b.get("iac")
        report.temporal_risk   = p1b.get("temporal")
        report.schema_change   = p1b.get("schema")
        report.functional_validation = p1b.get("functional")
        report.cross_repo_impact = p1b.get("xrepo")
        report.qa_scenarios    = p1b.get("qa")
        report.reference_impact = p1b.get("ref")
        report.performance_impact = p1b.get("perf")
        report.data_privacy       = p1b.get("privacy")
        report.maintainability    = p1b.get("maint")
        report.license_compliance = p1b.get("license")
        report.observability      = p1b.get("obs")
        self._record_advanced_usage(report, p1b)

        # Selection can only NARROW the server-configured phase ceiling, never widen it.
        want_phase2 = selected is None or bool(selected & {"dependency", "test_coverage", "interface", "risk"})
        if self.phase < 2 or not want_phase2:
            return self._finalize(report, budget, {h.file_path for h in request.hunks}, self._changed_lines(request), self._source_lines(request))

        # ── Phase 2: dependency + test_coverage + interface (parallel) ────────
        p2_agents = {}
        if _want(selected, "dependency"):
            p2_agents[AgentName.DEPENDENCY] = (self._dep, ctx.get(AgentName.DEPENDENCY, {}))
        if _want(selected, "test_coverage"):
            p2_agents[AgentName.TEST_COVERAGE] = (self._test, ctx.get(AgentName.TEST_COVERAGE, {}))
        if _want(selected, "interface"):
            p2_agents[AgentName.INTERFACE] = (self._iface, ctx.get(AgentName.INTERFACE, {}))
        p2 = self._run_parallel(p2_agents, request, budget)

        report.dependency    = p2.get(AgentName.DEPENDENCY)
        report.test_coverage = p2.get(AgentName.TEST_COVERAGE)
        report.interface     = p2.get(AgentName.INTERFACE)
        self._record_usage(report, p2, AgentName.DEPENDENCY, AgentName.TEST_COVERAGE, AgentName.INTERFACE)

        budget.donate_unused("dependency", "risk")

        # ── Risk (sequential — needs all previous results) ────────────────────
        if _want(selected, "risk"):
            risk_ctx = build_partial_report_context(report)
            report.risk = self._risk.run(request, budget, risk_ctx)
            self._record_single(report, report.risk, AgentName.RISK)

        want_phase3 = selected is None or "remediation" in selected
        if self.phase < 3 or not want_phase3:
            return self._finalize(report, budget, {h.file_path for h in request.hunks}, self._changed_lines(request), self._source_lines(request))

        # ── Phase 3: remediation (always last) ───────────────────────────────
        if _want(selected, "remediation"):
            rem_ctx = build_full_report_context(report)
            report.remediation = self._rem.run(request, budget, rem_ctx)
            self._record_single(report, report.remediation, AgentName.REMEDIATION)

        return self._finalize(report, budget, {h.file_path for h in request.hunks}, self._changed_lines(request), self._source_lines(request))

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
            res_functional: Optional[Any]
            res_xrepo:     Optional[Any]
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

        # Advanced agents must also receive their per-request context slice so the
        # UI's LLM override (custom endpoint/key) reaches them — not a bare {}.
        def _actx(state, agent):
            return state["context"].get(getattr(agent, "agent_name", None), {}) or {}

        def node_ast(state: PipelineState) -> dict:
            return {"res_ast": orch._ast.run(state["request"], state["budget"], _actx(state, orch._ast))}

        def node_entropy(state: PipelineState) -> dict:
            return {"res_entropy": orch._entropy.run(state["request"], state["budget"], _actx(state, orch._entropy))}

        def node_taint(state: PipelineState) -> dict:
            return {"res_taint": orch._taint.run(state["request"], state["budget"], _actx(state, orch._taint))}

        def node_iac(state: PipelineState) -> dict:
            return {"res_iac": orch._iac.run(state["request"], state["budget"], _actx(state, orch._iac))}

        def node_temporal(state: PipelineState) -> dict:
            return {"res_temporal": orch._temporal.run(state["request"], state["budget"], _actx(state, orch._temporal))}

        def node_schema(state: PipelineState) -> dict:
            return {"res_schema": orch._schema.run(state["request"], state["budget"], _actx(state, orch._schema))}

        def node_functional(state: PipelineState) -> dict:
            return {"res_functional": orch._func.run(state["request"], state["budget"], _actx(state, orch._func))}

        def node_xrepo(state: PipelineState) -> dict:
            return {"res_xrepo": orch._xrepo.run(state["request"], state["budget"], _actx(state, orch._xrepo))}

        def node_qa(state: PipelineState) -> dict:
            return {"res_qa": orch._qa.run(state["request"], state["budget"], _actx(state, orch._qa))}

        def node_ref(state: PipelineState) -> dict:
            return {"res_ref": orch._ref.run(state["request"], state["budget"], _actx(state, orch._ref))}

        def node_perf(state: PipelineState) -> dict:
            return {"res_perf": orch._perf.run(state["request"], state["budget"], _actx(state, orch._perf))}

        def node_privacy(state: PipelineState) -> dict:
            return {"res_privacy": orch._privacy.run(state["request"], state["budget"], _actx(state, orch._privacy))}

        def node_maint(state: PipelineState) -> dict:
            return {"res_maint": orch._maint.run(state["request"], state["budget"], _actx(state, orch._maint))}

        def node_license(state: PipelineState) -> dict:
            return {"res_license": orch._license.run(state["request"], state["budget"], _actx(state, orch._license))}

        def node_obs(state: PipelineState) -> dict:
            return {"res_obs": orch._obs.run(state["request"], state["budget"], _actx(state, orch._obs))}

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
                functional_validation=state.get("res_functional"),
                cross_repo_impact=state.get("res_xrepo"),
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
                functional_validation=state.get("res_functional"),
                cross_repo_impact=state.get("res_xrepo"),
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
        builder.add_node("functional", node_functional)
        builder.add_node("xrepo",    node_xrepo)
        builder.add_node("qa",       node_qa)
        builder.add_node("ref",      node_ref)
        builder.add_node("perf",     node_perf)
        builder.add_node("privacy",  node_privacy)
        builder.add_node("maint",    node_maint)
        builder.add_node("license",  node_license)
        builder.add_node("obs",      node_obs)

        _advanced = (
            "security", "ast", "entropy", "taint", "iac", "temporal",
            "schema", "functional", "xrepo", "qa", "ref", "perf", "privacy",
            "maint", "license", "obs",
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
            "res_iac": None, "res_temporal": None, "res_schema": None, "res_functional": None,
            "res_xrepo": None, "res_qa": None, "res_ref": None,
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
            functional_validation=final.get("res_functional"),
            cross_repo_impact=final.get("res_xrepo"),
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
        report.llm_coverage = self._llm_coverage(request, False)  # langgraph path: no deep-scan

        slot_names = [
            (AgentName.CODE_ANALYSIS,   "res_code"),
            (AgentName.SECURITY,        "res_security"),
            (AgentName.AST_ANALYSIS,    "res_ast"),
            (AgentName.SECRETS_ENTROPY, "res_entropy"),
            (AgentName.TAINT_ANALYSIS,  "res_taint"),
            (AgentName.IAC_ANALYSIS,    "res_iac"),
            (AgentName.TEMPORAL_RISK,   "res_temporal"),
            (AgentName.SCHEMA_CHANGE,   "res_schema"),
            (AgentName.FUNCTIONAL_VALIDATION, "res_functional"),
            (AgentName.CROSS_REPO_IMPACT,     "res_xrepo"),
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

        return self._finalize(report, budget, {h.file_path for h in request.hunks}, self._changed_lines(request), self._source_lines(request))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _run_parallel_advanced(
        self,
        request: AnalysisRequest,
        budget:  TokenBudgetManager,
        ctx:     dict,
        selected: set[str] | None = None,
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
            "functional": self._func,
            "xrepo":    self._xrepo,
        }
        if selected is not None:
            agents_map = {k: v for k, v in agents_map.items()
                          if _P1B_SHORT_TO_AGENT[k].value in selected}
        if not agents_map:
            return results
        # `ctx` is the per-agent map {AgentName: {...}}. Each agent must receive
        # ITS OWN context slice — not the whole map — otherwise context.get(
        # "model_config") is None and the agent silently ignores the per-request
        # LLM override (custom endpoint/key), falling back to env settings. That
        # was the cause of "code/security connect but the deep-scan agents fail".
        def _agent_ctx(agent):
            an = getattr(agent, "agent_name", None)
            slice_ = ctx.get(an, {}) if isinstance(ctx, dict) else {}
            # Tolerate a flat context (e.g. {"model_config": ...}) being passed in.
            if not slice_ and isinstance(ctx, dict) and "model_config" in ctx:
                slice_ = ctx
            return slice_ or {}

        with ThreadPoolExecutor(max_workers=len(agents_map)) as pool:
            futures = {
                pool.submit(agent.run, request, budget, _agent_ctx(agent)): name
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

        # Function-context expansion: when a local checkout is available, give the
        # deep-review LLM agents the ENCLOSING FUNCTIONS around each change, not
        # just ±3 diff lines — review accuracy follows reviewer-grade context.
        fn_ctx_block = ""
        try:
            from config.settings import get_settings
            repo_path = getattr(get_settings(), "repo_local_path", "")
            if repo_path and request.hunks:
                from ingestion.context_expander import expand_function_context, format_context_block
                fn_ctx_block = format_context_block(expand_function_context(request.hunks, repo_path))
                if fn_ctx_block:
                    log.info("[%s] Function-context expansion: %d file(s) enriched",
                             request.request_id, fn_ctx_block.count("--- "))
        except Exception as exc:
            log.debug("[%s] Function-context expansion skipped: %s", request.request_id, exc)

        if self._ctx_engine:
            per_agent = self._ctx_engine.build_all(request)
            if base:
                for k in per_agent:
                    per_agent[k].update(base)
            if fn_ctx_block:
                for agent in (AgentName.CODE_ANALYSIS, AgentName.SECURITY):
                    per_agent.setdefault(agent, {})["function_context"] = fn_ctx_block
            return per_agent

        if base or fn_ctx_block:
            from core.models import AgentName as AN
            per_agent = {agent: dict(base) for agent in AN}
            if fn_ctx_block:
                per_agent[AN.CODE_ANALYSIS]["function_context"] = fn_ctx_block
                per_agent[AN.SECURITY]["function_context"] = fn_ctx_block
            return per_agent
        return {}

    def _run_parallel(
        self,
        agents:  dict[AgentName, tuple],
        request: AnalysisRequest,
        budget:  TokenBudgetManager,
    ) -> dict[AgentName, Any]:
        results: dict[AgentName, Any] = {}
        if not agents:
            return results
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

    @staticmethod
    def _changed_lines(request) -> dict:
        """{file_path: set(added line numbers)} from the request diff hunks."""
        from ingestion.diff_parser import iter_added_lines
        out = {}
        for h in request.hunks:
            nums = {ln for ln, _ in iter_added_lines(h.content)}
            if nums:
                out.setdefault(h.file_path, set()).update(nums)
        return out

    @staticmethod
    def _llm_coverage(request, deep_scan_used: bool) -> dict:
        """How thoroughly the LLM agents reviewed a (possibly large) PR.

        mode: 'batched'  → deep-scan ran every file through the model in batches;
              'full'      → all changed (signal) files fit the default LLM budget;
              'partial'   → large PR: only the highest-churn files fit the LLM
                            budget (the rest still got the deterministic checks)."""
        from agents.base_agent import count_files_in_llm_budget
        c = count_files_in_llm_budget(request.hunks)
        if deep_scan_used:
            mode, reviewed = "batched", c["total"]
        else:
            reviewed = c["reviewed"]
            mode = "full" if reviewed >= c["signal"] else "partial"
        return {
            "total_files":     c["total"],
            "reviewed_by_llm": reviewed,
            "skipped_noise":   c["skipped_noise"],
            "deep_scan":       bool(deep_scan_used),
            "mode":            mode,
        }

    @staticmethod
    def _source_lines(request) -> dict:
        """{normalised_file_path: [(source_line_no, text), …]} for ALL new-file
        lines (added + context). Used by the false-positive guard to verify
        claims like "N+1 inside a loop" against the real surrounding code."""
        from ingestion.diff_parser import iter_source_lines
        out: dict[str, list[tuple[int, str]]] = {}
        for h in request.hunks:
            key = (h.file_path or "").replace("\\", "/").strip().lstrip("./").lower()
            for ln, _kind, text in iter_source_lines(h.content):
                out.setdefault(key, []).append((ln, text))
        return out

    def _finalize(self, report: AnalysisReport, budget: TokenBudgetManager,
                  changed_files: set[str] | None = None,
                  changed_lines: dict[str, set[int]] | None = None,
                  source_lines: dict[str, list] | None = None) -> AnalysisReport:
        summary = budget.summary()
        report.token_budget = summary["total_allocated"]
        if changed_files is None and changed_lines:
            changed_files = set(changed_lines.keys())

        # ── Finding quality: snap wrong LLM line numbers to the real diff and
        # flag speculative/hedged findings (excluded from the gate). Addresses
        # the two biggest LLM-review complaints: wrong lines + false positives.
        try:
            from governance.finding_quality import correct_findings
            if changed_lines:
                correct_findings(report, changed_lines, source_lines)
        except Exception as exc:
            log.debug("[%s] Finding-quality pass skipped: %s", report.request_id, exc)

        # ── False-positive guard: kill three recurring LLM hallucination classes
        # against the real diff — N+1 with no loop, "PII" on operational columns,
        # and index/PK/FK lock warnings on a brand-new (empty) table.
        try:
            from governance.false_positive_guard import guard_false_positives
            guard_false_positives(report, source_lines)
        except Exception as exc:
            log.debug("[%s] False-positive guard skipped: %s", report.request_id, exc)

        # ── Generalised evidence guard: flag any agent finding (code, AST, perf,
        # privacy, IaC, maintainability, observability) that cites a file not in
        # this diff — almost always a hallucination. Flagged, never deleted.
        try:
            from governance.evidence import filter_all_unsubstantiated
            if changed_files:
                filter_all_unsubstantiated(report, changed_files)
        except Exception as exc:
            log.debug("[%s] Cross-agent evidence guard skipped: %s", report.request_id, exc)

        # ── Auto-suppress known false positives (reviewer feedback loop) ─────
        # Runs BEFORE the gate so a repeatedly-dismissed check can't block merges.
        try:
            from governance.feedback_store import get_feedback_store
            from governance.suppression import apply_suppressions
            apply_suppressions(report, report.repo_url, get_feedback_store())
        except Exception as exc:
            log.debug("[%s] Suppression skipped: %s", report.request_id, exc)

        # ── Cross-agent correlation: dedupe + corroborate + rank into Top Issues ─
        # Runs after the evidence guard (unverified flags set) and suppression, so
        # the ranked list reflects only what the reviewer should actually act on.
        try:
            from governance.correlation import correlate_findings
            report.top_issues = correlate_findings(report)
        except Exception as exc:
            log.debug("[%s] Correlation skipped: %s", report.request_id, exc)

        # ── Reviewer review plan: triage every changed file into must-fix /
        # needs-review / auto-approvable so a reviewer knows where to look first.
        try:
            from governance.review_plan import build_review_plan
            report.review_plan = build_review_plan(report, changed_files)
        except Exception as exc:
            log.debug("[%s] Review plan skipped: %s", report.request_id, exc)

        # ── Derive blast radius from real impact signals when no service graph ──
        # Without a configured dependency graph the graph-based score is 0, which
        # is misleading when there ARE breaking changes / downstream call-sites.
        # Fall back to reference-impact + interface signals so the metric reflects
        # actual blast radius. Never lowers a real graph-derived score.
        try:
            from governance.blast_radius import derive_blast_radius
            derived = derive_blast_radius(report)
            if derived is not None:
                report.dependency.blast_radius_score = derived
                log.info("[%s] Blast radius derived from impact signals: %d", report.request_id, derived)
        except Exception as exc:
            log.debug("[%s] Blast-radius derivation skipped: %s", report.request_id, exc)

        # ── Reconcile the displayed rationale with the FINAL metrics ──────────
        # The risk agent's LLM rationale is written mid-pipeline (before blast
        # radius is derived, and referencing the now-dropped coverage delta), so
        # it can contradict the headline tiles. Replace it with a deterministic,
        # accurate summary built from the final values.
        try:
            from governance.rationale import build_rationale
            if report.risk is not None:
                # Preserve the LLM's narrative as a secondary line; make the
                # headline the deterministic, metrics-accurate summary.
                if not report.risk.ai_rationale:
                    report.risk.ai_rationale = report.risk.rationale
                report.risk.rationale = build_rationale(report)
        except Exception as exc:
            log.debug("[%s] Rationale reconciliation skipped: %s", report.request_id, exc)

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

        # ── Compliance mapping (OWASP / PCI-DSS / CWE Top 25) ────────────────
        try:
            from governance.compliance import assess as assess_compliance
            report.compliance = assess_compliance(report)
        except Exception as exc:
            log.debug("[%s] Compliance mapping skipped: %s", report.request_id, exc)

        report.files_changed = len(changed_files) if changed_files else 0
        report.files_changed_list = sorted(changed_files) if changed_files else []
        report.agent_run_summary = report.compute_agent_run_summary()

        report.freeze_gate()   # snapshot computed gate/risk so storage never re-derives
        self._audit.log_analysis_completed(
            report.request_id,
            report.gate_decision.value,
            report.final_risk.value,
            report.total_tokens,
        )
        s = report.agent_run_summary
        log.info(
            "[%s] Complete. Gate=%s Risk=%s Tokens=%d/%d Files=%d Agents=%d ok/%d fallback/%d skipped",
            report.request_id,
            report.gate_decision.value,
            report.final_risk.value,
            report.total_tokens,
            report.token_budget,
            report.files_changed,
            s.get("successful", 0), s.get("fallback", 0), s.get("skipped", 0),
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
        for key, agent_name in _P1B_SHORT_TO_AGENT.items():
            result = results.get(key)
            if result and getattr(result, "token_usage", 0):
                report.token_usage.append(AgentTokenUsage(
                    agent=agent_name,
                    tokens_used=result.token_usage,
                    model=result.model_used,
                    duration_s=getattr(result, "duration_s", 0.0),
                ))
