"""
output/pr_commenter.py
-----------------------
Posts analysis results as formatted comments on Bitbucket / GitHub pull requests.
Idempotent: finds and updates an existing bot comment rather than creating duplicates.
"""
from __future__ import annotations
import logging
import re
import requests
from core.models import AnalysisReport, CodeFix, GateDecision, RiskLevel

log = logging.getLogger(__name__)

_BOT_TAG = "<!-- impact-analyzer-bot -->"

# Required GitHub token scopes for comment posting
_GH_REQUIRED_SCOPES = {"repo", "public_repo"}   # at least one of these must be present

# Minimum required scopes message shown to users
_GH_SCOPE_HELP = (
    "Token needs one of: 'repo' (private repos) or 'public_repo' (public repos). "
    "For fine-grained PATs: enable 'Pull requests: Read and write'. "
    "Update at: https://github.com/settings/tokens"
)


class InsufficientScopeError(Exception):
    """Raised when a GitHub token lacks the write scopes needed to post PR comments."""
    def __init__(self, found: set[str], required: set[str], hint: str = "") -> None:
        self.found    = found
        self.required = required
        self.hint     = hint or _GH_SCOPE_HELP
        super().__init__(
            f"Token missing required scope. Has: {found or {'(none)'}}, "
            f"needs one of: {required}. {self.hint}"
        )


