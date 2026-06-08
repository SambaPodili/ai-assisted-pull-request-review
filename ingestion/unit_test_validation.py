"""
ingestion/unit_test_validation.py
----------------------------------
Method-level unit-test scenario validation.

For each method touched by a PR it answers: does it have a test, and which of the
important test scenarios (happy path, invalid input, boundary, null, error path,
state/side-effects, regression) are actually covered by the tests in this PR?

Pure heuristic / deterministic — no LLM. Language-aware for Java, Python, JS/TS,
Go. Conservative: when unsure it marks a scenario "required" so reviewers decide.
"""
from __future__ import annotations
import re

from core.models import AnalysisRequest, MethodTestCoverage

# ── Scenario categories ────────────────────────────────────────────────────────
HAPPY      = "happy path"
INVALID    = "invalid input"
BOUNDARY   = "boundary / edge"
NULL       = "null / empty"
ERROR      = "error / exception"
SIDE_FX    = "state / side-effects"
REGRESSION = "regression (the fix)"
SECURITY   = "security (authz / injection)"
CONCURRENCY = "concurrency / thread-safety"
DATA       = "data integrity / serialization"
BACKCOMPAT = "backward compatibility"

# ── Method declaration patterns ────────────────────────────────────────────────
_DECL = [
    # Java/Kotlin/C#: modifiers <type> name(args)
    re.compile(r'\b(?:public|private|protected|static|final|\s)+[\w<>\[\],.?]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w,\s]+)?\{?'),
    # Python: def name(args)
    re.compile(r'\bdef\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)'),
    # JS/TS: function name(args) | name(args) { | const name = (args) =>
    re.compile(r'\bfunction\s+(\w+)\s*\(([^)]*)\)'),
    re.compile(r'\b(\w+)\s*\(([^)]*)\)\s*\{'),
    # Go: func name(args)
    re.compile(r'\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\(([^)]*)\)'),
]

_TEST_FILE = ("test", "spec", "__tests__", "_test.")
_IGNORE_NAMES = {"if", "for", "while", "switch", "catch", "return", "new", "super",
                 "this", "function", "constructor", "get", "set"}


def _is_test_file(path: str) -> bool:
    p = (path or "").lower()
    return any(s in p for s in _TEST_FILE)


def _changed_methods(hunks) -> dict[str, dict]:
    """Map method name → {file, is_new, body}. Scans ALL hunk lines (added +
    context) so both newly-added and modified-in-place methods are captured."""
    out: dict[str, dict] = {}
    for h in hunks:
        if _is_test_file(h.file_path):
            continue
        added_lines, all_lines = [], []
        for ln in (h.content or "").splitlines():
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            added = ln.startswith("+")
            text = ln[1:] if ln[:1] in "+- " else ln
            all_lines.append(text)
            if added:
                added_lines.append(text)
        body = "\n".join(all_lines)
        added_body = "\n".join(added_lines)
        for text in all_lines:
            for pat in _DECL:
                m = pat.search(text)
                if not m:
                    continue
                name, args = m.group(1), (m.group(2) if m.lastindex and m.lastindex >= 2 else "")
                # Skip keywords, and PascalCase names — those are types/classes/
                # constructors/exceptions (e.g. `new IllegalArgumentException()`),
                # not the methods we validate (methods are lower/camelCase).
                if name in _IGNORE_NAMES or len(name) < 2 or name[0].isupper():
                    continue
                if re.search(r'\b(new|throw|return)\s+' + re.escape(name) + r'\s*\(', text):
                    continue   # a call/throw, not a declaration
                if name not in out:
                    out[name] = {
                        "file": h.file_path,
                        "is_new": bool(re.search(r'(?:def|func|function)\s+' + re.escape(name), added_body)
                                       or re.search(r'\b' + re.escape(name) + r'\s*\([^)]*\)\s*\{', added_body)),
                        "args": args.strip(),
                        "body": body,
                    }
                break
    return out


