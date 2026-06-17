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

def _bb_server_whoami(cfg: GitConfig, h: dict) -> str:
    """Discover the authenticated user's slug on Bitbucket Server / Data Center.

    There is no `/user` (current user) REST endpoint, so with token-only auth
    (no username typed in the UI) we identify the caller via:
      1. The `X-AUSERNAME` response header — set on every authenticated REST
         response (may be URL-encoded).
      2. The `/plugins/servlet/applinks/whoami` servlet — returns the plain
         username slug.
    Returns "" if neither works (e.g. anonymous access).
    """
    from urllib.parse import unquote
    root = cfg.base_url.rstrip("/")

    def _fetch(url: str):
        try:
            return requests.get(url, headers=h, timeout=TIMEOUT, verify=True)
        except requests.exceptions.SSLError:
            return requests.get(url, headers=h, timeout=TIMEOUT, verify=False)
        except requests.exceptions.RequestException:
            return None

    r = _fetch(f"{root}/rest/api/1.0/application-properties")
    if r is not None:
        who = unquote(r.headers.get("X-AUSERNAME", "") or "").strip()
        if who and who.lower() != "anonymous":
            return who

    r = _fetch(f"{root}/plugins/servlet/applinks/whoami")
    if r is not None and r.status_code == 200:
        who = (r.text or "").strip().splitlines()[0].strip() if (r.text or "").strip() else ""
        if who and who.lower() != "anonymous" and len(who) <= 100 and "<" not in who:
            return who
    return ""


@router.post("/verify")
async def verify_credentials(cfg: GitConfig):
    h = _headers(cfg)
    if cfg.provider in ("github", "github_enterprise"):
        d = _get(f"{_github_base(cfg)}/user", h)
        return {"ok": True, "login": d.get("login",""), "name": d.get("name",""), "avatar": d.get("avatar_url","")}
    elif cfg.provider == "bitbucket_server":
        base = _bb_server_base(cfg)
        login = (cfg.username or "").strip()
        # Token-only auth: no username typed — ask the server who we are.
        if not login:
            login = _bb_server_whoami(cfg, h)
        name  = login
        email = ""
        # Fetch the real user profile so we show the display name, not the slug.
        try:
            if login:
                d = _get(f"{base}/users/{login}", h)
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

class UserLookupRequest(BaseModel):
    cfg:      GitConfig
    username: str          # the username / slug to look up


@router.post("/user")
async def lookup_user(body: UserLookupRequest):
    """Look up a git user by username/slug → {found, slug, display_name, email}.

    Maps the provider's profile to the fields the user-management screen stores:
    `name` ← displayName, external `user_id` ← slug. Bitbucket Server is the
    primary target (GET /users/{slug}); GitHub and Bitbucket Cloud are supported
    best-effort.
    """
    cfg, h = body.cfg, _headers(body.cfg)
    u = (body.username or "").strip()
    if not u:
        return {"found": False}
    try:
        if cfg.provider in ("github", "github_enterprise"):
            d = _get(f"{_github_base(cfg)}/users/{quote(u, safe='')}", h)
            return {"found": True, "slug": d.get("login", u),
                    "display_name": d.get("name") or d.get("login", u),
                    "email": d.get("email", "") or ""}
        if cfg.provider == "bitbucket_server":
            d = _get(f"{_bb_server_base(cfg)}/users/{quote(u, safe='')}", h)
            return {"found": True,
                    "slug": d.get("slug") or d.get("name") or u,
                    "display_name": d.get("displayName") or d.get("name") or u,
                    "email": d.get("emailAddress", "")}
        # Bitbucket Cloud — workspace member lookup (best effort)
        ws = cfg.workspace or ""
        d = _get(f"https://api.bitbucket.org/2.0/workspaces/{ws}/members/{quote(u, safe='')}", h)
        usr = d.get("user", d)
        return {"found": True,
                "slug": usr.get("nickname") or usr.get("account_id") or u,
                "display_name": usr.get("display_name") or u, "email": ""}
    except Exception as exc:
        log.debug("user lookup failed for %s: %s", u, exc)
        return {"found": False}


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


class FileRequest(BaseModel):
    cfg: GitConfig; repo_slug: str; path: str; ref: str = ""


