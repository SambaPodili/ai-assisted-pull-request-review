"""
api/routes/git_proxy.py - Backend proxy for Git providers (fixes CORS for Bitbucket Server)
"""
from __future__ import annotations
import base64, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/git", tags=["git-proxy"])
TIMEOUT = 20

class GitConfig(BaseModel):
    provider:    str           # github | github_enterprise | bitbucket_cloud | bitbucket_server
    base_url:    str = ""
    auth_mode:   str = "token" # token | basic
    token:       str = ""
    username:    str = ""
    password:    str = ""
    workspace:   str = ""      # Bitbucket Cloud workspace slug
    project_key: str = ""      # Bitbucket Server: optional project key (e.g. "BANK")
                               # If blank, repos are fetched for ALL projects

def _headers(cfg: GitConfig) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.auth_mode == "token" and cfg.token:
        h["Authorization"] = f"Bearer {cfg.token}"
    elif cfg.username and cfg.password:
        h["Authorization"] = "Basic " + base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
    return h

def _github_base(cfg: GitConfig) -> str:
    return (cfg.base_url.rstrip("/") + "/api/v3") if cfg.provider == "github_enterprise" and cfg.base_url else "https://api.github.com"

def _bb_server_base(cfg: GitConfig) -> str:
    return cfg.base_url.rstrip("/") + "/rest/api/1.0"

def _get(url: str, headers: dict, params: dict | None = None) -> dict:
    try:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT, verify=True)
    except requests.exceptions.SSLError:
        r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT, verify=False)
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(502, f"Cannot reach Git provider: {e}")
    if r.status_code == 401: raise HTTPException(401, "Authentication failed — check token/credentials")
    if r.status_code == 403: raise HTTPException(403, "Insufficient permissions — check token scopes")
    if r.status_code == 404: raise HTTPException(404, "Resource not found")
    r.raise_for_status()
    return r.json() if r.content else {}

def _get_text(url: str, headers: dict) -> str:
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=True)
    except requests.exceptions.SSLError:
        r = requests.get(url, headers=headers, timeout=TIMEOUT, verify=False)
    if r.status_code in (401, 403): raise HTTPException(r.status_code, "Auth error fetching diff")
    r.raise_for_status()
    return r.text

@router.post("/verify")
async def verify_credentials(cfg: GitConfig):
    h = _headers(cfg)
    if cfg.provider in ("github", "github_enterprise"):
        d = _get(f"{_github_base(cfg)}/user", h)
        return {"ok": True, "login": d.get("login",""), "name": d.get("name",""), "avatar": d.get("avatar_url","")}
    elif cfg.provider == "bitbucket_server":
        base = _bb_server_base(cfg)
        login = cfg.username or ""
        name  = cfg.username or ""
        email = ""
        # Fetch the real user profile so we show the display name, not the slug.
        try:
            if cfg.username:
                d = _get(f"{base}/users/{cfg.username}", h)
                login = d.get("slug") or d.get("name") or login
                name  = d.get("displayName") or d.get("name") or login
                email = d.get("emailAddress", "")
        except Exception:
            pass
        return {"ok": True, "login": login, "name": name, "display_name": name,
                "email": email, "version": "Bitbucket Server"}
    else:
        d = _get("https://api.bitbucket.org/2.0/user", h)
        return {"ok": True, "login": d.get("username", d.get("nickname","")), "name": d.get("display_name","")}

# ── Bitbucket Server helpers ─────────────────────────────────────────────────

def _bb_list_all_projects(base: str, h: dict) -> list[dict]:
    """Paginate through all projects on a Bitbucket Server instance."""
    projects = []
    start = 0
    while True:
        d = _get(f"{base}/projects", h, {"limit": 100, "start": start})
        for p in d.get("values", []):
            projects.append({
                "key":         p.get("key", ""),
                "name":        p.get("name", ""),
                "description": p.get("description", ""),
                "type":        p.get("type", "NORMAL"),
            })
        if d.get("isLastPage", True):
            break
        start = d.get("nextPageStart", start + 100)
    return projects


def _bb_repos_for_project(base: str, h: dict, project_key: str, limit: int = 300) -> list[dict]:
    """Paginate through all repos in one Bitbucket Server project."""
    repos = []
    start = 0
    while len(repos) < limit:
        d = _get(
            f"{base}/projects/{project_key}/repos",
            h,
            {"limit": 100, "start": start},
        )
        for r in d.get("values", []):
            repos.append(_normalise_bb_server_repo(r, project_key))
        if d.get("isLastPage", True):
            break
        start = d.get("nextPageStart", start + 100)
    return repos


def _normalise_bb_server_repo(r: dict, project_key: str = "") -> dict:
    proj = r.get("project", {})
    key  = project_key or proj.get("key", "")
    slug = r.get("slug", "")
    return {
        "full_name":   f"{key}/{slug}",
        "name":        r.get("name", slug),
        "slug":        slug,
        "project":     key,
        "description": r.get("description", ""),
        "language":    None,
        # Clone URLs — handy for auto-clone reference search
        "clone_urls": {
            lnk.get("name"): lnk.get("href")
            for lnk in r.get("links", {}).get("clone", [])
        },
    }


