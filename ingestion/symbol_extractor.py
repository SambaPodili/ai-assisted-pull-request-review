"""
ingestion/symbol_extractor.py
-------------------------------
Extract the names of functions, methods, and classes that were defined or
modified in a diff hunk.

We scan both added (+) and removed (-) lines so that renamed or
signature-changed symbols are captured on both sides.

Coverage:
  Python, JavaScript, TypeScript, Java, Kotlin, Scala, Go, Rust, C, C++,
  C#, Swift, Dart, Ruby, PHP, Elixir, Haskell, Lua, R, Shell — plus a
  generic fallback for everything else.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from core.models import DiffHunk


@dataclass
class ExtractedSymbol:
    name:     str
    kind:     str   # "function" | "method" | "class" | "interface" | "trait" | "struct"
    line:     int   # 1-based line number within the hunk content
    is_added: bool  # True → from a + line; False → from a - line


# ── Per-language pattern sets ─────────────────────────────────────────────────
# Each entry: (kind, compiled_pattern)
# Group 1 must capture the symbol name.

_PYTHON = [
    ("function",  re.compile(r'^[+-]\s*(?:async\s+)?def\s+(\w+)\s*\(')),
    ("class",     re.compile(r'^[+-]\s*class\s+(\w+)\s*[:(]')),
]

_JS_TS = [
    ("function",  re.compile(r'^[+-]\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+(\w+)\s*[(<]')),
    ("class",     re.compile(r'^[+-]\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)')),
    ("function",  re.compile(r'^[+-]\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\(|[a-zA-Z_]\w*\s*=>)')),
    ("method",    re.compile(r'^[+-]\s*(?:public|private|protected|static|async|override|\s)*\s+(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::\s*\w+\s*)?\{')),
    ("interface", re.compile(r'^[+-]\s*(?:export\s+)?interface\s+(\w+)')),
]

_JAVA = [
    ("method",    re.compile(r'^[+-]\s*(?:@\w+\s+)*(?:public|private|protected|static|final|synchronized|abstract|\s)+\s+(?:<[^>]+>\s+)?(?:\w[\w.<>\[\]]*)\s+(\w+)\s*\(')),
    ("class",     re.compile(r'^[+-]\s*(?:public|private|protected|abstract|final|\s)*\s*class\s+(\w+)')),
    ("interface", re.compile(r'^[+-]\s*(?:public\s+)?interface\s+(\w+)')),
]

_KOTLIN = [
    ("function",  re.compile(r'^[+-]\s*(?:(?:public|private|protected|internal|override|suspend|inline|open|abstract)\s+)*fun\s+(\w+)\s*[(<]')),
    ("class",     re.compile(r'^[+-]\s*(?:(?:data|sealed|abstract|open|inner)\s+)*class\s+(\w+)')),
    ("interface", re.compile(r'^[+-]\s*(?:fun\s+)?interface\s+(\w+)')),
]

_SCALA = [
    ("function",  re.compile(r'^[+-]\s*(?:override\s+)?def\s+(\w+)\s*[({:]')),
    ("class",     re.compile(r'^[+-]\s*(?:case\s+|abstract\s+|sealed\s+)?class\s+(\w+)')),
    ("trait",     re.compile(r'^[+-]\s*trait\s+(\w+)')),
    ("struct",    re.compile(r'^[+-]\s*object\s+(\w+)')),
]

_GO = [
    ("function",  re.compile(r'^[+-]\s*func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*[(<(]')),
    ("struct",    re.compile(r'^[+-]\s*type\s+(\w+)\s+struct')),
    ("interface", re.compile(r'^[+-]\s*type\s+(\w+)\s+interface')),
]

_RUST = [
    ("function",  re.compile(r'^[+-]\s*(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*[<(]')),
    ("struct",    re.compile(r'^[+-]\s*(?:pub(?:\(\w+\))?\s+)?struct\s+(\w+)')),
    ("trait",     re.compile(r'^[+-]\s*(?:pub(?:\(\w+\))?\s+)?trait\s+(\w+)')),
    ("class",     re.compile(r'^[+-]\s*(?:pub(?:\(\w+\))?\s+)?enum\s+(\w+)')),
    ("function",  re.compile(r'^[+-]\s*impl\s+(?:[^{]+\s+for\s+)?(\w+)')),
]

_C_CPP = [
    ("function",  re.compile(r'^[+-]\s*(?:static\s+|inline\s+|virtual\s+|explicit\s+)*(?:[\w:*&<>\s]+)\s+(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:override\s*)?\s*[{;]')),
    ("class",     re.compile(r'^[+-]\s*(?:class|struct)\s+(\w+)')),
]

_CSHARP = [
    ("method",    re.compile(r'^[+-]\s*(?:(?:public|private|protected|internal|static|virtual|override|async|abstract|sealed)\s+)+(?:\w[\w.<>\[\]?]*\s+)+(\w+)\s*\(')),
    ("class",     re.compile(r'^[+-]\s*(?:(?:public|private|protected|internal|abstract|sealed|static|partial)\s+)*class\s+(\w+)')),
    ("interface", re.compile(r'^[+-]\s*(?:public\s+)?interface\s+(\w+)')),
]

_SWIFT = [
    ("function",  re.compile(r'^[+-]\s*(?:(?:public|private|internal|fileprivate|open|override|static|class|mutating|async)\s+)*func\s+(\w+)\s*[(<]')),
    ("class",     re.compile(r'^[+-]\s*(?:(?:public|open|final)\s+)*class\s+(\w+)')),
    ("struct",    re.compile(r'^[+-]\s*struct\s+(\w+)')),
    ("trait",     re.compile(r'^[+-]\s*protocol\s+(\w+)')),
]

_DART = [
    ("function",  re.compile(r'^[+-]\s*(?:(?:static|async|Future<\w+>|\w+)\s+)+(\w+)\s*\(')),
    ("class",     re.compile(r'^[+-]\s*(?:abstract\s+)?class\s+(\w+)')),
]

_RUBY = [
    ("function",  re.compile(r'^[+-]\s*def\s+(?:self\.)?(\w+[\?!]?)')),
    ("class",     re.compile(r'^[+-]\s*class\s+(\w+)')),
    ("function",  re.compile(r'^[+-]\s*module\s+(\w+)')),
]

_PHP = [
    ("function",  re.compile(r'^[+-]\s*(?:public|private|protected|static|abstract|\s)*\s*function\s+(\w+)\s*\(')),
    ("class",     re.compile(r'^[+-]\s*(?:abstract\s+|final\s+)?class\s+(\w+)')),
    ("interface", re.compile(r'^[+-]\s*interface\s+(\w+)')),
    ("trait",     re.compile(r'^[+-]\s*trait\s+(\w+)')),
]

_ELIXIR = [
    ("function",  re.compile(r'^[+-]\s*def(?:p)?\s+(\w+)\s*[({(]')),
    ("class",     re.compile(r'^[+-]\s*defmodule\s+([\w.]+)')),
]

_HASKELL = [
    ("function",  re.compile(r'^[+-](\w+)\s*::')),   # type signature
    ("function",  re.compile(r'^[+-](\w+)\s+\w+\s*=')),
]

_LUA = [
    ("function",  re.compile(r'^[+-]\s*(?:local\s+)?function\s+([\w.:]+)\s*\(')),
    ("function",  re.compile(r'^[+-]\s*(?:local\s+)?(\w+)\s*=\s*function\s*\(')),
]

_R = [
    ("function",  re.compile(r'^[+-]\s*(\w+)\s*<-\s*function\s*\(')),
]

_SHELL = [
    ("function",  re.compile(r'^[+-]\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{')),
]

# Generic fallback — catches most C-family and scripting language definitions
_GENERIC = [
    ("function",  re.compile(r'^[+-]\s*(?:function|def|func|fn|sub|proc|method|procedure)\s+(\w+)\s*[({(]')),
    ("class",     re.compile(r'^[+-]\s*(?:class|struct|interface|trait|protocol|module)\s+(\w+)')),
]

_LANG_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    "python":     _PYTHON,
    "javascript": _JS_TS,
    "typescript": _JS_TS,
    "java":       _JAVA,
    "kotlin":     _KOTLIN,
    "scala":      _SCALA,
    "go":         _GO,
    "rust":       _RUST,
    "c":          _C_CPP,
    "cpp":        _C_CPP,
    "csharp":     _CSHARP,
    "swift":      _SWIFT,
    "dart":       _DART,
    "ruby":       _RUBY,
    "php":        _PHP,
    "elixir":     _ELIXIR,
    "haskell":    _HASKELL,
    "lua":        _LUA,
    "r":          _R,
    "shell":      _SHELL,
}


def extract_changed_symbols(hunk: DiffHunk) -> list[ExtractedSymbol]:
    """
    Return all function/class/method definitions found in the changed lines
    of this hunk.  Both added (+) and removed (-) lines are scanned so that
    renamed or signature-changed symbols appear on both sides.
    """
    patterns = _LANG_PATTERNS.get(hunk.language, _GENERIC)
    results:  list[ExtractedSymbol] = []
    seen:     set[tuple[str, bool]] = set()

    for lineno, raw in enumerate(hunk.content.splitlines(), start=1):
        if not raw or raw[0] not in ("+", "-") or raw.startswith(("+++", "---")):
            continue
        is_added = raw[0] == "+"
        for kind, pat in patterns:
            m = pat.match(raw)
            if m:
                name = m.group(1).strip()
                if name and (name, is_added) not in seen:
                    seen.add((name, is_added))
                    results.append(ExtractedSymbol(
                        name=name, kind=kind, line=lineno, is_added=is_added
                    ))
                break   # first matching pattern wins for this line

    return results


def unique_symbol_names(hunks: list[DiffHunk]) -> list[str]:
    """Return deduplicated symbol names in extraction order."""
    names: list[str] = []
    seen:  set[str]  = set()
    for hunk in hunks:
        for sym in extract_changed_symbols(hunk):
            if sym.name not in seen:
                seen.add(sym.name)
                names.append(sym.name)
    return names


def ranked_symbol_names(hunks: list[DiffHunk], max_symbols: int = 30) -> list[str]:
    """
    Return deduplicated symbol names ranked by estimated blast radius.

    Prioritises symbols most worth tracing for large PRs:
      +3  appears in multiple hunks (widely touched)
      +2  added/new on a + line (live callers exist)
      +2  class, interface, struct, or trait (broader impact than a method)
      +1  public name (no leading underscore)
      -1  only appears on removed lines (may no longer exist)

    Falls back to extraction order when scores are equal so the result is
    deterministic.
    """
    from collections import defaultdict

    score:    dict[str, int]  = defaultdict(int)
    hunk_count: dict[str, int] = defaultdict(int)
    kind_map: dict[str, str]  = {}
    seen_added: set[str]      = set()
    seen_removed: set[str]    = set()
    order:    list[str]       = []
    seen_order: set[str]      = set()

    for hunk in hunks:
        for sym in extract_changed_symbols(hunk):
            n = sym.name
            hunk_count[n] += 1
            kind_map[n]    = sym.kind
            if n not in seen_order:
                seen_order.add(n)
                order.append(n)
            if sym.is_added:
                seen_added.add(n)
            else:
                seen_removed.add(n)

    for n in order:
        if hunk_count[n] > 1:
            score[n] += 3
        if n in seen_added:
            score[n] += 2
        if kind_map.get(n) in ("class", "interface", "struct", "trait", "enum"):
            score[n] += 2
        if not n.startswith("_"):
            score[n] += 1
        if n in seen_removed and n not in seen_added:
            score[n] -= 1  # deleted-only — callers may not exist anymore

    ranked = sorted(order, key=lambda n: (-score[n], order.index(n)))
    return ranked[:max_symbols]
