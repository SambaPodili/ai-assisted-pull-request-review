"""
agents/functional_validation_agent.py
--------------------------------------
FSD tracking & functional impact analysis.

Consumes the Functional Specification Documents uploaded with the analysis
(request.metadata["functional_docs"] = [{name, text}]) and:

  1. Extracts the spec's REQUIREMENTS (shall/must/should sentences or numbered
     items) and validates the code change against each one —
     implemented / partial / missing / contradicted.
  2. Derives FUNCTIONAL IMPACT: which business functions this change touches,
     at what risk, which dependent repos consume them, and where to point
     regression testing.

Two passes:
  • Deterministic — requirement extraction + keyword↔diff matching (no tokens).
    Never claims "contradicted" (that judgement needs semantics).
  • LLM — semantic validation per requirement + business-impact narrative,
    grounded by the deterministic pass and the declared dependent repos.

No FSD uploaded → a static result with a clear note (zero tokens).
"""
from __future__ import annotations

import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    FunctionalValidationResult, FSDRequirement, FunctionalImpact,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent, format_hunks_for_prompt

# Sentences that look like requirements
_REQ_RE = re.compile(
    r'(?i)\b(shall|must(?:\s+not)?|should(?:\s+not)?|will|needs? to|required to|'
    r'is required|are required|has to|have to)\b')
# Numbered spec items, e.g. "3.2.1 The system ..."
_NUM_RE = re.compile(r'^\s*(\d+(?:\.\d+)*)[.)]?\s+(.{20,})')

_STOPWORDS = {
    "shall", "must", "should", "will", "with", "that", "this", "from", "into",
    "have", "been", "they", "their", "when", "then", "than", "what", "where",
    "which", "would", "could", "system", "application", "user", "users", "data",
    "the", "and", "for", "are", "not", "all", "any", "each", "able", "also",
    "needs", "need", "required", "store", "stored", "value", "values", "field",
    "fields", "page", "screen", "shown", "show", "display", "displayed",
}

_MAX_REQS      = 30
_MAX_DOC_CHARS = 20_000


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r'(?<=[.;])\s+|\n{2,}', text) if s.strip()]


def _extract_requirements(docs: list[dict]) -> list[FSDRequirement]:
    """Pull requirement-like statements out of the uploaded FSDs."""
    reqs: list[FSDRequirement] = []
    counter = 0
    for doc in docs[:10]:
        name = str(doc.get("name") or "FSD")
        text = str(doc.get("text") or "")[:_MAX_DOC_CHARS]
        for line in text.splitlines():
            m = _NUM_RE.match(line)
            if m and _REQ_RE.search(m.group(2)):
                counter += 1
                reqs.append(FSDRequirement(req_id=m.group(1), text=m.group(2).strip()[:300],
                                           source_doc=name))
                if len(reqs) >= _MAX_REQS:
                    return reqs
        # Sentence pass for unnumbered specs
        for sent in _split_sentences(text):
            if len(sent) < 25 or not _REQ_RE.search(sent):
                continue
            if any(sent[:80] == r.text[:80] for r in reqs):
                continue
            counter += 1
            reqs.append(FSDRequirement(req_id=f"R{counter}", text=sent[:300], source_doc=name))
            if len(reqs) >= _MAX_REQS:
                return reqs
    return reqs


def _keywords(text: str) -> set[str]:
    """Significant lowercase tokens incl. split camelCase identifiers."""
    words = re.findall(r'[A-Za-z][A-Za-z0-9_]{3,}', text)
    out: set[str] = set()
    for w in words:
        for part in re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', w).replace('_', ' ').split():
            p = part.lower()
            if len(p) >= 4 and p not in _STOPWORDS:
                out.add(p)
    return out


def _diff_keyword_index(request: AnalysisRequest) -> dict[str, set[str]]:
    """{file_path: keyword set of its ADDED lines}."""
    idx: dict[str, set[str]] = {}
    for h in request.hunks:
        added = "\n".join(l[1:] for l in h.content.splitlines()
                          if l.startswith("+") and not l.startswith("+++"))
        if added.strip():
            idx[h.file_path] = _keywords(added)
    return idx


