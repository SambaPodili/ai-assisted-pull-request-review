"""
governance/false_positive_guard.py
-----------------------------------
Deterministic guards against three recurring LLM false-positive classes seen in
real reviews, all keyed on the actual diff content (zero tokens):

  1. N+1 "inside a loop" — a performance finding that claims a loop where the
     diff has NO loop construct anywhere near the cited line. (The model often
     narrates "…call inside loop" for a flat sequence of DAO calls.)

  2. "Potential PII" on operational columns — a data-privacy finding on a field
     like error_message / error_code / *_status / *_id that carries no real PII
     signal (no name/email/phone/ssn/address/dob/card/account/etc.).

  3. CREATE INDEX / PK / FK "may lock the table" — a schema finding for an index
     (or inline PK/FK) on a table that is CREATED in the same change set. A
     brand-new, empty table cannot be locked by its own index build, so the
     "prefer CONCURRENTLY / online DDL" warning is noise.

Findings are MARKED low-confidence (PII/perf → `unverified`; schema → severity
lowered + `on_new_table`), never deleted — same philosophy as
governance/evidence.py and governance/finding_quality.py.

Runs in orchestrator._finalize, BEFORE the evidence guard and correlation.
"""
from __future__ import annotations

import logging
import re

from core.models import AnalysisReport, RiskLevel

log = logging.getLogger(__name__)


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lstrip("./").lower()


# ── 1. N+1 loop verification ─────────────────────────────────────────────────
# Loop constructs across languages: for/while/do, Java/JS forEach + streams,
# Python comprehensions, Kotlin/Scala .map{ }.
_LOOP_RE = re.compile(
    r"(?i)(?:\bfor\b|\bwhile\b|\bdo\b|\bforeach\b|\.for_?each\s*\(|"
    r"\.stream\s*\(|\.map\s*[\(\{]|\bfor\s+\w+\s+in\b)"
)
_N1_LINE_WINDOW = 12   # how far from the cited line a loop may legitimately sit


def _lines_for(source_lines: dict[str, list[tuple[int, str]]], file_path: str):
    fp = _norm(file_path)
    if fp in source_lines:
        return source_lines[fp]
    base = fp.rsplit("/", 1)[-1]
    for k, v in source_lines.items():
        if k.rsplit("/", 1)[-1] == base:
            return v
    return None


def _guard_n1(report: AnalysisReport, source_lines) -> int:
    perf = report.performance_impact
    if not perf:
        return 0
    flagged = 0
    for f in getattr(perf, "findings", None) or []:
        if getattr(f, "unverified", False):
            continue
        cat  = (getattr(f, "category", "") or "").lower()
        desc = (getattr(f, "description", "") or "").lower()
        claims_loop = (
            "n_plus_1" in cat or "n+1" in cat or "n+1" in desc or
            "inside loop" in desc or "in a loop" in desc or "inside a loop" in desc or
            ("loop" in desc and ("quer" in desc or "call" in desc or "db" in desc))
        )
        if not claims_loop:
            continue
        lines = _lines_for(source_lines, getattr(f, "file_path", ""))
        if lines is None:
            continue   # file not in diff — evidence guard handles that
        cur = int(getattr(f, "line", 0) or 0)
        if cur:
            texts = [t for (ln, t) in lines if abs(ln - cur) <= _N1_LINE_WINDOW]
        else:
            texts = [t for (_, t) in lines]
        if not any(_LOOP_RE.search(t) for t in texts):
            f.unverified = True
            flagged += 1
    return flagged