# ── List projects (Bitbucket Server only) ─────────────────────────────────────

@router.post("/projects")
async def list_projects(cfg: GitConfig):
    """
    Return all projects visible to the authenticated user on Bitbucket Server.
    For other providers returns an empty list.
    """
    if cfg.provider != "bitbucket_server":
        return {"projects": [], "count": 0}
    h = _headers(cfg)
    projects = _bb_list_all_projects(_bb_server_base(cfg), h)
    return {"projects": projects, "count": len(projects)}


# ── List repos ────────────────────────────────────────────────────────────────

@router.post("/repos")
async def list_repos(cfg: GitConfig):
    h = _headers(cfg); repos = []

    if cfg.provider in ("github", "github_enterprise"):
        page = 1
        while len(repos) < 300:
            d = _get(f"{_github_base(cfg)}/user/repos", h, {"per_page":100,"sort":"updated","page":page})
            repos.extend(d if isinstance(d, list) else [])
            if not d or len(d) < 100: break
            page += 1

    elif cfg.provider == "bitbucket_server":
        base = _bb_server_base(cfg)
        # Only the explicit project key selects a single project. (workspace is a
        # Bitbucket *Cloud* concept and may be auto-filled with the username — using
        # it here would wrongly target a project named after the user and fail.)
        key  = (cfg.project_key or "").strip().upper()

        if key:
            # ── Fast path: single project ──────────────────────────────────
            log.info("BB Server: fetching repos for project %s", key)
            repos = _bb_repos_for_project(base, h, key)
        else:
            # ── Fan-out: all projects → all repos ──────────────────────────
            log.info("BB Server: no project key — fetching all projects first")
            projects = _bb_list_all_projects(base, h)
            log.info("BB Server: found %d projects — fetching repos in parallel", len(projects))

            # Fetch each project's repos concurrently (up to 10 threads)
            MAX_REPOS = 500
            def _fetch_project(proj_key: str) -> list[dict]:
                try:
                    return _bb_repos_for_project(base, h, proj_key, limit=100)
                except Exception as exc:
                    log.warning("BB Server: failed to fetch repos for %s: %s", proj_key, exc)
                    return []

            with ThreadPoolExecutor(max_workers=min(10, len(projects) or 1)) as pool:
                futures = {pool.submit(_fetch_project, p["key"]): p["key"] for p in projects}
                for future in as_completed(futures):
                    repos.extend(future.result())
                    if len(repos) >= MAX_REPOS:
                        break

            repos = repos[:MAX_REPOS]
            log.info("BB Server: collected %d repos across %d projects", len(repos), len(projects))

    else:
        # Bitbucket Cloud
        ws  = cfg.workspace
        url = f"https://api.bitbucket.org/2.0/repositories/{ws}?pagelen=100&sort=-updated_on"
        while url and len(repos) < 300:
            d = _get(url, h); repos.extend(d.get("values",[])); url = d.get("next","")

    return {"repos": repos, "count": len(repos)}

