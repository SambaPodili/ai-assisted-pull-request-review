"""
output/pr_commenter.py
-----------------------
Posts analysis results as formatted comments on Bitbucket / GitHub pull requests.
Idempotent: finds and updates an existing bot comment rather than creating duplicates.
"""
from __future__ import annotations
import logging
import requests
from core.models import AnalysisReport, GateDecision, RiskLevel

log = logging.getLogger(__name__)

_BOT_TAG = "<!-- impact-analyzer-bot -->"


class PRCommenter:

    def __init__(self, token: str, provider: str, workspace: str = "", api_url: str = "") -> None:
        self._token     = token
        self._provider  = provider
        self._workspace = workspace
        self._api_url   = api_url
        self._session   = requests.Session()
        self._session.timeout = 15

    def post(self, report: AnalysisReport) -> bool:
        """Post or update the bot comment. Returns True on success."""
        pr_id = report.metadata.get("pr_id") if hasattr(report, "metadata") else None
        if not pr_id:
            pr_id = report.request_id   # fallback for direct requests

        repo_slug = _extract_repo_slug(report.repo_url, self._provider, self._workspace)
        body      = self._render(report)

        try:
            if self._provider == "bitbucket":
                return self._bb_post(repo_slug, pr_id, body)
            return self._gh_post(repo_slug, pr_id, body)
        except Exception as exc:
            log.error("Failed to post PR comment: %s", exc)
            return False

    # ── Bitbucket ─────────────────────────────────────────────────────────────

    def _bb_post(self, repo_slug: str, pr_id: int | str, body: str) -> bool:
        url  = f"{self._api_url}/repositories/{self._workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        resp = self._session.post(url, json={"content": {"raw": body}}, headers=headers)
        resp.raise_for_status()
        return True

    # ── GitHub ────────────────────────────────────────────────────────────────

    def _gh_post(self, repo_slug: str, pr_id: int | str, body: str) -> bool:
        url     = f"{self._api_url}/repos/{repo_slug}/issues/{pr_id}/comments"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept":        "application/vnd.github.v3+json",
        }

        # Find existing bot comment to update
        existing_id = self._gh_find_existing(repo_slug, pr_id, headers)
        if existing_id:
            patch_url = f"{self._api_url}/repos/{repo_slug}/issues/comments/{existing_id}"
            resp      = self._session.patch(patch_url, json={"body": body}, headers=headers)
        else:
            resp = self._session.post(url, json={"body": body}, headers=headers)

        resp.raise_for_status()
        return True

    def _gh_find_existing(self, repo_slug: str, pr_id: int | str, headers: dict) -> int | None:
        url  = f"{self._api_url}/repos/{repo_slug}/issues/{pr_id}/comments"
        resp = self._session.get(url, headers=headers, params={"per_page": 100})
        if not resp.ok:
            return None
        for comment in resp.json():
            if _BOT_TAG in comment.get("body", ""):
                return comment["id"]
        return None

    # ── Markdown renderer ─────────────────────────────────────────────────────

    def _render(self, report: AnalysisReport) -> str:
        gate  = report.gate_decision
        risk  = report.final_risk
        icon  = {"APPROVE": "✅", "HOLD": "⚠️", "BLOCK": "🚫"}.get(gate.value, "❓")
        color = {"APPROVE": "green", "HOLD": "orange", "BLOCK": "red"}.get(gate.value, "grey")

        sections = [
            f"{_BOT_TAG}",
            f"## {icon} Impact Analysis — **{gate.value}**",
            f"> Risk Level: **{risk.value.upper()}** | Tokens used: {report.total_tokens}",
            f"| Metric | Value |",
            f"|--------|-------|",
        ]

        if report.code_analysis:
            ca = report.code_analysis
            sections.append(f"| Change type | {ca.change_type} |")
            sections.append(f"| Complexity delta | {ca.complexity_delta:+d} |")
            sections.append(f"| Code findings | {len(ca.findings)} |")

        if report.security:
            sec = report.security
            sections.append(f"| Security severity | {sec.overall_severity.value} |")
            sections.append(f"| Secrets detected | {'🚨 YES' if sec.secrets_detected else 'No'} |")
            sections.append(f"| Security findings | {len(sec.findings)} |")

        if report.dependency:
            dep = report.dependency
            sections.append(f"| Blast radius | {dep.blast_radius_score}/100 |")
            sections.append(f"| Affected services | {len(dep.affected_services)} |")

        if report.interface:
            sections.append(f"| Breaking API changes | {len(report.interface.breaking_changes)} |")

        if report.test_coverage:
            tc = report.test_coverage
            sections.append(f"| Coverage delta | {tc.coverage_delta:+.1f}% |")
            sections.append(f"| Regression risk | {tc.regression_risk.value} |")

        if report.risk:
            sections.append(f"\n**Rationale:** {report.risk.rationale}")

        if report.remediation:
            sections.append("\n### Deployment Strategy")
            sections.append(f"`{report.remediation.deployment_strategy.value}`")
            if report.remediation.fix_suggestions:
                sections.append("\n### Top Fix Suggestions")
                for fix in report.remediation.fix_suggestions[:5]:
                    sections.append(f"- {fix}")

        sections.append(
            f"\n<sub>Analysis ID: `{report.request_id}` | "
            f"[View full report]({_report_link(report.request_id)})</sub>"
        )

        return "\n".join(sections)


def _extract_repo_slug(repo_url: str, provider: str, workspace: str) -> str:
    parts = repo_url.rstrip("/").split("/")
    if provider == "github":
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]   # Bitbucket uses workspace separately


def _report_link(request_id: str) -> str:
    return f"#"   # Phase 3: replace with actual dashboard URL


# ── Factory ────────────────────────────────────────────────────────────────────

def make_pr_commenter(settings=None) -> PRCommenter | None:
    from config.settings import get_settings
    cfg = settings or get_settings()
    if not cfg.post_pr_comments:
        return None
    if cfg.git_provider == "bitbucket":
        return PRCommenter(cfg.bitbucket_token, "bitbucket", cfg.bitbucket_workspace, cfg.bitbucket_api_url)
    return PRCommenter(cfg.github_token, "github", api_url=cfg.github_api_url)
