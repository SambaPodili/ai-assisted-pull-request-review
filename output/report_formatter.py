"""
output/report_formatter.py
---------------------------
Renders AnalysisReport into human-readable Markdown or structured JSON.
Used for: API responses, file export, email attachments, audit records.
"""
from __future__ import annotations
import json
from datetime import datetime
from core.models import AnalysisReport, GateDecision, RiskLevel

# Grouped by gate tier (see governance/gate_policy.py) so the report reads in
# the same priority order the gate acts on: unrated sorts with critical/high
# since the gate fail-safe-BLOCKs on it too, not with "nothing to worry about".
_CVE_SEV_ORDER = {"critical": 3, "high": 3, "": 3, "medium": 2, "low": 1}


def to_markdown(report: AnalysisReport) -> str:
    """Render a full Markdown report suitable for export or email."""
    lines = []

    gate  = report.gate_decision
    risk  = report.final_risk
    icon  = {"APPROVE": "✅", "HOLD": "⚠️", "BLOCK": "🚫"}.get(gate.value, "❓")

    lines += [
        f"# {icon} Impact Analysis Report — {gate.value}",
        f"",
        f"**Repository:**  {report.repo_url}",
        f"**Change:**      `{report.source_ref}` → `{report.target_ref}`",
        f"**Risk Level:**  {risk.value.upper()}",
        f"**Phase Run:**   {report.phase_run}",
        f"**Completed:**   {report.completed_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Request ID:**  `{report.request_id}`",
        f"**Tokens Used:** {report.total_tokens}",
        f"",
    ]

    # Ranked, cross-agent-deduplicated Top Issues lead the report.
    try:
        from governance.correlation import top_issues_markdown
        top_md = top_issues_markdown(report)
        if top_md:
            lines += ["---", top_md]
    except Exception:
        pass

    # ── Code Analysis ──────────────────────────────────────────────────────────
    if report.code_analysis:
        ca = report.code_analysis
        lines += [
            "---",
            "## 📝 Code Analysis",
            f"- **Summary:** {ca.summary}",
            f"- **Change type:** {ca.change_type}",
            f"- **Complexity delta:** {ca.complexity_delta:+d}",
            f"- **Findings:** {len(ca.findings)}",
        ]
        if ca.findings:
            lines.append("")
            lines.append("| File | Lines | Severity | Category | Description |")
            lines.append("|------|-------|----------|----------|-------------|")
            for f in ca.findings[:10]:
                lines.append(f"| `{f.file_path}` | {f.line_range} | {f.severity.value} | {f.category} | {f.description} |")
        lines.append("")

    # ── Security ───────────────────────────────────────────────────────────────
    if report.security:
        sec = report.security
        secrets_flag = "🚨 **DETECTED**" if sec.secrets_detected else "None detected"
        lines += [
            "---",
            "## 🔒 Security Review",
            f"- **Overall severity:** {sec.overall_severity.value.upper()}",
            f"- **Secrets detected:** {secrets_flag}",
            f"- **Findings:** {len(sec.findings)}",
        ]
        if sec.compliance_flags:
            lines.append("\n**Compliance Flags:**")
            for flag in sec.compliance_flags:
                lines.append(f"- ⚠️ {flag}")
        if sec.findings:
            lines.append("\n| File | Lines | Severity | CWE | Description |")
            lines.append("|------|-------|----------|-----|-------------|")
            for f in sec.findings[:10]:
                lines.append(f"| `{f.file_path}` | {f.line_range} | **{f.severity.value}** | {f.cwe_id} | {f.description} |")
        lines.append("")

    # ── Dependency ─────────────────────────────────────────────────────────────
    if report.dependency:
        dep = report.dependency
        lines += [
            "---",
            "## 🔗 Dependency Impact",
            f"- **Blast radius score:** {dep.blast_radius_score}/100",
            f"- **Affected services:** {len(dep.affected_services)}",
            f"- **Changed packages:** {', '.join(dep.changed_packages) or 'None'}",
        ]
        if dep.affected_services:
            lines.append(f"- **Services:** {', '.join(dep.affected_services[:10])}")
        if dep.cve_findings:
            sev_icon = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🔵"}
            lines.append("- **⚠️ CVEs:**")
            for c in sorted(dep.cve_findings, key=lambda c: _CVE_SEV_ORDER.get((c.severity or "").lower(), 0), reverse=True):
                icon = sev_icon.get((c.severity or "").lower(), "❓")
                fix  = f" — fix: bump to `{c.fixed_version}`" if c.fixed_version else " — no published fix"
                lines.append(f"  - {icon} **{c.cve_id}** ({c.severity or 'severity unknown'}) in `{c.package}`{fix}")
        elif dep.cve_hits:
            lines.append(f"- **⚠️ CVEs:** {', '.join(dep.cve_hits)}")
        lines.append("")

    # ── Interface ──────────────────────────────────────────────────────────────
    if report.interface:
        iface = report.interface
        lines += [
            "---",
            "## 🔌 Interface / API Contracts",
            f"- **Breaking changes:** {len(iface.breaking_changes)}",
            f"- **Affected consumers:** {len(iface.affected_consumers)}",
        ]
        if iface.breaking_changes:
            lines.append("\n| Interface | Path | Break Type | Severity |")
            lines.append("|-----------|------|------------|----------|")
            for b in iface.breaking_changes[:10]:
                consumers = ", ".join(b.consumers) if b.consumers else "unknown"
                lines.append(f"| {b.interface_type} | `{b.path}` | {b.break_type} | {b.severity.value} |")
        lines.append("")

    # ── Test Coverage ──────────────────────────────────────────────────────────
    if report.test_coverage:
        tc = report.test_coverage
        lines += [
            "---",
            "## 🧪 Test Coverage",
            f"- **Test gaps (changed files without tests):** {len(tc.uncovered_paths)}",
            f"- **Regression risk:** {tc.regression_risk.value}",
        ]
        if tc.generated_stubs:
            lines.append("\n**Generated Test Stubs:**")
            for stub in tc.generated_stubs[:3]:
                lines.append(f"```java\n{stub}\n```")
        lines.append("")

    # ── Schema Changes ─────────────────────────────────────────────────────────
    if report.schema_change and report.schema_change.changes:
        sc = report.schema_change
        icon_sc = "🚫" if sc.has_destructive else ("⚠️" if sc.changes else "ℹ️")
        lines += [
            "---",
            "## 🗄️ Schema Changes",
            f"- **Gate contribution:** {sc.gate_contribution}",
            f"- **Rollback risk:** {sc.rollback_risk.value}",
            f"- **Destructive:** {'Yes 🚨' if sc.has_destructive else 'No'}",
            f"- **Irreversible:** {'Yes 🚨' if sc.has_irreversible else 'No'}",
            f"- **Migration files:** {', '.join(sc.migration_files) or 'None'}",
            f"- **Summary:** {sc.summary}",
        ]
        if sc.changes:
            lines.append("\n| File | Table | Change Type | Severity | Reversible | Description |")
            lines.append("|------|-------|-------------|----------|------------|-------------|")
            for c in sc.changes[:15]:
                rev = "✅" if c.reversible else "❌"
                lines.append(f"| `{c.file_path}` | {c.table_name or '—'} | {c.change_type} | {c.severity.value} | {rev} | {c.description} |")
        lines.append("")

    # ── Risk ───────────────────────────────────────────────────────────────────
    if report.risk:
        r = report.risk
        lines += [
            "---",
            "## ⚖️ Risk Assessment",
            f"- **Gate decision:** **{r.gate_decision.value}**",
            f"- **Risk score:** {r.risk_score}/100",
            f"- **Rollback feasibility:** {r.rollback_feasibility}",
            f"- **Deployment guidance:** {r.deployment_guidance}",
            f"- **Rationale:** {r.rationale}",
            "",
        ]

    # ── Remediation ────────────────────────────────────────────────────────────
    if report.remediation:
        rem = report.remediation
        lines += [
            "---",
            "## 🔧 Remediation",
            f"**Deployment Strategy:** `{rem.deployment_strategy.value}`",
            "",
            "**Fix Suggestions:**",
        ]
        for i, fix in enumerate(rem.fix_suggestions, 1):
            lines.append(f"{i}. {fix}")
        lines += ["", "**Validation Checklist:**"]
        for i, item in enumerate(rem.validation_checklist, 1):
            lines.append(f"- [ ] {i}. {item}")
        lines += [
            "",
            "**Executive Summary:**",
            f"> {rem.executive_summary}",
            "",
        ]

    # ── Token usage ────────────────────────────────────────────────────────────
    lines += [
        "---",
        "## 📊 Token & Timing",
        f"| Agent | Tokens | Time (s) | Model |",
        f"|-------|--------|----------|-------|",
    ]
    for u in report.token_usage:
        lines.append(f"| {u.agent.value} | {u.tokens_used} | {u.duration_s:.2f} | {u.model} |")
    lines.append(f"| **TOTAL** | **{report.total_tokens}** | **{report.duration_s:.2f}** | — |")

    return "\n".join(lines)


