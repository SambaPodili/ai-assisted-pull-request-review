"""
ingestion/diff_parser.py
-------------------------
Parses raw unified diff text into structured DiffHunk objects.
Used by both the webhook handlers and the direct analysis endpoint.
"""
from __future__ import annotations
import re
from core.models import DiffHunk
from ingestion.language_registry import detect_language as _detect_language


def parse_diff(diff_text: str) -> list[DiffHunk]:
    """
    Split a raw unified diff into per-file DiffHunk objects.

    Handles:
      - Standard git diff (diff --git a/... b/...)
      - Patch format (+++ b/... lines)
      - Binary file markers (skipped)
    """
    if not diff_text or not diff_text.strip():
        return []

    hunks:        list[DiffHunk] = []
    current_file: str            = ""
    current_lines: list[str]     = []
    additions:     int           = 0
    deletions:     int           = 0

    def _flush() -> None:
        nonlocal current_file, current_lines, additions, deletions
        if current_file and current_lines:
            hunks.append(DiffHunk(
                file_path=current_file,
                language=detect_language(current_file),
                additions=additions,
                deletions=deletions,
                content="".join(current_lines),
            ))
        current_file  = ""
        current_lines = []
        additions     = 0
        deletions     = 0

    for line in diff_text.splitlines(keepends=True):
        stripped = line.rstrip("\n")

        if stripped.startswith("+++ b/") or stripped.startswith("+++ /"):
            _flush()
            current_file = stripped[6:].strip() if stripped.startswith("+++ b/") else stripped[4:].strip()
            current_lines.append(line)
        elif stripped.startswith("--- ") or stripped.startswith("diff --git"):
            current_lines.append(line)
        elif stripped.startswith("Binary files"):
            continue   # skip binary files
        elif stripped.startswith("+") and not stripped.startswith("+++"):
            additions += 1
            current_lines.append(line)
        elif stripped.startswith("-") and not stripped.startswith("---"):
            deletions += 1
            current_lines.append(line)
        else:
            current_lines.append(line)

    _flush()
    return hunks


def detect_language(file_path: str) -> str:
    """Detect programming language from file path (extension + filename)."""
    return _detect_language(file_path)


def filter_hunks_by_language(hunks: list[DiffHunk], languages: list[str]) -> list[DiffHunk]:
    """Filter to only hunks matching specified languages."""
    return [h for h in hunks if h.language in languages]


def filter_hunks_by_path(hunks: list[DiffHunk], patterns: list[str]) -> list[DiffHunk]:
    """Exclude hunks matching any path pattern (e.g. tests, generated code)."""
    result = []
    for hunk in hunks:
        if not any(re.search(p, hunk.file_path) for p in patterns):
            result.append(hunk)
    return result


def summarise_diff(hunks: list[DiffHunk]) -> dict:
    """Return a token-efficient diff summary for context assembly."""
    return {
        "total_files":   len(hunks),
        "total_churn":   sum(h.churn for h in hunks),
        "total_additions": sum(h.additions for h in hunks),
        "total_deletions": sum(h.deletions for h in hunks),
        "languages":     list(dict.fromkeys(h.language for h in hunks)),
        "files":         [
            {"path": h.file_path, "lang": h.language, "adds": h.additions, "dels": h.deletions}
            for h in hunks
        ],
    }
