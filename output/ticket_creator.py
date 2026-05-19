"""
output/ticket_creator.py
-------------------------
Automated Jira and ServiceNow ticket creation for affected teams.
Phase 4 — cross-service impact flow output.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass

import requests

from core.models import AnalysisReport, GateDecision, RiskLevel

log = logging.getLogger(__name__)
BLAST_RADIUS_TICKET_THRESHOLD = 30


@dataclass
class TicketResult:
    provider:  str
    ticket_id: str
    url:       str
    success:   bool
    error:     str = ""


class JiraTicketCreator:
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str) -> None:
        self._base    = base_url.rstrip("/")
        self._project = project_key
        self._session = requests.Session()
        self._session.auth    = (email, api_token)
        self._session.headers = {"Accept": "application/json", "Content-Type": "application/json"}
        self._session.timeout = 15

    def create_impact_ticket(self, report: AnalysisReport, affected_team: str = "") -> TicketResult:
        gate    = report.gate_decision
        risk    = report.final_risk
        summary = f"[Impact Analysis] {gate.value} — {report.source_ref} → {report.target_ref}"
        priority = {"BLOCK": "Highest", "HOLD": "High", "APPROVE": "Medium"}.get(gate.value, "Medium")
        labels   = ["impact-analysis", f"gate-{gate.value.lower()}", f"risk-{risk.value}"]
        if affected_team:
            labels.append(f"team-{affected_team.lower().replace(' ', '-')}")

        desc_text = "\n".join([
            f"Repository: {report.repo_url}",
            f"Change: {report.source_ref} → {report.target_ref}",
            f"Gate: {gate.value} | Risk: {risk.value} | Request ID: {report.request_id}",
            report.risk.rationale if report.risk else "",
        ])

        payload = {"fields": {
            "project":     {"key": self._project},
            "summary":     summary,
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": desc_text}]}
            ]},
            "issuetype": {"name": "Task"},
            "priority":  {"name": priority},
            "labels":    labels,
        }}

        try:
            resp = self._session.post(f"{self._base}/rest/api/3/issue", json=payload)
            resp.raise_for_status()
            key = resp.json().get("key", "")
            return TicketResult("jira", key, f"{self._base}/browse/{key}", True)
        except Exception as exc:
            log.error("[Jira] %s", exc)
            return TicketResult("jira", "", "", False, str(exc))


class ServiceNowTicketCreator:
    def __init__(self, instance: str, username: str, password: str) -> None:
        self._base    = f"https://{instance}.service-now.com/api/now/table"
        self._instance = instance
        self._session = requests.Session()
        self._session.auth    = (username, password)
        self._session.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        self._session.timeout = 15

    def create_change_request(self, report: AnalysisReport) -> TicketResult:
        if report.gate_decision == GateDecision.APPROVE:
            return TicketResult("servicenow", "", "", True)

        risk_map = {RiskLevel.LOW: "4", RiskLevel.MEDIUM: "3", RiskLevel.HIGH: "2", RiskLevel.CRITICAL: "1"}
        payload  = {
            "short_description": f"AI Impact Analysis: {report.gate_decision.value} — {report.source_ref}",
            "type":              "Normal",
            "risk":              risk_map.get(report.final_risk, "3"),
            "impact":            "2" if report.gate_decision == GateDecision.HOLD else "1",
            "u_analysis_id":     report.request_id,
            "u_gate_decision":   report.gate_decision.value,
        }
        try:
            resp   = self._session.post(f"{self._base}/change_request", json=payload)
            resp.raise_for_status()
            data   = resp.json().get("result", {})
            number = data.get("number", "")
            sys_id = data.get("sys_id", "")
            url    = f"https://{self._instance}.service-now.com/nav_to.do?uri=change_request.do?sys_id={sys_id}"
            return TicketResult("servicenow", number, url, True)
        except Exception as exc:
            log.error("[ServiceNow] %s", exc)
            return TicketResult("servicenow", "", "", False, str(exc))


def make_ticket_creators(settings=None) -> tuple[JiraTicketCreator | None, ServiceNowTicketCreator | None]:
    from config.settings import get_settings
    cfg  = settings or get_settings()
    jira = snow = None
    ju   = getattr(cfg, "jira_url",   "")
    jt   = getattr(cfg, "jira_token", "")
    jp   = getattr(cfg, "jira_project_key", "")
    je   = getattr(cfg, "jira_email",  "")
    if ju and jt:
        jira = JiraTicketCreator(ju, je, jt, jp)
    si   = getattr(cfg, "servicenow_instance", "")
    su   = getattr(cfg, "servicenow_user",     "")
    sp   = getattr(cfg, "servicenow_password",  "")
    if si and su:
        snow = ServiceNowTicketCreator(si, su, sp)
    return jira, snow


async def auto_create_tickets(report: AnalysisReport, settings=None) -> list[TicketResult]:
    results: list[TicketResult] = []
    jira, snow = make_ticket_creators(settings)
    dep = report.dependency
    if dep and dep.blast_radius_score >= BLAST_RADIUS_TICKET_THRESHOLD and jira:
        seen: set[str] = set()
        for node in dep.dependency_nodes[:5]:
            team = node.team or "Unknown"
            if team not in seen:
                seen.add(team)
                results.append(jira.create_impact_ticket(report, affected_team=team))
    if report.gate_decision in (GateDecision.HOLD, GateDecision.BLOCK) and snow:
        results.append(snow.create_change_request(report))
    return results
