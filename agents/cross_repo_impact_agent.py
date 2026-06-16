"""
agents/cross_repo_impact_agent.py
----------------------------------
Cross-Repo Deep Impact Agent — for every call-site of a changed symbol found in a
reviewer-declared DOWNSTREAM repo, decide whether the primary change actually
BREAKS that caller, why, and the concrete fix the downstream repo must make.

This is the "deep analysis of dependent repos" layer. Where reference-impact just
locates call-sites and consumer-impact maps explicit interface/schema breaks, this
agent reasons about EACH call-site against how the changed symbol actually changed:

  • removed / renamed       → the call no longer resolves (compile / 404 / NoSuchMethod)
  • signature_changed       → arg list / return type no longer matches the call
  • modified (body only)    → behaviour may differ; assumptions at the call-site may break

Inputs (no extra network from the backend):
  • metadata.external_references — call-sites {symbol,file_path,line,context,repo}
    that the frontend already fetched from the declared repos via /api/v1/git/xref.
    `context` is the enclosing caller code when xref enrichment is available, else a
    single line.
  • The primary diff — to classify how each symbol changed (and old→new arity).

Deterministic static pass runs always (zero tokens); the LLM pass sharpens the
reason/fix per call-site using the caller context. No declared repos / no
call-sites → the agent is a cheap no-op (analysed=False), so normal single-repo
PRs pay nothing.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, RiskLevel,
    CrossRepoImpact, CrossRepoImpactResult,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt
from ingestion.symbol_extractor import extract_changed_symbols

log = logging.getLogger(__name__)

_SEV_ORDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]


def _worst(severities: list[RiskLevel]) -> RiskLevel:
    return max(severities, key=_SEV_ORDER.index) if severities else RiskLevel.LOW


def _arity(sig_line: str) -> int:
    """Best-effort parameter count from a signature line: the text inside the
    FIRST (...) pair, split on top-level commas. Returns -1 when no parens."""
    m = re.search(r"\(([^)]*)\)", sig_line or "")
    if not m:
        return -1
    inner = m.group(1).strip()
    if not inner:
        return 0
    # crude top-level comma split (ignores generics/nested parens — good enough)
    depth, count, has = 0, 0, False
    for ch in inner:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
        if not ch.isspace():
            has = True
    return (count + 1) if has else 0


def classify_symbol_changes(request: AnalysisRequest) -> dict[str, dict]:
    """{symbol_name: {change_kind, old_sig, new_sig, old_arity, new_arity}}.

    change_kind: removed (only on '-'), signature_changed (both sides, arity or
    text differs), modified (only on '+', i.e. new/body change)."""
    added: dict[str, str] = {}
    removed: dict[str, str] = {}
    for hunk in request.hunks or []:
        for sym in extract_changed_symbols(hunk):
            # recover the raw signature line text for arity comparison
            raw = _raw_sig_line(hunk.content, sym.line)
            (added if sym.is_added else removed)[sym.name] = raw

    out: dict[str, dict] = {}
    for name in set(added) | set(removed):
        old, new = removed.get(name), added.get(name)
        if old is not None and new is None:
            kind = "removed"
        elif old is not None and new is not None:
            kind = "signature_changed" if _arity(old) != _arity(new) or \
                   _norm(old) != _norm(new) else "modified"
        else:
            kind = "modified"
        out[name] = {
            "change_kind": kind,
            "old_sig": (old or "").strip()[:200],
            "new_sig": (new or "").strip()[:200],
            "old_arity": _arity(old) if old else -1,
            "new_arity": _arity(new) if new else -1,
        }
    return out


def _raw_sig_line(hunk_content: str, lineno: int) -> str:
    lines = hunk_content.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].lstrip("+-").strip()
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _references(request: AnalysisRequest) -> list[dict]:
    """Call-sites in declared downstream repos (metadata.external_references)."""
    raw = (getattr(request, "metadata", None) or {}).get("external_references") or []
    out = []
    for it in raw[:400]:
        if not isinstance(it, dict):
            continue
        repo = str(it.get("repo") or "").strip()
        path = str(it.get("file_path") or it.get("path") or "").strip()
        if not repo or not path:
            continue   # only real cross-repo hits (a repo label is required)
        out.append({
            "symbol": str(it.get("symbol") or "").strip(),
            "file_path": path,
            "line": int(it.get("line") or 0),
            "context": str(it.get("context") or "")[:1500],
            "repo": repo,
        })
    return out


# Deterministic impact per change kind (used by the static pass and as the LLM floor)
_KIND_IMPACT = {
    "removed":           ("breaks", RiskLevel.HIGH,
                          "The symbol was removed or renamed in this change, so this call no longer resolves."),
    "signature_changed": ("likely", RiskLevel.HIGH,
                          "The signature changed; this call-site's argument list / return handling likely no longer matches."),
    "modified":          ("possible", RiskLevel.MEDIUM,
                          "The implementation changed; behaviour this call-site relies on may differ — verify."),
}


def _static_impacts(request: AnalysisRequest) -> CrossRepoImpactResult:
    refs = _references(request)
    changes = classify_symbol_changes(request)
    if not refs:
        return CrossRepoImpactResult(analysed=False, summary="No downstream call-sites to analyse.")

    impacts: list[CrossRepoImpact] = []
    for r in refs:
        sym = r["symbol"]
        meta = changes.get(sym) or {}
        kind = meta.get("change_kind", "modified")
        impact, sev, reason = _KIND_IMPACT.get(kind, _KIND_IMPACT["modified"])
        # sharpen the reason with the actual signature delta when we have it
        if kind == "signature_changed" and meta.get("old_arity", -1) != meta.get("new_arity", -1) \
           and meta.get("old_arity", -1) >= 0 and meta.get("new_arity", -1) >= 0:
            reason = (f"Parameter count changed from {meta['old_arity']} to {meta['new_arity']} "
                      f"(`{meta.get('old_sig','')}` → `{meta.get('new_sig','')}`); update this call.")
            fix = "Update the call to match the new parameter list."
        elif kind == "removed":
            fix = f"Stop calling `{sym}` here, or point it at the replacement symbol."
        else:
            fix = f"Re-test this usage of `{sym}` against the new behaviour."
        impacts.append(CrossRepoImpact(
            repo=r["repo"], file_path=r["file_path"], line=r["line"], symbol=sym,
            change_kind=kind, impact=impact, severity=sev, reason=reason,
            suggested_fix=fix, caller_context=r["context"],
        ))

    repos = sorted({i.repo for i in impacts})
    breaking = sum(1 for i in impacts if i.impact in ("breaks", "likely"))
    overall = _worst([i.severity for i in impacts])
    return CrossRepoImpactResult(
        impacts=impacts,
        repos_analysed=repos,
        total_call_sites=len(impacts),
        breaking_count=breaking,
        overall_risk=overall,
        analysed=True,
        summary=(f"{len(impacts)} downstream call-site(s) across {len(repos)} repo(s); "
                 f"{breaking} likely to break."),
        fallback_used=True,
    )


class CrossRepoImpactAgent(BaseAgent[CrossRepoImpactResult]):

    agent_name       = AgentName.CROSS_REPO_IMPACT
    output_model     = CrossRepoImpactResult
    output_token_cap = 4000

    system_prompt = (
        "You are a senior engineer assessing the blast radius of a code change on its "
        "DOWNSTREAM consumer repositories.\n"
        "You are given:\n"
        "  • How each changed symbol changed (removed / signature_changed / modified, with "
        "old→new signatures).\n"
        "  • A list of call-sites in downstream repos, each with the caller's surrounding code.\n\n"
        "For EACH call-site decide whether the change breaks it. Output a JSON "
        "CrossRepoImpactResult where each `impacts` item has:\n"
        "  - repo, file_path, line, symbol (copy from the call-site)\n"
        "  - change_kind: removed | signature_changed | modified\n"
        "  - impact: breaks | likely | possible | unlikely\n"
        "  - severity: CRITICAL | HIGH | MEDIUM | LOW\n"
        "  - reason: 1 sentence grounded in the caller code (quote the offending call)\n"
        "  - suggested_fix: the concrete edit the downstream repo must make\n"
        "Rules: a removed/renamed symbol that is still called = breaks/HIGH. An arg-count or "
        "type mismatch vs the new signature = likely/HIGH. A body-only change where the call "
        "still type-checks = possible/MEDIUM or unlikely/LOW. Do NOT invent call-sites — only "
        "assess the ones provided. Also set repos_analysed, total_call_sites, breaking_count "
        "(breaks+likely), overall_risk, analysed=true, and a 1-sentence summary.\n"
        "Output ONLY valid JSON. No prose."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        changes = context.get("_symbol_changes") or classify_symbol_changes(request)
        refs    = context.get("_references") or _references(request)

        chg_lines = "\n".join(
            f"  {name}: {m['change_kind']}"
            + (f"  ({m['old_sig']} → {m['new_sig']})" if m.get("old_sig") or m.get("new_sig") else "")
            for name, m in changes.items()
        ) or "  (no symbol signature changes detected)"

        call_lines = []
        for i, r in enumerate(refs[:40], 1):
            call_lines.append(
                f"\n[{i}] repo={r['repo']} {r['file_path']}:{r['line']} symbol={r['symbol']}\n"
                f"caller code:\n{r['context'] or '(no context captured)'}"
            )
        calls = "\n".join(call_lines) or "  (no downstream call-sites)"

        return (
            f"Primary repo: {request.repo_url}\n"
            f"How the changed symbols changed:\n{chg_lines}\n\n"
            f"Downstream call-sites to assess ({len(refs)}):\n{calls}\n\n"
            f"Primary diff (for reference):\n"
            f"{format_hunks_for_prompt(request.hunks, max_chars_per_hunk=1200)}"
        )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> CrossRepoImpactResult:
        ctx = context or {}
        t0  = time.monotonic()

        refs = _references(request)
        if not refs:
            # No declared downstream repos / no call-sites — cheap no-op.
            self.report_static_progress(request)
            result = CrossRepoImpactResult(analysed=False,
                                           summary="No downstream repos declared (or no call-sites found).")
            self.log_done(request, result, mode="skip", note="no downstream call-sites")
            return result

        static = _static_impacts(request)

        agent_key = self.agent_name.value
        remaining = budget.get_remaining(agent_key) if hasattr(budget, "get_remaining") else 0
        if remaining > 1500:
            try:
                ctx["_symbol_changes"] = classify_symbol_changes(request)
                ctx["_references"]     = refs
                llm = super().run(request, budget, ctx)
                # Trust the LLM's per-call-site verdicts, but never lose call-sites:
                # if it dropped some, fall back to the static set.
                if llm and llm.impacts:
                    llm.analysed = True
                    if not llm.repos_analysed:
                        llm.repos_analysed = static.repos_analysed
                    llm.total_call_sites = llm.total_call_sites or len(llm.impacts)
                    llm.breaking_count = sum(1 for i in llm.impacts if i.impact in ("breaks", "likely"))
                    llm.overall_risk = _worst([i.severity for i in llm.impacts])
                    llm.fallback_used = False
                    # LLM path already logged by base.run().
                    return llm
            except Exception as exc:
                log.warning("[%s] cross_repo_impact LLM failed, using static: %s",
                            request.request_id, exc)

        static.duration_s = round(time.monotonic() - t0, 2)
        self.log_done(request, static, mode="static",
                      note=f"{static.total_call_sites} call-site(s), {static.breaking_count} breaking")
        return static

    def fallback_result(self, request: AnalysisRequest) -> CrossRepoImpactResult:
        return _static_impacts(request)
