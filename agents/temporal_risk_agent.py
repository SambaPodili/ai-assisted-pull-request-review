"""
agents/temporal_risk_agent.py
------------------------------
Cross-PR temporal pattern detection.

Standard analysis looks at each PR in isolation. Temporal analysis looks at
the history of changes across multiple PRs to detect patterns that only
become visible over time.

Patterns detected:

  1. CHANGE FATIGUE — files changed too frequently
     A file changed 8 times in 30 days is unstable. Unstable code has 3–5x
     higher defect rates than stable code (from Microsoft Research data).
     Triggers HOLD with recommendation to address technical debt.

  2. SECURITY EROSION — security controls removed incrementally
     PR #1: Removes rate limiting from AuthService
     PR #2: Weakens password validation (2 weeks later)
     PR #3: Removes 2FA requirement (3 weeks later)
     Each individual change passes. Together: complete authentication bypass.

  3. RISK TREND — overall risk increasing over time
     If the past 4 weeks show avg_risk 30 → 45 → 58 → 72, something is
     systemically wrong. Triggers HOLD on current PR + team alert.

  4. INCIDENT CORRELATION — changed files previously caused incidents
     If payments/PaymentService.java was changed in the 48 hours before
     3 past production incidents, it deserves extra scrutiny.

  5. HIGH-FREQUENCY HOTSPOT — single file changed too many times
     Files changed 10+ times in 30 days are maintenance nightmares.
     They also concentrate risk: one bad change affects many features.

The temporal store (SQLite-backed) persists analysis history across restarts.
After each analysis completes, the orchestrator saves a change record.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    TemporalRiskResult, FileChangePattern, RiskLevel,
)
from agents.base_agent import BaseAgent
from storage.temporal_store import get_temporal_store, FileHistory


# Thresholds
CHANGE_FATIGUE_THRESHOLD     = 4    # changes in 30 days → change fatigue
HIGH_FREQUENCY_THRESHOLD     = 8    # changes in 30 days → hotspot
SECURITY_EROSION_LOOKBACK    = 14   # days to look back for security erosion
AVG_RISK_ESCALATION_DELTA    = 15   # risk score increase over trend → escalating


class TemporalRiskAgent(BaseAgent[TemporalRiskResult]):

    agent_name   = AgentName.RISK
    output_model = TemporalRiskResult

    system_prompt = (
        "You are a risk analyst specialising in temporal code change patterns for banking systems.\n"
        "Review the historical change patterns for this repository and identify:\n"
        "  1. Files with change fatigue (changed too often → high defect rate)\n"
        "  2. Security erosion patterns (security controls removed across multiple PRs)\n"
        "  3. Whether the overall risk trend is improving or degrading\n"
        "  4. Files correlated with previous incidents\n\n"
        "Output: escalating_pattern, security_erosion, change_fatigue (file list), risk_trend, hot_files.\n"
        "Output ONLY compact JSON."
    )

    def __init__(self, api_key: str | None = None, db_path: str = "data/temporal.db") -> None:
        super().__init__(api_key)
        self._store = get_temporal_store(db_path)

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> TemporalRiskResult:
        """Always use deterministic temporal analysis (no LLM needed for pattern matching)."""
        return self.fallback_result(request)

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        result = self.fallback_result(request)
        hot_files_str = "\n".join(
            f"  {fp.file_path}: changed {fp.change_count}x, avg_risk={fp.avg_risk_score}"
            for fp in result.hot_files
        )
        return (
            f"Repository: {request.repo_url}\n"
            f"Changed files in this PR: {request.changed_files}\n"
            f"Risk trend: {result.risk_trend}\n"
            f"Security erosion: {result.security_erosion}\n"
            f"Change fatigue files: {result.change_fatigue}\n"
            f"Hot files (30-day history):\n{hot_files_str or '  None'}\n"
        )

    def fallback_result(self, request: AnalysisRequest) -> TemporalRiskResult:
        """Deterministic temporal pattern detection using the historical store."""
        repo_url = request.repo_url

        # ── 1. Hot file detection ─────────────────────────────────────────────
        hot_files_raw = self._store.get_hot_files(
            repo_url=repo_url,
            days=30,
            min_changes=CHANGE_FATIGUE_THRESHOLD,
        )

        # Map to output model
        hot_files:     list[FileChangePattern] = []
        change_fatigue: list[str] = []
        incident_files = set(self._store.get_incident_correlated_files(repo_url))

        for h in hot_files_raw:
            incident_corr = any(fp in incident_files for fp in [h.file_path])
            hot_files.append(FileChangePattern(
                file_path=h.file_path,
                change_count=h.change_count,
                last_changed=h.last_changed,
                avg_risk_score=h.avg_risk_score,
                incident_correlated=incident_corr,
            ))
            if h.change_count >= CHANGE_FATIGUE_THRESHOLD:
                change_fatigue.append(h.file_path)

        # ── 2. Check current PR files against historical data ─────────────────
        for changed_file in request.changed_files:
            history = self._store.get_file_history(repo_url, changed_file, days=30)
            if history and history.change_count >= CHANGE_FATIGUE_THRESHOLD:
                if changed_file not in change_fatigue:
                    change_fatigue.append(changed_file)
                # Add to hot_files if not already there
                if not any(h.file_path == changed_file for h in hot_files):
                    hot_files.append(FileChangePattern(
                        file_path=changed_file,
                        change_count=history.change_count,
                        last_changed=history.last_changed,
                        avg_risk_score=history.avg_risk_score,
                        incident_correlated=changed_file in incident_files,
                    ))

        # ── 3. Risk trend analysis ────────────────────────────────────────────
        trend_data = self._store.get_risk_trend(repo_url, weeks=8)
        risk_trend = trend_data.trend

        # ── 4. Escalating pattern detection ──────────────────────────────────
        escalating = (risk_trend in ("degrading", "critical"))
        if trend_data.avg_scores and len(trend_data.avg_scores) >= 3:
            recent_avg = sum(trend_data.avg_scores[-2:]) / 2
            if recent_avg > 70:
                escalating = True

        # ── 5. Security erosion detection ─────────────────────────────────────
        security_erosion = self._detect_security_erosion(request, repo_url)

        return TemporalRiskResult(
            hot_files=hot_files,
            escalating_pattern=escalating,
            security_erosion=security_erosion,
            change_fatigue=change_fatigue,
            risk_trend=risk_trend,
            window_days=30,
        )

    def _detect_security_erosion(self, request: AnalysisRequest, repo_url: str) -> bool:
        """
        Detect incremental removal of security controls.
        Looks at recent history of security-sensitive files being changed
        with declining security severity (suggesting controls are being removed).
        """
        for file_path in request.changed_files:
            # Only check security-relevant files
            if not _is_security_sensitive(file_path):
                continue
            history = self._store.get_file_history(repo_url, file_path, days=SECURITY_EROSION_LOOKBACK)
            if not history:
                continue
            # If file was changed multiple times recently and had security findings decreasing
            # (which could mean they were "fixed" by removing the check rather than fixing it)
            if history.change_count >= 3 and history.had_secrets:
                return True
            # High-risk gate history followed by lower → security being removed
            if (history.gates.count("BLOCK") + history.gates.count("HOLD")) >= 2:
                return True
        return False

    def record_analysis(self, report) -> None:
        """
        Called by orchestrator after analysis completes to save the change record.
        Enables future temporal analysis.
        """
        from storage.temporal_store import FileChangeRecord
        now = datetime.utcnow().isoformat()
        risk_score = report.risk.risk_score if report.risk else 0
        gate       = report.gate_decision.value if hasattr(report.gate_decision, 'value') else str(report.gate_decision)
        sec_sev    = report.security.overall_severity.value if report.security else "low"
        has_secrets = report.security.secrets_detected if report.security else False

        for hunk in getattr(report, "_request_hunks", []):
            record = FileChangeRecord(
                repo_url=report.repo_url,
                file_path=hunk.file_path,
                request_id=report.request_id,
                risk_score=risk_score,
                gate_decision=gate,
                security_severity=sec_sev,
                has_secrets=has_secrets,
                changed_at=now,
            )
            self._store.record_change(record)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SECURITY_SENSITIVE_PATTERNS = (
    "auth", "security", "login", "password", "token", "credential",
    "crypto", "encryption", "key", "permission", "access", "role",
    "payment", "transaction", "card", "account", "audit",
)

def _is_security_sensitive(file_path: str) -> bool:
    lower = file_path.lower()
    return any(p in lower for p in _SECURITY_SENSITIVE_PATTERNS)
