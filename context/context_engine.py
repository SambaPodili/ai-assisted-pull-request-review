"""
context/context_engine.py
--------------------------
Assembles the context dict that agents receive alongside the AnalysisRequest.

Context includes:
  • Semantically similar code snippets from the vector store
  • Dependency graph data (affected services, service map)
  • Known API consumers from the contract registry
  • Test file list for the test coverage agent
  • Compliance framework configuration

Designed to be token-efficient: only includes what each agent actually needs.
"""
from __future__ import annotations
import logging
from typing import Any

from core.models import AnalysisRequest, AgentName
from storage.vector_store import VectorStore, InMemoryVectorStore
from storage.graph_store  import GraphStore, _NullGraphStore

log = logging.getLogger(__name__)


class ContextEngine:
    """
    Builds per-agent context dicts from available data sources.

    Usage:
        engine = ContextEngine(vector_store=vs, graph_store=gs)
        ctx    = engine.build(request, agent=AgentName.SECURITY)
    """

    def __init__(
        self,
        vector_store:      VectorStore | None  = None,
        graph_store:       GraphStore  | None  = None,
        contract_registry: dict[str, list[str]] | None = None,   # {endpoint: [consumers]}
        test_index:        list[str] | None    = None,            # list of test file paths
        compliance_frameworks: list[str] | None = None,
    ) -> None:
        self._vs       = vector_store      or InMemoryVectorStore()
        self._gs       = graph_store       or _NullGraphStore()
        self._registry = contract_registry or {}
        self._tests    = test_index        or []
        from config.settings import get_settings
        cfg            = get_settings()
        self._frameworks = compliance_frameworks or cfg.compliance_frameworks

    def build(self, request: AnalysisRequest, agent: AgentName) -> dict[str, Any]:
        """Build a context dict tailored to a specific agent."""
        base = {
            "compliance_frameworks": self._frameworks,
            "repo_url":              request.repo_url,
        }

        if agent == AgentName.CODE_ANALYSIS:
            return {**base, **self._code_context(request)}

        if agent == AgentName.SECURITY:
            return {**base, **self._security_context(request)}

        if agent == AgentName.DEPENDENCY:
            return {**base, **self._dependency_context(request)}

        if agent == AgentName.TEST_COVERAGE:
            return {**base, **self._test_context(request)}

        if agent == AgentName.INTERFACE:
            return {**base, **self._interface_context(request)}

        return base   # RISK, REMEDIATION get report context from orchestrator

    def build_all(self, request: AnalysisRequest) -> dict[AgentName, dict[str, Any]]:
        """Build context dicts for all agents in a single pass."""
        return {
            agent: self.build(request, agent)
            for agent in AgentName
            if agent not in (AgentName.ORCHESTRATOR, AgentName.RISK, AgentName.REMEDIATION)
        }

    # ── Per-agent context builders ────────────────────────────────────────────

    def _code_context(self, request: AnalysisRequest) -> dict:
        similar = []
        for hunk in request.hunks[:3]:    # limit to 3 files to control tokens
            hits = self._vs.search(query=hunk.file_path, n_results=2)
            similar.extend(h.get("text", "")[:200] for h in hits)    # 200 chars max each
        return {"similar_code_patterns": similar}

    def _security_context(self, request: AnalysisRequest) -> dict:
        return {
            "compliance_frameworks": self._frameworks,
            "known_sensitive_files": self._find_sensitive_files(request),
        }

    def _dependency_context(self, request: AnalysisRequest) -> dict:
        service_map: dict[str, list[str]] = {}
        for hunk in request.hunks:
            node_name = hunk.file_path.split("/")[0]   # rough service name from path
            predecessors = self._gs.get_predecessors(node_name)
            if predecessors:
                service_map[node_name] = predecessors
        return {
            "service_map":       service_map,
            "graph_node_count":  self._gs.node_count(),
        }

    def _test_context(self, request: AnalysisRequest) -> dict:
        return {
            "test_files":            self._tests,
            "current_coverage_pct":  "unknown",   # Phase 3: integrate with JaCoCo/pytest-cov
        }

    def _interface_context(self, request: AnalysisRequest) -> dict:
        consumers: dict[str, list[str]] = {}
        for path, cons in self._registry.items():
            for hunk in request.hunks:
                if path in hunk.file_path:
                    consumers[path] = cons
        return {"known_consumers": consumers}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _find_sensitive_files(request: AnalysisRequest) -> list[str]:
        sensitive_patterns = (
            "payment", "transaction", "account", "auth", "security",
            "crypto", "key", "secret", "password", "credential", "card",
        )
        return [
            h.file_path for h in request.hunks
            if any(p in h.file_path.lower() for p in sensitive_patterns)
        ]
