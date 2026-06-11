"""
agents/taint_analysis_agent.py
--------------------------------
Multi-step taint analysis: tracks user-controlled data from source to sink.

Standard security scanners detect single-line injection:
    db.execute("SELECT * FROM t WHERE id=" + request.get("id"))  ← obvious

Taint analysis detects multi-step flows across multiple lines/files:
    Step 1:  userId = request.getParameter("userId")      ← tainted source
    Step 2:  cached = cache.store("uid", userId)          ← propagated
    Step 3:  val = cache.get("uid")                       ← still tainted
    Step 4:  db.query("SELECT * FROM users WHERE id="+val)← sink! (SQL injection)

Implemented as a three-pass algorithm over the diff:

  Pass 1: SOURCE IDENTIFICATION
    Find assignments from tainted sources (user input, env vars, file reads, etc.)
    Mark the variable as tainted.

  Pass 2: TAINT PROPAGATION
    Track assignments where right-hand side contains a tainted variable.
    Build a propagation chain: tainted → derived → further_derived

  Pass 3: SINK DETECTION
    Find usage of any tainted variable in a dangerous sink (SQL, exec, file, response)

LLM enhancement: Sonnet reviews the taint paths for false positives and adds
                 context about whether the path is actually exploitable.
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    TaintAnalysisResult, TaintPath, TaintSource, TaintSink, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent


# ── Source patterns ───────────────────────────────────────────────────────────

_SOURCES: list[tuple[re.Pattern, str]] = [
    # Web input
    (re.compile(r'(\w+)\s*=\s*request\.(?:getParameter|get|args\.get|form\.get|json\.get|data\.get)\s*\('),  'request_param'),
    (re.compile(r'(\w+)\s*=\s*req\.(?:body|params|query|headers)\b'),                                        'request_param'),
    (re.compile(r'(\w+)\s*=\s*@(?:PathVariable|RequestParam|RequestBody|RequestHeader)'),                    'request_param'),
    # Java/Spring controller signatures: @RequestParam String q / @PathVariable("id") Long id —
    # the parameter NAME is what's tainted, declared in the method signature (no assignment).
    (re.compile(r'@(?:PathVariable|RequestParam|RequestBody|RequestHeader)(?:\([^)]*\))?\s+(?:final\s+)?(?:[A-Z]\w*(?:<[^>]+>)?\s+)?(\w+)\s*[,)]'), 'request_param'),
    # Servlet API with a typed declaration: String q = request.getParameter("q")
    (re.compile(r'(\w+)\s*=\s*\w*[rR]equest\.getParameter\s*\('),                                            'request_param'),

    # Environment / config
    (re.compile(r'(\w+)\s*=\s*(?:os\.environ(?:\.get)?\s*[\[(]|os\.getenv\s*\(|System\.getenv\s*\()'),  'env_var'),
    (re.compile(r'(\w+)\s*=\s*process\.env\.\w+'),                                                       'env_var'),

    # File reads
    (re.compile(r'(\w+)\s*=\s*(?:open|file\.read|Path.*read_text|FileReader)\s*\('),                         'file_read'),
    (re.compile(r'(\w+)\s*=\s*(?:fs\.readFile|readFileSync)\s*\('),                                          'file_read'),

    # Database reads (data from DB can be tainted if not sanitised)
    (re.compile(r'(\w+)\s*=\s*(?:db|cursor|conn)\.(?:execute|fetchone|fetchall|find|findOne)\s*\('),          'db_read'),
    (re.compile(r'(\w+)\s*=\s*(?:repository|repo)\.findBy'),                                                  'db_read'),

    # CLI arguments
    (re.compile(r'(\w+)\s*=\s*(?:sys\.argv|argv|args\.parse)\b'),                                            'cli_arg'),
    (re.compile(r'(\w+)\s*=\s*(?:getopt|argparse)\b'),                                                        'cli_arg'),
]

# ── Sink patterns ─────────────────────────────────────────────────────────────

_SINKS: list[tuple[re.Pattern, str, str, RiskLevel]] = [
    # SQL injection sinks — match the function call; variable detection done separately
    (re.compile(r'(?:execute|executeQuery|executeUpdate|createQuery|nativeQuery|rawQuery|cursor\.execute)\s*\([^)]*\+'), 'sql_query', 'CWE-89', RiskLevel.CRITICAL),
    (re.compile(r'(?:db|conn|session)\.(?:query|execute|run)\s*\([^)]*\+'),                                             'sql_query', 'CWE-89', RiskLevel.CRITICAL),
    # Java: jdbcTemplate.query(sql, …) / stmt.executeQuery(sql) — the SQL is built on a
    # PRIOR line (typed declaration + concat), so no '+' appears inside the call itself.
    (re.compile(r'(?:\w*[jJ]dbc\w*|\w*[tT]emplate|stmt|statement|entityManager|em)\.(?:query\w*|execute\w*|update|batchUpdate)\s*\('),  'sql_query', 'CWE-89', RiskLevel.CRITICAL),
    (re.compile(r'f["\'].*(?:SELECT|INSERT|UPDATE|DELETE|WHERE).*\{(\w+)\}'),                                           'sql_query', 'CWE-89', RiskLevel.CRITICAL),

    # Command injection sinks
    (re.compile(r'(?:os\.system|subprocess\.(?:call|run|Popen)|exec|eval)\s*\(\s*[^)]*(\w+)'),                               'exec',      'CWE-78',  RiskLevel.CRITICAL),
    (re.compile(r'Runtime\.getRuntime\(\)\.exec\s*\(\s*[^)]*(\w+)'),                                                         'exec',      'CWE-78',  RiskLevel.CRITICAL),

    # Path traversal sinks
    (re.compile(r'(?:open|Path|File|FileInputStream|FileReader)\s*\(\s*[^)]*(\w+)'),                                         'file_write', 'CWE-22', RiskLevel.HIGH),
    (re.compile(r'(?:fs\.writeFile|createWriteStream)\s*\(\s*[^)]*(\w+)'),                                                   'file_write', 'CWE-22', RiskLevel.HIGH),

    # SSRF sinks
    (re.compile(r'(?:requests\.get|requests\.post|fetch|urllib\.request\.urlopen|HttpClient)\s*\(\s*[^)]*(\w+)'),             'http_request', 'CWE-918', RiskLevel.HIGH),

    # XSS sinks (for web-facing banking apps)
    (re.compile(r'(?:response\.write|res\.send|render_template|template\.format)\s*\(\s*[^)]*(\w+)'),                        'http_response', 'CWE-79', RiskLevel.HIGH),

    # Logging (information disclosure)
    (re.compile(r'(?:log\.|logger\.|print|console\.log|System\.out\.print)\s*\([^)]*(\w+)'),                                 'log',       'CWE-532', RiskLevel.MEDIUM),

    # Deserialization
    (re.compile(r'(?:pickle\.loads|yaml\.load\b|ObjectInputStream|fromJson|deserialize)\s*\(\s*[^)]*(\w+)'),                 'deserialization', 'CWE-502', RiskLevel.CRITICAL),
]

# ── Propagation pattern ───────────────────────────────────────────────────────

# Matches: target_var = ... (assignment pattern), including Java/TS typed
# declarations like `String sql = ...` / `final List<Row> rows = ...`.
# Use lookahead so RHS isn't consumed.
_PROPAGATION = re.compile(r'(?:(?:final\s+|const\s+|let\s+|var\s+)?[A-Za-z_][\w.<>\[\],\s]*?\s+)?(\w+)\s*=\s*(?!=)')


class TaintAnalysisAgent(BaseAgent[TaintAnalysisResult]):

    agent_name   = AgentName.TAINT_ANALYSIS
    output_model = TaintAnalysisResult

    system_prompt = (
        "You are an expert in data flow security analysis for banking applications.\n"
        "Review the taint paths identified by static analysis:\n"
        "  • Confirm or reject each path (is it actually exploitable?)\n"
        "  • Identify false positives (tainted variable is sanitised before sink)\n"
        "  • Identify multi-file paths not captured by single-file analysis\n"
        "  • Add CWE ID and severity for each confirmed path\n"
        "  • Note if sanitization/validation is present but insufficient\n\n"
        "Output ONLY compact JSON."
    )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> TaintAnalysisResult:
        """Run deterministic taint analysis, then enhance with LLM if budget allows."""
        taint_result = self._run_taint_analysis(request)

        # LLM enhancement only if we found actual paths to review
        llm_attempted = bool(taint_result.taint_paths) and budget.get_remaining("security") > 2000
        if llm_attempted:
            try:
                enhanced = super().run(request, budget, context or {})
                # Merge LLM findings
                for path in enhanced.taint_paths:
                    taint_result.taint_paths.append(path)
                taint_result.model_used    = enhanced.model_used
                taint_result.token_usage   = enhanced.token_usage
                taint_result.fallback_used = False
            except Exception:
                pass

        # Static-only path (no paths to review, or low budget) — report progress
        if not llm_attempted:
            self.report_static_progress(request, getattr(taint_result, "duration_s", 0.0))

        return taint_result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        result = self._run_taint_analysis(request)
        paths_str = "\n".join(
            f"  {i+1}. Source: {p.source.variable}={p.source.source} at {p.source.file_path}:{p.source.line}\n"
            f"     Sink: {p.sink.variable} → {p.sink.sink} at {p.sink.file_path}:{p.sink.line}\n"
            f"     Propagation: {' → '.join(p.steps)}"
            for i, p in enumerate(result.taint_paths)
        )
        diff = "\n\n".join(h.content for h in request.hunks)
        trimmed = trim_diff_for_budget(diff, max_tokens_approx=2000)
        return (
            f"Repository: {request.repo_url}\n"
            f"Detected taint paths:\n{paths_str or '  (none detected by static analysis)'}\n\n"
            f"Sources found: {result.sources_found}  Sinks found: {result.sinks_found}\n\n"
            f"Diff for context:\n{trimmed}"
        )

    def fallback_result(self, request: AnalysisRequest) -> TaintAnalysisResult:
        return self._run_taint_analysis(request)

    # ── Core taint engine ─────────────────────────────────────────────────────

    def _run_taint_analysis(self, request: AnalysisRequest) -> TaintAnalysisResult:
        """
        Three-pass taint analysis across all diff hunks.
        """
        from ingestion.diff_parser import iter_added_lines
        all_lines: list[tuple[str, str, int]] = []   # (line_content, file_path, source_line)
        for hunk in request.hunks:
            for src_line, content in iter_added_lines(hunk.content):
                all_lines.append((content, hunk.file_path, src_line))

        # ── Pass 1: find sources ──────────────────────────────────────────
        tainted: dict[str, TaintSource] = {}   # variable → TaintSource
        for content, file_path, line_no in all_lines:
            for pattern, source_kind in _SOURCES:
                m = pattern.search(content)
                if m:
                    var_name = m.group(1)
                    if var_name and var_name not in ('None', 'True', 'False', 'self', 'cls'):
                        tainted[var_name] = TaintSource(
                            file_path=file_path,
                            line=line_no,
                            variable=var_name,
                            source=source_kind,
                        )

        sources_found = len(tainted)

        # ── Pass 2: propagate taint ───────────────────────────────────────
        changed = True
        iterations = 0
        while changed and iterations < 5:
            changed = False
            iterations += 1
            for content, file_path, line_no in all_lines:
                m = _PROPAGATION.match(content.strip())
                if not m:
                    continue
                target = m.group(1)
                if target in tainted:
                    continue
                # Check if any tainted variable appears on the right side
                rhs = content[m.end():]
                for tvar in list(tainted.keys()):
                    if re.search(r'\b' + re.escape(tvar) + r'\b', rhs):
                        original = tainted[tvar]
                        tainted[target] = TaintSource(
                            file_path=file_path,
                            line=line_no,
                            variable=target,
                            source=original.source,
                        )
                        changed = True
                        break

        # ── Pass 3: find sinks ────────────────────────────────────────────
        taint_paths: list[TaintPath] = []
        sinks_found = 0

        for content, file_path, line_no in all_lines:
            # Check for sanitization/validation on this line
            if _is_sanitized(content):
                continue

            for pattern, sink_kind, cwe, severity in _SINKS:
                m = pattern.search(content)
                if not m:
                    continue
                sinks_found += 1

                # Find if any tainted variable reaches this sink
                for var_name, source in tainted.items():
                    if re.search(r'\b' + re.escape(var_name) + r'\b', content):
                        # Build propagation chain
                        steps = _build_chain(var_name, tainted, source.variable)
                        desc  = _build_description(source.source, sink_kind, cwe, var_name)
                        taint_paths.append(TaintPath(
                            source=source,
                            sink=TaintSink(
                                file_path=file_path,
                                line=line_no,
                                variable=var_name,
                                sink=sink_kind,
                            ),
                            steps=steps,
                            cwe=cwe,
                            severity=severity,
                            description=desc,
                        ))

        # Deduplicate paths (same source + sink combo)
        seen: set[tuple] = set()
        unique_paths: list[TaintPath] = []
        for path in taint_paths:
            key = (path.source.file_path, path.source.line, path.sink.file_path, path.sink.line)
            if key not in seen:
                seen.add(key)
                unique_paths.append(path)

        has_injection     = any(p.sink.sink == 'sql_query'   for p in unique_paths)
        has_traversal     = any(p.sink.sink == 'file_write'  for p in unique_paths)
        has_ssrf          = any(p.sink.sink == 'http_request' for p in unique_paths)

        return TaintAnalysisResult(
            taint_paths=unique_paths,
            sources_found=sources_found,
            sinks_found=sinks_found,
            has_injection=has_injection,
            has_path_traversal=has_traversal,
            has_ssrf=has_ssrf,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

_SANITIZERS = re.compile(
    r'(?i)(PreparedStatement|parameterized|bind_param|escape|sanitize|validate|'
    r'htmlspecialchars|encodeURIComponent|strip_tags|clean|allowlist|whitelist|'
    r'isValid|validate\w*|check\w*|verify\w*)'
)

def _is_sanitized(line: str) -> bool:
    """Returns True if the line contains a sanitization call."""
    return bool(_SANITIZERS.search(line))


def _build_chain(target: str, tainted: dict[str, TaintSource], original: str) -> list[str]:
    """Build the propagation chain from original tainted var to the sink var."""
    if target == original:
        return [original]
    return [original, "...", target]


def _build_description(source_kind: str, sink_kind: str, cwe: str, var: str) -> str:
    source_labels = {
        'request_param': 'user-controlled HTTP parameter',
        'env_var':       'environment variable',
        'file_read':     'file read operation',
        'db_read':       'database query result',
        'cli_arg':       'command-line argument',
    }
    sink_labels = {
        'sql_query':      'SQL query',
        'exec':           'OS command execution',
        'file_write':     'file path operation',
        'http_request':   'outbound HTTP request (SSRF risk)',
        'http_response':  'HTTP response (XSS risk)',
        'log':            'log output (information disclosure)',
        'deserialization':'deserialization call',
    }
    src = source_labels.get(source_kind, source_kind)
    snk = sink_labels.get(sink_kind, sink_kind)
    return f"[{cwe}] Variable '{var}' flows from {src} directly to {snk} without sanitisation."
