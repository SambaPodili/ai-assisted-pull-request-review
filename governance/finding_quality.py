"""
governance/finding_quality.py
------------------------------
Constrain LLM findings with deterministic ground truth from the diff.

Two corrections, applied to the LLM-driven agents (security, code_analysis) whose
findings carry a free-text line_range the model often gets wrong or hedges:

  1. LINE ANCHORING — the model frequently reports a plausible-but-wrong line
     ("line 21-25" when the change is at line 393). We snap each finding's line
     to the NEAREST actually-changed line in the cited file, so the displayed
     location matches the real diff.

  2. SPECULATION FLAGGING — conditional, hedged findings ("…if X is not using
     parameterised queries", "ensure…", "should verify…") are advice, not
     confirmed issues. They are marked `unverified` (kept and shown, but
     de-emphasised and excluded from the gate), cutting false-positive noise
     without hiding anything.

Deterministic, zero tokens. Runs in orchestrator._finalize.
"""
from __future__ import annotations

import logging
import re

from core.models import AnalysisReport

log = logging.getLogger(__name__)

# A run of "within the same sentence" characters: any non-(period/newline), but a
# period IS allowed when it sits inside a qualified name like `dao.method` (dot
# followed by a word char). Without this, "if a.b is not ..." stopped at the dot
# and the hedge went undetected.
_SAME_SENT = r"(?:[^.\n]|\.(?=\w)){0,80}"

# Conditional / advisory phrasing → the finding is speculative, not confirmed.
_HEDGE = re.compile(
    r"(?i)("
    rf"\bif\b{_SAME_SENT}\b(?:not|isn'?t|aren'?t|fail|fails|missing|without)\b"   # "...if X is not ..."
    rf"|potential{_SAME_SENT}\bif\b"
    r"|\b(?:ensure|make sure|consider|please verify|verify that|"
    r"should be (?:verified|reviewed|checked|validated)|it is recommended)\b"
    rf"|\b(?:may|might|could)\b{_SAME_SENT}\b(?:be|lead to|cause|result in|allow|expose)\b"
    r")"
)

# Leading-hedge: the finding OPENS with a speculation word ("Potential SQL
# injection…", "Possible PII exposure…", "May leak…"). This is the single most
# common shape of an LLM false positive. Used for RANKING/confidence only (it is
# intentionally NOT fed into the gate — see correct_findings — so a genuinely
# worded-cautiously CRITICAL is downranked but never silently auto-passed).
_LEADING_SPECULATION = re.compile(
    r"(?i)^\s*\W*"
    r"(?:potential(?:ly)?|possible|possibly|may\b|might\b|could\b|"
    r"appears?\s+to|seems?\s+to|likely\b|suspected|suspicious(?:ly)?)"
)


def is_speculative(text: str) -> bool:
    """Broad speculation test for RANKING (Top Issues), not the gate: a finding
    that opens with a hedge word OR contains conditional/advisory phrasing."""
    t = text or ""
    return bool(_LEADING_SPECULATION.search(t) or _HEDGE.search(t))


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lstrip("./").lower()


def _first_int(line_range: str) -> int:
    m = re.search(r"\d+", str(line_range or ""))
    return int(m.group()) if m else 0


def _located_llm_findings(report: AnalysisReport):
    """Yield (finding, is_deterministic) for the LLM agents whose lines/claims
    we want to ground. Deterministic agents already have exact lines."""
    sec = report.security
    if sec:
        det = bool(getattr(sec, "fallback_used", False))   # regex SAST path = precise
        for f in getattr(sec, "findings", None) or []:
            yield f, det
    ca = report.code_analysis
    if ca:
        det = bool(getattr(ca, "fallback_used", False))
        for f in getattr(ca, "findings", None) or []:
            yield f, det


def correct_findings(report: AnalysisReport, changed_lines: dict[str, set[int]]) -> tuple[int, int]:
    """Anchor wrong line numbers and flag speculative findings.

    changed_lines: {normalised_file_path: {added line numbers}} from the diff.
    Returns (lines_anchored, speculative_flagged).
    """
    anchored = 0
    flagged = 0
    norm_map = { _norm(k): v for k, v in (changed_lines or {}).items() }

    for f, is_det in _located_llm_findings(report):
        # ── 1. Line anchoring (only for findings whose file is in the diff) ──
        fp = _norm(getattr(f, "file_path", ""))
        lines = norm_map.get(fp)
        if not lines and fp:
            base = fp.rsplit("/", 1)[-1]
            for k, v in norm_map.items():
                if k.rsplit("/", 1)[-1] == base:
                    lines = v
                    break
        if lines:
            cur = _first_int(getattr(f, "line_range", ""))
            if cur not in lines:
                nearest = min(lines, key=lambda L: abs(L - cur)) if cur else min(lines)
                if hasattr(f, "line_range"):
                    f.line_range = str(nearest)
                    anchored += 1

        # ── 2. Speculation flagging (skip deterministic findings) ──
        if not is_det and not getattr(f, "unverified", False):
            desc = getattr(f, "description", "") or ""
            sev = str(getattr(getattr(f, "severity", ""), "value", getattr(f, "severity", ""))).lower()
            # Flag hedged/speculative findings ("Potential…", "Possible…", "if X is
            # not…") as low-confidence so they're excluded from the gate AND the
            # compliance roll-up. CRITICAL findings are NEVER auto-flagged — a real
            # critical worded cautiously must still hold the gate.
            if sev != "critical" and is_speculative(desc):
                if hasattr(f, "unverified"):
                    f.unverified = True
                    flagged += 1

    # Recompute the security severity that drives the gate so newly-flagged
    # speculative findings stop forcing a HOLD.
    sec = report.security
    if sec and flagged:
        from governance.evidence import _SEV_ORDER, RiskLevel
        worst = RiskLevel.LOW
        for f in getattr(sec, "findings", None) or []:
            if not getattr(f, "unverified", False) and \
               _SEV_ORDER.get(f.severity, 0) > _SEV_ORDER.get(worst, 0):
                worst = f.severity
        sec.overall_severity = worst

    if anchored or flagged:
        note = (f"Finding quality: {anchored} line number(s) snapped to the real diff, "
                f"{flagged} speculative finding(s) marked low-confidence (excluded from the gate).")
        report.suppressed_notes = (report.suppressed_notes or []) + [note]
        log.info("[%s] %s", report.request_id, note)
    return anchored, flagged