def _get_github_scopes(token: str, api_base: str, session: requests.Session) -> set[str]:
    """
    Return the set of OAuth scopes granted to a GitHub token.

    GitHub returns the granted scopes in the 'X-OAuth-Scopes' response header
    on every API call.  Fine-grained PATs do NOT return this header — for those
    we return an empty set and let the caller decide whether to proceed.
    """
    try:
        resp = session.get(
            f"{api_base}/user",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        scope_header = resp.headers.get("X-OAuth-Scopes", "")
        scopes = {s.strip() for s in scope_header.split(",") if s.strip()}
        log.debug("GitHub token scopes: %s", scopes)
        return scopes
    except Exception as exc:
        log.debug("Could not fetch GitHub token scopes: %s", exc)
        return set()


class PRCommenter:

    def __init__(self, token: str, provider: str, workspace: str = "", api_url: str = "") -> None:
        self._token     = token
        self._provider  = provider
        self._workspace = workspace
        self._api_url   = _normalise_api_url(api_url, provider)
        self._session   = requests.Session()
        self._session.timeout = 15

    def post(
        self,
        report:    AnalysisReport,
        pr_id:     str | int | None = None,
        repo_slug: str = "",
    ) -> bool:
        """
        Post or update the bot comment. Returns True on success.

        pr_id and repo_slug can be supplied explicitly (from the API request body)
        so the method doesn't fall back to the analysis request_id when the report
        was submitted without webhook PR metadata.
        """
        # Prefer explicit pr_id, then report metadata, never fall back to request_id
        resolved_pr = (
            pr_id
            or (report.pr.pr_number if report.pr and report.pr.pr_number else None)
        )
        if not resolved_pr:
            log.warning("post_pr_comment: no PR id available — cannot post comment")
            return False

        resolved_slug = (
            repo_slug
            or _extract_repo_slug(report.repo_url, self._provider, self._workspace)
        )
        body = self._render(report)

        try:
            if self._provider in ("bitbucket", "bitbucket_cloud"):
                return self._bb_post(resolved_slug, resolved_pr, body)
            elif self._provider == "bitbucket_server":
                return self._bb_server_post(resolved_slug, resolved_pr, body)
            else:
                return self._gh_post(resolved_slug, resolved_pr, body)
        except Exception as exc:
            log.error("Failed to post PR comment: %s", exc)
            return False

    def post_text(self, body: str, pr_id: str | int, repo_slug: str = "") -> bool:
        """Post an arbitrary Markdown body as a single PR-level comment (used by
        the review workflow to post the reviewer-validated 'real issues')."""
        if not pr_id or not body:
            return False
        try:
            if self._provider in ("bitbucket", "bitbucket_cloud"):
                return self._bb_post(repo_slug, pr_id, body)
            elif self._provider == "bitbucket_server":
                return self._bb_server_post(repo_slug, pr_id, body)
            return self._gh_post(repo_slug, pr_id, body)
        except Exception as exc:
            log.error("post_text failed: %s", exc)
            return False

    def reply_to_comment(self, repo_slug: str, pr_id: str | int, body: str,
                          in_reply_to_id: str | int | None = None) -> str | None:
        """Post a reply in a PR comment thread (interactive chat replies —
        see governance/reply_answerer.py). Always creates a NEW comment,
        never patches an existing one (unlike post(), which maintains one
        rolling bot summary comment). Returns the new comment's id, or None
        on failure.

        `in_reply_to_id` threads the reply under a specific GitHub PR review
        comment (only meaningful for pull_request_review_comment-originated
        replies — GitHub's Issues API has no threading concept, so an
        issue_comment-originated reply is posted as a new top-level comment
        regardless of this argument)."""
        try:
            if self._provider in ("bitbucket", "bitbucket_cloud"):
                return self._bb_reply(repo_slug, pr_id, body, in_reply_to_id)
            elif self._provider == "bitbucket_server":
                log.warning("reply_to_comment: Bitbucket Server threaded replies not supported — posting top-level")
                ok = self._bb_server_post(repo_slug, pr_id, body)
                return "unknown" if ok else None
            return self._gh_reply(repo_slug, pr_id, body, in_reply_to_id)
        except Exception as exc:
            log.error("reply_to_comment failed: %s", exc)
            return None

    # ── Bitbucket Cloud ───────────────────────────────────────────────────────

    def _bb_post(self, repo_slug: str, pr_id: int | str, body: str) -> bool:
        url     = f"https://api.bitbucket.org/2.0/repositories/{self._workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        resp    = self._session.post(url, json={"content": {"raw": body}}, headers=headers)
        resp.raise_for_status()
        return True

    def _bb_reply(self, repo_slug: str, pr_id: int | str, body: str,
                   in_reply_to_id: str | int | None) -> str | None:
        url     = f"https://api.bitbucket.org/2.0/repositories/{self._workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        payload: dict = {"content": {"raw": body}}
        if in_reply_to_id is not None:
            payload["parent"] = {"id": in_reply_to_id}
        resp = self._session.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return str(resp.json().get("id")) if resp.content else None

    # ── Bitbucket Server ──────────────────────────────────────────────────────

    def _bb_server_post(self, repo_slug: str, pr_id: int | str, body: str) -> bool:
        proj, repo = (repo_slug.split("/", 1) + [""])[:2] if "/" in repo_slug else (self._workspace, repo_slug)
        url     = f"{self._api_url}/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/comments"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        resp    = self._session.post(url, json={"text": body}, headers=headers)
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

    def _gh_reply(self, repo_slug: str, pr_id: int | str, body: str,
                   in_reply_to_id: str | int | None) -> str | None:
        headers = {
            "Authorization": f"token {self._token}",
            "Accept":        "application/vnd.github.v3+json",
        }
        if in_reply_to_id is not None:
            # Threaded reply under a specific PR review comment — the only
            # GitHub endpoint that supports true comment threading.
            url  = f"{self._api_url}/repos/{repo_slug}/pulls/{pr_id}/comments"
            resp = self._session.post(url, json={"body": body, "in_reply_to": int(in_reply_to_id)}, headers=headers)
        else:
            # No threading concept for issue-level comments — post a new
            # top-level PR comment instead.
            url  = f"{self._api_url}/repos/{repo_slug}/issues/{pr_id}/comments"
            resp = self._session.post(url, json={"body": body}, headers=headers)
        resp.raise_for_status()
        return str(resp.json().get("id")) if resp.content else None

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
        ]

        # Lead with the reviewer triage (must-fix / needs-review / auto-approvable),
        # then the ranked Top Issues — the "where do I look" view before metrics.
        try:
            from governance.review_plan import review_plan_markdown
            rp_md = review_plan_markdown(report)
            if rp_md:
                sections.append(rp_md)
        except Exception:
            pass

        try:
            from governance.correlation import top_issues_markdown
            top_md = top_issues_markdown(report)
            if top_md:
                sections.append(top_md)
        except Exception:
            pass

        sections += [
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
            sections.append(f"| Test gaps (changed files w/o tests) | {len(tc.uncovered_paths)} |")
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


def _normalise_api_url(url: str, provider: str) -> str:
    """
    Return the correct REST API base URL for the given provider.

    Users often paste the web UI URL (https://github.com) instead of the
    API endpoint — we correct that silently so callers never see 404/422.
    """
    if provider in ("github",):
        # Strip any accidentally supplied web-UI URL
        if not url or url.rstrip("/") in ("https://github.com", "http://github.com", "github.com"):
            return "https://api.github.com"
        # GitHub Enterprise Server — append /api/v3 if missing
        if "github.com" not in url and "/api/v3" not in url:
            return url.rstrip("/") + "/api/v3"
        return url.rstrip("/")
    elif provider == "bitbucket_server":
        # Bitbucket Server REST API
        base = url.rstrip("/")
        if base and "/rest/api/1.0" not in base:
            return base + "/rest/api/1.0"
        return base
    elif provider in ("bitbucket", "bitbucket_cloud"):
        return "https://api.bitbucket.org/2.0"
    return url.rstrip("/")


def post_bb_server_decision(decision: str, token: str, base_url: str, repo_slug: str,
                            pr_id: str | int, workspace: str = "", reason: str = "",
                            report_id: str = "") -> dict:
    """Reflect a reviewer's gate decision on a Bitbucket Server PR WITHOUT merging
    or closing it — does TWO things:
      1. Participant REVIEW status  (APPROVE→APPROVED, HOLD/BLOCK→NEEDS_WORK)
      2. Commit BUILD-STATUS check  (APPROVE→SUCCESSFUL, HOLD→INPROGRESS, BLOCK→FAILED)
         on the PR's head commit — what branch merge-checks can require.
    Best-effort; returns {ok, review, status_check, errors}."""
    decision = (decision or "").upper()
    part  = {"APPROVE": "APPROVED", "HOLD": "NEEDS_WORK", "BLOCK": "NEEDS_WORK"}.get(decision)
    state = {"APPROVE": "SUCCESSFUL", "HOLD": "INPROGRESS", "BLOCK": "FAILED"}.get(decision)
    if not part:
        return {"ok": False, "review": None, "status_check": None,
                "errors": [f"unknown decision: {decision}"]}

    api  = _normalise_api_url(base_url, "bitbucket_server")          # …/rest/api/1.0
    root = api.split("/rest/api/1.0")[0].rstrip("/")                 # server root
    proj, repo = (repo_slug.split("/", 1) + [""])[:2] if "/" in repo_slug else (workspace, repo_slug)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    s = requests.Session()

    def _req(method, url, **kw):
        try:
            return s.request(method, url, headers=h, timeout=20, **kw)
        except requests.exceptions.SSLError:
            return s.request(method, url, headers=h, timeout=20, verify=False, **kw)
        except requests.exceptions.RequestException:
            return None

    out = {"ok": True, "review": None, "status_check": None, "errors": []}

    # Resolve the reviewer's slug (needed for the participant status) via X-AUSERNAME
    user = ""
    for path in ("/inbox/pull-requests/count", "/application-properties"):
        r = _req("GET", f"{api}{path}")
        if r is not None and r.headers.get("X-AUSERNAME"):
            from urllib.parse import unquote
            cand = unquote(r.headers["X-AUSERNAME"]).strip()
            if cand and cand.lower() != "anonymous":
                user = cand
                break

    # PR head commit (for the build status)
    commit = ""
    pr = _req("GET", f"{api}/projects/{proj}/repos/{repo}/pull-requests/{pr_id}")
    if pr is not None and pr.ok:
        try:
            commit = (pr.json().get("fromRef") or {}).get("latestCommit") or ""
        except Exception:
            commit = ""

    # 1. Review (participant status) — Approve / Needs-work
    if user:
        r = _req("PUT", f"{api}/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/participants/{user}",
                 json={"user": {"name": user}, "status": part})
        out["review"] = getattr(r, "status_code", None)
        if not (r is not None and r.ok):
            out["errors"].append(f"review {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")
    else:
        out["errors"].append("could not resolve reviewer slug (X-AUSERNAME) — review status skipped")

    # 2. Status check (build status on the PR head commit)
    if commit:
        body = {"state": state, "key": "GTO-REVIEW", "name": "GTO Pull Request Review Framework",
                "url": (_report_link(report_id) if report_id else (base_url or root)),
                "description": (reason or decision)[:255]}
        r = _req("POST", f"{root}/rest/build-status/1.0/commits/{commit}", json=body)
        out["status_check"] = getattr(r, "status_code", None)
        if not (r is not None and r.ok):
            out["errors"].append(f"status_check {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")
    else:
        out["errors"].append("could not resolve PR head commit — status check skipped")

    out["ok"] = not out["errors"]
    return out


def _decision_session(headers):
    """A requests.Session + a _req that falls back to verify=False on a self-signed
    cert and returns None on other network errors (best-effort PR actions)."""
    s = requests.Session()
    def _req(method, url, **kw):
        try:
            return s.request(method, url, headers=headers, timeout=20, **kw)
        except requests.exceptions.SSLError:
            return s.request(method, url, headers=headers, timeout=20, verify=False, **kw)
        except requests.exceptions.RequestException:
            return None
    return _req


def post_github_decision(decision: str, token: str, base_url: str, repo_slug: str,
                         pr_id: str | int, reason: str = "", report_id: str = "") -> dict:
    """Reflect a reviewer decision on a GitHub / GitHub Enterprise PR (no merge):
      • PR REVIEW  (APPROVE→APPROVE, HOLD→COMMENT, BLOCK→REQUEST_CHANGES)
      • commit STATUS (APPROVE→success, HOLD→pending, BLOCK→failure) on the head sha."""
    decision = (decision or "").upper()
    event = {"APPROVE": "APPROVE", "HOLD": "COMMENT", "BLOCK": "REQUEST_CHANGES"}.get(decision)
    state = {"APPROVE": "success", "HOLD": "pending", "BLOCK": "failure"}.get(decision)
    if not event:
        return {"ok": False, "review": None, "status_check": None, "errors": [f"unknown decision: {decision}"]}
    api = _normalise_api_url(base_url, "github")
    _req = _decision_session({"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"})
    out = {"ok": True, "review": None, "status_check": None, "errors": []}

    sha = ""
    pr = _req("GET", f"{api}/repos/{repo_slug}/pulls/{pr_id}")
    if pr is not None and pr.ok:
        try: sha = (pr.json().get("head") or {}).get("sha") or ""
        except Exception: sha = ""

    # Review — REQUEST_CHANGES/COMMENT require a body; APPROVE may include one.
    rbody = {"event": event, "body": (f"[{decision}] {reason}".strip()[:600] or decision)}
    r = _req("POST", f"{api}/repos/{repo_slug}/pulls/{pr_id}/reviews", json=rbody)
    out["review"] = getattr(r, "status_code", None)
    if not (r is not None and r.ok):
        out["errors"].append(f"review {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")

    if sha:
        body = {"state": state, "context": "GTO Pull Request Review Framework",
                "description": (reason or decision)[:140],
                "target_url": (_report_link(report_id) if report_id else (base_url or api))}
        r = _req("POST", f"{api}/repos/{repo_slug}/statuses/{sha}", json=body)
        out["status_check"] = getattr(r, "status_code", None)
        if not (r is not None and r.ok):
            out["errors"].append(f"status_check {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")
    else:
        out["errors"].append("could not resolve PR head sha — status check skipped")

    out["ok"] = not out["errors"]
    return out


def post_bb_cloud_decision(decision: str, token: str, repo_slug: str, pr_id: str | int,
                           workspace: str = "", reason: str = "", report_id: str = "") -> dict:
    """Reflect a reviewer decision on a Bitbucket Cloud PR (no merge/decline):
      • PR review  (APPROVE→approve, HOLD/BLOCK→request-changes)
      • commit build STATUS (APPROVE→SUCCESSFUL, HOLD→INPROGRESS, BLOCK→FAILED)."""
    decision = (decision or "").upper()
    review = {"APPROVE": "approve", "HOLD": "request-changes", "BLOCK": "request-changes"}.get(decision)
    state  = {"APPROVE": "SUCCESSFUL", "HOLD": "INPROGRESS", "BLOCK": "FAILED"}.get(decision)
    if not review:
        return {"ok": False, "review": None, "status_check": None, "errors": [f"unknown decision: {decision}"]}
    api = "https://api.bitbucket.org/2.0"
    base = f"{api}/repositories/{workspace}/{repo_slug}"
    _req = _decision_session({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    out = {"ok": True, "review": None, "status_check": None, "errors": []}

    sha = ""
    pr = _req("GET", f"{base}/pullrequests/{pr_id}")
    if pr is not None and pr.ok:
        try: sha = ((pr.json().get("source") or {}).get("commit") or {}).get("hash") or ""
        except Exception: sha = ""

    r = _req("POST", f"{base}/pullrequests/{pr_id}/{review}")
    out["review"] = getattr(r, "status_code", None)
    if not (r is not None and r.ok):
        out["errors"].append(f"review {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")

    if sha:
        body = {"key": "GTO-REVIEW", "state": state, "name": "GTO Pull Request Review Framework",
                "url": (_report_link(report_id) if report_id else api),
                "description": (reason or decision)[:200]}
        r = _req("POST", f"{base}/commit/{sha}/statuses/build", json=body)
        out["status_check"] = getattr(r, "status_code", None)
        if not (r is not None and r.ok):
            out["errors"].append(f"status_check {getattr(r, 'status_code', '?')}: {getattr(r, 'text', '')[:120]}")
    else:
        out["errors"].append("could not resolve PR head commit — status check skipped")

    out["ok"] = not out["errors"]
    return out


def post_pr_decision(decision: str, provider: str, token: str, base_url: str, repo_slug: str,
                     pr_id: str | int, workspace: str = "", reason: str = "", report_id: str = "") -> dict:
    """Dispatch a reviewer gate decision to the right provider's PR-action helper
    (review + status check, never merge/close). Supports Bitbucket Server/Cloud and
    GitHub Cloud/Enterprise."""
    p = (provider or "").lower()
    if p == "bitbucket_server":
        return post_bb_server_decision(decision, token, base_url, repo_slug, pr_id, workspace, reason, report_id)
    if p in ("github", "github_enterprise"):
        return post_github_decision(decision, token, base_url, repo_slug, pr_id, reason, report_id)
    if p in ("bitbucket", "bitbucket_cloud"):
        return post_bb_cloud_decision(decision, token, repo_slug, pr_id, workspace, reason, report_id)
    return {"ok": False, "review": None, "status_check": None, "errors": [f"unsupported provider: {provider}"]}


def _extract_repo_slug(repo_url: str, provider: str, workspace: str) -> str:
    parts = repo_url.rstrip("/").split("/")
    if provider in ("github", "github_enterprise"):
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]   # Bitbucket uses workspace separately


def _report_link(request_id: str) -> str:
    return f"#"   # Replace with actual dashboard URL if hosted


# ── Finding collection ────────────────────────────────────────────────────────

_FIX_LINE_RE = re.compile(r'@@ line (\d+) @@')


def _fix_diff_line(fix: CodeFix) -> int | None:
    """Line number a CodeFix targets, parsed from its `diff`'s `@@ line N @@`
    marker (CodeFix has no direct line field) — mirrors the identical parsing
    in agents/remediation_agent.py::_fix_line and
    vscode-extension/src/codeActions.ts::parseFixLine. Kept as a local copy
    rather than a cross-module import, matching how this parsing is already
    duplicated across those two independent modules."""
    m = _FIX_LINE_RE.search(fix.diff)
    return int(m.group(1)) if m else None


def _find_matching_fix(file_path: str, line: int, code_fixes: list[CodeFix]) -> CodeFix | None:
    """Deterministic (confidence="high") fixes only, exact file+line match —
    unlike the VS Code panel's forgiving same-file fallback, a suggestion fence
    here is one-click-apply on a real PR, so a fuzzy match is worse than no
    suggestion at all."""
    if not file_path or not line:
        return None
    for fix in code_fixes:
        if fix.confidence == "high" and fix.file_path == file_path and _fix_diff_line(fix) == line:
            return fix
    return None


def _collect_inline_findings(report: AnalysisReport) -> list[dict]:
    """
    Gather all findings that have a file path, normalised to:
      { file_path, line, severity, category, message, fix }

    line=0 means we know the file but not an exact line — we still include
    these so they appear as file-level comments rather than being dropped.
    `fix` is a matching deterministic CodeFix (or None) — see _find_matching_fix.
    """
    findings: list[dict] = []

    from config.settings import get_settings
    fences_enabled = get_settings().pr_suggestion_fences_enabled
    code_fixes = (report.remediation.code_fixes if report.remediation else []) if fences_enabled else []

    def _add(file_path: str, line: int, severity: str, category: str, message: str) -> None:
        if not file_path:
            return
        fp = file_path.strip()
        ln = max(0, int(line or 0))
        findings.append({
            "file_path": fp,
            "line":      ln,
            "severity":  severity.lower(),
            "category":  category,
            "message":   message,
            "fix":       _find_matching_fix(fp, ln, code_fixes),
        })

    # Security — SecurityFinding uses file_path, line_range (str), severity (enum),
    # cwe_id, description. No 'title'/'line_number' fields exist.
    def _first_line(line_range) -> int:
        import re as _re
        m = _re.search(r"\d+", str(line_range or ""))
        return int(m.group()) if m else 0

    for f in (report.security.findings if report.security else []):
        sev = getattr(f, "severity", "medium")
        sev = getattr(sev, "value", sev)   # RiskLevel → "high"
        cwe = getattr(f, "cwe_id", "")
        desc = getattr(f, "description", "") or "Security issue"
        msg  = f"{cwe + ': ' if cwe else ''}{desc}"
        _add(getattr(f, "file_path", ""),
             _first_line(getattr(f, "line_range", 0)),
             str(sev), "Security", msg)

    # Performance
    for f in (report.performance_impact.findings if report.performance_impact else []):
        _add(f.file_path, f.line, f.severity, "Performance", f.description)

    # Data privacy
    for f in (report.data_privacy.pii_findings if report.data_privacy else []):
        _add(f.file_path, f.line, f.risk_level, "Privacy",
             f"{f.pii_type.upper()} field — {f.description}")

    # Maintainability
    for f in (report.maintainability.issues if report.maintainability else []):
        _add(f.file_path, f.line, f.severity, "Quality", f.description)

    # Observability
    for f in (report.observability.findings if report.observability else []):
        _add(f.file_path, f.line, f.severity, "Observability", f.description)

    # Reference impact — annotate direct callers
    for ref in (report.reference_impact.references if report.reference_impact else [])[:15]:
        if ref.depth == 1:
            _add(ref.file_path, ref.line, "low", "Impact",
                 f"Calls `{ref.symbol}` — this site may be affected by the change")

    # Deduplicate by (file, line, category, first-60-chars of message)
    seen: set[tuple] = set()
    deduped = []
    for f in findings:
        key = (f["file_path"], f["line"], f["category"], f["message"][:60])
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    return deduped


def _group_by_file(findings: list[dict]) -> dict[str, list[dict]]:
    """
    Group findings by file_path, sorted by severity (critical first) within each group.
    """
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    groups: dict[str, list[dict]] = {}
    for f in findings:
        groups.setdefault(f["file_path"], []).append(f)
    for fp in groups:
        groups[fp].sort(key=lambda x: sev_order.get(x["severity"], 9))
    return groups


def _render_file_comment(file_path: str, findings: list[dict]) -> str:
    """
    Render all findings for a single file into one markdown comment block.
    This becomes the body of a single file-level review comment.
    """
    sev_icon = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🔵"}
    lines = [
        f"<!-- impact-analyzer-file -->\n### 🔍 CAR findings — `{file_path}`\n",
        "| Severity | Category | Line | Finding |",
        "|---|---|---|---|",
    ]
    for f in findings:
        icon    = sev_icon.get(f["severity"], "ℹ️")
        line_s  = str(f["line"]) if f["line"] else "—"
        msg     = f["message"].replace("|", "\\|")
        lines.append(f"| {icon} **{f['severity']}** | {f['category']} | {line_s} | {msg} |")

    rendered_lines: set[int] = set()
    for f in findings:
        fix: CodeFix | None = f.get("fix")
        # Multiple findings often land on the same line (e.g. security + privacy
        # + quality all flag the same hardcoded secret) — render each matched
        # fix once, not once per finding that happens to share its line.
        if fix is None or f["line"] in rendered_lines:
            continue
        rendered_lines.add(f["line"])
        lines.append(
            f"\n<details><summary>Suggested fix (line {f['line']})</summary>\n\n"
            f"```suggestion\n{fix.after}\n```\n"
            f"{fix.explanation}\n</details>"
        )

    lines.append(f"\n<sub>_CAR auto-review · {len(findings)} finding(s)_</sub>")
    return "\n".join(lines)


def _render_pr_summary_comment(report: AnalysisReport, file_groups: dict[str, list[dict]]) -> str:
    """
    Render the top-level PR comment: overall risk + gate + per-file finding count.
    This replaces the per-agent summary table with a developer-friendly overview.
    """
    gate      = report.gate_decision
    risk      = report.final_risk
    gate_icon = {"APPROVE": "✅", "HOLD": "⚠️", "BLOCK": "🚫"}.get(gate.value, "❓")
    total_findings = sum(len(v) for v in file_groups.values())

    lines = [
        f"{_BOT_TAG}",
        f"## {gate_icon} GTO Pull Request Review Framework — **{gate.value}**",
        f"",
        f"> **Risk Level:** `{risk.value.upper()}` &nbsp;·&nbsp; "
        f"**{total_findings}** finding(s) across **{len(file_groups)}** file(s)",
        f"",
    ]

    # Deployment strategy
    if report.remediation:
        strategy = getattr(report.remediation, "deployment_strategy", None)
        if strategy:
            lines += [f"**Deployment strategy:** `{strategy.value}`", ""]

    # Risk rationale
    if report.risk and report.risk.rationale:
        lines += [f"> {report.risk.rationale}", ""]

    # Ranked Top Issues lead the summary — the rest is supporting detail.
    try:
        from governance.correlation import top_issues_markdown
        top_md = top_issues_markdown(report)
        if top_md:
            lines += [top_md]
    except Exception:
        pass

    # Per-file summary table
    if file_groups:
        sev_icon = {"critical": "🚨", "high": "🔴", "medium": "🟡", "low": "🔵"}
        lines += [
            "### Files with findings",
            "",
            "| File | Findings | Top severity |",
            "|---|---|---|",
        ]
        for fp, flist in sorted(file_groups.items()):
            top_sev  = flist[0]["severity"]
            icon     = sev_icon.get(top_sev, "ℹ️")
            lines.append(f"| `{fp}` | {len(flist)} | {icon} {top_sev} |")
        lines.append("")

    # Key actions
    if report.remediation and getattr(report.remediation, "fix_suggestions", []):
        lines += ["### Top actions", ""]
        for fix in report.remediation.fix_suggestions[:5]:
            lines.append(f"- {fix}")
        lines.append("")

    # AI-generated sequence diagram — narrative, not call-graph-verified (see
    # core.models.MermaidDiagram). Collapsed by default since it's most
    # likely to be imperfect on exactly the complex PRs it's shown for.
    diagrams = getattr(report.remediation, "diagrams", []) if report.remediation else []
    if diagrams:
        d = diagrams[0]
        lines += [
            "<details><summary>📊 AI-generated sequence diagram (unverified)</summary>",
            "",
            "```mermaid",
            d.mermaid_source,
            "```",
            "",
            f"⚠️ {d.note}",
            "</details>",
            "",
        ]

    lines.append(
        f"<sub>Analysis ID: `{report.request_id}` &nbsp;·&nbsp; "
        f"[View full report in CAR dashboard]({_report_link(report.request_id)})</sub>"
    )
    return "\n".join(lines)


# ── Comment posting ───────────────────────────────────────────────────────────

def post_inline_comments(
    report:    AnalysisReport,
    token:     str,
    provider:  str,
    workspace: str = "",
    base_url:  str = "",
    repo_slug: str = "",
    pr_id:     str = "",
) -> int:
    """
    Post a grouped file-level comment for every file that has findings, plus
    one overall PR summary comment at the PR level.

    Strategy per provider
    ─────────────────────
    GitHub / GHE
      • One GitHub PR Review (POST /pulls/{pr}/reviews) containing:
          - body:     overall summary (replaces the PRCommenter summary comment)
          - comments: one entry per file, anchored to the first finding's line
        This is a single API call that posts everything atomically.
      • Falls back to individual issue-level comments if the Review API fails
        (e.g. no commit SHA available, or token lacks pull_requests write scope).

    Bitbucket Server
      1. POST top-level PR comment → overall summary
      2. POST one anchored comment per file → grouped findings table

    Bitbucket Cloud
      Same two-step pattern as Bitbucket Server.

    Returns total number of comments successfully posted.
    """
    all_findings = _collect_inline_findings(report)
    file_groups  = _group_by_file(all_findings)
    summary_body = _render_pr_summary_comment(report, file_groups)
    api_base     = _normalise_api_url(base_url, provider)

    if not file_groups and not summary_body:
        return 0

    session = requests.Session()
    session.timeout = 20
    posted = 0
    pid = str(pr_id).replace("#", "") or str(getattr(report.pr, "pr_number", ""))

    # Normalised to exactly what ReplyEvent.provider produces (github|bitbucket,
    # never the enterprise/cloud/server variants) so a later reply's lookup in
    # pr_comment_map (governance/review_session_store.py) actually matches.
    _provider_key = "github" if provider in ("github", "github_enterprise") else "bitbucket"

    def _record_posted(slug_: str, comment_id, fp: str = "", line=0) -> None:
        """Capture a just-posted comment's id so a later reply to it can be
        correlated back to this report/file — see governance/reply_answerer.py.
        Best-effort: a failure here must never break comment posting itself."""
        if not comment_id:
            return
        try:
            from governance.review_session_store import get_review_store
            get_review_store().record_posted_comment(
                provider=_provider_key, repo_slug=slug_, pr_id=pid,
                comment_id=str(comment_id), request_id=report.request_id,
                file_path=fp, line=str(line),
            )
        except Exception:
            log.debug("record_posted_comment failed", exc_info=True)

    # ── GitHub / GitHub Enterprise ────────────────────────────────────────────
    if provider in ("github", "github_enterprise"):
        headers = {
            "Authorization": f"token {token}",
            "Accept":        "application/vnd.github.v3+json",
        }
        slug = repo_slug or _extract_repo_slug(report.repo_url, provider, workspace)
        if not pid:
            log.warning("post_inline_comments: no PR id — skipping")
            return 0

        # ── Scope check — fail fast with a helpful message ────────────────
        # Classic PATs return X-OAuth-Scopes; fine-grained PATs return nothing
        # (empty set). We proceed for fine-grained PATs and let the API decide.
        scopes = _get_github_scopes(token, api_base, session)
        is_fine_grained = len(scopes) == 0   # no X-OAuth-Scopes header → fine-grained
        has_write_scope  = bool(scopes & _GH_REQUIRED_SCOPES)

        if scopes and not has_write_scope:
            # Classic PAT confirmed missing write scope — raise immediately
            raise InsufficientScopeError(
                found=scopes,
                required=_GH_REQUIRED_SCOPES,
                hint=_GH_SCOPE_HELP,
            )

        if is_fine_grained:
            log.info(
                "GitHub fine-grained PAT detected (no X-OAuth-Scopes header) — "
                "proceeding; if 403 occurs add 'Pull requests: Read and write' permission."
            )

        # Fetch PR to get the head commit SHA
        head_sha = ""
        try:
            pr_data  = session.get(f"{api_base}/repos/{slug}/pulls/{pid}", headers=headers)
            pr_data.raise_for_status()
            head_sha = pr_data.json().get("head", {}).get("sha", "")
        except Exception as exc:
            log.warning("Could not fetch PR head SHA: %s", exc)

        if head_sha and file_groups:
            # ── Strategy A: single Review call (preferred) ────────────────
            review_comments = []
            for fp, flist in file_groups.items():
                anchor_line = next((f["line"] for f in flist if f["line"] > 0), 1)
                review_comments.append({
                    "path":  fp,
                    "line":  anchor_line,
                    "side":  "RIGHT",
                    "body":  _render_file_comment(fp, flist),
                })

            review_payload = {
                "commit_id": head_sha,
                "body":      summary_body,
                "event":     "COMMENT",
                "comments":  review_comments,
            }
            try:
                resp = session.post(
                    f"{api_base}/repos/{slug}/pulls/{pid}/reviews",
                    headers=headers, json=review_payload,
                )
                if resp.status_code in (200, 201):
                    posted = len(file_groups) + 1
                    log.info(
                        "GitHub PR review posted: %d file comment(s) + summary (pid=%s)",
                        len(file_groups), pid,
                    )
                    # Correlate each posted review comment back to its file —
                    # the response's `comments` array mirrors review_payload's
                    # order, one id per file we submitted.
                    try:
                        posted_comments = resp.json().get("comments", [])
                        for (fp, flist), pc in zip(file_groups.items(), posted_comments):
                            anchor = next((f["line"] for f in flist if f["line"] > 0), 1)
                            _record_posted(slug, pc.get("id"), fp, anchor)
                    except Exception:
                        log.debug("Could not correlate GitHub review comments", exc_info=True)
                    return posted
                elif resp.status_code == 403:
                    err_msg = resp.json().get("message", resp.text[:200])
                    if is_fine_grained:
                        raise InsufficientScopeError(
                            found=set(),
                            required={"pull_requests: write"},
                            hint=(
                                f"Fine-grained PAT error: {err_msg}. "
                                "Go to GitHub → Settings → Developer settings → "
                                "Personal access tokens → Fine-grained tokens → "
                                "your token → Repository permissions → "
                                "Pull requests → set to 'Read and write'."
                            ),
                        )
                    raise InsufficientScopeError(
                        found=scopes,
                        required=_GH_REQUIRED_SCOPES,
                        hint=f"GitHub returned 403: {err_msg}. {_GH_SCOPE_HELP}",
                    )
                else:
                    log.warning(
                        "GitHub review API returned %d — falling back to issue comments: %s",
                        resp.status_code, resp.text[:300],
                    )
            except InsufficientScopeError:
                raise   # propagate to route
            except Exception as exc:
                log.warning("GitHub review API failed, falling back to issue comments: %s", exc)

        # ── Strategy B: issue-level comments (fallback when no commit SHA) ─
        # Summary first
        try:
            resp = session.post(
                f"{api_base}/repos/{slug}/issues/{pid}/comments",
                headers=headers, json={"body": summary_body},
            )
            if resp.status_code == 403:
                err_msg = resp.json().get("message", resp.text[:150])
                raise InsufficientScopeError(
                    found=scopes,
                    required=_GH_REQUIRED_SCOPES,
                    hint=f"GitHub returned 403 on issues comment: {err_msg}. {_GH_SCOPE_HELP}",
                )
            if resp.status_code in (200, 201):
                posted += 1
        except InsufficientScopeError:
            raise
        except Exception as exc:
            log.debug("Summary comment fallback failed: %s", exc)

        # One comment per file
        for fp, flist in file_groups.items():
            body = _render_file_comment(fp, flist)
            anchor = next((f["line"] for f in flist if f["line"] > 0), 1)
            try:
                if head_sha:
                    payload: dict = {
                        "body": body, "commit_id": head_sha,
                        "path": fp, "line": anchor, "side": "RIGHT",
                    }
                    resp = session.post(
                        f"{api_base}/repos/{slug}/pulls/{pid}/comments",
                        headers=headers, json=payload,
                    )
                else:
                    resp = session.post(
                        f"{api_base}/repos/{slug}/issues/{pid}/comments",
                        headers=headers, json={"body": body},
                    )
                if resp.status_code in (200, 201):
                    posted += 1
                    _record_posted(slug, resp.json().get("id"), fp, anchor)
                elif resp.status_code == 403:
                    log.warning("File comment 403 for %s — token may lack write scope", fp)
                else:
                    log.debug("File comment failed (%d) for %s: %s",
                              resp.status_code, fp, resp.text[:200])
            except Exception as exc:
                log.debug("File comment error for %s: %s", fp, exc)

    # ── Bitbucket Server ──────────────────────────────────────────────────────
    elif provider == "bitbucket_server":
        headers  = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        slug     = repo_slug or _extract_repo_slug(report.repo_url, provider, workspace)
        proj, repo = (slug.split("/", 1) + [""])[:2] if "/" in slug else (workspace, slug)
        if not pid:
            log.warning("post_inline_comments: no PR id for Bitbucket Server")
            return 0
        base_pr  = f"{api_base}/projects/{proj}/repos/{repo}/pull-requests/{pid}"

        # 1. Overall summary as top-level PR comment
        try:
            resp = session.post(f"{base_pr}/comments", headers=headers,
                                json={"text": summary_body})
            if resp.status_code in (200, 201):
                posted += 1
                log.info("BB Server: posted PR summary comment (pid=%s)", pid)
            else:
                log.warning("BB Server summary comment failed (%d): %s",
                            resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("BB Server summary comment error: %s", exc)

        # 2. One anchored comment per file
        for fp, flist in file_groups.items():
            anchor_line = next((f["line"] for f in flist if f["line"] > 0), 1)
            payload = {
                "text": _render_file_comment(fp, flist),
                "anchor": {
                    "line":     anchor_line,
                    "lineType": "ADDED",
                    "fileType": "TO",
                    "path":     fp,
                },
            }
            try:
                resp = session.post(f"{base_pr}/comments", headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    posted += 1
                    _record_posted(slug, resp.json().get("id"), fp, anchor_line)
                else:
                    log.debug("BB Server file comment failed (%d) for %s: %s",
                              resp.status_code, fp, resp.text[:200])
            except Exception as exc:
                log.debug("BB Server file comment error for %s: %s", fp, exc)

    # ── Bitbucket Cloud ───────────────────────────────────────────────────────
    elif provider in ("bitbucket", "bitbucket_cloud"):
        ws      = workspace
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        slug    = repo_slug or _extract_repo_slug(report.repo_url, provider, workspace)
        if not pid:
            log.warning("post_inline_comments: no PR id for Bitbucket Cloud")
            return 0
        base_pr = f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/pullrequests/{pid}"

        # 1. Overall summary as top-level PR comment
        try:
            resp = session.post(f"{base_pr}/comments", headers=headers,
                                json={"content": {"raw": summary_body}})
            if resp.status_code in (200, 201):
                posted += 1
                log.info("BB Cloud: posted PR summary comment (pid=%s)", pid)
            else:
                log.warning("BB Cloud summary comment failed (%d): %s",
                            resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("BB Cloud summary comment error: %s", exc)

        # 2. One anchored comment per file
        for fp, flist in file_groups.items():
            anchor_line = next((f["line"] for f in flist if f["line"] > 0), 1)
            payload = {
                "content": {"raw": _render_file_comment(fp, flist)},
                "inline":  {"to": anchor_line, "path": fp},
            }
            try:
                resp = session.post(f"{base_pr}/comments", headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    posted += 1
                    _record_posted(slug, resp.json().get("id"), fp, anchor_line)
                else:
                    log.debug("BB Cloud file comment failed (%d) for %s: %s",
                              resp.status_code, fp, resp.text[:200])
            except Exception as exc:
                log.debug("BB Cloud file comment error for %s: %s", fp, exc)

    log.info(
        "post_inline_comments: posted %d comment(s) covering %d file(s) + summary",
        posted, len(file_groups),
    )
    return posted


# ── Factory ────────────────────────────────────────────────────────────────────

def make_pr_commenter(settings=None) -> PRCommenter | None:
    from config.settings import get_settings
    cfg = settings or get_settings()
    if not cfg.post_pr_comments:
        return None
    if cfg.git_provider == "bitbucket":
        return PRCommenter(cfg.bitbucket_token, "bitbucket", cfg.bitbucket_workspace, cfg.bitbucket_api_url)
    return PRCommenter(cfg.github_token, "github", api_url=cfg.github_api_url)