def to_summary_json(report: AnalysisReport) -> dict:
    """Compact JSON summary for API responses and dashboards."""
    return {
        "request_id":    report.request_id,
        "gate":          report.gate_decision.value,
        "risk":          report.final_risk.value,
        "risk_score":    report.risk.risk_score if report.risk else 0,
        "phase":         report.phase_run,
        "repo":          report.repo_url,
        "source_ref":    report.source_ref,
        "target_ref":    report.target_ref,
        "comparison":    f"{report.source_ref} → {report.target_ref}",
        "total_tokens":  report.total_tokens,
        "duration_s":    report.duration_s,
        "completed_at":  report.completed_at.isoformat(),
        "errors":        report.errors,
        "files_changed": report.files_changed,
        "agent_run_summary": report.agent_run_summary or report.compute_agent_run_summary(),
        "metrics": {
            "security_findings":  len(report.security.findings)           if report.security   else 0,
            "secrets_detected":   report.security.secrets_detected        if report.security   else False,
            "blast_radius":       report.dependency.blast_radius_score    if report.dependency else 0,
            "breaking_changes":   len(report.interface.breaking_changes)  if report.interface  else 0,
            "coverage_delta":     report.test_coverage.coverage_delta     if report.test_coverage else 0.0,
            "taint_paths":        len(report.taint_analysis.taint_paths)  if report.taint_analysis else 0,
            "iac_findings":       len(report.iac_analysis.findings)       if report.iac_analysis   else 0,
            "entropy_findings":   len(report.secrets_entropy.findings)    if report.secrets_entropy else 0,
            "schema_changes":     len(report.schema_change.changes)       if report.schema_change   else 0,
            "schema_destructive": report.schema_change.has_destructive    if report.schema_change   else False,
        },
        "agent_timings": [
            {"agent": u.agent.value, "tokens": u.tokens_used, "duration_s": u.duration_s, "model": u.model}
            for u in report.token_usage
        ],
    }


