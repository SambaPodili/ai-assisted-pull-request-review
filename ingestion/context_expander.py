"""
ingestion/context_expander.py
------------------------------
Function-context expansion: give LLM agents the code AROUND the change, not just
the raw hunks.

A diff shows ±3 lines of context. Real review questions — "is this input
validated earlier in the method?", "does this branch close the resource?" —
need the enclosing function. When REPO_LOCAL_PATH points at a checkout, this
module locates each hunk's added lines in the working tree and extracts the
surrounding function (signature-bounded window), so the security/code agents
reason over the same context a human reviewer would open in the IDE.

Deterministic, read-only, capped (chars per file, files per request). Returns {}
when no repo path is configured — agents fall back to hunks-only, exactly as
before.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_FILES          = 8       # most-impactful files only (hunk order = ranked)
_MAX_CHARS_PER_FILE = 2_500
_WINDOW_UP          = 40      # max lines scanned upward for the signature
_WINDOW_DOWN        = 60      # max lines included below the match

# Function/method signature starts (Python, Java/Kotlin/C#, JS/TS)
_SIG_RE = re.compile(
    r'^\s*(?:'
    r'(?:async\s+)?def\s+\w+\s*\(|'                                     # python
    r'(?:public|private|protected|static|final|synchronized|abstract|override|internal)\b[^;=]*\([^;]*\)\s*(?:throws [\w, ]+)?\s*\{?|'  # java/c#/kotlin
    r'(?:export\s+)?(?:async\s+)?function\s+\w+\s*\(|'                  # js/ts
    r'\w+\s*[:=]\s*(?:async\s+)?\([^)]*\)\s*=>'                         # arrow fn
    r')'
)

_SKIP_FILES = re.compile(r'(?i)(\.min\.|\.lock$|\.sum$|\.png$|\.jpg$|\.pdf$|\.jar$|/node_modules/|/dist/|/target/)')


def _added_lines(hunk_content: str) -> list[str]:
    """Meaningful added lines from a hunk (skip blanks/braces — too ambiguous to locate)."""
    out = []
    for raw in hunk_content.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            s = raw[1:].strip()
            if len(s) >= 8 and s not in ("{", "}", "});"):
                out.append(s)
    return out


def _resolve(repo_path: Path, file_path: str) -> Path | None:
    """Find the changed file under the repo root (tolerates path-prefix drift)."""
    p = repo_path / file_path
    if p.is_file():
        return p
    base = file_path.replace("\\", "/").split("/")[-1]
    try:
        matches = [m for m in repo_path.rglob(base)
                   if m.is_file() and not _SKIP_FILES.search(str(m))]
    except OSError:
        return None
    if len(matches) == 1:
        return matches[0]
    # several candidates — pick the one whose tail matches most of the diff path
    want = file_path.replace("\\", "/").lower()
    best, best_len = None, 0
    for m in matches:
        s = str(m).replace("\\", "/").lower()
        for k in range(len(want), 0, -1):
            if s.endswith(want[-k:]):
                if k > best_len:
                    best, best_len = m, k
                break
    return best


def _enclosing_window(lines: list[str], hit: int) -> tuple[int, int]:
    """(start, end) line indexes of the enclosing function around `hit`."""
    start = max(0, hit - _WINDOW_UP)
    for i in range(hit, max(0, hit - _WINDOW_UP) - 1, -1):
        if _SIG_RE.match(lines[i]):
            start = i
            break
    end = min(len(lines), hit + _WINDOW_DOWN)
    # stop at the next top-level signature below (keeps one function per window)
    for j in range(hit + 1, min(len(lines), hit + _WINDOW_DOWN)):
        if _SIG_RE.match(lines[j]) and j > hit + 3:
            end = j
            break
    return start, end


def expand_function_context(hunks, repo_path: str,
                            max_files: int = _MAX_FILES,
                            max_chars_per_file: int = _MAX_CHARS_PER_FILE) -> dict[str, str]:
    """Return {file_path: enclosing-function source} for the changed files.

    Best-effort and silent: unreadable files, unmatched lines, or a missing
    repo path simply yield no context for that file.
    """
    if not repo_path:
        return {}
    root = Path(repo_path)
    if not root.is_dir():
        return {}

    out: dict[str, str] = {}
    for hunk in hunks[:max_files * 2]:
        if len(out) >= max_files:
            break
        fp = getattr(hunk, "file_path", "") or ""
        if not fp or _SKIP_FILES.search(fp) or fp in out:
            continue
        target = _resolve(root, fp)
        if target is None:
            continue
        try:
            text = target.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if not lines:
            continue

        # Locate the first added line that exists verbatim in the working tree
        hit = -1
        for added in _added_lines(getattr(hunk, "content", "") or ""):
            for i, l in enumerate(lines):
                if l.strip() == added:
                    hit = i
                    break
            if hit >= 0:
                break
        if hit < 0:
            continue

        s, e = _enclosing_window(lines, hit)
        snippet = "\n".join(f"{i + 1:5d}| {lines[i]}" for i in range(s, e))
        if len(snippet) > max_chars_per_file:
            snippet = snippet[:max_chars_per_file] + "\n…(truncated)"
        out[fp] = snippet
    return out


def format_context_block(ctx_map: dict[str, str]) -> str:
    """Render the context map as a prompt block ('' when empty)."""
    if not ctx_map:
        return ""
    parts = ["SURROUNDING CODE (full enclosing functions from the working tree — "
             "use for understanding only; report findings ONLY on changed lines):"]
    for fp, snip in ctx_map.items():
        parts.append(f"--- {fp} ---\n{snip}")
    return "\n\n".join(parts)
