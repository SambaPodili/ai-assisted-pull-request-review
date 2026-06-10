"""
ingestion/reference_finder.py
-------------------------------
Find call-sites and usages of changed symbols across the codebase.

Backends (tried in order, first available wins):

  1. local_grep  — ripgrep / grep over a local clone (fastest, zero API cost).
                   Activated when REPO_LOCAL_PATH is set in settings.

  2. auto_clone  — shallow-clones the repo into a temp dir, greps it, then
                   deletes the clone.  Activated automatically when GITHUB_TOKEN
                   + a github.com repo URL are available.  No manual path needed.

  3. github_api  — GitHub code search API (one request per symbol, 30 rps).
                   Fallback when git is not available or clone fails.
                   Only indexes the default branch.

  4. diff_scan   — scans the PR diff itself for cross-file symbol usages.
                   Zero config; limited to files in the current PR.

  5. none        — returns empty results; analysis degrades gracefully.

Each backend returns a list of SymbolReference objects.
"""
from __future__ import annotations
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import TYPE_CHECKING

from core.models import SymbolReference

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_CLONE_TIMEOUT_S = 120   # max seconds for git clone

# Files/dirs to always exclude from local search
_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".gradle", ".idea", ".vscode",
    "vendor", "third_party", ".mypy_cache", ".pytest_cache",
}

# Limit refs per symbol to avoid noise on very common names
_MAX_REFS_PER_SYMBOL = 50
_MAX_SYMBOLS         = 30   # skip long-tail if a diff changes hundreds of things
_GREP_WORKERS        = 8    # parallel grep threads
_GREP_WALL_CLOCK_S   = 45   # total wall-clock budget for the grep phase


# ── Backend: local grep ───────────────────────────────────────────────────────

def _has_rg() -> bool:
    return shutil.which("rg") is not None


def _grep_local(symbol: str, repo_path: str) -> list[SymbolReference]:
    """
    Search for bare-word usages of *symbol* in a local repo directory.
    Uses ripgrep (rg) if available, falls back to GNU grep.
    """
    root  = Path(repo_path)
    refs: list[SymbolReference] = []

    # Build exclude args
    if _has_rg():
        excludes = [arg for d in _EXCLUDE_DIRS for arg in ("--glob", f"!{d}/**")]
        cmd = [
            "rg", "--line-number", "--no-heading", "--color=never",
            "--max-count", str(_MAX_REFS_PER_SYMBOL),
            *excludes,
            r"\b" + re.escape(symbol) + r"\b",
            str(root),
        ]
    else:
        cmd = [
            "grep", "-rn", "--include=*",
            "-m", str(_MAX_REFS_PER_SYMBOL),
            r"\b" + symbol + r"\b",
            str(root),
        ]

    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20
        )
        for line in out.stdout.splitlines():
            # ripgrep: path:lineno:content  /  grep: path:lineno:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno_str, context = parts[0], parts[1], parts[2]
            # Strip repo root prefix for cleaner display
            try:
                rel = Path(fpath).relative_to(root)
            except ValueError:
                rel = Path(fpath)
            refs.append(SymbolReference(
                symbol=symbol,
                file_path=str(rel),
                line=int(lineno_str) if lineno_str.isdigit() else 0,
                context=context.strip()[:200],
            ))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("local grep failed for '%s': %s", symbol, exc)

    return refs


# ── Backend: GitHub code search API ──────────────────────────────────────────

def _github_search(symbol: str, repo_url: str, token: str) -> list[SymbolReference]:
    """
    Use GitHub REST code-search to find references.
    https://docs.github.com/en/rest/search/search#search-code

    Rate limit: 30 authenticated requests/min; we keep this reasonable by
    batching symbol queries and honouring the Retry-After header.
    """
    # Extract owner/repo from URL (handles https and git@ forms)
    m = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', repo_url)
    if not m:
        log.debug("Cannot parse GitHub repo from URL: %s", repo_url)
        return []

    repo_slug = m.group(1)
    query     = urllib.parse.quote(f"{symbol} repo:{repo_slug}")
    url       = f"https://api.github.com/search/code?q={query}&per_page=30"

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":    "impact-analyzer/1.0",
    })

    refs: list[SymbolReference] = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for item in data.get("items", [])[:_MAX_REFS_PER_SYMBOL]:
            refs.append(SymbolReference(
                symbol=symbol,
                file_path=item.get("path", ""),
                line=0,   # GitHub search does not return line numbers
                context=item.get("name", ""),
                repo=repo_slug,
            ))
    except urllib.error.HTTPError as exc:
        log.debug("GitHub code search HTTP %s for '%s': %s", exc.code, symbol, exc.reason)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.debug("GitHub code search failed for '%s': %s", symbol, exc)

    return refs