def _static_validate(request: AnalysisRequest) -> FunctionalValidationResult:
    """Deterministic pass: extraction + keyword matching. Honest about limits —
    statuses are 'partial'/'not_addressed' evidence hints, never 'contradicted'."""
    docs = (request.metadata or {}).get("functional_docs") or []
    if not docs:
        return FunctionalValidationResult(notes=[
            "No Functional Specification Document uploaded — add FSDs in "
            "Configure → Functional Docs to validate this change against the spec."])

    reqs = _extract_requirements(docs)
    if not reqs:
        return FunctionalValidationResult(
            docs_analysed=[str(d.get("name") or "FSD") for d in docs[:10]],
            notes=["No requirement-style statements (shall/must/should…) found in the "
                   "uploaded document(s) — validation needs an FSD with explicit requirements."])

    file_kw = _diff_keyword_index(request)
    connected = [str(x) for x in ((request.metadata or {}).get("connected_repos") or [])]
    touched = 0
    impacts: list[FunctionalImpact] = []

    for r in reqs:
        rk = _keywords(r.text)
        best_file, best_hits = "", 0
        for fp, kws in file_kw.items():
            hits = len(rk & kws)
            if hits > best_hits:
                best_file, best_hits = fp, hits
        if best_hits >= 2:
            r.status, touched = "partial", touched + 1
            r.evidence = best_file
            r.notes = f"{best_hits} requirement keyword(s) appear in this file's added lines — semantic check recommended."
            impacts.append(FunctionalImpact(
                function=" ".join(r.text.split()[:8]),
                impact=f"Changed code in {best_file} relates to this requirement.",
                risk="medium" if "must" in r.text.lower() or "shall" in r.text.lower() else "low",
                affected_repos=connected,
                test_focus=best_file,
            ))
        else:
            r.status = "not_addressed"
            r.notes = "No overlap with the changed code — likely out of this PR's scope."

    coverage = round(100.0 * touched / max(len(reqs), 1), 1)
    return FunctionalValidationResult(
        requirements=reqs,
        impacts=impacts[:10],
        docs_analysed=[str(d.get("name") or "FSD") for d in docs[:10]],
        coverage_pct=coverage,
        summary=(f"{len(reqs)} requirement(s) extracted from {len(docs)} FSD(s); "
                 f"{touched} relate to this change (keyword match — run with an LLM "
                 f"for semantic validation)."),
        fallback_used=True,
    )


class FunctionalValidationAgent(BaseAgent[FunctionalValidationResult]):

    agent_name       = AgentName.FUNCTIONAL_VALIDATION
    output_model     = FunctionalValidationResult
    output_token_cap = 4000

    system_prompt = (
        "You are a senior business analyst + engineer validating a code change against a "
        "Functional Specification Document (FSD) for an enterprise banking system.\n"
        "You are given: extracted requirements, the FSD excerpt, the code diff, and the "
        "dependent repositories that consume this service.\n\n"
        "For EVERY listed requirement, set status:\n"
        "  implemented  — the diff fully realises the requirement (cite file evidence)\n"
        "  partial      — the diff addresses it incompletely (say what's missing in notes)\n"
        "  missing      — the requirement is in scope for this change but absent from the diff\n"
        "  contradicted — the diff does the OPPOSITE of the requirement (quote both sides)\n"
        "  not_addressed — unrelated to this change (most requirements; this is normal)\n"
        "Then list functional impacts: business function, how the change affects it, risk, "
        "which of the PROVIDED dependent repos consume it (never invent repo names), and "
        "where to focus regression testing.\n"
        "Set has_contradiction=true only with quoted evidence on both sides.\n"
        "Output ONLY compact JSON matching the schema."
    )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> FunctionalValidationResult:
        docs = (request.metadata or {}).get("functional_docs") or []
        if not docs:
            result = _static_validate(request)        # the "no FSD" note path
            self.report_static_progress(request)
            self.log_done(request, result, mode="skip", note="no FSD uploaded")
            return result
        # Base class: LLM with deterministic fallback on any failure/budget issue.
        return super().run(request, budget, context or {})

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        static = _static_validate(request)
        docs   = (request.metadata or {}).get("functional_docs") or []
        connected = [str(x) for x in ((request.metadata or {}).get("connected_repos") or [])]

        req_block = "\n".join(
            f"  [{r.req_id}] {r.text}  (keyword pre-match: {r.status}"
            + (f", file: {r.evidence}" if r.evidence else "") + ")"
            for r in static.requirements[:_MAX_REQS]) or "  (none extracted — derive from the FSD excerpt)"
        fsd_excerpt = "\n\n".join(
            f"--- {d.get('name', 'FSD')} ---\n{str(d.get('text') or '')[:6000]}" for d in docs[:3])
        diff = format_hunks_for_prompt(request.hunks, max_chars_per_hunk=1800, focus="general")

        return (
            f"Repository: {request.repo_url}\n"
            f"Dependent repositories (consumers): {', '.join(connected) or '(none declared)'}\n\n"
            f"EXTRACTED REQUIREMENTS:\n{req_block}\n\n"
            f"FSD EXCERPT:\n{trim_diff_for_budget(fsd_excerpt, max_tokens_approx=2500)}\n\n"
            f"CODE DIFF:\n{diff}"
        )

    def fallback_result(self, request: AnalysisRequest) -> FunctionalValidationResult:
        return _static_validate(request)
