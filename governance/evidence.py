"""
governance/evidence.py
------------------------
Hallucination guard: every security finding should point at a file that was
actually changed in this diff. Findings that cite a file NOT in the changeset are
almost always model hallucinations — they're dropped so reviewers only ever see
substantiated, actionable issues (with a real file:line to look at).

Conservative: a finding with no file at all is kept (it may be a repo-level
observation); only findings whose cited path can't be matched to any changed
file are removed.
"""
from __future__ import annotations
import logging

from core.models import AnalysisReport, RiskLevel

log = logging.getLogger(__name__)

_SEV_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}


def _matches(finding_path: str, changed: set[str], changed_bases: set[str]) -> bool:
    fp = (finding_path or "").strip().lstrip("./").lower()
    if not fp:
        return True   # no path → not a location claim we can disprove; keep
    if fp in changed:
        return True
    base = fp.rsplit("/", 1)[-1]
    if base in changed_bases:
        return True
    # tolerate partial/relative paths in either direction
    return any(fp.endswith(c) or c.endswith(fp) for c in changed)


def filter_unsubstantiated(report: AnalysisReport, changed_files: set[str]) -> int:
    """Flag (do NOT delete) security findings whose cited file isn't in the diff.

    Such findings are kept and shown to the reviewer but marked `unverified`, and
    the recomputed `overall_severity` ignores them — so the gate is driven only by
    findings tied to real changed files, while nothing is silently hidden.
    Returns the count flagged.
    """
    sec = report.security
    if not sec or not getattr(sec, "findings", None) or not changed_files:
        return 0

    changed = {c.strip().lstrip("./").lower() for c in changed_files if c}
    changed_bases = {c.rsplit("/", 1)[-1] for c in changed}

    flagged = 0
    for f in sec.findings:
        verified = _matches(getattr(f, "file_path", ""), changed, changed_bases)
        f.unverified = not verified
        if not verified:
            flagged += 1

    # Severity that drives the gate considers VERIFIED findings only.
    sev = RiskLevel.LOW
    for f in sec.findings:
        if not getattr(f, "unverified", False) and _SEV_ORDER.get(f.severity, 0) > _SEV_ORDER.get(sev, 0):
            sev = f.severity
    sec.overall_severity = sev

    if flagged:
        note = (f"{flagged} finding(s) cite files not in this diff — kept and shown, "
                "but marked 'location unverified' and excluded from the gate.")
        report.suppressed_notes = (report.suppressed_notes or []) + [note]
        log.info("[%s] %s", report.request_id, note)
    return flagged