# ── 2. PII non-evidence guard ────────────────────────────────────────────────
# Real PII signals — if NONE of these appear in the finding (type/context) the
# "Potential PII" claim is unsubstantiated.
_REAL_PII_RE = re.compile(
    r"(?i)\b("
    r"email|e-mail|phone|mobile|msisdn|ssn|social.?security|nric|fin|"
    r"passport|national.?id|tax.?id|"
    r"credit.?card|card.?number|cardno|pan|cvv|iban|account.?(?:no|number)|"
    r"date.?of.?birth|dob|birth.?date|"
    r"first.?name|last.?name|full.?name|surname|given.?name|"
    r"address|postcode|post.?code|zip.?code|"
    r"ip.?address|geo.?location|latitude|longitude|"
    r"salary|income|biometric|fingerprint"
    r")\b"
)
_REAL_PII_TYPES = {
    "email", "phone", "ssn", "credit_card", "dob", "passport",
    "address", "name", "generic_pii", "ip", "national_id",
}
# Obviously-operational fields the model loves to mislabel as PII.
_OPERATIONAL_RE = re.compile(
    r"(?i)\b("
    r"error_?message|error_?code|response_?message|response_?status|response_?code|"
    r"file_?status|batch_?id|status|state|flag|"
    r"created_?at|updated_?at|timestamp|version|sequence|count|total"
    r")\b"
)


def _guard_pii(report: AnalysisReport) -> int:
    priv = report.data_privacy
    if not priv:
        return 0
    flagged = 0
    for f in getattr(priv, "pii_findings", None) or []:
        if getattr(f, "unverified", False):
            continue
        ptype = (getattr(f, "pii_type", "") or "").lower()
        blob  = f"{ptype} {getattr(f, 'description', '') or ''} {getattr(f, 'context', '') or ''}"
        has_real = ptype in _REAL_PII_TYPES or bool(_REAL_PII_RE.search(blob))
        if has_real:
            continue
        # No real PII signal. Flag when the field is clearly operational OR the
        # claim is hedged ("potential/possible") — i.e. an unsubstantiated guess.
        looks_operational = bool(_OPERATIONAL_RE.search(blob))
        hedged = bool(re.match(r"(?i)\s*\W*(potential|possible|may|might|could)", blob))
        if looks_operational or hedged:
            f.unverified = True
            flagged += 1
    return flagged


# ── 3. Schema: index/PK/FK on a brand-new table can't lock ───────────────────
_INDEXY = {"add_index", "create_index", "add_constraint", "add_foreign_key",
           "add_primary_key", "add_column"}


def _guard_schema_new_table(report: AnalysisReport) -> int:
    sc = report.schema_change
    if not sc:
        return 0
    changes = getattr(sc, "changes", None) or []
    new_tables = {
        (getattr(c, "table_name", "") or "").strip().lower()
        for c in changes
        if (getattr(c, "change_type", "") or "").lower() in ("add_table", "create_table")
        and (getattr(c, "table_name", "") or "").strip()
    }
    if not new_tables:
        return 0
    adjusted = 0
    for c in changes:
        ct = (getattr(c, "change_type", "") or "").lower()
        tbl = (getattr(c, "table_name", "") or "").strip().lower()
        if ct in _INDEXY and tbl and tbl in new_tables and not getattr(c, "on_new_table", False):
            c.on_new_table = True
            # An index/constraint created with a fresh, empty table holds no lock.
            if c.severity in (RiskLevel.MEDIUM, RiskLevel.HIGH):
                c.severity = RiskLevel.LOW
            note = (f" (created together with the new table '{c.table_name}' — "
                    f"no locking/online-DDL concern on an empty table)")
            if note.strip() not in (c.description or ""):
                c.description = (c.description or "").rstrip(".") + "." + note
            adjusted += 1
    return adjusted


def guard_false_positives(report: AnalysisReport,
                          source_lines: dict[str, list[tuple[int, str]]] | None) -> tuple[int, int, int]:
    """Apply all three deterministic guards. Returns (n1, pii, schema) counts."""
    n1 = _guard_n1(report, source_lines or {})
    pii = _guard_pii(report)
    schema = _guard_schema_new_table(report)
    if n1 or pii or schema:
        bits = []
        if n1:     bits.append(f"{n1} N+1 finding(s) with no loop in the diff")
        if pii:    bits.append(f"{pii} PII finding(s) with no PII signal")
        if schema: bits.append(f"{schema} index/constraint(s) on a brand-new table")
        note = "False-positive guard: " + "; ".join(bits) + " marked low-confidence."
        report.suppressed_notes = (report.suppressed_notes or []) + [note]
        log.info("[%s] %s", report.request_id, note)
    return n1, pii, schema