def _required_scenarios(name: str, args: str, body: str, file: str, is_bugfix: bool) -> list[str]:
    req = [HAPPY]
    has_args = bool(args.strip())
    if has_args:
        req += [INVALID, NULL]
    bl = body.lower()
    fl = (file or "").lower()
    if re.search(r'\b(for|while|foreach|\.size\(|\.length|len\(|index|count|range|<=|>=|\+\+|--)\b', bl) \
       or re.search(r'[<>]=?', body):
        req.append(BOUNDARY)
    if re.search(r'\b(throw|throws|raise|panic|return\s+err|error|exception|optional|nullpointer)\b', bl):
        req.append(ERROR)
    if re.search(r'\b(save|update|delete|insert|persist|set[A-Z]|self\.\w+\s*=|this\.\w+\s*=|void\s)\b', body):
        req.append(SIDE_FX)
    # Security-relevant code → require authz / injection / sanitisation tests.
    if re.search(r'(auth|crypt|password|token|secret|jwt|oauth|permission|role|rbac|'
                 r'sql|query|exec|sanitiz|escap|deserializ|ldap|xml|redirect)', fl + " " + bl):
        req.append(SECURITY)
    # Shared/concurrent state → require thread-safety tests.
    if re.search(r'(synchronized|volatile|atomic|concurrent|threadlocal|@async|asyncio|'
                 r'mutex|\block\b|semaphore|static\s+\w+\s*=|runnable|executor)', bl):
        req.append(CONCURRENCY)
    # Serialization / migrations → require round-trip / integrity / rollback tests.
    if re.search(r'(serial|deserial|tojson|fromjson|parse|marshal|unmarshal|migrat|'
                 r'schema|\.sql|flyway|liquibase|mapper|dto)', fl + " " + bl):
        req.append(DATA)
    # Public API / contract surface → require backward-compatibility tests.
    if re.search(r'(controller|endpoint|@(get|post|put|delete|request)mapping|@path|'
                 r'router|route|openapi|swagger|public\s+api|/v\d)', fl + " " + bl):
        req.append(BACKCOMPAT)
    if is_bugfix:
        req.append(REGRESSION)
    # de-dup preserving order
    return list(dict.fromkeys(req))


def _test_corpus(hunks) -> str:
    return "\n".join(h.content for h in hunks if _is_test_file(h.file_path)).lower()


def _covered_scenarios(name: str, required: list[str], tests: str) -> list[str]:
    """Heuristically detect which scenarios the PR's test code evidences."""
    if not tests or name.lower() not in tests:
        return []
    covered = []
    has_assert = bool(re.search(r'\b(assert\w*|expect\w*|should\w*|verify|require\.)', tests))
    if HAPPY in required and has_assert:
        covered.append(HAPPY)
    if INVALID in required and re.search(r'\b(invalid|illegal|bad|malformed|reject|wrong)\b', tests):
        covered.append(INVALID)
    if NULL in required and re.search(r'\b(null|none|nil|empty|isempty|blank|missing)\b', tests):
        covered.append(NULL)
    if BOUNDARY in required and re.search(r'\b(boundary|edge|max|min|zero|negative|overflow|limit|\b0\b)\b', tests):
        covered.append(BOUNDARY)
    if ERROR in required and re.search(r'(assertthrows|expectexception|pytest\.raises|\.tothrow|assert_raises|expect\([^)]*\)\.tothrow|catch)', tests):
        covered.append(ERROR)
    if SIDE_FX in required and re.search(r'\b(verify|times\(|mock|saved|persisted|wascalled|getvalue|assertequals.*get)\b', tests):
        covered.append(SIDE_FX)
    if SECURITY in required and re.search(r'(unauthor|forbidden|\b401\b|\b403\b|injection|xss|sanitiz|'
                                          r'permission|denied|accessdenied|authz|csrf|escap)', tests):
        covered.append(SECURITY)
    if CONCURRENCY in required and re.search(r'(concurren|thread|parallel|\brace\b|executorservice|'
                                             r'countdownlatch|gather|runblocking|atomic|synchron)', tests):
        covered.append(CONCURRENCY)
    if DATA in required and re.search(r'(roundtrip|round-trip|serial|deserial|tojson|fromjson|'
                                      r'migrat|rollback|schema|marshal)', tests):
        covered.append(DATA)
    if BACKCOMPAT in required and re.search(r'(backward|back-compat|compat|legacy|deprecat|\bv1\b|oldclient|existingconsumer)', tests):
        covered.append(BACKCOMPAT)
    if REGRESSION in required and re.search(r'\b(regression|bug|issue|jira|ticket|reproduc)\b', tests):
        covered.append(REGRESSION)
    return covered


