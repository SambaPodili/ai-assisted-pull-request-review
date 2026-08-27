"""
ingestion/git_client.py
------------------------
HTTP client for fetching diffs from Bitbucket Cloud/Server and GitHub / GHE.
Pure data fetching — no analysis logic.

Bitbucket Server/Data Center note: UNTESTED against a real Server instance
(no Bitbucket Server available to verify against in development — everything
else in this codebase was live-tested, this wasn't). The URL shapes below
mirror output/pr_commenter.py's already-established (and presumably
tested-in-production) `_bb_server_post`/`_normalise_api_url` conventions —
`projects/{key}/repos/{slug}/...`, `/rest/api/1.0` base — but the specific
diff/file-content endpoints here are new. If diffs come back empty or
malformed against a real Server instance, this is the first place to check.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
import requests

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 400_000   # ~100k tokens; hard cap to protect LLM context window


@dataclass
class GitClientConfig:
    provider:    str    # "bitbucket" (Cloud) | "bitbucket_server" | "github"
    base_url:    str
    token:       str
    workspace:   str = ""     # Bitbucket Cloud workspace slug, or Server project key fallback
    verify_ssl:  bool = True  # False for a corporate Bitbucket Server behind a self-signed/
                              # internal-CA cert — same GIT_SSL_NO_VERIFY setting already used
                              # for git-clone operations (ingestion/reference_finder.py),
                              # now also honoured here for REST API calls. INSECURE; opt in.


def _split_project_repo(repo_slug: str, workspace: str) -> tuple[str, str]:
    """Bitbucket Server addresses a repo as (project key, repo slug), not a
    single slug — accept "PROJ/repo" directly, or fall back to `workspace`
    as the project key when repo_slug is bare. Mirrors the same splitting
    output/pr_commenter.py::_bb_server_post already does."""
    if "/" in repo_slug:
        proj, repo = repo_slug.split("/", 1)
        return proj, repo
    return workspace, repo_slug


class GitClient:

    def __init__(self, config: GitClientConfig) -> None:
        self._cfg     = config
        self._session = requests.Session()
        self._session.headers.update(self._build_headers())
        self._session.timeout = 30
        self._session.verify  = config.verify_ssl

    # ── Public API ────────────────────────────────────────────────────────────

    def get_pr_diff(self, repo_slug: str, pr_id: int) -> str:
        if self._cfg.provider == "bitbucket":
            return self._fetch(
                f"{self._cfg.base_url}/repositories/{self._cfg.workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/diff"
            )
        if self._cfg.provider == "bitbucket_server":
            proj, repo = _split_project_repo(repo_slug, self._cfg.workspace)
            return self._fetch(
                f"{self._cfg.base_url}/projects/{proj}/repos/{repo}/pull-requests/{pr_id}/diff",
                headers={"Accept": "text/plain"},
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
        if self._cfg.provider == "bitbucket_server":
            proj, repo = _split_project_repo(repo_slug, self._cfg.workspace)
            return self._fetch(
                f"{self._cfg.base_url}/projects/{proj}/repos/{repo}/compare/diff",
                headers={"Accept": "text/plain"},
                params={"from": source, "to": target},
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
        if self._cfg.provider == "bitbucket_server":
            proj, repo = _split_project_repo(repo_slug, self._cfg.workspace)
            return self._fetch(
                f"{self._cfg.base_url}/projects/{proj}/repos/{repo}/commits/{sha}/diff",
                headers={"Accept": "text/plain"},
            )
        return self._gh_accept(
            f"{self._cfg.base_url}/repos/{repo_slug}/commits/{sha}",
            "application/vnd.github.v3.diff",
        )

    def get_file_content(self, repo_slug: str, ref: str, path: str) -> str | None:
        """Raw content of `path` at `ref`, or None if it doesn't exist there
        (a 404 is expected/normal — most repos won't have every optional
        config file). Used for reading trust-boundary config files (e.g.
        .gto.yaml) from a specific branch — see
        ingestion/path_review_config.py."""
        try:
            if self._cfg.provider == "bitbucket":
                resp = self._session.get(
                    f"{self._cfg.base_url}/repositories/{self._cfg.workspace}/{repo_slug}"
                    f"/src/{ref}/{path}"
                )
            elif self._cfg.provider == "bitbucket_server":
                proj, repo = _split_project_repo(repo_slug, self._cfg.workspace)
                resp = self._session.get(
                    f"{self._cfg.base_url}/projects/{proj}/repos/{repo}/raw/{path}",
                    params={"at": ref},
                )
            else:
                resp = self._session.get(
                    f"{self._cfg.base_url}/repos/{repo_slug}/contents/{path}",
                    params={"ref": ref},
                    headers={"Accept": "application/vnd.github.v3.raw"},
                )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            log.debug("get_file_content(%s@%s:%s) failed", repo_slug, ref, path, exc_info=True)
            return None

    def list_pr_files(self, repo_slug: str, pr_id: int) -> list[str]:
        """List file paths changed in a PR (without full diff content)."""
        if self._cfg.provider == "github":
            resp = self._session.get(
                f"{self._cfg.base_url}/repos/{repo_slug}/pulls/{pr_id}/files",
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return [f.get("filename", "") for f in resp.json()]
        # Bitbucket (Cloud or Server): parse the raw diff for +++ lines —
        # simpler and provider-agnostic vs. each REST API's own file-list shape.
        diff = self.get_pr_diff(repo_slug, pr_id)
        return [
            line[6:].strip()
            for line in diff.splitlines()
            if line.startswith("+++ b/")
        ]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch(self, url: str, headers: dict | None = None, params: dict | None = None) -> str:
        log.debug("Fetching diff: %s", url)
        resp = self._session.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.text[:MAX_DIFF_CHARS]

    def _gh_accept(self, url: str, accept: str) -> str:
        log.debug("Fetching GH diff: %s", url)
        resp = self._session.get(url, headers={"Accept": accept})
        resp.raise_for_status()
        return resp.text[:MAX_DIFF_CHARS]

    def _build_headers(self) -> dict[str, str]:
        if self._cfg.provider in ("bitbucket", "bitbucket_server"):
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

def _normalise_bitbucket_server_url(url: str) -> str:
    """`https://bitbucket.mycompany.com` → `.../rest/api/1.0` — same
    correction output/pr_commenter.py::_normalise_api_url already applies,
    duplicated here rather than imported to keep this module's only
    dependency on `requests` (no cross-import onto the PR-commenting layer)."""
    base = (url or "").rstrip("/")
    if base and "/rest/api/1.0" not in base:
        return base + "/rest/api/1.0"
    return base


def make_git_client(settings=None) -> GitClient:
    """Build GitClient from settings (or read from env via get_settings())."""
    from config.settings import get_settings
    cfg = settings or get_settings()

    verify_ssl = not bool(getattr(cfg, "git_ssl_no_verify", False))

    if cfg.git_provider == "bitbucket_server":
        return GitClient(GitClientConfig(
            provider="bitbucket_server",
            base_url=_normalise_bitbucket_server_url(cfg.bitbucket_api_url),
            token=cfg.bitbucket_token,
            workspace=cfg.bitbucket_workspace,   # used as the project key fallback
            verify_ssl=verify_ssl,
        ))
    if cfg.git_provider == "bitbucket":
        return GitClient(GitClientConfig(
            provider="bitbucket",
            base_url=cfg.bitbucket_api_url,
            token=cfg.bitbucket_token,
            workspace=cfg.bitbucket_workspace,
            verify_ssl=verify_ssl,
        ))
    return GitClient(GitClientConfig(
        provider="github",
        base_url=cfg.github_api_url,
        token=cfg.github_token,
        verify_ssl=verify_ssl,
    ))