@router.post("/file")
async def fetch_file(body: FileRequest):
    """Fetch a single file's raw content at a ref. Returns {found, content}.
    Used for repo-aware test coverage (fetching existing test files). A missing
    file returns found=false (not an error)."""
    cfg, h, path, ref = body.cfg, _headers(body.cfg), body.path.lstrip("/"), (body.ref or "").strip()
    try:
        if cfg.provider in ("github", "github_enterprise"):
            url = f"{_github_base(cfg)}/repos/{body.repo_slug}/contents/{path}"
            if ref: url += f"?ref={ref}"
            txt = _get_text(url, {**h, "Accept": "application/vnd.github.v3.raw"})
        elif cfg.provider == "bitbucket_server":
            proj, repo = (body.repo_slug.split("/", 1) + [""])[:2]
            url = f"{_bb_server_base(cfg)}/projects/{proj}/repos/{repo}/raw/{path}"
            if ref: url += f"?at={ref}"
            txt = _get_text(url, h)
        else:
            ws = cfg.workspace or body.repo_slug.split("/")[0]; slug = body.repo_slug.split("/")[-1]
            url = f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/src/{ref or 'HEAD'}/{path}"
            txt = _get_text(url, h)
        if txt and txt.strip():
            return {"found": True, "path": path, "content": txt[:200_000]}
    except Exception:
        pass
    return {"found": False, "path": path, "content": ""}


def _raw_file(cfg: GitConfig, h: dict, slug: str, path: str, ref: str) -> str:
    """Synchronous raw-file fetch (mirror first, then provider). '' on miss."""
    path = (path or "").lstrip("/")
    if not path:
        return ""
    # Warm mirror — read the checked-out file directly, no network.
    try:
        from ingestion.repo_mirror import resolve_repo_dir
        d = resolve_repo_dir(slug)
        if d:
            from pathlib import Path
            f = Path(d) / path
            if f.is_file():
                return f.read_text(errors="replace")[:200_000]
    except Exception:
        pass
    try:
        if cfg.provider in ("github", "github_enterprise"):
            url = f"{_github_base(cfg)}/repos/{slug}/contents/{path}" + (f"?ref={ref}" if ref else "")
            return _get_text(url, {**h, "Accept": "application/vnd.github.v3.raw"})[:200_000]
        if cfg.provider == "bitbucket_server":
            proj, repo = (slug.split("/", 1) + [""])[:2]
            url = f"{_bb_server_base(cfg)}/projects/{proj}/repos/{repo}/raw/{path}" + (f"?at={ref}" if ref else "")
            return _get_text(url, h)[:200_000]
        ws = cfg.workspace or slug.split("/")[0]; name = slug.split("/")[-1]
        url = f"https://api.bitbucket.org/2.0/repositories/{ws}/{name}/src/{ref or 'HEAD'}/{path}"
        return _get_text(url, h)[:200_000]
    except Exception:
        return ""


def _enrich_caller_context(cfg: GitConfig, refs: list[dict], ref_map: dict,
                           default_ref: str, max_files: int = 25) -> None:
    """Replace each call-site's single-line `context` with the ENCLOSING caller
    function, so downstream deep-impact analysis sees real usage, not one line.

    Best-effort and bounded: at most `max_files` fetches; failures leave the
    original single-line context untouched. Files are fetched once and reused
    across multiple hits in the same file.
    """
    from ingestion.context_expander import _enclosing_window
    h = _headers(cfg)
    cache: dict[tuple, list[str]] = {}
    fetched = 0
    for r in refs:
        line = int(r.get("line") or 0)
        if line <= 0:
            continue
        slug, path = r.get("repo", ""), r.get("file_path", "")
        key = (slug, path)
        if key not in cache:
            if fetched >= max_files:
                continue
            fetched += 1
            txt = _raw_file(cfg, h, slug, path, ref_map.get(slug, "") or default_ref)
            cache[key] = txt.splitlines() if txt else []
        lines = cache[key]
        if not lines:
            continue
        hit = min(max(0, line - 1), len(lines) - 1)
        s, e = _enclosing_window(lines, hit)
        snippet = "\n".join(f"{i + 1:5d}| {lines[i]}" for i in range(s, e))
        if snippet.strip():
            r["context"] = snippet[:1800]

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

# ── Cross-repo reference search (provider code-search APIs) ────────────────────
# Powers automatic call-site tracing in reviewer-declared dependent repos, so the
# Interface / blast-radius analysis reflects real downstream usage instead of a
# manual checklist. Credentials stay in this proxy layer (never in the pipeline).

def _post_json(url: str, headers: dict, payload: dict) -> dict:
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT, verify=True)
    except requests.exceptions.SSLError:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT, verify=False)
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(502, f"Cannot reach Git provider: {e}")
    r.raise_for_status()
    return r.json() if r.content else {}