def validate(request: AnalysisRequest) -> tuple[list[MethodTestCoverage], str]:
    """Return per-method scenario coverage + a one-line summary."""
    methods = _changed_methods(request.hunks)
    if not methods:
        return [], ""

    meta = request.metadata or {}
    src_ref = (request.source_ref or "") + " " + str(meta.get("title", "")) + " " + str(meta.get("pr_title", ""))
    is_bugfix = bool(re.search(r'(?i)\b(fix|bug|hotfix|patch|defect|issue)\b', src_ref))

    tests = _test_corpus(request.hunks)
    results: list[MethodTestCoverage] = []
    total_req = total_missing = 0
    for name, info in sorted(methods.items()):
        required = _required_scenarios(name, info["args"], info["body"], info["file"], is_bugfix)
        covered  = _covered_scenarios(name, required, tests)
        missing  = [s for s in required if s not in covered]
        has_test = name.lower() in tests
        results.append(MethodTestCoverage(
            method=name, file_path=info["file"], is_new=info["is_new"],
            has_test=has_test, required_scenarios=required,
            covered_scenarios=covered, missing_scenarios=missing,
        ))
        total_req += len(required)
        total_missing += len(missing)

    summary = (f"{len(results)} changed method(s) · {total_req} scenario(s) recommended · "
               f"{total_missing} not yet covered")
    return results, summary


# Test-method declarations across languages.
_TEST_METHOD = re.compile(
    r'(?:@Test\b[\s\S]{0,80}?\b(\w+)\s*\(|'           # Java/Kotlin @Test ... name(
    r'\bdef\s+(test_\w+)\s*\(|'                        # Python def test_*
    r'\b(?:it|test)\s*\(\s*[\'"]([^\'"]+)[\'"]|'       # JS/TS it("...") / test("...")
    r'\bfunc\s+(Test\w+)\s*\()'                        # Go func Test*
)
_ASSERTION = re.compile(
    r'(assert\w*|expect\w*|should\w*|verify\s*\(|require\.|\.tobe|\.toequal|\.tothrow|'
    r'pytest\.raises|assertthat|t\.(error|fatal|fail))', re.IGNORECASE
)


def hollow_tests(request: AnalysisRequest) -> list[str]:
    """Test methods ADDED in this PR that contain no assertions — they pass
    trivially and give false confidence. Returns 'file::testName' entries."""
    out: list[str] = []
    for h in request.hunks:
        if not _is_test_file(h.file_path):
            continue
        added = "\n".join(l[1:] for l in (h.content or "").splitlines()
                          if l.startswith("+") and not l.startswith("+++"))
        if not added.strip():
            continue
        matches = list(_TEST_METHOD.finditer(added))
        for i, m in enumerate(matches):
            name = next((g for g in m.groups() if g), "")
            # Segment = this test's body, bounded by the next test declaration
            # (so one test's assertions don't count for the previous one).
            end = matches[i + 1].start() if i + 1 < len(matches) else len(added)
            seg = added[m.start(): min(end, m.start() + 1200)]
            if name and not _ASSERTION.search(seg):
                out.append(f"{h.file_path.split('/')[-1]}::{name}")
    # de-dup, cap
    return list(dict.fromkeys(out))[:20]
