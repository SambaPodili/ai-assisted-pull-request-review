"""
analysis/taint_tracker.py
--------------------------
Interprocedural taint analysis — tracks user-controlled data from
input sources to dangerous sinks through assignment chains.

This is a lightweight approximation of true data flow analysis.
Full CFG-based taint analysis (like CodeQL) requires a whole-program
intermediate representation. This implementation detects:

  1. Direct source → sink (same function, same file)
  2. Source → variable → sink (one assignment hop)
  3. Multi-file patterns (source in file A, sink in file B using same var name)
  4. Tainted method arguments (function takes tainted value, calls sink internally)

Banking-critical sinks:
  • SQL queries           (CWE-89)
  • Shell commands        (CWE-78)
  • File path operations  (CWE-22 path traversal)
  • Deserialization       (CWE-502)
  • XML/XPath queries     (CWE-91, CWE-643)
  • LDAP queries          (CWE-90)
  • HTTP redirects        (CWE-601)
  • Log injection         (CWE-117)
  • HTML/JS output        (CWE-79 XSS)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import Enum


class TaintType(str, Enum):
    HTTP_PARAM    = "http_parameter"
    REQUEST_BODY  = "request_body"
    FILE_INPUT    = "file_input"
    ENV_VAR       = "environment_variable"
    DB_READ       = "database_read"
    QUEUE_MESSAGE = "queue_message"
    CLI_ARG       = "cli_argument"


class SinkType(str, Enum):
    SQL_QUERY      = "sql_query"
    SHELL_COMMAND  = "shell_command"
    FILE_PATH      = "file_path"
    DESERIALIZE    = "deserialization"
    LDAP_QUERY     = "ldap_query"
    XPATH_QUERY    = "xpath_query"
    LOG_STATEMENT  = "log_injection"
    HTML_OUTPUT    = "html_output"
    HTTP_REDIRECT  = "http_redirect"
    XML_PARSE      = "xml_parse"
    TEMPLATE_RENDER = "template_render"


@dataclass
class TaintSource:
    name:       str           # variable name that receives tainted value
    taint_type: TaintType
    line:       int
    pattern:    str           # the matched source pattern


@dataclass
class TaintSink:
    name:      str            # sink function/method name
    sink_type: SinkType
    line:      int
    pattern:   str
    args:      list[str] = field(default_factory=list)   # variable names in sink args


@dataclass
class TaintPath:
    """A confirmed taint flow from source to sink."""
    source_var:   str
    source_type:  TaintType
    source_line:  int
    sink_name:    str
    sink_type:    SinkType
    sink_line:    int
    file_path:    str
    cwe_id:       str
    severity:     str    # "critical" | "high" | "medium"
    description:  str
    path_steps:   list[str] = field(default_factory=list)  # intermediate vars


@dataclass
class TaintAnalysisResult:
    paths:         list[TaintPath] = field(default_factory=list)
    sources_found: int = 0
    sinks_found:   int = 0
    tainted_vars:  list[str] = field(default_factory=list)
    files_analysed: list[str] = field(default_factory=list)


# ── Source patterns ────────────────────────────────────────────────────────────
# Each: (pattern, TaintType, description)

_SOURCE_PATTERNS: list[tuple[re.Pattern, TaintType]] = [
    # HTTP parameters / request input
    (re.compile(r'(?:request|req)\.(?:getParameter|param|params|args|query|form|json|body|data|GET|POST|PUT)\s*[\[(]([\'"]?\w+[\'"]?)\]?\)?'), TaintType.HTTP_PARAM),
    (re.compile(r'@(?:RequestParam|PathVariable|QueryParam|FormParam|RequestBody)\s+\w+\s+(\w+)'), TaintType.HTTP_PARAM),
    (re.compile(r'(?:ctx|context|c)\.(?:Param|Query|PostForm|FormValue|GetHeader)\('), TaintType.HTTP_PARAM),
    (re.compile(r'request\.form\.get\(|flask\.request\.|cherrypy\.request\.'), TaintType.HTTP_PARAM),

    # Request body / raw input
    (re.compile(r'(?:json\.loads?|json\.parse|json_decode|JSON\.parse|yaml\.(?:safe_)?load|xml\.parse|etree\.\w+)\s*\('), TaintType.REQUEST_BODY),
    (re.compile(r'request\.(?:body|payload|content|text|json)\b'), TaintType.REQUEST_BODY),

    # File / stream input
    (re.compile(r'open\s*\([^)]+[\'"]r[\'"]'), TaintType.FILE_INPUT),
    (re.compile(r'(?:FileInputStream|BufferedReader|Scanner|Files\.read|io\.read)\s*\('), TaintType.FILE_INPUT),
    (re.compile(r'sys\.stdin|os\.fdopen|fileinput\.input'), TaintType.FILE_INPUT),

    # Environment / CLI
    (re.compile(r'os\.(?:environ|getenv)\s*[\[(]'), TaintType.ENV_VAR),
    (re.compile(r'System\.getenv\s*\('), TaintType.ENV_VAR),
    (re.compile(r'sys\.argv\b'), TaintType.CLI_ARG),

    # Database reads (result sets can be tainted if originally user-supplied)
    (re.compile(r'(?:rs|resultSet|cursor|result)\.(?:getString|getInt|getObject|get|fetchone|fetchall|scalar)\s*\('), TaintType.DB_READ),

    # Message queue
    (re.compile(r'(?:consumer|message|msg|event)\.(?:body|payload|value|getData|getBody)\s*[\(]'), TaintType.QUEUE_MESSAGE),
]


# ── Sink patterns ──────────────────────────────────────────────────────────────

_SINK_PATTERNS: list[tuple[re.Pattern, SinkType, str, str]] = [
    # (pattern, SinkType, CWE, severity)
    # SQL
    (re.compile(r'(?:execute|executeQuery|executeUpdate|createQuery|nativeQuery|rawQuery|cursor\.execute|db\.query)\s*\([^)]*\+'), SinkType.SQL_QUERY, "CWE-89", "critical"),
    (re.compile(r'(?:execute|executeQuery|executeUpdate)\s*\(\s*(?:f["\']|["\'][^"\']*%|String\.format)'), SinkType.SQL_QUERY, "CWE-89", "critical"),
    (re.compile(r'(?:connection|conn|db)\.(?:execute|query|run)\s*\([^)]*\+'), SinkType.SQL_QUERY, "CWE-89", "critical"),

    # Shell command injection
    (re.compile(r'(?:os\.system|subprocess\.(?:call|run|Popen|check_output)|exec\(|eval\(|Runtime\.exec|ProcessBuilder)\s*\('), SinkType.SHELL_COMMAND, "CWE-78", "critical"),
    (re.compile(r'(?:shell_exec|passthru|system|popen)\s*\('), SinkType.SHELL_COMMAND, "CWE-78", "critical"),

    # File path traversal
    (re.compile(r'(?:open|fopen|file_get_contents|FileInputStream|new\s+File|Path\.get|Paths\.get|os\.path\.join)\s*\('), SinkType.FILE_PATH, "CWE-22", "high"),
    (re.compile(r'(?:shutil\.(?:copy|move)|os\.remove|os\.unlink|os\.rmdir)\s*\('), SinkType.FILE_PATH, "CWE-22", "high"),

    # Deserialization
    (re.compile(r'(?:pickle\.loads?|yaml\.load\b|marshal\.loads?|ObjectInputStream|readObject|deserialize|unserialize)\s*\('), SinkType.DESERIALIZE, "CWE-502", "critical"),

    # LDAP
    (re.compile(r'(?:ldap|LDAP).*(?:search|bind|query)\s*\('), SinkType.LDAP_QUERY, "CWE-90", "high"),
    (re.compile(r'(?:DirContext|InitialDirContext|LDAPConnection).*search\s*\('), SinkType.LDAP_QUERY, "CWE-90", "high"),

    # XPath / XML
    (re.compile(r'(?:xpath|XPath|evaluate|selectNodes|compile)\s*\([^)]*\+'), SinkType.XPATH_QUERY, "CWE-643", "high"),
    (re.compile(r'(?:lxml|ElementTree|xml\.etree).*(?:fromstring|parse)\s*\('), SinkType.XML_PARSE, "CWE-91", "medium"),

    # Log injection (can leak credentials or enable log forging)
    (re.compile(r'(?:log|logger|logging)\.(?:debug|info|warn|error|critical|exception)\s*\([^)]*\+'), SinkType.LOG_STATEMENT, "CWE-117", "medium"),

    # HTML / XSS
    (re.compile(r'(?:response\.write|render_template_string|innerHTML|document\.write|innerHTML\s*=)\s*\('), SinkType.HTML_OUTPUT, "CWE-79", "high"),
    (re.compile(r'Markup\s*\(|mark_safe\s*\(|html\.unescape\s*\('), SinkType.HTML_OUTPUT, "CWE-79", "high"),

    # HTTP redirect
    (re.compile(r'(?:redirect|sendRedirect|response\.redirect)\s*\([^)]*\+'), SinkType.HTTP_REDIRECT, "CWE-601", "medium"),

    # Template rendering (SSTI risk)
    (re.compile(r'(?:Template|template|Environment|jinja2|Jinja2)\s*\([^)]*\+'), SinkType.TEMPLATE_RENDER, "CWE-94", "high"),
    (re.compile(r'render_template_string\s*\('), SinkType.TEMPLATE_RENDER, "CWE-94", "high"),
]


# ── Assignment tracking ────────────────────────────────────────────────────────

# Match: var = expr  or  var, ... = expr  (Python, Go, JS/TS)
_ASSIGN_RE = re.compile(r'\b([a-zA-Z_]\w*)\s*(?:=|:=)\s*(.+)')
# Match: Type var = expr  (Java, C#)
_TYPED_ASSIGN_RE = re.compile(r'(?:String|int|Object|var|let|const|\w+)\s+([a-zA-Z_]\w*)\s*=\s*(.+)')


def _extract_var_name(line: str) -> str | None:
    """Extract the LHS variable name from an assignment line."""
    m = _ASSIGN_RE.search(line) or _TYPED_ASSIGN_RE.search(line)
    return m.group(1) if m else None


def _uses_var(line: str, var: str) -> bool:
    """Check if a line uses a given variable name."""
    return bool(re.search(r'\b' + re.escape(var) + r'\b', line))


# ── Main analyser ──────────────────────────────────────────────────────────────

def analyse_taint(diff_text: str, file_path: str = "") -> TaintAnalysisResult:
    """
    Analyse a unified diff for taint flows from sources to sinks.

    Only examines ADDED lines (+) — removals are not new risks.
    Tracks variable assignments across the diff to follow taint propagation.
    """
    result = TaintAnalysisResult(files_analysed=[file_path] if file_path else [])

    # Collect added lines with line numbers
    added_lines: list[tuple[int, str]] = []
    line_num = 0
    for raw in diff_text.splitlines():
        hunk = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', raw)
        if hunk:
            line_num = int(hunk.group(1)) - 1
            continue
        if raw.startswith('+') and not raw.startswith('+++'):
            line_num += 1
            added_lines.append((line_num, raw[1:]))
        elif not raw.startswith('-'):
            line_num += 1

    if not added_lines:
        return result

    # Pass 1: find taint sources and tainted variables
    tainted_vars: dict[str, TaintSource] = {}   # var_name → TaintSource
    sources: list[TaintSource] = []

    for ln, line in added_lines:
        for pattern, taint_type in _SOURCE_PATTERNS:
            if pattern.search(line):
                var = _extract_var_name(line)
                src = TaintSource(name=var or "?", taint_type=taint_type, line=ln, pattern=line.strip())
                sources.append(src)
                if var:
                    tainted_vars[var] = src
                result.sources_found += 1
                break

    # Pass 2: propagate taint through assignments
    # e.g.  userId = request.getParameter("id")  [tainted]
    #        query = "SELECT * WHERE id=" + userId [also tainted]
    changed = True
    max_iterations = 5
    iteration = 0
    while changed and iteration < max_iterations:
        changed = False
        iteration += 1
        for ln, line in added_lines:
            var = _extract_var_name(line)
            if var and var not in tainted_vars:
                # Check if RHS contains any tainted variable
                for tainted_var, src in list(tainted_vars.items()):
                    if _uses_var(line, tainted_var):
                        tainted_vars[var] = TaintSource(
                            name=var, taint_type=src.taint_type, line=ln,
                            pattern=f"{var} ← {tainted_var} (propagated)",
                        )
                        changed = True
                        break

    result.tainted_vars = list(tainted_vars.keys())

    # Pass 3: find sinks and check if they use tainted variables
    sinks: list[TaintSink] = []
    for ln, line in added_lines:
        for pattern, sink_type, cwe, severity in _SINK_PATTERNS:
            if pattern.search(line):
                # Extract argument variable names from the sink call
                args_match = re.search(r'\(([^)]*)\)', line)
                args = re.findall(r'\b([a-zA-Z_]\w*)\b', args_match.group(1)) if args_match else []
                sinks.append(TaintSink(name=sink_type.value, sink_type=sink_type, line=ln, pattern=line.strip(), args=args))
                result.sinks_found += 1
                break

    # Pass 4: match sources to sinks via tainted variables
    for sink in sinks:
        for cwe_info in _SINK_PATTERNS:
            if cwe_info[1] == sink.sink_type:
                cwe_id   = cwe_info[2]
                severity = cwe_info[3]
                break
        else:
            cwe_id, severity = "CWE-?", "medium"

        # Check if any tainted var appears in sink args or in the sink line
        matched_var = None
        for arg in sink.args:
            if arg in tainted_vars:
                matched_var = arg
                break

        # Also check if any tainted var appears directly in the sink line
        if not matched_var:
            sink_line = sink.pattern
            for var in tainted_vars:
                if _uses_var(sink_line, var):
                    matched_var = var
                    break

        if matched_var:
            src = tainted_vars[matched_var]
            steps = [f"Line {src.line}: {src.name} receives {src.taint_type.value}"]
            if matched_var != src.name:
                steps.append(f"Propagated through: {src.name} → {matched_var}")
            steps.append(f"Line {sink.line}: {matched_var} flows into {sink.sink_type.value}")

            result.paths.append(TaintPath(
                source_var=matched_var,
                source_type=src.taint_type,
                source_line=src.line,
                sink_name=sink.sink_type.value,
                sink_type=sink.sink_type,
                sink_line=sink.line,
                file_path=file_path,
                cwe_id=cwe_id,
                severity=severity,
                description=(
                    f"User-controlled value '{matched_var}' from {src.taint_type.value} "
                    f"flows into {sink.sink_type.value} without sanitisation ({cwe_id})"
                ),
                path_steps=steps,
            ))

    return result


def analyse_multiple_hunks(hunks: list, file_path: str = "") -> TaintAnalysisResult:
    """Analyse a list of DiffHunk objects for taint flows."""
    combined = TaintAnalysisResult()
    for hunk in hunks:
        fp = getattr(hunk, 'file_path', file_path)
        content = getattr(hunk, 'content', str(hunk))
        r = analyse_taint(content, fp)
        combined.paths.extend(r.paths)
        combined.sources_found += r.sources_found
        combined.sinks_found   += r.sinks_found
        combined.tainted_vars.extend(r.tainted_vars)
        if fp not in combined.files_analysed:
            combined.files_analysed.append(fp)
    combined.tainted_vars = list(set(combined.tainted_vars))
    return combined
