"""
output/notification.py
-----------------------
Sends alerts to Slack and/or Microsoft Teams for HOLD / BLOCK gate decisions.
Only fires when the gate is not APPROVE — avoids alert fatigue.
"""
from __future__ import annotations
import logging
import requests
from core.models import AnalysisReport, GateDecision

log = logging.getLogger(__name__)


class NotificationService:

    def __init__(
        self,
        slack_webhook:  str = "",
        teams_webhook:  str = "",
    ) -> None:
        self._slack  = slack_webhook
        self._teams  = teams_webhook
        self._session = requests.Session()
        self._session.timeout = 10

    def notify(self, report: AnalysisReport) -> None:
        """Send notifications for HOLD or BLOCK decisions. No-op for APPROVE."""
        if report.gate_decision == GateDecision.APPROVE:
            return

        if self._slack:
            self._send_slack(report)
        if self._teams:
            self._send_teams(report)

    # ── Slack ─────────────────────────────────────────────────────────────────

    def _send_slack(self, report: AnalysisReport) -> None:
        gate   = report.gate_decision
        icon   = "🚫" if gate == GateDecision.BLOCK else "⚠️"
        color  = "#FF0000" if gate == GateDecision.BLOCK else "#FF8C00"

        payload = {
            "attachments": [
                {
                    "color": color,
                    "pretext": f"{icon} *Impact Analysis: {gate.value}*",
                    "fields": [
                        {"title": "Repository", "value": report.repo_url,        "short": False},
                        {"title": "Change",     "value": f"`{report.source_ref}` → `{report.target_ref}`", "short": False},
                        {"title": "Risk",       "value": report.final_risk.value, "short": True},
                        {"title": "Tokens",     "value": str(report.total_tokens), "short": True},
                    ],
                    "footer": f"Request ID: {report.request_id}",
                }
            ]
        }

        if report.risk and report.risk.rationale:
            payload["attachments"][0]["fields"].append(
                {"title": "Rationale", "value": report.risk.rationale, "short": False}
            )

        try:
            resp = self._session.post(self._slack, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Slack notification failed: %s", exc)

    # ── Microsoft Teams ────────────────────────────────────────────────────────

    def _send_teams(self, report: AnalysisReport) -> None:
        gate  = report.gate_decision
        color = "FF0000" if gate == GateDecision.BLOCK else "FF8C00"

        payload = {
            "@type":      "MessageCard",
            "@context":   "http://schema.org/extensions",
            "themeColor": color,
            "summary":    f"Impact Analysis: {gate.value}",
            "sections": [
                {
                    "activityTitle":    f"Impact Analysis — {gate.value}",
                    "activitySubtitle": f"{report.source_ref} → {report.target_ref}",
                    "facts": [
                        {"name": "Repository", "value": report.repo_url},
                        {"name": "Risk Level",  "value": report.final_risk.value},
                        {"name": "Tokens Used", "value": str(report.total_tokens)},
                        {"name": "Request ID",  "value": report.request_id},
                    ],
                }
            ],
        }

        try:
            resp = self._session.post(self._teams, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            log.error("Teams notification failed: %s", exc)


# ── Factory ────────────────────────────────────────────────────────────────────

def make_notification_service(settings=None) -> NotificationService:
    from config.settings import get_settings
    cfg = settings or get_settings()
    return NotificationService(
        slack_webhook=cfg.slack_webhook_url,
        teams_webhook=cfg.teams_webhook_url,
    )