def _search_bb_server(cfg: GitConfig, h: dict, slug: str, symbol: str) -> list[dict]:
    """Bitbucket Server / Data Center code search (POST /rest/search/latest/search)."""
    proj, repo = (slug.split("/", 1) + [""])[:2]
    url = cfg.base_url.rstrip("/") + "/rest/search/latest/search"
    query = symbol + (f" repo:{repo}" if repo else "") + (f" project:{proj}" if proj else "")
    data = _post_json(url, h, {"query": query, "entities": {"code": {}}, "limit": {"primary": 20}})
    out = []
    for v in ((data.get("code") or {}).get("values") or []):
        rslug = (v.get("repository") or {}).get("slug", "")
        if repo and rslug and rslug.lower() != repo.lower():
            continue
        path = v.get("file", "")
        line, ctx = 0, ""
        for grp in (v.get("hitContexts") or []):
            for hc in grp:
                txt = str(hc.get("text", ""))
                if symbol.lower() in txt.lower():
                    line, ctx = int(hc.get("line") or 0), txt.strip()[:300]
                    break
            if line:
                break
        out.append({"symbol": symbol, "file_path": path, "line": line,
                    "context": ctx, "repo": rslug or repo or slug})
    return out


def _search_github(cfg: GitConfig, h: dict, slug: str, symbol: str) -> list[dict]:
    """GitHub / GHE code search. No line numbers are returned by the API."""
    url = f"{_github_base(cfg)}/search/code?q={quote(symbol + ' repo:' + slug)}&per_page=20"
    data = _get(url, {**h, "Accept": "application/vnd.github+json"})
    return [{"symbol": symbol, "file_path": it.get("path", ""), "line": 0, "context": "",
             "repo": (it.get("repository") or {}).get("full_name", slug)}
            for it in (data.get("items") or [])]


def _search_bb_cloud(cfg: GitConfig, h: dict, slug: str, symbol: str) -> list[dict]:
    """Bitbucket Cloud code search (GET /2.0/workspaces/{ws}/search/code)."""
    ws = cfg.workspace or slug.split("/")[0]
    name = slug.split("/")[-1]
    url = (f"https://api.bitbucket.org/2.0/workspaces/{ws}/search/code"
           f"?search_query={quote(symbol + ' repo:' + name)}&pagelen=20")
    data = _get(url, h)
    out = []
    for v in (data.get("values") or []):
        path = (v.get("file") or {}).get("path", "")
        line, ctx = 0, ""
        for cm in (v.get("content_matches") or []):
            for ln in (cm.get("lines") or []):
                txt = "".join(s.get("text", "") for s in (ln.get("segments") or []))
                if symbol.lower() in txt.lower():
                    line, ctx = int(ln.get("line") or 0), txt.strip()[:300]
                    break
            if line:
                break
        out.append({"symbol": symbol, "file_path": path, "line": line, "context": ctx, "repo": name})
    return out


def _clone_url(cfg: GitConfig, slug: str) -> tuple[str, str]:
    """Build an auth-injected HTTPS clone URL for a dependent repo.

    Returns (url, secret); secret is returned so it can be masked from logs.
    Empty url = unsupported provider.
    """
    secret = cfg.token or cfg.password or ""
    # A token must go in the PASSWORD position, so a synthetic username is needed
    # when the reviewer authenticated with a token only (no username). Bitbucket
    # Server HTTP access tokens fail as `https://<token>@host/...` (token as
    # username, empty password → rc=128); they work as `x-token-auth:<token>@`.
    default_user = "x-token-auth" if cfg.provider in (
        "github", "github_enterprise", "bitbucket_cloud", "bitbucket_server") else ""
    user = cfg.username or (default_user if cfg.token else "")
    if secret and user:
        auth = f"{quote(user, safe='')}:{quote(secret, safe='')}@"
    elif secret:
        auth = f"{quote(secret, safe='')}@"
    else:
        auth = ""

    if cfg.provider in ("github", "github_enterprise"):
        host = cfg.base_url.split("://")[-1].rstrip("/") if (cfg.provider == "github_enterprise" and cfg.base_url) else "github.com"
        return f"https://{auth}{host}/{slug}.git", secret
    if cfg.provider == "bitbucket_server":
        host = cfg.base_url.split("://")[-1].rstrip("/")
        proj, repo = (slug.split("/", 1) + [""])[:2]
        return f"https://{auth}{host}/scm/{proj}/{repo}.git", secret
    if cfg.provider == "bitbucket_cloud":
        ws = cfg.workspace or slug.split("/")[0]
        name = slug.split("/")[-1]
        return f"https://{auth}bitbucket.org/{ws}/{name}.git", secret
    return "", secret


