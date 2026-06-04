"""
ingestion/diff_parser.py
-------------------------
Parses raw unified diff text into structured DiffHunk objects.
Used by both the webhook handlers and the direct analysis endpoint.
"""
from __future__ import annotations
import re
from typing import Iterator
from core.models import DiffHunk
from ingestion.language_registry import detect_language as _detect_language

_HUNK_HEADER = re.compile(r"^@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s*@@")


def iter_added_lines(content: str) -> Iterator[tuple[int, str]]:
    """
    Yield (source_line_number, text) for every ADDED ('+') line in a unified
    diff hunk, using the ``@@ -a,b +c,d @@`` headers to compute the TRUE
    new-file line number.

    Why this exists: naively numbering lines with enumerate() over the raw diff
    text counts the @@ headers and +/- prefixes, producing diff-relative
    positions instead of real source lines — which is why findings showed wrong
    line numbers. Context lines advance the counter; removed lines and headers
    do not appear in the new file and are skipped.
    """
    new_line = 0
    for raw in content.splitlines():
        m = _HUNK_HEADER.match(raw)
        if m:
            new_line = int(m.group(1)) - 1   # next line will be the first in this hunk
            continue
        if raw.startswith(("+++", "---", "diff ", "index ", "Binary files")):
            continue
        if raw.startswith("-"):
            continue                          # removed — not in the new file
        # added or context line both advance the new-file counter
        new_line += 1
        if raw.startswith("+"):
            yield new_line, raw[1:]


def iter_numbered_lines(content: str) -> Iterator[tuple[int, str]]:
    """
    Yield (source_line_number, raw_line_with_prefix) for every diff line that a
    scanner might inspect — added, context, AND removed — with an accurate
    new-file line number. Drop-in replacement for `enumerate(content.splitlines(), 1)`
    in static scanners that branch on the +/- prefix themselves.

    Added & context lines advance the new-file counter. Removed lines report the
    position they were removed from (counter not advanced) so a finding about a
    deleted line still anchors near the change. @@ headers and file markers are
    skipped.
    """
    new_line = 0
    for raw in content.splitlines():
        m = _HUNK_HEADER.match(raw)
        if m:
            new_line = int(m.group(1)) - 1
            continue
        if raw.startswith(("+++", "---", "diff ", "index ", "Binary files")):
            continue
        if raw.startswith("-"):
            yield new_line + 1, raw       # removed: anchor to current new-file position
            continue
        new_line += 1
        yield new_line, raw


def source_line_map(content: str) -> list[int]:
    """
    Return a list aligned 1:1 with ``content.splitlines()`` giving the new-file
    source line number for each array index. For scanners that need both the
    array index (e.g. to walk backwards for the filename) AND the true source
    line: read ``source_line_map(content)[idx]`` instead of ``idx + 1``.
    Headers and removed lines map to the nearest new-file position.
    """
    out: list[int] = []
    new_line = 0
    for raw in content.splitlines():
        m = _HUNK_HEADER.match(raw)
        if m:
            new_line = int(m.group(1)) - 1
            out.append(new_line)
            continue
        if raw.startswith(("+++", "---", "diff ", "index ", "Binary files")):
            out.append(new_line)
            continue
        if raw.startswith("-"):
            out.append(new_line + 1)   # removed — anchor to current position
            continue
        new_line += 1
        out.append(new_line)
    return out


def iter_source_lines(content: str) -> Iterator[tuple[int, str, str]]:
    """
    Yield (source_line_number, kind, text) for every line that exists in the
    NEW file, where kind is 'add' or 'context'. Headers and removed lines are
    consumed for accurate numbering but not yielded.
    """
    new_line = 0
    for raw in content.splitlines():
        m = _HUNK_HEADER.match(raw)
        if m:
            new_line = int(m.group(1)) - 1
            continue
        if raw.startswith(("+++", "---", "diff ", "index ", "Binary files")):
            continue
        if raw.startswith("-"):
            continue
        new_line += 1
        if raw.startswith("+"):
            yield new_line, "add", raw[1:]
        else:
            yield new_line, "context", raw[1:] if raw.startswith(" ") else raw


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
