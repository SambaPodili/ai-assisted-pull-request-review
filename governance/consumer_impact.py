"""
governance/consumer_impact.py
------------------------------
Trace breaking changes to the EXACT downstream call-sites they will break,
and label the likely failure mode.

It cross-references two things the analysis already produced:
  • Breaking changes  — from the interface agent (removed/renamed endpoints,
    type changes) and destructive schema changes.
  • The reference graph — real caller file:line sites from the reference-impact
    agent (grep / auto-clone backends).

Output: a list of ConsumerImpact records ("endpoint /v1/refund removed → called
in payment_client.py:88 → HTTP 404"), so a reviewer sees who actually breaks,
not just that "something" broke.
"""
from __future__ import annotations

from core.models import AnalysisReport, ConsumerImpact


# Map a break type to a concrete runtime failure mode for the caller.
_FAILURE_MODE = {
    "removed":             "HTTP 404 / NoSuchMethod — endpoint or symbol no longer exists",
    "renamed":             "HTTP 404 / AttributeError — old name no longer resolves",
    "type_change":         "Deserialization / type-mismatch error at the call site",
    "required_added":      "HTTP 400 — new required field/param not supplied by caller",
    "schema_incompatible": "Runtime contract error — payload no longer matches schema",
    "drop_column":         "SQL/ORM error — column referenced by this code no longer exists",
    "drop_table":          "SQL error — table referenced by this code no longer exists",
    "alter_column":        "Type/constraint error reading this column",
}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def trace_consumer_impacts(report: AnalysisReport, max_items: int = 30) -> list[ConsumerImpact]:
    """
    Build ConsumerImpact records by matching breaking changes against caller
    references. Conservative: only emits a record when a real caller reference
    plausibly matches the changed symbol/endpoint.
    """
    impacts: list[ConsumerImpact] = []

    ri = getattr(report, "reference_impact", None)
    references = list(getattr(ri, "references", None) or []) if ri else []

    # Index references by the symbol they call (lowercased) for quick lookup
    refs_by_symbol: dict[str, list] = {}
    for ref in references:
        refs_by_symbol.setdefault(_norm(getattr(ref, "symbol", "")), []).append(ref)

    def _token(s: str) -> str:
        """Last path/identifier token of an endpoint or symbol, e.g. '/v1/refund' → 'refund'."""
        s = (s or "").rstrip("/")
        return _norm(s.split("/")[-1].split(".")[-1].split("(")[0])

    seen: set[tuple] = set()

    def _add(change, change_type, ref, severity):
        key = (change, getattr(ref, "file_path", ""), getattr(ref, "line", 0))
        if key in seen:
            return
        seen.add(key)
        impacts.append(ConsumerImpact(
            change=change,
            change_type=change_type,
            file_path=getattr(ref, "file_path", ""),
            line=getattr(ref, "line", 0),
            symbol=getattr(ref, "symbol", ""),
            failure_mode=_FAILURE_MODE.get(change_type, "Behavioural change — verify caller"),
            severity=severity,
        ))

    # ── 1) Interface breaking changes ─────────────────────────────────────────
    iface = getattr(report, "interface", None)
    for b in (getattr(iface, "breaking_changes", None) or []):
        path        = getattr(b, "path", "") or getattr(b, "endpoint", "")
        break_type  = _norm(getattr(b, "break_type", "removed"))
        severity    = getattr(getattr(b, "severity", None), "value", getattr(b, "severity", "high"))
        tok         = _token(path)
        # Match any caller reference whose symbol token equals the endpoint token
        matched = []
        for sym, refs in refs_by_symbol.items():
            if tok and (tok == sym or tok in sym or sym in tok):
                matched.extend(refs)
        for ref in matched[:8]:
            _add(path or tok, break_type, ref, str(severity))
        # Even with no caller match, record the change so it isn't lost
        if not matched and path:
            impacts.append(ConsumerImpact(
                change=path, change_type=break_type, file_path="", line=0, symbol="",
                failure_mode=_FAILURE_MODE.get(break_type, "Verify consumers"),
                severity=str(severity),
            ))

    # ── 2) Destructive schema changes ─────────────────────────────────────────
    sc = getattr(report, "schema_change", None)
    for ch in (getattr(sc, "changes", None) or []):
        ctype = _norm(getattr(ch, "change_type", ""))
        if ctype not in ("drop_column", "drop_table", "alter_column"):
            continue
        col   = _norm(getattr(ch, "column_name", "") or getattr(ch, "column", ""))
        table = _norm(getattr(ch, "table_name", "") or getattr(ch, "table", ""))
        target = col or table
        sev = getattr(getattr(ch, "severity", None), "value", getattr(ch, "severity", "high"))
        for sym, refs in refs_by_symbol.items():
            if target and (target == sym or target in sym):
                for ref in refs[:5]:
                    _add(f"{table}.{col}".strip("."), ctype, ref, str(sev))

    # Sort: critical/high first, then by file
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    impacts.sort(key=lambda x: (order.get(_norm(x.severity), 9), x.file_path))
    return impacts[:max_items]