def _split_slug_refs(slugs: list[str], repo_refs: dict | None = None,
                     default_ref: str = "") -> tuple[list[str], dict[str, str]]:
    """Separate per-repo branch/tag from slugs.

    Each dependent repo can pin its own release branch, expressed either as a
    ``slug@ref`` string (e.g. ``SCV/billing@release/2.1``) or via the ``repo_refs``
    map. Precedence: repo_refs[slug] > embedded @ref > default_ref. Returns
    (clean_slugs, {slug: ref}). Note: branch names may contain '/', so we split
    on the FIRST '@' only.
    """
    repo_refs = repo_refs or {}
    clean: list[str] = []
    refmap: dict[str, str] = {}
    for s in slugs:
        if not s:
            continue
        slug, _, embedded = s.partition("@")
        slug = slug.strip()
        if not slug:
            continue
        if slug not in refmap:
            clean.append(slug)
        refmap[slug] = (repo_refs.get(slug) or repo_refs.get(s) or embedded or default_ref or "").strip()
    return clean, refmap


class XrefRequest(BaseModel):
    cfg:         GitConfig
    repo_slugs:  list[str] = []   # dependent repos: "project/repo", optionally "project/repo@release/2.1"
    symbols:     list[str] = []   # changed symbols; derived from diff_text if empty
    diff_text:   str = ""
    ref:         str = ""
    repo_refs:   dict[str, str] = {}   # optional {slug: release branch/tag} overrides
    max_symbols: int = 12
    max_refs:    int = 200
    allow_clone: bool = True      # shallow-clone + grep fallback when search yields nothing
    max_clone_repos: int = 3      # cap clones (each is bandwidth + time)
    deep_context: bool = True     # expand each call-site to its enclosing caller function
    max_context_files: int = 25   # cap raw-file fetches for context expansion


