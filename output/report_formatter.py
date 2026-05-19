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
        if dep.cve_hits:
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
            f"- **Coverage delta:** {tc.coverage_delta:+.1f}%",
            f"- **Regression risk:** {tc.regression_risk.value}",
            f"- **Uncovered paths:** {len(tc.uncovered_paths)}",
        ]
        if tc.generated_stubs:
            lines.append("\n**Generated Test Stubs:**")
            for stub in tc.generated_stubs[:3]:
                lines.append(f"```java\n{stub}\n```")
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
        "## 📊 Token Usage",
        f"| Agent | Model | Tokens |",
        f"|-------|-------|--------|",
    ]
    for u in report.token_usage:
        lines.append(f"| {u.agent.value} | {u.model} | {u.tokens_used} |")
    lines.append(f"| **TOTAL** | — | **{report.total_tokens}** |")

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
        "metrics": {
            "security_findings":  len(report.security.findings)           if report.security   else 0,
            "secrets_detected":   report.security.secrets_detected        if report.security   else False,
            "blast_radius":       report.dependency.blast_radius_score    if report.dependency else 0,
            "breaking_changes":   len(report.interface.breaking_changes)  if report.interface  else 0,
            "coverage_delta":     report.test_coverage.coverage_delta     if report.test_coverage else 0.0,
            "taint_paths":        len(report.taint_analysis.taint_paths)  if report.taint_analysis else 0,
            "iac_findings":       len(report.iac_analysis.findings)       if report.iac_analysis   else 0,
            "entropy_findings":   len(report.secrets_entropy.findings)    if report.secrets_entropy else 0,
        },
    }
