"""
governance/gate_policy.py
--------------------------
Deterministic gate enforcement.

The risk agent (an LLM) proposes a gate decision. That proposal is advisory —
this module enforces hard, code-defined rules on top of it so the final gate
is **trustworthy, consistent, and auditable**, never less restrictive than the
evidence demands.

Final gate = MOST RESTRICTIVE of (LLM proposal, policy rules).

Each rule that fires is recorded with a human-readable reason, so a reviewer
sees exactly *why* a PR is blocked or held — not just a number.

Rules are intentionally conservative and banking-aligned (MAS TRM, PCI-DSS).
Override individual thresholds via config/settings if needed.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from core.models import AnalysisReport, GateDecision, RiskLevel

# Ordering so we can take the "most restrictive"
_SEVERITY = {GateDecision.APPROVE: 0, GateDecision.HOLD: 1, GateDecision.BLOCK: 2}


@dataclass
class PolicyResult:
    gate:        GateDecision
    reasons:     list[str] = field(default_factory=list)   # why the policy gate is what it is
    overrode_llm: bool = False                              # did policy change the LLM's call?
    llm_gate:    GateDecision = GateDecision.APPROVE


def _is_high_or_critical(sev) -> bool:
    s = getattr(sev, "value", sev)
    return str(s).lower() in ("high", "critical")


def _is_critical(sev) -> bool:
    s = getattr(sev, "value", sev)
    return str(s).lower() == "critical"


def _has_content(f) -> bool:
    """A finding counts toward the gate only if it carries real content AND is
    not 'unverified' (cites a file not in the diff). Guards against phantom
    findings (severity set, no content) and unsubstantiated/hallucinated ones —
    both kept for display elsewhere, but neither may block a merge."""
    if getattr(f, "unverified", False):
        return False
    return bool(
        (getattr(f, "description", "") or "").strip()
        or (getattr(f, "cwe_id", "") or getattr(f, "cwe", "") or "").strip()
        or (getattr(f, "file_path", "") or getattr(f, "file", "") or "").strip()
    )


def evaluate_policy(report: AnalysisReport, settings=None) -> PolicyResult:
    """
    Apply deterministic gate rules to a completed report.

    Returns a PolicyResult whose .gate is the FINAL enforced gate
    (most-restrictive of the LLM proposal and the rules that fired).
    """
    from config.settings import get_settings
    cfg = settings or get_settings()

    cov_block_threshold = getattr(cfg, "gate_coverage_block_pct", -15.0)
    cov_hold_threshold  = getattr(cfg, "gate_coverage_hold_pct", -5.0)
    blast_block         = getattr(cfg, "gate_blast_radius_block", 70)

    block_reasons: list[str] = []
    hold_reasons:  list[str] = []

    # ── BLOCK rules (any one → BLOCK) ─────────────────────────────────────────
    sec = report.security
    if sec:
        if getattr(sec, "secrets_detected", False):
            block_reasons.append("Hardcoded secret/credential detected (PCI-DSS Req 8.2)")
        crit_sec = [f for f in (sec.findings or [])
                    if _is_critical(getattr(f, "severity", "")) and _has_content(f)]
        if crit_sec:
            block_reasons.append(
                f"{len(crit_sec)} critical security finding(s) "
                f"(e.g. {getattr(crit_sec[0],'cwe_id','') or 'injection/auth'})"
            )

    # Secrets-entropy agent is a second, independent secrets signal
    se = report.secrets_entropy
    if se and any(_is_critical(getattr(f, "severity", "")) for f in (getattr(se, "findings", []) or [])):
        block_reasons.append("High-entropy secret detected by entropy scanner")

    # Taint analysis: data-flow-proven injection / SSRF / path traversal
    ta = report.taint_analysis
    if ta:
        taint_kinds = []
        if getattr(ta, "has_injection", False):       taint_kinds.append("injection (SQLi/command/template)")
        if getattr(ta, "has_ssrf", False):            taint_kinds.append("SSRF")
        if getattr(ta, "has_path_traversal", False):  taint_kinds.append("path traversal")
        if taint_kinds:
            block_reasons.append(
                f"Tainted data flow to a dangerous sink: {', '.join(taint_kinds)} "
                f"({len(getattr(ta, 'taint_paths', []) or [])} path(s))"
            )

    # Infrastructure-as-Code misconfigurations (public buckets, wildcard IAM, etc.)
    iac = report.iac_analysis
    if iac:
        iac_findings = getattr(iac, "findings", []) or []
        crit_iac = [f for f in iac_findings if _is_critical(getattr(f, "severity", ""))]
        high_iac = [f for f in iac_findings if _is_high_or_critical(getattr(f, "severity", ""))]
        if crit_iac:
            block_reasons.append(
                f"{len(crit_iac)} critical infrastructure misconfiguration(s) "
                f"(e.g. {getattr(crit_iac[0], 'kind', '') or 'insecure resource'})"
            )
        elif high_iac:
            hold_reasons.append(f"{len(high_iac)} high-severity infrastructure misconfiguration(s)")

    # Schema: destructive + irreversible migration is a data-loss risk
    sc = report.schema_change
    if sc and getattr(sc, "has_destructive", False) and getattr(sc, "has_irreversible", False):
        block_reasons.append("Destructive AND irreversible database migration (data-loss risk)")

    # Dependency CVEs (known vulnerabilities shipped)
    dep = report.dependency
    if dep and getattr(dep, "cve_hits", None):
        block_reasons.append(f"{len(dep.cve_hits)} known CVE(s) in changed dependencies")

    # Licence: viral copyleft in proprietary code
    lic = report.license_compliance
    if lic and getattr(lic, "has_copyleft", False):
        block_reasons.append("Copyleft (GPL/AGPL) licence introduced — legal/IP exposure")

    # Very large blast radius with no mitigation
    if dep and getattr(dep, "blast_radius_score", 0) > blast_block:
        hold_reasons.append(
            f"Blast radius {dep.blast_radius_score}/100 exceeds {blast_block} — "
            "stage behind a feature flag / canary"
        )

    # ── HOLD rules (any one → at least HOLD) ──────────────────────────────────
    if sec:
        high_sec = [f for f in (sec.findings or [])
                    if _is_high_or_critical(getattr(f, "severity", "")) and _has_content(f)]
        if high_sec and not block_reasons:
            hold_reasons.append(f"{len(high_sec)} high-severity security finding(s)")

    iface = report.interface
    if iface and getattr(iface, "breaking_changes", None):
        hold_reasons.append(
            f"{len(iface.breaking_changes)} breaking API change(s) — confirm consumer migration"
        )

    tc = report.test_coverage
    if tc is not None:
        delta = float(getattr(tc, "coverage_delta", 0) or 0)
        if delta <= cov_block_threshold:
            block_reasons.append(f"Test coverage dropped {delta:.1f}% (≥ {abs(cov_block_threshold):.0f}% drop)")
        elif delta <= cov_hold_threshold:
            hold_reasons.append(f"Test coverage dropped {delta:.1f}%")

    if sc and getattr(sc, "changes", None) and not (sc.has_destructive and sc.has_irreversible):
        hold_reasons.append("Database schema migration present — verify rollback plan")

    dp = report.data_privacy
    if dp and getattr(dp, "unencrypted_pii_count", 0) > 0:
        hold_reasons.append(f"{dp.unencrypted_pii_count} unencrypted PII field(s) (GDPR/PDPA)")

    # ── Resolve policy gate ───────────────────────────────────────────────────
    if block_reasons:
        policy_gate = GateDecision.BLOCK
        reasons = block_reasons + hold_reasons
    elif hold_reasons:
        policy_gate = GateDecision.HOLD
        reasons = hold_reasons
    else:
        policy_gate = GateDecision.APPROVE
        reasons = []

    # LLM's proposed gate (advisory)
    llm_gate = report.risk.gate_decision if report.risk else GateDecision.HOLD

    # Final = most restrictive of the two
    final = policy_gate if _SEVERITY[policy_gate] >= _SEVERITY[llm_gate] else llm_gate
    overrode = _SEVERITY[final] > _SEVERITY[llm_gate]

    if overrode:
        reasons = reasons + [
            f"Policy raised gate from AI proposal ({llm_gate.value}) to {final.value} "
            f"based on the hard rules above."
        ]
    elif _SEVERITY[final] < _SEVERITY[llm_gate]:
        # never happens (final is max), but keep the explanation slot
        pass

    return PolicyResult(gate=final, reasons=reasons, overrode_llm=overrode, llm_gate=llm_gate)
