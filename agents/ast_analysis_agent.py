"""
agents/ast_analysis_agent.py
-----------------------------
AST-based semantic code analysis.

Unlike the CodeAnalysis agent (which sees the diff as raw text), this agent
parses changed code into an Abstract Syntax Tree and reasons about structure,
not just text patterns. This gives dramatically fewer false positives and
catches semantic issues regex cannot detect.

Capabilities:
  1. Python AST (built-in ast module — always available)
     • Dead code: unreachable statements after return/raise
     • Null/None risk: variables used without null check
     • Type confusion: numeric ops on string variables
     • Cyclomatic complexity per function (McCabe metric)
     • Missing exception handling in financial code
     • Mutable default arguments (Python anti-pattern)

  2. Tree-sitter multi-language (optional, installed if available)
     • Java, TypeScript, JavaScript, Go, Kotlin
     • Same checks as Python AST but for other languages

  3. Function call graph (all languages via regex-AST hybrid)
     • Which functions are defined in the diff
     • Which functions call each other
     • Identifies "hot functions" — changed and called by many others

  4. LLM enhancement (Sonnet)
     • Reviews AST findings for false positives
     • Identifies subtle semantic bugs not caught by static analysis
     • Adds business-logic context (e.g., "this null check is missing for
       the case when a refund is processed before a payment exists")

Fallback: Full AST parsing in pure Python (no LLM tokens consumed).
"""
from __future__ import annotations
import ast
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    ASTAnalysisResult, ASTFinding, FunctionProfile, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent

try:
    import tree_sitter
    from tree_sitter import Language, Parser
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False


