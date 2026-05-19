"""
analysis/ast_parser.py
-----------------------
Tree-sitter based AST parser for semantic code analysis.

What AST gives us that diff text cannot:
  1. Function complexity (cyclomatic complexity of CHANGED functions only)
  2. Call sites — which other functions call the changed function
  3. Null/bounds checks — is there validation before a dangerous operation?
  4. Type context — is a variable Decimal or float? (critical for financial code)
  5. Dead code — functions that are defined but never called
  6. Missing error handling — try/catch present in changed code paths?

Supported languages (graceful fallback to regex for others):
  Python, Java, JavaScript/TypeScript, Go

Requires: pip install tree-sitter tree-sitter-python tree-sitter-java
          tree-sitter-javascript tree-sitter-go
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── Optional tree-sitter imports ──────────────────────────────────────────────

_PARSERS: dict[str, Any] = {}     # language → Parser
_HAS_TS = False

try:
    from tree_sitter import Language, Parser as TSParser

    def _load_lang(mod_name: str, lang_names: list[str]) -> Any | None:
        try:
            mod = __import__(mod_name)
            return Language(mod.language())
        except Exception as e:
            log.debug("tree-sitter lang %s unavailable: %s", mod_name, e)
            return None

    _lang_map = {
        "python":     ("tree_sitter_python",     ["python"]),
        "java":       ("tree_sitter_java",        ["java"]),
        "javascript": ("tree_sitter_javascript",  ["javascript"]),
        "typescript": ("tree_sitter_typescript",  ["typescript"]),
        "go":         ("tree_sitter_go",          ["go"]),
    }

    for _lang, (_mod, _aliases) in _lang_map.items():
        _language = _load_lang(_mod, _aliases)
        if _language:
            _p = TSParser(_language)
            _PARSERS[_lang] = _p
            for _alias in _aliases:
                _PARSERS[_alias] = _p
            _HAS_TS = True

    if _HAS_TS:
        log.info("tree-sitter loaded for: %s", list({k for k in _PARSERS}))
except ImportError:
    log.info("tree-sitter not installed — using regex AST fallback")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FunctionInfo:
    name:              str
    start_line:        int
    end_line:          int
    parameters:        list[str] = field(default_factory=list)
    calls:             list[str] = field(default_factory=list)   # functions this one calls
    has_null_check:    bool = False
    has_try_except:    bool = False
    has_input_validation: bool = False
    cyclomatic_complexity: int = 1
    is_changed:        bool = False   # True if this function is in the diff


@dataclass
class ASTResult:
    language:       str
    functions:      list[FunctionInfo] = field(default_factory=list)
    changed_funcs:  list[str] = field(default_factory=list)   # names of changed functions
    call_graph:     dict[str, list[str]] = field(default_factory=dict)
    missing_null_checks: list[str] = field(default_factory=list)  # functions lacking null checks
    missing_error_handling: list[str] = field(default_factory=list)
    high_complexity: list[tuple[str, int]] = field(default_factory=list)  # (name, complexity)
    dead_functions:  list[str] = field(default_factory=list)
    parse_method:    str = "regex"   # "tree-sitter" or "regex"


# ── Tree-sitter AST parsing ───────────────────────────────────────────────────

def _ts_get_functions(node: Any, source: bytes, language: str) -> list[FunctionInfo]:
    """Walk tree-sitter AST and extract function definitions."""
    functions: list[FunctionInfo] = []

    def _walk(n: Any) -> None:
        kind = n.type
        if kind in ("function_definition", "method_declaration", "function_declaration",
                    "arrow_function", "method_definition", "func_declaration"):
            info = _extract_function(n, source, language)
            if info:
                functions.append(info)
        for child in n.children:
            _walk(child)

    _walk(node)
    return functions


def _extract_function(node: Any, source: bytes, language: str) -> FunctionInfo | None:
    name = ""
    params: list[str] = []

    for child in node.children:
        if child.type in ("identifier", "name"):
            name = source[child.start_byte:child.end_byte].decode(errors="replace")
        elif child.type in ("parameters", "formal_parameters", "parameter_list"):
            params = _extract_params(child, source)

    if not name:
        return None

    body_text = source[node.start_byte:node.end_byte].decode(errors="replace")
    calls     = _extract_calls_regex(body_text)
    complexity = _cyclomatic_complexity(body_text, language)

    has_null   = bool(re.search(r'\b(?:null|None|nil|undefined)\b.*(?:check|if|!=|is not)', body_text, re.I))
    has_try    = bool(re.search(r'\b(?:try|catch|except|rescue|recover)\b', body_text))
    has_valid  = bool(re.search(r'\b(?:validate|sanitize|sanitise|check|assert|require|guard)\b', body_text, re.I))

    return FunctionInfo(
        name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parameters=params,
        calls=calls,
        has_null_check=has_null,
        has_try_except=has_try,
        has_input_validation=has_valid,
        cyclomatic_complexity=complexity,
    )


def _extract_params(node: Any, source: bytes) -> list[str]:
    params = []
    for child in node.children:
        if child.type in ("identifier", "typed_identifier", "required_parameter"):
            params.append(source[child.start_byte:child.end_byte].decode(errors="replace").split(":")[0].strip())
    return params


# ── Cyclomatic complexity ──────────────────────────────────────────────────────

_COMPLEXITY_PATTERNS = {
    "python":     re.compile(r'\b(?:if|elif|for|while|except|and|or|assert)\b'),
    "java":       re.compile(r'\b(?:if|else if|for|while|catch|&&|\|\||case)\b'),
    "javascript": re.compile(r'\b(?:if|else if|for|while|catch|&&|\|\||case|\?)\b'),
    "typescript": re.compile(r'\b(?:if|else if|for|while|catch|&&|\|\||case|\?)\b'),
    "go":         re.compile(r'\b(?:if|for|case|select|&&|\|\|)\b'),
}

def _cyclomatic_complexity(body: str, language: str) -> int:
    pattern = _COMPLEXITY_PATTERNS.get(language, _COMPLEXITY_PATTERNS["python"])
    return 1 + len(pattern.findall(body))


# ── Call extraction ───────────────────────────────────────────────────────────

_CALL_RE = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')

def _extract_calls_regex(body: str) -> list[str]:
    keywords = {"if", "for", "while", "switch", "catch", "except", "return",
                "print", "len", "range", "str", "int", "float", "bool"}
    return list({m.group(1) for m in _CALL_RE.finditer(body)
                 if m.group(1) not in keywords})


# ── Regex-based AST fallback ──────────────────────────────────────────────────

_FUNC_PATTERNS = {
    "python":     re.compile(r'^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)\s*(?:->[^:]+)?:', re.M),
    "java":       re.compile(r'(?:public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws[^{]+)?\{', re.M),
    "javascript": re.compile(r'(?:(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>)', re.M),
    "go":         re.compile(r'func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)', re.M),
    "kotlin":     re.compile(r'fun\s+(\w+)\s*\(([^)]*)\)', re.M),
}

def _regex_get_functions(source: str, language: str) -> list[FunctionInfo]:
    pattern = _FUNC_PATTERNS.get(language, _FUNC_PATTERNS.get("java"))
    if not pattern:
        return []

    functions = []
    lines = source.splitlines()
    for m in pattern.finditer(source):
        name = m.group(1) or m.group(3) or ""
        if not name:
            continue
        start_line = source[:m.start()].count('\n') + 1
        # Rough end estimate: next function or 50 lines
        end_line = min(start_line + 50, len(lines))
        body = '\n'.join(lines[start_line-1:end_line])
        complexity = _cyclomatic_complexity(body, language)
        has_null   = bool(re.search(r'\b(?:null|None|nil)\b', body) and re.search(r'\bif\b', body))
        has_try    = bool(re.search(r'\b(?:try|catch|except)\b', body))
        has_valid  = bool(re.search(r'\b(?:validate|sanitize|check|assert)\b', body, re.I))

        functions.append(FunctionInfo(
            name=name,
            start_line=start_line,
            end_line=end_line,
            calls=_extract_calls_regex(body),
            has_null_check=has_null,
            has_try_except=has_try,
            has_input_validation=has_valid,
            cyclomatic_complexity=complexity,
        ))
    return functions


# ── Public interface ──────────────────────────────────────────────────────────

def parse_file(source_code: str, language: str, changed_lines: set[int] | None = None) -> ASTResult:
    """
    Parse source code and return structured ASTResult.

    changed_lines: set of line numbers that appear in the diff (to mark changed functions).
    """
    lang = language.lower()
    functions: list[FunctionInfo] = []
    parse_method = "regex"

    if lang in _PARSERS and _HAS_TS:
        try:
            parser = _PARSERS[lang]
            tree   = parser.parse(source_code.encode())
            functions = _ts_get_functions(tree.root_node, source_code.encode(), lang)
            parse_method = "tree-sitter"
        except Exception as exc:
            log.warning("tree-sitter parse failed for %s: %s — using regex", lang, exc)
            functions = _regex_get_functions(source_code, lang)
    else:
        functions = _regex_get_functions(source_code, lang)

    # Mark changed functions
    if changed_lines:
        for fn in functions:
            if any(fn.start_line <= ln <= fn.end_line for ln in changed_lines):
                fn.is_changed = True

    changed_funcs = [fn.name for fn in functions if fn.is_changed]
    call_graph    = {fn.name: fn.calls for fn in functions}

    # Functions called by others (to detect dead code)
    all_callees: set[str] = set()
    for calls in call_graph.values():
        all_callees.update(calls)
    func_names = {fn.name for fn in functions}

    result = ASTResult(
        language=lang,
        functions=functions,
        changed_funcs=changed_funcs,
        call_graph=call_graph,
        missing_null_checks=[fn.name for fn in functions if fn.is_changed and not fn.has_null_check and fn.parameters],
        missing_error_handling=[fn.name for fn in functions if fn.is_changed and not fn.has_try_except],
        high_complexity=[(fn.name, fn.cyclomatic_complexity) for fn in functions
                         if fn.is_changed and fn.cyclomatic_complexity >= 10],
        dead_functions=[fn.name for fn in functions
                        if fn.name not in all_callees and not fn.name.startswith(("test_", "Test", "main", "__"))],
        parse_method=parse_method,
    )
    return result


def extract_changed_lines(diff_content: str) -> dict[str, set[int]]:
    """
    Extract the set of added/changed line numbers per file from a unified diff.
    Returns: {file_path: {line_numbers}}
    """
    result: dict[str, set[int]] = {}
    current_file = ""
    current_line = 0

    for raw in diff_content.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:].strip()
            result.setdefault(current_file, set())
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            if current_file:
                result[current_file].add(current_line)
        elif not raw.startswith("-"):
            current_line += 1

    return result
