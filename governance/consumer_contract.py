"""
governance/consumer_contract.py
--------------------------------
Consumer-CONTRACT compatibility — a step beyond governance/consumer_impact.py.

consumer_impact.py answers "does a caller reference the changed SYMBOL/endpoint
at all" (matched by function/class/endpoint token, via reference_impact's
symbol index). It structurally can't answer a narrower, more common question:
a producer's DTO/schema REMOVED or RETYPED one FIELD — is that specific field
actually read by a consumer's own code, not just "does the consumer call this
class/endpoint somewhere."

This module correlates agents/interface_agent.py's field-level ContractBreak
records (field_name/old_type/new_type — see that model) against the ALREADY-
COLLECTED cross-repo reference context lines
(report.reference_impact.references[].context) for a literal, whole-word
occurrence of the broken field's name in a DIFFERENT repo than the producer's
own. A hit is real, specific evidence: not just "consumer calls this class",
but "this consumer source line mentions the exact field that broke."

Known scope limit (v1): the context available here is the ONE line the
reference-impact agent's symbol search already captured — captured by
searching for the SYMBOL/class name, not the field name, so it misses a field
reference sitting on a different line of the same consumer file. Closing that
gap needs a dedicated per-field grep of each consumer repo (the fuller design:
extend the producer delta, fetch consumer DTO/model files, run a real
schema/field compatibility engine) — deferred as a separate, larger piece of
work. This module is the deterministic first slice of that design: real
evidence when it fires, and silently nothing (never a fabricated finding) when
the one captured line doesn't happen to mention the field.
"""
from __future__ import annotations

import re

from core.models import AnalysisReport, ConsumerImpact

# break_type -> failure mode. Only "removed" and "type_change" are populated
# with field_name today (agents/interface_agent.py::_parse_openapi_diff) — a
# field newly marked required has no parser support yet, so it's deliberately
# not listed here rather than promising a check that never fires.
_FIELD_FAILURE_MODE = {
    "removed":     "Deserialization / KeyError / AttributeError — field no longer exists",
    "type_change": "Deserialization / type-mismatch error reading this field",
}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def check_consumer_contracts(report: AnalysisReport, max_items: int = 20) -> list[ConsumerImpact]:
    """Field-level contract-incompatibility records — see module docstring.
    Conservative by construction: only emits a record when the broken field's
    name literally appears (whole-word) in a real cross-repo reference's
    already-captured context line. Returns [] on missing/empty inputs — never
    raises, safe to call unconditionally from the finalize pipeline."""
    iface = getattr(report, "interface", None)
    field_breaks = [
        b for b in (getattr(iface, "breaking_changes", None) or [])
        if getattr(b, "field_name", None) and _norm(getattr(b, "break_type", "")) in _FIELD_FAILURE_MODE
    ]
    if not field_breaks:
        return []

    ri = getattr(report, "reference_impact", None)
    references = list(getattr(ri, "references", None) or []) if ri else []
    # Only CROSS-REPO references matter here — a same-repo hit on this field
    # is already whatever code_analysis/interface flag directly in this diff;
    # the whole point of this module is surfacing breakage in OTHER repos.
    cross_repo_refs = [r for r in references if getattr(r, "repo", "")]
    if not cross_repo_refs:
        return []

    impacts: list[ConsumerImpact] = []
    seen: set[tuple] = set()
    for b in field_breaks:
        field = (b.field_name or "").strip()
        if not field:
            continue
        pattern = re.compile(rf"\b{re.escape(field)}\b")
        break_type = _norm(b.break_type)
        severity = getattr(b.severity, "value", b.severity)
        change_desc = f"field '{field}'"
        if break_type == "type_change" and b.old_type:
            change_desc += (
                f" (`{b.old_type}` → `{b.new_type}`)" if b.new_type else f" (was `{b.old_type}`)"
            )
        for ref in cross_repo_refs:
            if not pattern.search(getattr(ref, "context", "") or ""):
                continue
            key = (field, ref.repo, ref.file_path, ref.line)
            if key in seen:
                continue
            seen.add(key)
            impacts.append(ConsumerImpact(
                change=change_desc,
                change_type=f"field_{break_type}",
                file_path=ref.file_path,
                line=ref.line,
                symbol=ref.symbol,
                failure_mode=_FIELD_FAILURE_MODE[break_type],
                severity=str(severity),
                repo=ref.repo,
            ))
            if len(impacts) >= max_items:
                return impacts
    return impacts
