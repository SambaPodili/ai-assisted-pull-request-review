"""
ingestion/git_client.py
------------------------
HTTP client for fetching diffs from Bitbucket Cloud/Server and GitHub / GHE.
Pure data fetching — no analysis logic.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
import requests

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 400_000   # ~100k tokens; hard cap to protect LLM context window


@dataclass
class GitClientConfig:
    provider:   str    # "bitbucket" | "github"
    base_url:   str
    token:      str
    workspace:  str = ""   # Bitbucket workspace slug


class GitClient:

    def __init__(self, config: GitClientConfig) -> None:
        self._cfg     = config
        self._session = requests.Session()
        self._session.headers.update(self._build_headers())
        self._session.timeout = 30

    # ── Public API ────────────────────────────────────────────────────────────

    def get_pr_diff(self, repo_slug: str, pr_id: int) -> str:
        if self._cfg.provider == "bitbucket":
            return self._fetch(
                f"{self._cfg.base_url}/repositories/{self._cfg.workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/diff"
            )
        return self._gh_accept(
            f"{self._cfg.base_url}/repos/{repo_slug}/pulls/{pr_id}",
            "application/vnd.github.v3.diff",
        )

    def get_branch_diff(self, repo_slug: str, source: str, target: str) -> str:
        if self._cfg.provider == "bitbucket":
            return self._fetch(
                f"{self._cfg.base_url}/repositories/{self._cfg.workspace}/{repo_slug}"
                f"/diff/{source}..{target}"
            )
        return self._gh_accept(
            f"{self._cfg.base_url}/repos/{repo_slug}/compare/{target}...{source}",
            "application/vnd.github.v3.diff",
        )

    def get_commit_diff(self, repo_slug: str, sha: str) -> str:
        if self._cfg.provider == "bitbucket":
            return self._fetch(
                f"{self._cfg.base_url}/repositories/{self._cfg.workspace}/{repo_slug}/diff/{sha}"
            )
        return self._gh_accept(
            f"{self._cfg.base_url}/repos/{repo_slug}/commits/{sha}",
            "application/vnd.github.v3.diff",
        )

    def list_pr_files(self, repo_slug: str, pr_id: int) -> list[str]:
        """List file paths changed in a PR (without full diff content)."""
        if self._cfg.provider == "github":
            resp = self._session.get(
                f"{self._cfg.base_url}/repos/{repo_slug}/pulls/{pr_id}/files",
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return [f.get("filename", "") for f in resp.json()]
        # Bitbucket: parse the diff for +++ lines
        diff = self.get_pr_diff(repo_slug, pr_id)
        return [
            line[6:].strip()
            for line in diff.splitlines()
            if line.startswith("+++ b/")
        ]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> str:
        log.debug("Fetching diff: %s", url)
        resp = self._session.get(url)
        resp.raise_for_status()
        return resp.text[:MAX_DIFF_CHARS]

    def _gh_accept(self, url: str, accept: str) -> str:
        log.debug("Fetching GH diff: %s", url)
        resp = self._session.get(url, headers={"Accept": accept})
        resp.raise_for_status()
        return resp.text[:MAX_DIFF_CHARS]

    def _build_headers(self) -> dict[str, str]:
        if self._cfg.provider == "bitbucket":
            return {
                "Authorization": f"Bearer {self._cfg.token}",
                "Accept":        "application/json",
            }
        return {
            "Authorization":        f"token {self._cfg.token}",
            "Accept":               "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


# ── Factory ────────────────────────────────────────────────────────────────────

def make_git_client(settings=None) -> GitClient:
    """Build GitClient from settings (or read from env via get_settings())."""
    from config.settings import get_settings
    cfg = settings or get_settings()

    if cfg.git_provider == "bitbucket":
        return GitClient(GitClientConfig(
            provider="bitbucket",
            base_url=cfg.bitbucket_api_url,
            token=cfg.bitbucket_token,
            workspace=cfg.bitbucket_workspace,
        ))
    return GitClient(GitClientConfig(
        provider="github",
        base_url=cfg.github_api_url,
        token=cfg.github_token,
    ))