@router.post("/xref")
async def cross_repo_references(body: XrefRequest):
    """Search reviewer-declared dependent repos for call-sites of changed symbols.

    Returns {references:[{symbol,file_path,line,context,repo}], searched_repos, backend}.
    Resilient by design: per repo×symbol failures are skipped so a search-disabled
    instance or a rate-limited symbol never aborts the whole request.
    """
    cfg = body.cfg
    symbols = [s for s in (body.symbols or []) if s]
    if not symbols and body.diff_text:
        try:
            from ingestion.diff_parser import parse_diff
            from ingestion.symbol_extractor import ranked_symbol_names
            symbols = ranked_symbol_names(parse_diff(body.diff_text), max_symbols=body.max_symbols)
        except Exception as exc:
            log.debug("xref symbol extraction failed: %s", exc)
            symbols = []
    symbols = list(dict.fromkeys(symbols))[:body.max_symbols]
    raw_slugs = list(dict.fromkeys(s for s in (body.repo_slugs or []) if s))[:25]
    slugs, ref_map = _split_slug_refs(raw_slugs, body.repo_refs, body.ref)
    if not symbols or not slugs:
        return {"references": [], "searched_repos": slugs, "symbols": symbols,
                "backend": "none", "truncated": False}

    refs: list[dict] = []
    errors = 0
    used: set[str] = set()

    # ── Phase 0: warm local mirror (instant, no network) ──────────────────────
    # If a dependent repo is checked out under REPOS_ROOT, grep it directly.
    mirrored: set[str] = set()
    try:
        from ingestion.repo_mirror import local_xref, is_mirrored
        for slug in slugs:
            if not is_mirrored(slug):
                continue
            mirrored.add(slug)
            for sr in local_xref(slug, symbols):
                refs.append({"symbol": sr.symbol, "file_path": sr.file_path,
                             "line": sr.line, "context": sr.context, "repo": sr.repo})
        if mirrored:
            used.add("local_mirror")
    except Exception as exc:
        log.debug("xref mirror phase failed: %s", exc)

    remaining = [s for s in slugs if s not in mirrored]

    # ── Phase 1: provider code search for repos not in the mirror ─────────────
    if cfg.provider == "bitbucket_server":
        search_fn, search_label = _search_bb_server, "bitbucket_server_search"
    elif cfg.provider in ("github", "github_enterprise"):
        search_fn, search_label = _search_github, "github_search"
    elif cfg.provider == "bitbucket_cloud":
        search_fn, search_label = _search_bb_cloud, "bitbucket_cloud_search"
    else:
        search_fn, search_label = None, "unsupported"

    if remaining and search_fn is not None:
        h = _headers(cfg)
        tasks = [(slug, sym) for slug in remaining for sym in symbols]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(search_fn, cfg, h, slug, sym): (slug, sym) for slug, sym in tasks}
            for fut in as_completed(futs):
                try:
                    got = fut.result()
                    if got:
                        used.add(search_label)
                    refs.extend(got)
                except Exception as exc:
                    errors += 1
                    log.debug("xref search failed for %s: %s", futs[fut], exc)

    # Dedupe on (repo, file, line, symbol)
    seen, deduped = set(), []
    for r in refs:
        key = (r.get("repo"), r.get("file_path"), r.get("line"), r.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    # ── Phase 2: shallow-clone fallback when nothing else found anything ──────
    # (e.g. Bitbucket Server with the search index disabled and no warm mirror).
    # Bounded by max_clone_repos; each clone is auto-deleted. Best-effort.
    if body.allow_clone and not deduped and remaining:
        try:
            from ingestion.reference_finder import clone_and_grep, _has_git
            if _has_git():
                for slug in remaining[:body.max_clone_repos]:
                    url, secret = _clone_url(cfg, slug)
                    if not url:
                        continue
                    label = slug.split("/")[-1]
                    got = clone_and_grep(url, symbols, ref=ref_map.get(slug, "") or body.ref,
                                         repo_label=label, secret=secret, timeout_s=50)
                    for sr in got:
                        deduped.append({"symbol": sr.symbol, "file_path": sr.file_path,
                                        "line": sr.line, "context": sr.context, "repo": sr.repo})
                    if got:
                        used.add("clone_grep")
        except Exception as exc:
            log.debug("xref clone fallback failed: %s", exc)

    if used:
        backend = "+".join(sorted(used))
    elif mirrored:
        backend = "local_mirror"      # mirror had the repo(s) but no hits
    elif search_fn is None:
        backend = "unsupported"
    else:
        backend = search_label        # searched, no hits

    truncated = len(deduped) > body.max_refs
    out_refs = deduped[:body.max_refs]

    # Deep context: expand each call-site to its enclosing caller function so the
    # cross-repo impact agent can judge whether the change actually breaks it.
    # Reported separately — it does NOT change the search `backend` label.
    context_enriched = False
    if body.deep_context and out_refs:
        try:
            _enrich_caller_context(cfg, out_refs, ref_map, body.ref, body.max_context_files)
            context_enriched = True
        except Exception as exc:
            log.debug("xref deep-context enrichment failed: %s", exc)

    return {"references": out_refs, "searched_repos": slugs,
            "symbols": symbols, "backend": backend, "errors": errors,
            "mirrored_repos": sorted(mirrored), "truncated": truncated,
            "context_enriched": context_enriched}


# ── Warm-mirror maintenance ───────────────────────────────────────────────────

class MirrorRequest(BaseModel):
    cfg:        GitConfig
    repo_slugs: list[str] = []   # "project/repo" or "project/repo@release/2.1"
    ref:        str = ""         # default branch/tag when a repo pins none
    repo_refs:  dict[str, str] = {}   # optional {slug: release branch/tag}


@router.post("/mirror/status")
async def mirror_status(body: MirrorRequest):
    """Report whether each dependent repo is present in the warm local mirror."""
    from ingestion.repo_mirror import resolve_repo_dir, mirror_root
    clean, _ = _split_slug_refs(body.repo_slugs)
    body = body.model_copy(update={"repo_slugs": clean})
    root = mirror_root()
    return {
        "root": root,
        "enabled": bool(root),
        "repos": [{"slug": s, "mirrored": bool(resolve_repo_dir(s))}
                  for s in (body.repo_slugs or [])],
    }


@router.post("/mirror/sync")
async def sync_mirror(body: MirrorRequest):
    """Clone (or `git fetch`) the dependent repos into REPOS_ROOT to keep the
    cross-repo reference mirror warm. Intended for a cron / CI job or a manual
    'refresh' action. Each repo is shallow — bandwidth-friendly."""
    from ingestion.repo_mirror import sync_repo, mirror_root
    root = mirror_root()
    if not root:
        raise HTTPException(400, "REPOS_ROOT is not configured on the server — set it to enable a warm mirror.")
    raw = list(dict.fromkeys(s for s in (body.repo_slugs or []) if s))[:50]
    slugs, ref_map = _split_slug_refs(raw, body.repo_refs, body.ref)
    results = []
    for slug in slugs:
        url, secret = _clone_url(body.cfg, slug)
        if not url:
            results.append({"slug": slug, "ok": False, "action": "skipped", "error": "unsupported provider"})
            continue
        res = sync_repo(url, slug, ref=ref_map.get(slug, ""), secret=secret, root=root)
        res["ref"] = ref_map.get(slug, "") or "(default)"
        results.append(res)
    return {"root": root, "synced": sum(1 for r in results if r.get("ok")),
            "total": len(results), "results": results}
