"""
governance/suppression.py
---------------------------
Close the reviewer-feedback loop: when reviewers have repeatedly marked a
particular (agent, category) as a false positive for a repo — and never marked
it valid — auto-suppress matching findings on future runs.

This raises signal-to-noise and stops known-noisy checks from blocking the gate,
WITHOUT silently hiding anything: the count + a note are recorded on the report
so reviewers can see what was suppressed and why.

Conservative by design (see SQLiteFeedbackStore.suppressed_categories): a check
that is ever genuinely useful is never suppressed.
"""
from __future__ import annotations
import logging

from core.models import AnalysisReport, RiskLevel

log = logging.getLogger(__name__)

_SEV_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}


def apply_suppressions(report: AnalysisReport, repo: str, store) -> int:
    """Remove security findings whose (agent='security', cwe) reviewers have
    flagged as a repeat false positive for this repo. Returns the count removed.
    Best-effort: any failure leaves the report untouched."""
    try:
        suppressed = store.suppressed_categories(repo=repo or "", min_fp=3)
    except Exception as exc:   # pragma: no cover - defensive
        log.debug("[%s] suppression lookup failed: %s", report.request_id, exc)
        return 0
    if not suppressed:
        return 0

    sec = report.security
    if not sec or not getattr(sec, "findings", None):
        return 0

    sec_cwes = {cat for (agent, cat) in suppressed if agent == "security" and cat}
    if not sec_cwes:
        return 0

    kept = []
    removed = 0
    removed_cwes: set[str] = set()
    for f in sec.findings:
        cwe = (getattr(f, "cwe_id", "") or "").strip()
        # SAFETY: never auto-delete a high/critical finding on a CWE basis — a
        # CWE dismissed as noise before (e.g. on a test file) can still be a REAL
        # vulnerability now. Only quiet low/medium noise; serious findings stay
        # fully visible AND keep driving the gate.
        if cwe and cwe in sec_cwes and _SEV_ORDER.get(f.severity, 0) <= _SEV_ORDER.get(RiskLevel.MEDIUM, 1):
            removed += 1
            removed_cwes.add(cwe)
            continue
        kept.append(f)

    if removed:
        sec.findings = kept
        # Recompute overall severity from what remains.
        sec.overall_severity = RiskLevel.LOW
        for f in kept:
            if _SEV_ORDER.get(f.severity, 0) > _SEV_ORDER.get(sec.overall_severity, 0):
                sec.overall_severity = f.severity
        report.suppressed_count = (report.suppressed_count or 0) + removed
        note = (f"Suppressed {removed} low/medium security finding(s) for "
                f"{', '.join(sorted(removed_cwes))} — repeatedly marked false positive by reviewers. "
                "(High/critical findings are never auto-suppressed.)")
        report.suppressed_notes = (report.suppressed_notes or []) + [note]
        log.info("[%s] %s", report.request_id, note)
    return removed
