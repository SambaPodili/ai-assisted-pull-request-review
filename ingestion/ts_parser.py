"""
ingestion/ts_parser.py
-----------------------
Tree-sitter based multi-language function analysis.

Replaces regex "AST-lite" heuristics with real parsing for Java, Kotlin, C#,
JavaScript/TypeScript and Go: accurate per-function cyclomatic complexity,
nesting depth and parameter counts — the metrics the AST agent previously only
got right for Python (via the stdlib `ast` module).

Optional dependency (pip install tree-sitter tree-sitter-language-pack — prebuilt
wheels, no compiler). When absent, HAS_TREE_SITTER is False and callers fall back
to the legacy regex path unchanged. Tree-sitter is error-tolerant, so partial
diff fragments parse well enough for metrics.

NOTE: the language-pack ships Rust-style bindings (node.kind() as a method);
official py-tree-sitter uses properties (node.type). The accessors below handle
both shapes so the module works regardless of which wheel resolves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    from tree_sitter_language_pack import get_parser
    HAS_TREE_SITTER = True
except ImportError:        # pragma: no cover - environment without the wheel
    HAS_TREE_SITTER = False
    log.info("tree-sitter-language-pack not installed — AST agent uses regex fallback "
             "for non-Python languages (pip install tree-sitter tree-sitter-language-pack)")

# DiffHunk.language / file-extension → grammar name
_LANG_MAP = {
    "java": "java", ".java": "java",
    "kotlin": "kotlin", ".kt": "kotlin", ".kts": "kotlin",
    "csharp": "csharp", "c#": "csharp", "cs": "csharp", ".cs": "csharp",
    "javascript": "javascript", "js": "javascript", ".js": "javascript", ".jsx": "javascript",
    "typescript": "typescript", "ts": "typescript", ".ts": "typescript", ".tsx": "typescript",
    "go": "go", ".go": "go",
}

_FUNC_TYPES = {
    "java":       {"method_declaration", "constructor_declaration"},
    "kotlin":     {"function_declaration"},
    "csharp":     {"method_declaration", "constructor_declaration", "local_function_statement"},
    "javascript": {"function_declaration", "method_definition", "arrow_function", "function_expression"},
    "typescript": {"function_declaration", "method_definition", "arrow_function", "function_expression"},
    "go":         {"function_declaration", "method_declaration"},
}

# Decision-point node types (McCabe +1 each) — union across grammars; node
# types that don't exist in a given grammar simply never match.
_DECISION_TYPES = {
    "if_statement", "if_expression",
    "for_statement", "enhanced_for_statement", "for_in_statement", "foreach_statement",
    "while_statement", "do_statement",
    "catch_clause", "catch_block", "except_clause",
    "conditional_expression", "ternary_expression",
    "switch_block_statement_group", "switch_case", "switch_section",
    "when_entry", "expression_case", "type_case",
    "boolean_operator", "conjunction_expression", "disjunction_expression",
    "guard_statement",
}
_BOOL_OPS = {"&&", "||"}

_BLOCK_TYPES = {"block", "statement_block", "function_body", "compound_statement"}


# ── Binding compatibility (method-style Rust bindings vs property-style) ──────

def _call(v):
    return v() if callable(v) else v


def _kind(node) -> str:
    k = getattr(node, "kind", None)
    if k is None:
        k = getattr(node, "type", "")
    return _call(k) or ""


def _children(node) -> list:
    cc = _call(getattr(node, "child_count", 0))
    return [node.child(i) for i in range(cc or 0)]


def _named_children(node) -> list:
    ncc = getattr(node, "named_child_count", None)
    if ncc is not None:
        return [node.named_child(i) for i in range(_call(ncc) or 0)]
    return [c for c in _children(node) if _call(getattr(c, "is_named", False))]


def _row(point) -> int:
    if hasattr(point, "row"):
        return point.row
    return point[0]


def _start_row(node) -> int:
    sp = getattr(node, "start_position", None) or getattr(node, "start_point", None)
    return _row(_call(sp))


def _end_row(node) -> int:
    ep = getattr(node, "end_position", None) or getattr(node, "end_point", None)
    return _row(_call(ep))


def _byte_range(node) -> tuple[int, int]:
    br = getattr(node, "byte_range", None)
    if br is not None:
        r = _call(br)
        if hasattr(r, "start"):
            return r.start, getattr(r, "end", getattr(r, "stop", r.start))
        return r[0], r[1]
    return _call(node.start_byte), _call(node.end_byte)


def _text(node, src_bytes: bytes) -> str:
    s, e = _byte_range(node)
    return src_bytes[s:e].decode(errors="replace")


def _field(node, name: str):
    try:
        return node.child_by_field_name(name)
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class FunctionMetrics:
    name:        str
    start_line:  int            # 1-based, within the analysed source fragment
    end_line:    int
    complexity:  int = 1        # McCabe (1 + decision points)
    max_nesting: int = 0
    param_count: int = 0
    line_count:  int = 0
    language:    str = ""


def grammar_for(language_or_ext: str) -> str | None:
    return _LANG_MAP.get((language_or_ext or "").strip().lower())


def supported(language_or_ext: str) -> bool:
    return HAS_TREE_SITTER and grammar_for(language_or_ext) is not None


def _node_name(node, src_bytes: bytes) -> str:
    n = _field(node, "name")
    if n is not None:
        return _text(n, src_bytes)
    for ch in _children(node):
        if "identifier" in _kind(ch):
            return _text(ch, src_bytes)
    return "(anonymous)"


def _count_params(node) -> int:
    p = _field(node, "parameters")
    if p is None:
        for ch in _children(node):
            k = _kind(ch)
            if "parameter" in k and ("list" in k or k.endswith("parameters")):
                p = ch
                break
    if p is None:
        return 0
    return sum(1 for ch in _named_children(p)
               if "parameter" in _kind(ch) or _kind(ch) == "identifier")


def _walk_metrics(node, depth: int) -> tuple[int, int]:
    """(decision_points, max_nesting) within the subtree."""
    decisions = 0
    max_depth = depth
    for ch in _children(node):
        t = _kind(ch)
        if t in _DECISION_TYPES or t in _BOOL_OPS:
            decisions += 1
        d2 = depth + (1 if t in _BLOCK_TYPES else 0)
        sub_dec, sub_depth = _walk_metrics(ch, d2)
        decisions += sub_dec
        max_depth = max(max_depth, sub_depth, d2)
    return decisions, max_depth


def analyze_functions(source: str, language_or_ext: str) -> list[FunctionMetrics]:
    """Parse `source` and return metrics for every function/method found.

    Returns [] when tree-sitter is unavailable, the language is unsupported,
    or the fragment can't be parsed.
    """
    g = grammar_for(language_or_ext)
    if not HAS_TREE_SITTER or g is None or not (source or "").strip():
        return []
    try:
        parser = get_parser(g)
        try:
            tree = parser.parse(source)              # rust-style binding: str
        except TypeError:
            tree = parser.parse(source.encode())     # py-tree-sitter: bytes
        root = _call(tree.root_node)
    except Exception as exc:
        log.debug("tree-sitter parse failed (%s): %s", g, exc)
        return []

    src_bytes  = source.encode()
    func_types = _FUNC_TYPES.get(g, set())
    out: list[FunctionMetrics] = []

    def visit(node):
        if _kind(node) in func_types:
            body = _field(node, "body") or node
            decisions, nesting = _walk_metrics(body, 0)
            start, end = _start_row(node) + 1, _end_row(node) + 1
            out.append(FunctionMetrics(
                name=_node_name(node, src_bytes),
                start_line=start,
                end_line=end,
                complexity=1 + decisions,
                max_nesting=nesting,
                param_count=_count_params(node),
                line_count=end - start + 1,
                language=g,
            ))
        for ch in _children(node):
            visit(ch)

    visit(root)
    return out
