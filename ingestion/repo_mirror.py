"""
ingestion/repo_mirror.py
-------------------------
Warm local mirror of dependent repos for instant cross-repo reference search.

Cloning per analysis run is correct but slow and blocks the request. The
production-grade path is a directory of working clones kept warm (refreshed by a
cron / CI job or the /git/mirror/sync endpoint). When a dependent repo is present
in this mirror, cross-repo grep is instant and never touches the network during
analysis.

Mirror root = settings.repos_root (REPOS_ROOT). Each repo lives at
``{root}/{owner-or-project}__{repo}`` (separators flattened), but a manually
placed clone named just ``{repo}`` is also discovered.
"""
from __future__ import annotations
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.models import SymbolReference
from ingestion.reference_finder import _grep_local, _has_git, _GREP_WORKERS

log = logging.getLogger(__name__)

_FETCH_TIMEOUT_S = 90


def mirror_root() -> str:
    try:
        from config.settings import get_settings
        return (get_settings().repos_root or "").strip()
    except Exception:
        return ""


def _safe_slug(slug: str) -> str:
    """`PROJ/repo` → `PROJ__repo` (a flat, filesystem-safe directory name)."""
    return slug.strip("/").replace("/", "__")


def resolve_repo_dir(slug: str, root: str | None = None) -> str | None:
    """Find the local working clone for *slug* under the mirror root, if any.

    Tries, in order: flattened slug dir, the nested slug path, and a bare
    last-segment dir (case-insensitive). Returns an absolute path or None.
    """
    root = (root if root is not None else mirror_root())
    if not root:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    last = slug.rstrip("/").split("/")[-1]
    candidates = [base / _safe_slug(slug), base / slug, base / last]
    for c in candidates:
        if c.is_dir():
            return str(c)
    # Case-insensitive scan over the top level (handles BANK vs bank, and a bare
    # "repo" slug matching a "PROJ__repo" directory via its trailing segment).
    want = {_safe_slug(slug).lower(), last.lower()}
    try:
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name.lower()
            if name in want or name.split("__")[-1] in want:
                return str(entry)
    except OSError:
        pass
    return None


def local_xref(slug: str, symbols: list[str], root: str | None = None) -> list[SymbolReference]:
    """Grep the warm local clone of *slug* for *symbols*. Returns [] if not mirrored.

    Each reference is stamped with the repo's last path segment.
    """
    repo_dir = resolve_repo_dir(slug, root)
    if not repo_dir or not symbols:
        return []
    label = slug.rstrip("/").split("/")[-1]
    refs: list[SymbolReference] = []
    with ThreadPoolExecutor(max_workers=min(len(symbols), _GREP_WORKERS)) as pool:
        futures = {pool.submit(_grep_local, sym, repo_dir): sym for sym in symbols}
        for fut in as_completed(futures):
            try:
                refs.extend(fut.result())
            except Exception as exc:
                log.debug("mirror grep failed for %s: %s", slug, exc)
    for r in refs:
        r.repo = label
    return refs


def is_mirrored(slug: str, root: str | None = None) -> bool:
    return resolve_repo_dir(slug, root) is not None


# ── Sync (keep mirror warm) ───────────────────────────────────────────────────

def sync_repo(clone_url: str, slug: str, ref: str = "", secret: str = "",
              root: str | None = None) -> dict:
    """Clone *slug* into the mirror if absent, else `git fetch` + hard-reset it.

    Shallow working clone (depth 1). Returns a status dict; never raises.
    *secret* is masked from logs.
    """
    root = (root if root is not None else mirror_root())
    if not root:
        return {"slug": slug, "ok": False, "action": "skipped", "error": "REPOS_ROOT not set"}
    if not _has_git():
        return {"slug": slug, "ok": False, "action": "skipped", "error": "git not available"}
    os.makedirs(root, exist_ok=True)
    repo_dir = Path(root) / _safe_slug(slug)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _mask(s: str) -> str:
        return s.replace(secret, "***") if secret else s

    try:
        if (repo_dir / ".git").is_dir():
            fetch = ["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin"] + ([ref] if ref else [])
            r = subprocess.run(fetch, capture_output=True, text=True, timeout=_FETCH_TIMEOUT_S, env=env)
            if r.returncode != 0:
                return {"slug": slug, "ok": False, "action": "fetch", "error": _mask(r.stderr)[:200]}
            subprocess.run(["git", "-C", str(repo_dir), "reset", "--hard", "FETCH_HEAD"],
                           capture_output=True, text=True, timeout=30, env=env)
            return {"slug": slug, "ok": True, "action": "fetched", "path": str(repo_dir)}
        cmd = ["git", "clone", "--depth", "1", "--single-branch"]
        if ref:
            cmd += ["--branch", ref]
        cmd += ["--", clone_url, str(repo_dir)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_FETCH_TIMEOUT_S, env=env)
        if r.returncode != 0 and ref:
            # branch may not exist downstream — retry on default branch
            cmd = ["git", "clone", "--depth", "1", "--single-branch", "--", clone_url, str(repo_dir)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=_FETCH_TIMEOUT_S, env=env)
        if r.returncode != 0:
            return {"slug": slug, "ok": False, "action": "clone", "error": _mask(r.stderr)[:200]}
        return {"slug": slug, "ok": True, "action": "cloned", "path": str(repo_dir)}
    except subprocess.TimeoutExpired:
        return {"slug": slug, "ok": False, "action": "timeout", "error": f"exceeded {_FETCH_TIMEOUT_S}s"}
    except Exception as exc:
        return {"slug": slug, "ok": False, "action": "error", "error": _mask(str(exc))[:200]}