# ── Backend: auto-clone ───────────────────────────────────────────────────────

def _has_git() -> bool:
    return shutil.which("git") is not None


def _build_clone_url(repo_url: str, token: str) -> str:
    """
    Inject the token into the clone URL so git can authenticate without
    storing credentials.

    Handles:
      https://github.com/owner/repo       → https://x-access-token:{token}@github.com/…
      https://github.com/owner/repo.git   → same
      owner/repo                          → https://x-access-token:{token}@github.com/…
    """
    slug = re.search(r'github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$', repo_url)
    if slug:
        return f"https://x-access-token:{token}@github.com/{slug.group(1)}.git"
    # bare slug form e.g. "owner/repo"
    if re.match(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', repo_url):
        return f"https://x-access-token:{token}@github.com/{repo_url}.git"
    return repo_url  # unrecognised — return as-is, let git fail gracefully


def _auto_clone_and_grep(
    symbols:  list[str],
    repo_url: str,
    token:    str,
    ref:      str = "",
) -> tuple[list[SymbolReference], bool]:
    """
    Shallow-clone the repo into a temp directory, run local grep against it,
    and clean up afterwards.

    Returns (refs, clone_succeeded).  The second element allows callers to
    distinguish "clone ok but symbol has no callers" from "clone failed" — the
    latter should fall through to the GitHub API backend.
    """
    clone_url = _build_clone_url(repo_url, token)
    tmp_dir   = tempfile.mkdtemp(prefix="ciaa_clone_")

    try:
        # Shallow clone — only the tip commit, no history
        clone_cmd = [
            "git", "clone",
            "--depth", "1",
            "--single-branch",
        ]
        if ref:
            clone_cmd += ["--branch", ref]
        clone_cmd += ["--", clone_url, tmp_dir]

        log.info("Auto-clone: cloning %s (ref=%s) → %s", repo_url, ref or "HEAD", tmp_dir)
        result = subprocess.run(
            clone_cmd,
            capture_output=True, text=True,
            timeout=_CLONE_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never prompt for password
        )
        if result.returncode != 0:
            # Mask token in log output
            err = result.stderr.replace(token, "***")
            log.warning("Auto-clone failed (rc=%d): %s", result.returncode, err[:300])
            return [], False   # signal failure so caller can try GitHub API

        log.info("Auto-clone succeeded — starting grep phase")
        refs: list[SymbolReference] = []
        with ThreadPoolExecutor(max_workers=min(len(symbols), _GREP_WORKERS)) as pool:
            futures = {pool.submit(_grep_local, sym, tmp_dir): sym for sym in symbols}
            try:
                for future in as_completed(futures, timeout=_GREP_WALL_CLOCK_S):
                    try:
                        refs.extend(future.result())
                    except Exception as exc:
                        log.debug("grep worker failed: %s", exc)
            except FuturesTimeout:
                log.warning("Auto-clone grep hit wall-clock limit — results are partial")

        log.info("Auto-clone (auto_clone): %d refs for %d symbols", len(refs), len(symbols))
        return refs, True   # clone succeeded (refs may be empty if symbol has no callers)

    except subprocess.TimeoutExpired:
        log.warning("Auto-clone timed out after %ds", _CLONE_TIMEOUT_S)
        return [], False
    except Exception as exc:
        log.warning("Auto-clone unexpected error: %s", exc)
        return [], False
    finally:
        # Always remove the temp clone — it can be hundreds of MB
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("Auto-clone: cleaned up %s", tmp_dir)
        except Exception:
            pass


# ── Provider-agnostic clone + grep (cross-repo fallback) ──────────────────────

def clone_and_grep(
    clone_url:  str,
    symbols:    list[str],
    ref:        str = "",
    repo_label: str = "",
    secret:     str = "",
    timeout_s:  int = 60,
) -> list[SymbolReference]:
    """Shallow-clone *any* repo URL (already auth-injected) and grep for symbols.

    Provider-agnostic alternative to code-search APIs: works even when the git
    server's search index is disabled (common on older Bitbucket Server), and
    yields accurate line numbers + context via ripgrep. Each returned reference
    is stamped with ``repo_label``. ``secret`` (if given) is masked from logs.
    Returns [] on any failure — callers degrade gracefully. The clone is always
    deleted afterwards.
    """
    if not _has_git() or not symbols or not clone_url:
        return []
    tmp_dir = tempfile.mkdtemp(prefix="ciaa_xref_")
    try:
        cmd = ["git", "clone", "--depth", "1", "--single-branch"]
        if ref:
            cmd += ["--branch", ref]
        cmd += ["--", clone_url, tmp_dir]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            err = result.stderr
            if secret:
                err = err.replace(secret, "***")
            log.warning("xref clone failed for %s (rc=%d): %s",
                        repo_label or "repo", result.returncode, err[:200])
            # The branch may not exist in this dependent repo — retry on default.
            if ref:
                return clone_and_grep(clone_url, symbols, ref="",
                                      repo_label=repo_label, secret=secret, timeout_s=timeout_s)
            return []

        refs: list[SymbolReference] = []
        with ThreadPoolExecutor(max_workers=min(len(symbols), _GREP_WORKERS)) as pool:
            futures = {pool.submit(_grep_local, sym, tmp_dir): sym for sym in symbols}
            try:
                for future in as_completed(futures, timeout=_GREP_WALL_CLOCK_S):
                    try:
                        refs.extend(future.result())
                    except Exception as exc:
                        log.debug("xref grep worker failed: %s", exc)
            except FuturesTimeout:
                log.warning("xref grep hit wall-clock limit for %s — partial", repo_label)
        for r in refs:
            r.repo = repo_label
        return refs
    except subprocess.TimeoutExpired:
        log.warning("xref clone timed out (%ds) for %s", timeout_s, repo_label or "repo")
        return []
    except Exception as exc:
        log.warning("xref clone error for %s: %s", repo_label or "repo", exc)
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Backend: diff-internal scan ───────────────────────────────────────────────

def find_references_in_diff(
    symbols: list[str],
    hunks: list,           # list[DiffHunk] — typed loosely to avoid circular import
) -> list[SymbolReference]:
    """
    Scan all diff hunks for symbol usages across OTHER files.

    Works with zero external configuration — a useful fallback when neither
    REPO_LOCAL_PATH nor GITHUB_TOKEN is set.  Only finds references between
    files that are already part of the current PR, so coverage is limited, but
    it still reveals intra-PR dependencies that the developer should know about.

    A line is counted only if:
      • the file is different from the file where the symbol is defined (to
        avoid trivially matching the definition itself), AND
      • the line is a context or added line (not a deletion marker).
    """
    if not symbols or not hunks:
        return []

    # Build a map: symbol → set of file_paths where it is *defined*
    # (i.e. the files that contain the defining hunk)
    # We do a quick definition scan: if the line defines (def/func/class) the
    # symbol we skip counting that file as a "reference" file.
    sym_def_files: dict[str, set[str]] = {s: set() for s in symbols}
    _DEF_RE = re.compile(
        r'^\+?\s*(?:def|class|func(?:tion)?|fn|sub)\s+(\w+)\s*[\(<:]'
    )
    for hunk in hunks:
        for line in hunk.content.splitlines():
            m = _DEF_RE.match(line)
            if m:
                name = m.group(1)
                if name in sym_def_files:
                    sym_def_files[name].add(hunk.file_path)

    refs: list[SymbolReference] = []
    for hunk in hunks:
        lines = hunk.content.splitlines()
        # Parse the starting source line from the @@ hunk header so we can
        # report accurate source line numbers rather than diff-file offsets.
        src_line = 0
        _hdr = re.search(r'@@ -\d+(?:,\d+)? \+(\d+)', hunk.content)
        if _hdr:
            src_line = int(_hdr.group(1)) - 1  # will be incremented before first use

        for raw in lines:
            # Skip diff metadata lines (don't advance src_line for them)
            if raw.startswith("---") or raw.startswith("+++") or raw.startswith("@@"):
                continue
            # Deletion lines exist in source but not in new file — don't count
            if raw.startswith("-"):
                continue
            # Context and added lines advance the new-file line counter
            src_line += 1
            # Strip leading +/space to get actual source content
            content = raw[1:] if raw.startswith("+") or raw.startswith(" ") else raw

            for sym in symbols:
                # Skip if this is the defining file
                if hunk.file_path in sym_def_files.get(sym, set()):
                    continue
                # Word-boundary match
                if re.search(r'\b' + re.escape(sym) + r'\b', content):
                    refs.append(SymbolReference(
                        symbol=sym,
                        file_path=hunk.file_path,
                        line=src_line,
                        context=content.strip()[:200],
                        depth=1,
                    ))

    # Deduplicate: keep first occurrence per (symbol, file_path)
    seen: set[tuple[str, str]] = set()
    deduped: list[SymbolReference] = []
    for r in refs:
        key = (r.symbol, r.file_path)
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    log.info("Reference finder (diff_scan): %d refs for %d symbols", len(deduped), len(symbols))
    return deduped


# ── Public API ────────────────────────────────────────────────────────────────

def find_references(
    symbols:      list[str],
    repo_url:     str = "",
    repo_path:    str = "",
    github_token: str = "",
    hunks:        list | None = None,
    source_ref:   str = "",
) -> tuple[list[SymbolReference], str]:
    """
    Find all call-sites for *symbols* using the best available backend.

    Returns (references, backend_name) where backend_name is one of:
      "local_grep" | "auto_clone" | "github_api" | "diff_scan" | "none"
    """
    if not symbols:
        return [], "none"

    symbols = symbols[:_MAX_SYMBOLS]

    # Backend 1: local grep — run all symbols in parallel, honour wall-clock budget
    if repo_path and Path(repo_path).is_dir():
        refs: list[SymbolReference] = []
        with ThreadPoolExecutor(max_workers=min(len(symbols), _GREP_WORKERS)) as pool:
            futures = {pool.submit(_grep_local, sym, repo_path): sym for sym in symbols}
            try:
                for future in as_completed(futures, timeout=_GREP_WALL_CLOCK_S):
                    try:
                        refs.extend(future.result())
                    except Exception as exc:
                        log.debug("grep worker failed: %s", exc)
            except FuturesTimeout:
                completed = sum(1 for f in futures if f.done())
                log.warning(
                    "Reference grep hit %ds wall-clock limit — "
                    "completed %d/%d symbols; results are partial",
                    _GREP_WALL_CLOCK_S, completed, len(symbols),
                )
        log.info("Reference finder (local_grep): %d refs for %d symbols", len(refs), len(symbols))
        return refs, "local_grep"

    # Backend 2: auto-clone — shallow git clone into temp dir, then local grep.
    # Preferred over GitHub code-search API because it:
    #   • finds symbols on feature branches (not just the indexed default branch)
    #   • supports multi-level BFS (L2/L3) via local grep on the clone
    #   • has no rate limits beyond the initial clone bandwidth
    if github_token and repo_url and "github.com" in repo_url and _has_git():
        refs, clone_ok = _auto_clone_and_grep(symbols, repo_url, github_token, ref=source_ref)
        if not clone_ok:
            # Clone itself failed (bad token, network, permissions) — fall through to
            # GitHub code-search API which may still work for public/indexed repos.
            log.info("Auto-clone failed — falling back to GitHub code-search API")
        elif refs:
            return refs, "auto_clone"
        else:
            # Clone succeeded but symbol genuinely has no callers — supplement with
            # intra-PR diff scan and report auto_clone as the primary backend.
            log.info("Auto-clone returned 0 refs — supplementing with diff_scan")
            if hunks:
                diff_refs = find_references_in_diff(symbols, hunks)
                if diff_refs:
                    return diff_refs, "diff_scan"
            return [], "auto_clone"

    # Backend 3: GitHub code search API (fallback when git binary is unavailable)
    if github_token and repo_url and "github.com" in repo_url:
        refs = []
        with ThreadPoolExecutor(max_workers=min(len(symbols), 4)) as pool:
            futures = {pool.submit(_github_search, sym, repo_url, github_token): sym
                       for sym in symbols}
            try:
                for future in as_completed(futures, timeout=_GREP_WALL_CLOCK_S):
                    try:
                        refs.extend(future.result())
                    except Exception as exc:
                        log.debug("GitHub search worker failed: %s", exc)
            except FuturesTimeout:
                log.warning("GitHub code search hit wall-clock limit — results are partial")
        log.info("Reference finder (github_api): %d refs for %d symbols", len(refs), len(symbols))
        if not refs and hunks:
            diff_refs = find_references_in_diff(symbols, hunks)
            if diff_refs:
                return diff_refs, "diff_scan"
        return refs, "github_api"

    # Backend 4: diff-internal scan — works with zero external config
    if hunks:
        refs = find_references_in_diff(symbols, hunks)
        return refs, "diff_scan"

    log.debug("Reference finder: no backend configured — returning empty")
    return [], "none"


# ── Multi-level traversal ─────────────────────────────────────────────────────

# Patterns for extracting callable names from full source files
_FUNC_PATTERNS = re.compile(
    r'(?:^|\s)'
    r'(?:'
    r'def (\w+)\s*\('                            # Python
    r'|class (\w+)[\s:(]'                        # Python / JS / Java
    r'|func(?:tion)?\s+(\w+)\s*[\(<]'           # Go / JS / Kotlin / Swift
    r'|(?:public|private|protected|static)\s+(?:\w+\s+)+(\w+)\s*\('   # Java / C#
    r'|fn\s+(\w+)\s*[\(<]'                      # Rust
    r'|sub\s+(\w+)\s*[\({]'                     # Perl / VB
    r')',
    re.MULTILINE,
)

# Short / generic names that produce too many false positives at deeper levels
_NOISY_NAMES = frozenset({
    "main", "init", "new", "get", "set", "run", "test", "setup", "teardown",
    "start", "stop", "open", "close", "read", "write", "print", "log",
    "error", "warn", "info", "debug", "update", "delete", "create",
    "handle", "process", "parse", "format", "check", "validate",
})


def _extract_file_symbols(abs_path: str) -> list[str]:
    """
    Extract defined function/class/method names from a full source file.
    Used to discover level-2+ symbols to search for.
    Bounded to 200 KB to avoid huge generated files.
    """
    try:
        with open(abs_path, "r", errors="ignore") as fh:
            content = fh.read(200_000)
    except OSError:
        return []

    names: set[str] = set()
    for m in _FUNC_PATTERNS.finditer(content):
        name = next((g for g in m.groups() if g), None)
        if name and len(name) >= 4 and name not in _NOISY_NAMES:
            names.add(name)
    return list(names)


def find_references_deep(
    symbols:      list[str],
    repo_url:     str = "",
    repo_path:    str = "",
    github_token: str = "",
    max_depth:    int = 2,
    hunks:        list | None = None,
    source_ref:   str = "",
) -> tuple[list[SymbolReference], str]:
    """
    BFS multi-level reference search.

      depth 1 — files that directly call the changed symbols
      depth 2 — files that call the depth-1 caller files' own functions
      depth N — … (bounded by max_depth)

    Level 2+ requires REPO_LOCAL_PATH: the agent reads each caller file,
    extracts its own function names, and searches for those next.
    GitHub API mode is used for depth=1 only (API rate limits make BFS
    impractical beyond one level).

    Returns (all_refs_sorted_by_depth, backend_name).
    """
    if not symbols:
        return [], "none"

    all_refs: list[SymbolReference] = []
    visited_symbols: set[str] = set(symbols)
    visited_files:   set[str] = set()
    current_symbols = list(symbols)[:_MAX_SYMBOLS]
    used_backend    = "none"
    # Maps symbol_name → source file path for the CURRENT depth level
    sym_to_file: dict[str, str] = {}

    for depth in range(1, max_depth + 1):
        if not current_symbols:
            break

        refs, backend = find_references(
            current_symbols,
            repo_url=repo_url,
            repo_path=repo_path,
            github_token=github_token,
            hunks=hunks,
            source_ref=source_ref,
        )
        used_backend = backend

        # Tag each reference with its traversal depth and parent file
        for ref in refs:
            ref.depth = depth
            if depth > 1 and ref.symbol in sym_to_file:
                ref.from_file = sym_to_file[ref.symbol]
        all_refs.extend(refs)

        # Deeper levels only work with local file access
        if depth >= max_depth or not repo_path or not Path(repo_path).is_dir():
            break

        # Discover new symbols from the caller files found at this depth
        new_files = {r.file_path for r in refs} - visited_files
        visited_files.update(new_files)

        # Build sym_to_file mapping for the NEXT depth level
        sym_to_file = {}
        for fp in new_files:
            abs_path = fp if os.path.isabs(fp) else os.path.join(repo_path, fp)
            file_syms = _extract_file_symbols(abs_path)
            for s in file_syms:
                if s not in visited_symbols and s not in sym_to_file:
                    sym_to_file[s] = fp

        # Cap depth-2+ symbols to avoid combinatorial explosion
        capped = list(sym_to_file.keys())[:_MAX_SYMBOLS]
        visited_symbols.update(capped)
        current_symbols = capped

        log.info(
            "Reference finder depth %d: %d new symbols from %d files",
            depth + 1, len(capped), len(new_files),
        )

    log.info(
        "Reference finder total: %d refs across %d depth levels (backend=%s)",
        len(all_refs), max_depth, used_backend,
    )
    return sorted(all_refs, key=lambda r: r.depth), used_backend


def group_by_file(refs: list[SymbolReference]) -> dict[str, list[SymbolReference]]:
    """Group references by file path, sorted by reference count descending."""
    groups: dict[str, list[SymbolReference]] = {}
    for ref in refs:
        groups.setdefault(ref.file_path, []).append(ref)
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def high_impact_files(refs: list[SymbolReference], threshold: int = 3) -> list[str]:
    """Files that reference changed symbols ≥ threshold times."""
    groups = group_by_file(refs)
    return [fp for fp, hits in groups.items() if len(hits) >= threshold]