@router.post("/prs/{repo_slug:path}")
async def list_prs(repo_slug: str, cfg: GitConfig):
    h = _headers(cfg)
    if cfg.provider in ("github", "github_enterprise"):
        d = _get(f"{_github_base(cfg)}/repos/{repo_slug}/pulls", h, {"state":"open","per_page":50,"sort":"updated"})
        return {"prs": d if isinstance(d, list) else []}
    elif cfg.provider == "bitbucket_server":
        proj, repo = (repo_slug.split("/",1) + [""])[:2]
        d = _get(f"{_bb_server_base(cfg)}/projects/{proj}/repos/{repo}/pull-requests", h, {"state":"OPEN","limit":50})
        prs = [{"id":p.get("id"),"number":p.get("id"),"title":p.get("title",""),"author":{"login":p.get("author",{}).get("displayName","")},"head":{"ref":p.get("fromRef",{}).get("displayId","")}, "base":{"ref":p.get("toRef",{}).get("displayId","")}} for p in d.get("values",[])]
        return {"prs": prs}
    else:
        ws = cfg.workspace or repo_slug.split("/")[0]; slug = repo_slug.split("/")[-1]
        d = _get(f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/pullrequests", h, {"state":"OPEN","pagelen":50})
        return {"prs": d.get("values",[])}

@router.post("/branches/{repo_slug:path}")
async def list_branches(repo_slug: str, cfg: GitConfig):
    h = _headers(cfg)
    if cfg.provider in ("github", "github_enterprise"):
        d = _get(f"{_github_base(cfg)}/repos/{repo_slug}/branches", h, {"per_page":100})
        return {"branches": d if isinstance(d, list) else []}
    elif cfg.provider == "bitbucket_server":
        proj, repo = (repo_slug.split("/",1) + [""])[:2]
        d = _get(f"{_bb_server_base(cfg)}/projects/{proj}/repos/{repo}/branches", h, {"limit":100,"orderBy":"MODIFICATION"})
        return {"branches": [{"name": b.get("displayId", b.get("id",""))} for b in d.get("values",[])]}
    else:
        ws = cfg.workspace or repo_slug.split("/")[0]; slug = repo_slug.split("/")[-1]
        d = _get(f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/refs/branches", h, {"pagelen":100})
        return {"branches": d.get("values",[])}

@router.post("/commits/{repo_slug:path}")
async def list_commits(repo_slug: str, cfg: GitConfig):
    h = _headers(cfg)
    if cfg.provider in ("github", "github_enterprise"):
        d = _get(f"{_github_base(cfg)}/repos/{repo_slug}/commits", h, {"per_page":30})
        return {"commits": d if isinstance(d, list) else []}
    elif cfg.provider == "bitbucket_server":
        proj, repo = (repo_slug.split("/",1) + [""])[:2]
        d = _get(f"{_bb_server_base(cfg)}/projects/{proj}/repos/{repo}/commits", h, {"limit":30})
        return {"commits": [{"sha":c.get("id",""),"hash":c.get("id",""),"commit":{"message":c.get("message",""),"author":{"name":c.get("author",{}).get("name","")}},"author":{"login":c.get("author",{}).get("name","")}} for c in d.get("values",[])]}
    else:
        ws = cfg.workspace or repo_slug.split("/")[0]; slug = repo_slug.split("/")[-1]
        d = _get(f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/commits", h, {"pagelen":30})
        return {"commits": d.get("values",[])}

class DiffRequest(BaseModel):
    cfg: GitConfig; change_type: str; repo_slug: str; source: str; target: str = ""; pr_id: str = ""

@router.post("/diff")
async def fetch_diff(body: DiffRequest):
    h = _headers(body.cfg); cfg = body.cfg
    try:
        if cfg.provider in ("github","github_enterprise"):
            dh = {**h, "Accept":"application/vnd.github.v3.diff"}
            if body.change_type == "pull_request":
                url = f"{_github_base(cfg)}/repos/{body.repo_slug}/pulls/{body.pr_id.replace('#','')}"
            elif body.change_type == "branch_diff":
                url = f"{_github_base(cfg)}/repos/{body.repo_slug}/compare/{body.target}...{body.source}"
            else:
                url = f"{_github_base(cfg)}/repos/{body.repo_slug}/commits/{body.source}"
            diff = _get_text(url, dh)
        elif cfg.provider == "bitbucket_server":
            proj, repo = (body.repo_slug.split("/",1)+[""])[:2]
            base = _bb_server_base(cfg)
            if body.change_type == "pull_request":
                d = _get(f"{base}/projects/{proj}/repos/{repo}/pull-requests/{body.pr_id.replace('#','')}/diff", h)
            elif body.change_type == "branch_diff":
                d = _get(f"{base}/projects/{proj}/repos/{repo}/compare/diff", h, {"from":body.target,"to":body.source})
            else:
                d = _get(f"{base}/projects/{proj}/repos/{repo}/diff/{body.source}", h)
            # Convert BB Server diff JSON to unified diff text
            lines = []
            for df in d.get("diffs",[]):
                s = df.get("source",{}); dst = df.get("destination",{})
                lines.append(f"--- a/{s.get('toString','unknown') if s else 'unknown'}")
                lines.append(f"+++ b/{dst.get('toString','unknown') if dst else 'unknown'}")
                for hunk in df.get("hunks",[]):
                    lines.append(f"@@ -{hunk.get('sourceLine',1)},{hunk.get('sourceSpan',0)} +{hunk.get('destinationLine',1)},{hunk.get('destinationSpan',0)} @@")
                    for seg in hunk.get("segments",[]):
                        px = {"+":"ADDED","-":"REMOVED"}.get(seg.get("type")," ") if False else {"ADDED":"+","REMOVED":"-","CONTEXT":" "}.get(seg.get("type")," ")
                        for ln in seg.get("lines",[]): lines.append(f"{px}{ln.get('line','')}")
            diff = "\n".join(lines)
        else:
            ws = cfg.workspace or body.repo_slug.split("/")[0]; slug = body.repo_slug.split("/")[-1]
            if body.change_type == "pull_request":
                url = f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/pullrequests/{body.pr_id.replace('#','')}/diff"
            elif body.change_type == "branch_diff":
                url = f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/diff/{body.source}..{body.target}"
            else:
                url = f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/diff/{body.source}"
            diff = _get_text(url, h)
        if len(diff) > 300_000: diff = diff[:300_000] + "\n...(truncated)"
        return {"diff": diff, "lines": diff.count("\n"), "size": len(diff)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(502, f"Diff error: {e}")