_SARIF_LEVEL = {
    "critical": "error",
    "high":     "error",
    "medium":   "warning",
    "low":      "note",
}


def to_sarif(report: AnalysisReport) -> dict:
    """Render `report.top_issues` as a SARIF 2.1.0 log (dict, JSON-serializable).

    Rule ids are synthesized per-report from each issue's category/CWE label —
    no static rule catalog exists in this codebase, so the driver's rule set is
    built dynamically from whatever categories actually appear. `line <= 0` is
    a legitimate value (an LLM finding with no resolvable line_range,
    governance/correlation.py) — SARIF permits a location with no `region`, so
    that's what's emitted rather than a fabricated startLine.
    """
    rules: dict[str, dict] = {}
    results = []

    for issue in report.top_issues:
        rule_id = issue.categories[0] if issue.categories else "gto-uncategorized"
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": rule_id,
                "shortDescription": {"text": issue.title},
            }

        text = issue.title
        if issue.descriptions:
            text = f"{text} — {issue.descriptions[0]}"
        if issue.unverified:
            text = f"{text} (unverified location)"

        artifact_location = {"uri": issue.file_path} if issue.file_path else {}
        physical_location: dict = {"artifactLocation": artifact_location}
        if issue.line > 0:
            physical_location["region"] = {"startLine": issue.line}

        results.append({
            "ruleId": rule_id,
            "level": _SARIF_LEVEL.get(issue.severity, "warning"),
            "message": {"text": text},
            "locations": [{"physicalLocation": physical_location}] if artifact_location else [],
        })

    return {
        "version": "2.1.0",
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "GTO",
                        "informationUri": "https://github.com/SambaPodili/code-impact-analysis-review",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def to_compliance_markdown(report: AnalysisReport) -> str:
    """Compliance/audit-facing report for a PR — distinct framing from
    `to_markdown` (which is developer-facing): gate rationale, human override
    history, full findings list, and what auto-suppression removed.

    v1 is Markdown only (no PDF library exists in this codebase) and has no
    true audit-log trail (`governance/audit_logger.py` is write-only, no
    read-by-request_id path exists) — override history instead comes from
    `governance.rbac.GateOverrideStore`, the same read-capable store `/overrides`
    already uses. Both limits are stated in the footer, not silently omitted.
    """
    from governance.rbac import get_gate_override_store

    gate = report.gate_decision
    icon = {"APPROVE": "✅", "HOLD": "⚠️", "BLOCK": "🚫"}.get(gate.value, "❓")

    lines = [
        f"# {icon} Compliance Report — {gate.value}",
        "",
        f"**Repository:**  {report.repo_url}",
        f"**Change:**      `{report.source_ref}` → `{report.target_ref}`",
        f"**Request ID:**  `{report.request_id}`",
        f"**Completed:**   {report.completed_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "---",
        "## Gate Decision",
        f"- **Final gate:** **{gate.value}**",
        f"- **AI-proposed gate:** {report.ai_proposed_gate or '—'}",
        f"- **Overridden by deterministic policy:** {'Yes' if report.gate_overridden_by_policy else 'No'}",
    ]
    if report.gate_policy_reasons:
        lines.append("- **Policy reasons:**")
        for reason in report.gate_policy_reasons:
            lines.append(f"  - {reason}")
    lines.append("")

    overrides = [o for o in get_gate_override_store().list_all() if o.request_id == report.request_id]
    lines += ["---", "## Human Override History"]
    if overrides:
        lines.append("")
        lines.append("| From | To | Reason | Overridden By | Team |")
        lines.append("|------|----|--------|--------------|----|")
        for o in overrides:
            lines.append(f"| {o.original_gate} | {o.override_to} | {o.reason} | {o.override_by} | {o.override_team} |")
    else:
        lines.append("_No human overrides recorded for this analysis._")
    lines.append("")

    lines += ["---", "## Findings"]
    if report.top_issues:
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        ranked = sorted(report.top_issues, key=lambda i: severity_order.get(i.severity, 4))
        lines.append("")
        lines.append("| Severity | File | Line | Title | Confidence | Agents |")
        lines.append("|----------|------|------|-------|------------|--------|")
        for issue in ranked:
            loc = f"{issue.line}" if issue.line > 0 else "—"
            lines.append(
                f"| {issue.severity.upper()} | `{issue.file_path}` | {loc} | {issue.title} | "
                f"{issue.confidence} | {', '.join(issue.agents)} |"
            )
    else:
        lines.append("_No findings._")
    lines.append("")

    lines += ["---", "## Finding Quality & Suppression Notes"]
    if report.suppressed_notes:
        for note in report.suppressed_notes:
            lines.append(f"- {note}")
    else:
        lines.append("_None for this analysis._")
    lines.append("")

    lines += [
        "---",
        "_Generated by GTO — Markdown export. PDF export and full audit-log-trail "
        "integration are planned upgrades, not included in this document._",
    ]

    return "\n".join(lines)
