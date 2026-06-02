"""
agents/reference_impact_agent.py
----------------------------------
Reference Impact Agent — finds where changed symbols are used across the
codebase and flags high-impact changes.

Three analysis layers:

  1. Symbol extraction (always runs)
     Parses the diff to identify every changed function, method, class, or
     struct definition using language-specific regex patterns.

  2. Reference discovery (runs when a backend is configured)
     Searches for usages of those symbols across the full repository via:
       • local_grep — ripgrep / grep over a local clone
       • github_api — GitHub code search API

  3. Shared-library break detection (always runs, zero tokens)
     Flags files in common/shared/core paths as potential cross-project
     interface breaks, even when no OpenAPI/proto schema changed.

  4. LLM enhancement (optional — Haiku, low token cost)
     Summarises the reference impact in plain English and assigns an
     intra_project_risk level based on reference density and symbol
     criticality.

Fallback: static analysis only (layers 1–3) when no LLM budget remains.
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    ReferenceImpactResult, SymbolReference, RiskLevel,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt
from ingestion.symbol_extractor import unique_symbol_names, ranked_symbol_names
from ingestion.reference_finder import (
    find_references_deep, high_impact_files as _high_impact_files,
    _MAX_SYMBOLS,
)


# ── Shared-library path heuristics ───────────────────────────────────────────

_SHARED_LIB_PATTERNS = re.compile(
    r'(?:^|/)(?:shared|common|core|lib|libs|util|utils|base|foundation|'
    r'platform|framework|sdk|api|client|contract|proto|schema|model|'
    r'domain|kernel|support|infrastructure)(?:/|$)',
    re.I,
)

def _is_shared_lib_path(file_path: str) -> bool:
    return bool(_SHARED_LIB_PATTERNS.search(file_path))


def _detect_shared_lib_breaks(request: AnalysisRequest) -> list[str]:
    """Return file paths that are in shared/common modules."""
    return [
        h.file_path
        for h in request.hunks
        if _is_shared_lib_path(h.file_path)
    ]


# ── Risk scoring ──────────────────────────────────────────────────────────────

def _score_risk(
    total_refs:   int,
    shared_breaks: int,
    symbol_count:  int,
) -> RiskLevel:
    if total_refs >= 20 or shared_breaks >= 3:
        return RiskLevel.HIGH
    if total_refs >= 5 or shared_breaks >= 1:
        return RiskLevel.MEDIUM
    if total_refs >= 1 or symbol_count >= 5:
        return RiskLevel.LOW
    return RiskLevel.LOW


# ── Static analysis (no LLM) ─────────────────────────────────────────────────

def _static_analysis(request: AnalysisRequest) -> ReferenceImpactResult:
    """Run symbol extraction, reference lookup, and shared-lib detection."""
    from config.settings import get_settings
    cfg = get_settings()

    # Use ranked selection: for large PRs this picks the most impactful symbols
    # rather than the arbitrary first-N from the diff.
    symbols = ranked_symbol_names(request.hunks, max_symbols=_MAX_SYMBOLS)
    repo_path     = getattr(cfg, "repo_local_path", "")
    github_token  = getattr(cfg, "github_token", "")
    max_depth     = getattr(cfg, "ref_max_depth", 2)

    refs, backend = find_references_deep(
        symbols,
        repo_url=request.repo_url,
        repo_path=repo_path,
        github_token=github_token,
        max_depth=max_depth,
        hunks=request.hunks,
        source_ref=request.source_ref,
    )

    shared_breaks = _detect_shared_lib_breaks(request)
    hi_files      = _high_impact_files(refs, threshold=3)
    risk          = _score_risk(len(refs), len(shared_breaks), len(symbols))

    # Build plain-text summary for fallback
    lines = [f"{len(symbols)} symbols changed; {len(refs)} references found via {backend}."]
    if shared_breaks:
        lines.append(f"⚠ Shared/common paths modified: {', '.join(shared_breaks[:5])}")
    if hi_files:
        lines.append(f"High-impact files (≥3 refs): {', '.join(hi_files[:5])}")

    return ReferenceImpactResult(
        changed_symbols=symbols,
        references=refs,
        total_references=len(refs),
        high_impact_files=hi_files,
        shared_lib_breaks=shared_breaks,
        intra_project_risk=risk,
        search_backend=backend,
        summary=" ".join(lines),
        fallback_used=True,
    )


# ── Agent class ───────────────────────────────────────────────────────────────

class ReferenceImpactAgent(BaseAgent[ReferenceImpactResult]):

    agent_name       = AgentName.REFERENCE_IMPACT
    output_model     = ReferenceImpactResult
    output_token_cap = 2000

    system_prompt = (
        "You are a senior engineer analysing how changed code affects the rest of a codebase.\n"
        "Given:\n"
        "  • The diff with changed symbols highlighted\n"
        "  • A list of files that reference those symbols\n"
        "  • Any shared/common library paths that were modified\n\n"
        "Return a JSON ReferenceImpactResult with:\n"
        "  - changed_symbols: list of function/class/method names changed\n"
        "  - references: list of {symbol, file_path, line, context, repo} objects\n"
        "  - total_references: integer\n"
        "  - high_impact_files: files with ≥3 references to changed symbols\n"
        "  - shared_lib_breaks: shared/common module paths that were changed\n"
        "  - intra_project_risk: CRITICAL | HIGH | MEDIUM | LOW\n"
        "  - search_backend: 'llm_inferred' (since you are doing the analysis)\n"
        "  - summary: 2-sentence plain-English description of the blast radius\n\n"
        "If references list is provided use it as-is; infer additional ones from diff context.\n"
        "Output ONLY valid JSON. No prose."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        symbols       = context.get("changed_symbols", ranked_symbol_names(request.hunks))
        refs          = context.get("references", [])
        shared_breaks = context.get("shared_lib_breaks", _detect_shared_lib_breaks(request))
        backend       = context.get("search_backend", "none")

        ref_lines = "\n".join(
            f"  {r.file_path}:{r.line}  [{r.symbol}]  {r.context}"
            for r in refs[:40]
        ) or "  (no references found — backend: " + backend + ")"

        return (
            f"Repository: {request.repo_url}\n"
            f"Changed symbols ({len(symbols)}): {', '.join(symbols[:20])}\n"
            f"Shared-lib paths changed: {shared_breaks or 'none'}\n\n"
            f"References found ({len(refs)}):\n{ref_lines}\n\n"
            f"Diff:\n{format_hunks_for_prompt(request.hunks, max_chars_per_hunk=1500)}"
        )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> ReferenceImpactResult:
        ctx = context or {}

        # Always run static analysis first (no LLM cost)
        static = _static_analysis(request)

        # Enrich the LLM context with static findings
        ctx["changed_symbols"]  = static.changed_symbols
        ctx["references"]       = static.references
        ctx["shared_lib_breaks"] = static.shared_lib_breaks
        ctx["search_backend"]   = static.search_backend

        # Try LLM enhancement if budget allows
        try:
            llm_result = super().run(request, budget, ctx)
            # Preserve the reference list and backend from static analysis
            # (LLM cannot produce real file:line references — only static can)
            if not llm_result.references:
                llm_result.references       = static.references
                llm_result.total_references = static.total_references
                llm_result.search_backend   = static.search_backend
                llm_result.high_impact_files = static.high_impact_files
            llm_result.fallback_used = False
            return llm_result
        except Exception:
            pass

        # Return static result
        return static

    def fallback_result(self, request: AnalysisRequest) -> ReferenceImpactResult:
        return _static_analysis(request)