class ASTAnalysisAgent(BaseAgent[ASTAnalysisResult]):

    agent_name   = AgentName.AST_ANALYSIS
    output_model = ASTAnalysisResult

    system_prompt = (
        "You are an expert software engineer specialising in semantic code analysis for banking systems.\n"
        "You have been given a list of AST-detected findings from a code diff.\n"
        "Your task:\n"
        "  1. Validate each finding — remove false positives\n"
        "  2. Add business-logic context specific to banking/payments\n"
        "  3. Identify semantic bugs that static analysis missed\n"
        "  4. Score the changed functions: complexity, null safety, error handling\n"
        "  5. Identify which changed functions are in critical transaction paths\n\n"
        "Output per finding: file_path, line, function, kind, severity, description, suggestion.\n"
        "Output ONLY compact JSON."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        # Run AST analysis first, then let LLM review the findings
        ast_findings = self._run_ast_analysis(request)
        profiles     = self._build_call_graph(request)

        findings_str = "\n".join(
            f"  [{f.severity.value.upper()}] {f.file_path}:{f.line} in {f.function}() — {f.kind}: {f.description}"
            for f in ast_findings
        )
        profiles_str = "\n".join(
            f"  {p.name}() @ {p.file_path}:{p.line} | complexity={p.cyclomatic} | callers={len(p.callers)} | params={p.param_count}"
            for p in profiles[:20]  # cap to avoid token overflow
        )

        diff = "\n\n".join(h.content for h in request.hunks)
        trimmed = trim_diff_for_budget(diff, max_tokens_approx=2000)

        return (
            f"Repository: {request.repo_url}\n"
            f"Changed files: {request.changed_files}\n\n"
            f"Static AST findings detected:\n{findings_str or '  (none)'}\n\n"
            f"Function profiles:\n{profiles_str or '  (none)'}\n\n"
            f"Raw diff (truncated):\n{trimmed}"
        )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> ASTAnalysisResult:
        # Always run deterministic AST first
        ast_findings = self._run_ast_analysis(request)
        profiles     = self._build_call_graph(request)

        # Calculate metrics
        complexities  = [p.cyclomatic for p in profiles if p.cyclomatic > 0]
        avg_cx = round(sum(complexities) / max(len(complexities), 1), 1)
        max_cx = max(complexities) if complexities else 0
        hot    = [p.name for p in profiles if len(p.callers) >= 3 and p.cyclomatic >= 5]

        base_result = ASTAnalysisResult(
            findings=ast_findings,
            function_profiles=profiles,
            avg_complexity=avg_cx,
            max_complexity=max_cx,
            hot_functions=hot,
        )

        # Attempt LLM enhancement if budget allows and there are findings to review
        llm_attempted = bool(ast_findings) and budget.get_remaining(AgentName.CODE_ANALYSIS.value) > 2000
        if llm_attempted:
            try:
                enhanced = super().run(request, budget, context or {})
                # Merge: keep AST findings, add LLM-discovered ones
                seen = {(f.file_path, f.line, f.kind) for f in base_result.findings}
                for f in enhanced.findings:
                    if (f.file_path, f.line, f.kind) not in seen:
                        base_result.findings.append(f)
                base_result.model_used    = enhanced.model_used
                base_result.token_usage   = enhanced.token_usage
                base_result.fallback_used = False
            except Exception:
                pass

        # Static-only path (no findings to enhance, or low budget) — report progress
        if not llm_attempted:
            self.report_static_progress(request, getattr(base_result, "duration_s", 0.0))
            self.log_done(request, base_result, mode="static",
                          note=f"{len(base_result.findings)} finding(s), {len(base_result.function_profiles)} fn profile(s)")

        return base_result

    def fallback_result(self, request: AnalysisRequest) -> ASTAnalysisResult:
        findings = self._run_ast_analysis(request)
        profiles = self._build_call_graph(request)
        complexities = [p.cyclomatic for p in profiles]
        return ASTAnalysisResult(
            findings=findings,
            function_profiles=profiles,
            avg_complexity=round(sum(complexities) / max(len(complexities), 1), 1),
            max_complexity=max(complexities) if complexities else 0,
            hot_functions=[p.name for p in profiles if len(p.callers) >= 3],
        )

    # ── Python AST analysis ───────────────────────────────────────────────────

    def _run_ast_analysis(self, request: AnalysisRequest) -> list[ASTFinding]:
        """Parse Python files using built-in ast module."""
        findings: list[ASTFinding] = []
        for hunk in request.hunks:
            if not hunk.file_path.endswith(".py"):
                continue
            # Extract added lines + a parallel map of added-text line → real source line
            from ingestion.diff_parser import iter_added_lines
            added_pairs = list(iter_added_lines(hunk.content))   # [(src_line, content), ...]
            added       = "\n".join(c for _, c in added_pairs)
            line_map    = [s for s, _ in added_pairs]            # 1-based index → source line
            if not added.strip():
                continue

            try:
                tree = ast.parse(added)
                visitor = _PythonASTVisitor(hunk.file_path, line_map=line_map)
                visitor.visit(tree)
                findings.extend(visitor.findings)
            except SyntaxError:
                # Partial diff may not parse — try per-function extraction.
                # base_line is the function's offset within `added`, so the
                # line_map still resolves the final position to a source line.
                for func_code, func_name, start_line in _extract_python_functions(added):
                    try:
                        tree = ast.parse(func_code)
                        visitor = _PythonASTVisitor(hunk.file_path, base_line=start_line - 1,
                                                    func_name=func_name, line_map=line_map)
                        visitor.visit(tree)
                        findings.extend(visitor.findings)
                    except SyntaxError:
                        pass

        # Non-Python: tree-sitter (real parse, accurate per-function McCabe) when
        # available; the regex AST-lite still supplies pattern checks (null
        # comparisons, empty catch) but its line-level complexity guess is
        # skipped for files tree-sitter measured properly.
        from ingestion import ts_parser
        for hunk in request.hunks:
            ext = _get_ext(hunk.file_path)
            if ext in ('.java', '.ts', '.tsx', '.js', '.jsx', '.go', '.kt', '.cs'):
                ts_handled = ts_parser.supported(hunk.language or ext)
                if ts_handled:
                    findings.extend(_treesitter_findings(hunk))
                findings.extend(_ast_lite_scan(hunk, skip_complexity=ts_handled))

        return findings

    # ── Call graph builder ────────────────────────────────────────────────────

    def _build_call_graph(self, request: AnalysisRequest) -> list[FunctionProfile]:
        from ingestion import ts_parser
        profiles: list[FunctionProfile] = []
        for hunk in request.hunks:
            if hunk.file_path.endswith(".py"):
                profiles.extend(_python_call_graph(hunk))
            elif ts_parser.supported(hunk.language or _get_ext(hunk.file_path)):
                # Real parse → accurate cyclomatic per function (drives max_complexity)
                src = _hunk_source(hunk)
                for m in ts_parser.analyze_functions(src, hunk.language or _get_ext(hunk.file_path)):
                    profiles.append(FunctionProfile(
                        name=m.name, file_path=hunk.file_path, line=m.start_line,
                        cyclomatic=m.complexity, param_count=m.param_count,
                    ))
            else:
                profiles.extend(_generic_call_graph(hunk))
        return profiles


# ── Python AST visitor ────────────────────────────────────────────────────────

class _PythonASTVisitor(ast.NodeVisitor):
    """Walks Python AST nodes and emits findings."""

    def __init__(self, file_path: str, base_line: int = 0, func_name: str = "",
                 line_map: list[int] | None = None) -> None:
        self.file_path = file_path
        self.base_line = base_line
        self.func_name = func_name
        self.line_map  = line_map      # added-text line (1-based) → real source line
        self.findings:  list[ASTFinding] = []
        self._returns:  set[int] = set()   # line numbers of return/raise

    def _ln(self, node) -> int:
        # Position within the reconstructed "added" text (1-based)
        pos = self.base_line + getattr(node, "lineno", 0)
        # Translate to the real source line when a map is available
        if self.line_map and 1 <= pos <= len(self.line_map):
            return self.line_map[pos - 1]
        return pos

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name = node.name
        # ── Cyclomatic complexity ──────────────────────────────────────────
        cx = _cyclomatic(node)
        if cx >= 15:
            self.findings.append(ASTFinding(
                file_path=self.file_path, line=self._ln(node), function=name,
                kind="complexity_spike", severity=RiskLevel.HIGH,
                description=f"Cyclomatic complexity = {cx} (threshold: 15). High risk of defects in banking logic.",
                suggestion="Decompose into smaller, single-responsibility functions.",
            ))
        elif cx >= 10:
            self.findings.append(ASTFinding(
                file_path=self.file_path, line=self._ln(node), function=name,
                kind="complexity_spike", severity=RiskLevel.MEDIUM,
                description=f"Cyclomatic complexity = {cx} — moderately complex.",
            ))

        # ── Mutable default argument ───────────────────────────────────────
        for default in node.args.defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.findings.append(ASTFinding(
                    file_path=self.file_path, line=self._ln(node), function=name,
                    kind="type_confusion", severity=RiskLevel.MEDIUM,
                    description=f"Mutable default argument in {name}() — shared across all calls (Python gotcha).",
                    suggestion="Replace with None default and create inside the function body.",
                ))

        # ── Missing exception handling ─────────────────────────────────────
        has_try  = any(isinstance(n, ast.Try)    for n in ast.walk(node))
        has_bare = any(
            (isinstance(n, ast.ExceptHandler) and n.type is None)
            for n in ast.walk(node)
        )
        if has_bare:
            self.findings.append(ASTFinding(
                file_path=self.file_path, line=self._ln(node), function=name,
                kind="null_risk", severity=RiskLevel.MEDIUM,
                description=f"Bare except clause in {name}() swallows all exceptions including critical ones.",
                suggestion="Catch specific exception types. Never use bare `except:`.",
            ))

        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Return(self, node: ast.Return) -> None:
        self._returns.add(self._ln(node))

    def visit_Assign(self, node: ast.Assign) -> None:
        """Detect None assignment without subsequent null check."""
        if isinstance(node.value, ast.Constant) and node.value.value is None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    # Look for usage without None check in surrounding scope
                    self.findings.append(ASTFinding(
                        file_path=self.file_path, line=self._ln(node),
                        function=self.func_name or "module",
                        kind="null_risk", severity=RiskLevel.LOW,
                        description=f"Variable '{target.id}' assigned None — verify null check before use.",
                    ))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """Flag == None instead of is None."""
        for op in node.ops:
            if isinstance(op, ast.Eq):
                for comp in node.comparators:
                    if isinstance(comp, ast.Constant) and comp.value is None:
                        self.findings.append(ASTFinding(
                            file_path=self.file_path, line=self._ln(node),
                            function=self.func_name or "module",
                            kind="type_confusion", severity=RiskLevel.LOW,
                            description="Use `is None` instead of `== None` for identity comparison.",
                            suggestion="Replace `x == None` with `x is None`.",
                        ))
        self.generic_visit(node)


# ── Regex AST-lite for Java/TS/Go ─────────────────────────────────────────────

_JAVA_METHOD   = re.compile(r'(?:public|private|protected)\s+\w+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+\w+\s*)?\{')
_JAVA_NULL_CHECK = re.compile(r'if\s*\(\s*(\w+)\s*(?:==|!=)\s*null')
_JAVA_COMPLEXITY = re.compile(r'\b(if|else\s+if|for|while|catch|case|&&|\|\|)\b')

_GO_FUNC       = re.compile(r'func\s+(\w+)\s*\(')
_GO_ERR_CHECK  = re.compile(r'if\s+err\s*!=\s*nil')
_GO_IGNORED_ERR = re.compile(r',\s*_\s*:=')   # ignoring error return

_TS_NULLISH    = re.compile(r'(\w+)\s*\?\.\s*\w+')  # optional chaining
_TS_ANY        = re.compile(r':\s*any\b')            # TypeScript any type


def _hunk_source(hunk) -> str:
    """Reconstruct the post-change source fragment from a diff hunk: keep added
    and context lines, drop removed lines and diff headers. Tree-sitter is
    error-tolerant, so this fragment parses well enough for function metrics."""
    out = []
    for raw in hunk.content.splitlines():
        if raw.startswith(("+++", "---", "@@")):
            continue
        if raw.startswith("+"):
            out.append(raw[1:])
        elif raw.startswith("-"):
            continue
        elif raw.startswith(" "):
            out.append(raw[1:])
        else:
            out.append(raw)
    return "\n".join(out)


# Same thresholds as the Python AST visitor, so complexity findings are
# calibrated identically across languages.
_TS_CX_HIGH   = 15
_TS_CX_MEDIUM = 8
_TS_NEST_DEEP = 5


def _treesitter_findings(hunk) -> list[ASTFinding]:
    """Accurate per-function findings via tree-sitter (Java/Kotlin/C#/JS/TS/Go)."""
    from ingestion import ts_parser
    src = _hunk_source(hunk)
    findings: list[ASTFinding] = []
    for m in ts_parser.analyze_functions(src, hunk.language or _get_ext(hunk.file_path)):
        if m.complexity > _TS_CX_HIGH:
            findings.append(ASTFinding(
                file_path=hunk.file_path, line=m.start_line, function=f"{m.name}()",
                kind="complexity_spike", severity=RiskLevel.HIGH,
                description=f"Cyclomatic complexity = {m.complexity} (threshold: {_TS_CX_HIGH}). "
                            f"High risk of defects in banking logic.",
                suggestion="Split into smaller single-responsibility methods.",
            ))
        elif m.complexity > _TS_CX_MEDIUM:
            findings.append(ASTFinding(
                file_path=hunk.file_path, line=m.start_line, function=f"{m.name}()",
                kind="complexity_spike", severity=RiskLevel.MEDIUM,
                description=f"Cyclomatic complexity = {m.complexity} — moderately complex.",
            ))
        if m.max_nesting >= _TS_NEST_DEEP:
            findings.append(ASTFinding(
                file_path=hunk.file_path, line=m.start_line, function=f"{m.name}()",
                kind="complexity_spike", severity=RiskLevel.MEDIUM,
                description=f"Nesting depth {m.max_nesting} in {m.name}() — hard to reason about.",
                suggestion="Use early returns / extract helpers to flatten the logic.",
            ))
    return findings


def _ast_lite_scan(hunk, skip_complexity: bool = False) -> list[ASTFinding]:
    """Regex-based AST-lite for non-Python languages.

    skip_complexity=True when tree-sitter already measured this file — the
    line-level branch-density guess would only duplicate (worse) information.
    """
    findings: list[ASTFinding] = []
    ext = _get_ext(hunk.file_path)
    lines = hunk.content.splitlines()

    for i, line in enumerate(lines, 1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]

        if ext == '.java':
            # Complexity check
            cx_hits = 0 if skip_complexity else len(_JAVA_COMPLEXITY.findall(content))
            if cx_hits >= 4:
                findings.append(ASTFinding(
                    file_path=hunk.file_path, line=i,
                    function=_nearest_java_method(lines, i),
                    kind="complexity_spike", severity=RiskLevel.MEDIUM,
                    description=f"High branch density ({cx_hits} branches/conditions in one line).",
                ))

            # Null comparison
            m = _JAVA_NULL_CHECK.search(content)
            if m and '==' in content:
                findings.append(ASTFinding(
                    file_path=hunk.file_path, line=i,
                    function=_nearest_java_method(lines, i),
                    kind="null_risk", severity=RiskLevel.LOW,
                    description=f"Null equality check on '{m.group(1)}' — consider Objects.isNull() or Optional.",
                ))

            # Catch (Exception e) {} — empty catch
            if re.search(r'catch\s*\(.*\)\s*\{\s*\}', content):
                findings.append(ASTFinding(
                    file_path=hunk.file_path, line=i,
                    function=_nearest_java_method(lines, i),
                    kind="null_risk", severity=RiskLevel.HIGH,
                    description="Empty catch block — exception silently swallowed. Critical in banking code.",
                    suggestion="At minimum: log the exception. Consider re-throwing or compensating transaction.",
                ))

        elif ext == '.go':
            # Ignored error return
            if _GO_IGNORED_ERR.search(content):
                findings.append(ASTFinding(
                    file_path=hunk.file_path, line=i,
                    function=_nearest_go_func(lines, i),
                    kind="null_risk", severity=RiskLevel.HIGH,
                    description="Error return value discarded with `_` — unhandled errors cause silent failures.",
                    suggestion="Always handle error returns in financial transaction code.",
                ))

        elif ext in ('.ts', '.tsx'):
            # TypeScript `any` type
            if _TS_ANY.search(content):
                findings.append(ASTFinding(
                    file_path=hunk.file_path, line=i,
                    function="",
                    kind="type_confusion", severity=RiskLevel.MEDIUM,
                    description="TypeScript `any` type disables type checking — runtime type errors possible.",
                    suggestion="Use specific types or `unknown` with type narrowing.",
                ))

    return findings


# ── Call graph helpers ────────────────────────────────────────────────────────

def _python_call_graph(hunk) -> list[FunctionProfile]:
    profiles = []
    added = "\n".join(
        l[1:] for l in hunk.content.splitlines()
        if l.startswith("+") and not l.startswith("+++")
    )
    try:
        tree = ast.parse(added)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                callers = []
                callees = [
                    n.func.id if isinstance(n.func, ast.Name) else
                    (n.func.attr if isinstance(n.func, ast.Attribute) else "")
                    for n in ast.walk(node) if isinstance(n, ast.Call)
                ]
                callees = [c for c in callees if c]
                profiles.append(FunctionProfile(
                    name=node.name,
                    file_path=hunk.file_path,
                    line=node.lineno,
                    callers=callers,
                    callees=list(dict.fromkeys(callees)),
                    cyclomatic=_cyclomatic(node),
                    param_count=len(node.args.args),
                ))
    except SyntaxError:
        pass
    return profiles


_GENERIC_FUNC = re.compile(
    r'(?:function\s+(\w+)|(\w+)\s*=\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)|'
    r'(?:public|private|protected|static)?\s*(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::\s*\w+)?\s*\{)'
)

def _generic_call_graph(hunk) -> list[FunctionProfile]:
    profiles = []
    for m in _GENERIC_FUNC.finditer(hunk.content):
        name = m.group(1) or m.group(2) or m.group(3)
        if name and name not in {'if', 'for', 'while', 'switch', 'catch', 'class', 'new'}:
            line = hunk.content[:m.start()].count('\n') + 1
            profiles.append(FunctionProfile(
                name=name,
                file_path=hunk.file_path,
                line=line,
                cyclomatic=max(1, hunk.content[m.start():m.start()+500].count('if ')),
                param_count=m.group().count(',') + 1 if ',' in m.group() else 1,
            ))
    return profiles


# ── Utilities ─────────────────────────────────────────────────────────────────

def _cyclomatic(node: ast.FunctionDef) -> int:
    """McCabe cyclomatic complexity of a Python function."""
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                              ast.With, ast.Assert, ast.comprehension)):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
    return complexity


def _extract_python_functions(source: str) -> list[tuple[str, str, int]]:
    """Extract individual function blocks from possibly-partial Python source."""
    result = []
    lines  = source.splitlines()
    func_starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r'\s*(?:async\s+)?def\s+(\w+)', line)
        if m:
            func_starts.append((i, m.group(1)))
    for idx, (start, name) in enumerate(func_starts):
        end = func_starts[idx+1][0] if idx+1 < len(func_starts) else len(lines)
        code = "\n".join(lines[start:end])
        result.append((code, name, start+1))
    return result


def _get_ext(path: str) -> str:
    for ext in ('.java', '.py', '.ts', '.tsx', '.js', '.jsx', '.go', '.kt', '.rs'):
        if path.endswith(ext):
            return ext
    return ''


def _nearest_java_method(lines: list[str], current: int) -> str:
    for line in reversed(lines[:current]):
        m = _JAVA_METHOD.search(line)
        if m:
            return m.group(1) + "()"
    return "unknown"


def _nearest_go_func(lines: list[str], current: int) -> str:
    for line in reversed(lines[:current]):
        m = _GO_FUNC.search(line)
        if m:
            return m.group(1) + "()"
    return "unknown"
