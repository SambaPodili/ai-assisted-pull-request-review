"""
governance/correlation.py
--------------------------
Cross-agent finding correlation + ranking — the "what must I look at?" engine.

A large PR run produces findings from up to 21 agents, and the same real issue
often surfaces 2–3 times (security + taint + AST flagging the same line) while a
critical item drowns in low-severity noise. This module:

  1. Normalises every located finding from every agent into one shape
  2. DEDUPES — findings on the same file + nearby line merge into one issue
  3. CORROBORATES — agreement across agents raises confidence and rank
  4. PENALISES findings whose location couldn't be verified against the diff
  5. Returns a ranked Top-N list stored on report.top_issues

Deterministic, zero tokens. Runs in orchestrator._finalize AFTER the evidence
guard (so `unverified` flags are already set) and after suppression.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from core.models import AnalysisReport, CorrelatedIssue

log = logging.getLogger(__name__)

# Severity weight — the base of the ranking score
_SEV_W = {"critical": 100.0, "high": 70.0, "medium": 40.0, "low": 15.0}
_SEV_ORDER = ["low", "medium", "high", "critical"]

# Confidence multiplier. Deterministic (static-rule) findings are precise by
# construction; LLM findings are good but fuzzier; unverified locations are
# kept visible but should never outrank verified ones.
_CONF_W = {"high": 1.0, "medium": 0.85, "low": 0.5}

# Corroboration bonus per EXTRA agent agreeing on the same location (capped).
_CORROBORATION_BONUS = 18.0
_CORROBORATION_CAP   = 36.0

# Line proximity for "same issue": findings within this many lines merge.
_LINE_BUCKET = 3


def _sev(value: Any, default: str = "medium") -> str:
    s = str(getattr(value, "value", value) or default).lower()
    return s if s in _SEV_W else default


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip().lstrip("./").lower()


def _collect(report: AnalysisReport) -> list[dict]:
    """Normalise every located finding into {agent, file, line, severity,
    category, description, unverified, deterministic}."""
    rows: list[dict] = []

    def add(agent, file_path, line, severity, category, description,
            unverified=False, deterministic=False):
        if not description:
            return
        rows.append({
            "agent": agent, "file": _norm_path(file_path), "line": int(line or 0),
            "severity": _sev(severity), "category": str(category or "").strip(),
            "description": str(description).strip()[:240],
            "unverified": bool(unverified), "deterministic": bool(deterministic),
        })

    sec = report.security
    if sec:
        det = bool(getattr(sec, "fallback_used", False))
        for f in getattr(sec, "findings", None) or []:
            add("security", f.file_path, str(getattr(f, "line_range", "") or "0").split("-")[0].split(",")[0] or 0,
                f.severity, getattr(f, "cwe_id", ""), f.description,
                getattr(f, "unverified", False), det)

    se = report.secrets_entropy
    if se:
        for f in getattr(se, "findings", None) or []:
            add("secrets_entropy", f.file_path, f.line, f.severity, f.kind,
                f"Possible secret ({f.kind}) in variable '{getattr(f, 'variable', '') or '?'}'",
                deterministic=True)

    ta = report.taint_analysis
    if ta:
        for p in getattr(ta, "taint_paths", None) or []:
            sink = getattr(p, "sink", None)
            add("taint_analysis", getattr(sink, "file_path", ""), getattr(sink, "line", 0),
                getattr(p, "severity", "high"), getattr(p, "cwe", ""),
                getattr(p, "description", "") or f"Tainted data reaches {getattr(sink, 'sink', 'sink')}",
                deterministic=True)

    ca = report.code_analysis
    if ca:
        det = bool(getattr(ca, "fallback_used", False))
        for f in getattr(ca, "findings", None) or []:
            line = str(getattr(f, "line_range", "") or "0").split("-")[0].split(",")[0]
            add("code_analysis", f.file_path, line if line.isdigit() else 0,
                f.severity, f.category, f.description, getattr(f, "unverified", False), det)

    ast = report.ast_analysis
    if ast:
        for f in getattr(ast, "findings", None) or []:
            add("ast_analysis", f.file_path, f.line, f.severity, f.kind,
                f.description, getattr(f, "unverified", False), True)

    iac = report.iac_analysis
    if iac:
        for f in getattr(iac, "findings", None) or []:
            add("iac_analysis", f.file_path, f.line, f.severity, f.kind,
                f.description, getattr(f, "unverified", False), True)

    perf = report.performance_impact
    if perf:
        det = bool(getattr(perf, "fallback_used", False))
        for f in getattr(perf, "findings", None) or []:
            add("performance_impact", f.file_path, f.line, f.severity,
                getattr(f, "category", ""), f.description, getattr(f, "unverified", False), det)

    priv = report.data_privacy
    if priv:
        det = bool(getattr(priv, "fallback_used", False))
        for f in getattr(priv, "pii_findings", None) or []:
            add("data_privacy", f.file_path, f.line, getattr(f, "risk_level", "medium"),
                getattr(f, "pii_type", "pii"), f.description, getattr(f, "unverified", False), det)

    maint = report.maintainability
    if maint:
        for i in getattr(maint, "issues", None) or []:
            add("maintainability", i.file_path, i.line, i.severity, i.kind,
                i.description, getattr(i, "unverified", False), True)

    obs = report.observability
    if obs:
        for f in getattr(obs, "findings", None) or []:
            add("observability", f.file_path, f.line, f.severity, f.kind,
                f.description, getattr(f, "unverified", False), True)

    sc = report.schema_change
    if sc:
        for c in getattr(sc, "changes", None) or []:
            sev = _sev(getattr(c, "severity", "medium"))
            if sev in ("high", "critical") or not getattr(c, "reversible", True):
                add("schema_change", c.file_path, 0, sev, c.change_type,
                    getattr(c, "description", "") or f"{c.change_type} on {getattr(c, 'table_name', '')}",
                    deterministic=True)

    iface = report.interface
    if iface:
        for b in getattr(iface, "breaking_changes", None) or []:
            add("interface", getattr(b, "path", ""), 0, getattr(b, "severity", "high"),
                getattr(b, "break_type", "breaking"),
                f"Breaking {getattr(b, 'interface_type', 'API')} change: {getattr(b, 'path', '')}",
                deterministic=True)

    return rows


def correlate_findings(report: AnalysisReport, max_issues: int = 10) -> list[CorrelatedIssue]:
    """Dedupe + corroborate + rank all findings into a Top-N issue list."""
    rows = _collect(report)
    if not rows:
        return []

    # ── Group by (file, line bucket). Pathless findings group by description. ──
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["file"]:
            key = (r["file"], r["line"] // _LINE_BUCKET if r["line"] else -1)
        else:
            key = ("", r["description"][:60].lower())
        groups[key].append(r)

    issues: list[CorrelatedIssue] = []
    for key, members in groups.items():
        agents     = sorted({m["agent"] for m in members})
        severities = [m["severity"] for m in members]
        worst      = max(severities, key=lambda s: _SEV_ORDER.index(s))
        unverified = all(m["unverified"] for m in members)   # one verified source clears it
        any_det    = any(m["deterministic"] and not m["unverified"] for m in members)

        # Confidence: deterministic source OR ≥2 agents agree → high;
        # all sources unverified → low; otherwise medium.
        if unverified:
            confidence = "low"
        elif any_det or len(agents) >= 2:
            confidence = "high"
        else:
            confidence = "medium"

        score = _SEV_W[worst] * _CONF_W[confidence]
        score += min(_CORROBORATION_BONUS * (len(agents) - 1), _CORROBORATION_CAP)
        if unverified:
            score *= 0.6   # additional rank penalty on top of low confidence

        # Title: lead with the most severe member's description
        lead = max(members, key=lambda m: _SEV_ORDER.index(m["severity"]))
        cats = sorted({m["category"] for m in members if m["category"]})
        descs, seen_d = [], set()
        for m in sorted(members, key=lambda m: -_SEV_ORDER.index(m["severity"])):
            d = m["description"]
            if d.lower() not in seen_d:
                seen_d.add(d.lower())
                descs.append(d)

        issues.append(CorrelatedIssue(
            title=lead["description"],
            file_path=members[0]["file"],
            line=max((m["line"] for m in members), default=0),
            severity=worst,
            confidence=confidence,
            score=round(score, 1),
            agents=agents,
            categories=cats[:6],
            descriptions=descs[:4],
            unverified=unverified,
        ))

    issues.sort(key=lambda i: -i.score)
    merged_away = len(rows) - len(issues)
    if merged_away > 0:
        log.info("[%s] Correlation merged %d duplicate finding(s) across agents",
                 report.request_id, merged_away)
    return issues[:max_issues]


def top_issues_markdown(report: AnalysisReport, limit: int = 5) -> str:
    """Compact Markdown block of the ranked Top Issues, for PR comments/reports.

    Returns "" when there are no correlated issues (callers skip the section).
    """
    issues = (getattr(report, "top_issues", None) or [])[:limit]
    if not issues:
        return ""
    sev_icon = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🔵"}
    lines = ["### 🎯 Top issues to review", ""]
    for i, it in enumerate(issues, 1):
        loc = f" — `{it.file_path}{':' + str(it.line) if it.line else ''}`" if it.file_path else ""
        corroborated = f" *(✓ {len(it.agents)} agents agree)*" if len(it.agents) > 1 else ""
        unv = " ⚠️ *location unverified*" if it.unverified else ""
        lines.append(f"{i}. {sev_icon.get(it.severity, 'ℹ️')} **{it.severity.upper()}** — "
                     f"{it.title}{loc}{corroborated}{unv}")
    lines.append("")
    return "\n".join(lines)
