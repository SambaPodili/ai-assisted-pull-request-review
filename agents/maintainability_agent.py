"""
agents/maintainability_agent.py
--------------------------------
Maintainability analysis using static detection (zero tokens) + optional LLM.

Static rules (run first, always):
  - Long functions      : consecutive + lines after a def; flag if >60 lines
  - Deep nesting        : leading spaces >= 20 or tabs >= 5 on added lines
  - Bare except         : `except:` or `except Exception: pass` on added lines
  - Swallowed exceptions: catch/except block with only pass/continue, no log/raise/return
  - Magic numbers       : integer literals != 0, 1, 2, -1 outside obvious contexts
  - Dead code after return/raise/break
  - Missing type hints  : new Python `def` without `->` and no `: type` params
  - TODO/FIXME left in  : added lines containing TODO or FIXME

Banking context: clean, auditable code is a regulatory hygiene requirement.
"""
from __future__ import annotations

import re
import time
import logging
from typing import Any

from core.models import (
    AgentName, AnalysisRequest, AgentResultBase,
    MaintainabilityIssue, MaintainabilityResult,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt

log = logging.getLogger(__name__)


# ── Severity deductions ───────────────────────────────────────────────────────

_DEDUCTION = {"high": 15, "medium": 8, "low": 3}


# ── Static detection helpers ──────────────────────────────────────────────────

_FUNC_DEF       = re.compile(r'^\+\s*(async\s+)?def\s+\w+\s*\(')
_BARE_EXCEPT    = re.compile(r'^\+\s*except\s*:\s*$')
_EXCEPT_EXC     = re.compile(r'^\+\s*except\s+Exception\s*:\s*$')
_EXCEPT_START   = re.compile(r'^\+\s*except[\s(:]')
# Java/C#/JS: empty catch block — `catch (...) {}` one-line, or `catch (...) {`
# whose next added line is just `}` (exception silently swallowed)
_EMPTY_CATCH_ONE = re.compile(r'catch\s*\([^)]*\)\s*\{\s*\}')
_CATCH_OPEN      = re.compile(r'catch\s*\([^)]*\)\s*\{?\s*$')
_MAGIC_INT      = re.compile(r'(?<!\w)(-?\d+)(?!\w)')
_SAFE_MAGIC     = {0, 1, 2, -1}
_TODO_FIXME     = re.compile(r'\b(TODO|FIXME)\b')
_CONTROL_FLOW   = re.compile(r'^\+\s*(return|raise|break)\b')
_INDENTED_CODE  = re.compile(r'^\+\s{2,}\S')
_LOG_OR_RAISE   = re.compile(r'\b(log|logger|logging|print|raise|return)\b', re.IGNORECASE)
_TYPE_HINT_PARAM = re.compile(r':\s*\w+')   # rough: at least one typed param
_RETURN_HINT    = re.compile(r'->')


def _leading_indent(line: str) -> tuple[int, int]:
    """Return (spaces, tabs) of indent in the code part (skip the leading +/-)."""
    code = line[1:] if line and line[0] in ('+', '-', ' ') else line
    spaces = len(code) - len(code.lstrip(' '))
    tabs   = len(code) - len(code.lstrip('\t'))
    return spaces, tabs


def _guess_file(lines: list[str], up_to: int) -> str:
    for line in reversed(lines[:up_to]):
        if line.startswith("+++ b/"):
            return line[6:].strip()
    return "unknown"


def _run_static(request: AnalysisRequest) -> list[MaintainabilityIssue]:
    issues: list[MaintainabilityIssue] = []

    from ingestion.diff_parser import source_line_map
    combined = "\n".join(h.content for h in request.hunks)
    lines    = combined.splitlines()
    src_map  = source_line_map(combined)   # array-index → real source line
    def _ln(i): return src_map[i] if i < len(src_map) else i + 1

    # State for multi-line checks
    in_func_def     = False
    func_start_line = 0
    func_line_count = 0
    func_file       = ""

    in_except       = False
    except_has_action = False
    except_start    = 0
    except_file     = ""

    prev_was_control = False   # for dead-code check

    for idx, line in enumerate(lines):
        is_added   = line.startswith("+") and not line.startswith("+++")
        is_removed = line.startswith("-") and not line.startswith("---")
        file_path  = _guess_file(lines, idx)

        # ── Long function detection (track + lines after def) ──────────────────
        if is_added and _FUNC_DEF.match(line):
            # close previous if open
            if in_func_def and func_line_count > 60:
                issues.append(MaintainabilityIssue(
                    kind="long_function",
                    severity="medium",
                    description=f"Function spans {func_line_count} lines (>60); consider splitting.",
                    file_path=func_file,
                    line=func_start_line,
                    suggestion="Break into smaller, single-responsibility functions.",
                ))
            in_func_def     = True
            func_start_line = _ln(idx)
            func_line_count = 0
            func_file       = file_path
        elif in_func_def:
            if is_added:
                func_line_count += 1
            elif not line.startswith((" ", "\t", "+", "-")):
                # Non-indented non-diff line means function ended
                if func_line_count > 60:
                    issues.append(MaintainabilityIssue(
                        kind="long_function",
                        severity="medium",
                        description=f"Function spans {func_line_count} lines (>60); consider splitting.",
                        file_path=func_file,
                        line=func_start_line,
                        suggestion="Break into smaller, single-responsibility functions.",
                    ))
                in_func_def = False

        # ── Deep nesting ───────────────────────────────────────────────────────
        if is_added:
            spaces, tabs = _leading_indent(line)
            if spaces >= 20 or tabs >= 5:
                depth = tabs if tabs >= 5 else spaces // 4
                issues.append(MaintainabilityIssue(
                    kind="deep_nesting",
                    severity="medium",
                    description=f"Nesting depth ~{depth} detected ({spaces} spaces); deeply nested code is hard to reason about.",
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion="Extract nested logic into helper functions or use early returns.",
                ))

        # ── Bare except ────────────────────────────────────────────────────────
        if is_added:
            code = line[1:].rstrip()
            if _BARE_EXCEPT.match(line):
                issues.append(MaintainabilityIssue(
                    kind="bare_except",
                    severity="high",
                    description="Bare `except:` catches all exceptions including KeyboardInterrupt and SystemExit.",
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion="Use `except Exception as e:` and log or re-raise the error.",
                ))
            elif _EXCEPT_EXC.match(line):
                # Look ahead: next added non-blank line only 'pass'?
                for j in range(idx + 1, min(idx + 4, len(lines))):
                    nxt = lines[j]
                    if nxt.startswith("+") and not nxt.startswith("+++"):
                        if nxt[1:].strip() in ("pass", "continue"):
                            issues.append(MaintainabilityIssue(
                                kind="bare_except",
                                severity="high",
                                description="`except Exception: pass` silently swallows all exceptions.",
                                file_path=file_path,
                                line=_ln(idx),
                                suggestion="Log the exception before passing, or re-raise it.",
                            ))
                        break

        # ── Java/C#/JS empty catch block (exception silently swallowed) ────────
        if is_added:
            code_j = line[1:]
            hit = bool(_EMPTY_CATCH_ONE.search(code_j))
            if not hit and _CATCH_OPEN.search(code_j):
                # opening catch — does the next added non-blank line close it empty?
                for j in range(idx + 1, min(idx + 3, len(lines))):
                    nxt = lines[j]
                    if nxt.startswith("+") and not nxt.startswith("+++"):
                        if nxt[1:].strip() in ("}", "{}", "{ }"):
                            hit = True
                        break
            if hit:
                issues.append(MaintainabilityIssue(
                    kind="swallowed_exception",
                    severity="high",
                    description="Empty catch block silently swallows the exception.",
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion="Log the exception (with context) or rethrow; never catch-and-ignore.",
                ))

        # ── Swallowed exceptions (except block with only pass/continue) ────────
        if is_added and _EXCEPT_START.match(line):
            in_except        = True
            except_has_action = False
            except_start     = _ln(idx)
            except_file      = file_path
        elif in_except:
            if is_added:
                code = line[1:].strip()
                if code and code not in ("pass", "continue", "..."):
                    if _LOG_OR_RAISE.search(code):
                        except_has_action = True
            else:
                # end of except block on a non-added line
                if not except_has_action and except_start > 0:
                    issues.append(MaintainabilityIssue(
                        kind="swallowed_exception",
                        severity="high",
                        description="Exception block appears to swallow the error (only pass/continue, no log/raise/return).",
                        file_path=except_file,
                        line=except_start,
                        suggestion="Log the exception at minimum: `log.exception('...')`",
                    ))
                in_except = False

        # ── Magic numbers ──────────────────────────────────────────────────────
        if is_added:
            code = line[1:]
            # Skip comments and string-heavy lines
            stripped = code.lstrip()
            if not stripped.startswith("#") and not stripped.startswith(("'", '"')):
                for m in _MAGIC_INT.finditer(code):
                    val_str = m.group(1)
                    try:
                        val = int(val_str)
                    except ValueError:
                        continue
                    if val in _SAFE_MAGIC:
                        continue
                    # Skip array sizing context: [N], range(N), etc.
                    before = code[:m.start()].rstrip()
                    if before.endswith(("[", "(", "range")):
                        continue
                    issues.append(MaintainabilityIssue(
                        kind="magic_number",
                        severity="low",
                        description=f"Magic number `{val}` in added code; intent is unclear.",
                        file_path=file_path,
                        line=_ln(idx),
                        suggestion=f"Replace {val} with a named constant (e.g. MAX_RETRIES = {val}).",
                    ))

        # ── Dead code after return/raise/break ────────────────────────────────
        if is_added:
            stripped = line[1:].strip()
            # Brace-language structure after `return` is NOT dead code: the line
            # `} catch (…) {` / `} finally {` / bare `}` closes the block — and
            # code after it (e.g. `return -1;` after the catch) is reachable.
            is_structural = stripped.startswith("}") or stripped in ("{", "});", ");")
            if prev_was_control and _INDENTED_CODE.match(line) and not is_structural:
                issues.append(MaintainabilityIssue(
                    kind="dead_code",
                    severity="medium",
                    description="Code appears immediately after a return/raise/break statement.",
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion="Remove unreachable code.",
                ))
            if is_structural:
                prev_was_control = False    # left the block — reachability resets
            else:
                prev_was_control = bool(_CONTROL_FLOW.match(line))
        else:
            prev_was_control = False

        # ── Missing type hints (Python) ────────────────────────────────────────
        if is_added and _FUNC_DEF.match(line):
            has_return = _RETURN_HINT.search(line)
            # Only flag Python files
            if not has_return and file_path.endswith(".py"):
                issues.append(MaintainabilityIssue(
                    kind="missing_type_hint",
                    severity="low",
                    description="New function definition lacks a return type annotation (`->`).",
                    file_path=file_path,
                    line=_ln(idx),
                    suggestion="Add `-> ReturnType` to the function signature.",
                ))

        # ── TODO / FIXME left in ───────────────────────────────────────────────
        if is_added and _TODO_FIXME.search(line):
            keyword = _TODO_FIXME.search(line).group(1)
            issues.append(MaintainabilityIssue(
                kind="todo_left_in",
                severity="low",
                description=f"`{keyword}` comment left in added code.",
                file_path=file_path,
                line=_ln(idx),
                suggestion="Resolve or track the issue in your ticketing system before merging.",
            ))

    # Close open long-function at EOF
    if in_func_def and func_line_count > 60:
        issues.append(MaintainabilityIssue(
            kind="long_function",
            severity="medium",
            description=f"Function spans {func_line_count} lines (>60); consider splitting.",
            file_path=func_file,
            line=func_start_line,
            suggestion="Break into smaller, single-responsibility functions.",
        ))

    # Close open swallowed-exception at EOF
    if in_except and not except_has_action and except_start > 0:
        issues.append(MaintainabilityIssue(
            kind="swallowed_exception",
            severity="high",
            description="Exception block appears to swallow the error (only pass/continue, no log/raise/return).",
            file_path=except_file,
            line=except_start,
            suggestion="Log the exception at minimum: `log.exception('...')`",
        ))

    return issues


def _dedup(issues: list[MaintainabilityIssue]) -> list[MaintainabilityIssue]:
    seen: set[tuple] = set()
    out:  list[MaintainabilityIssue] = []
    for iss in issues:
        key = (iss.kind, iss.line, iss.file_path)
        if key not in seen:
            seen.add(key)
            out.append(iss)
    return out


def _compute_score(issues: list[MaintainabilityIssue]) -> int:
    score = 100
    for iss in issues:
        score -= _DEDUCTION.get(iss.severity, 3)
    return max(0, score)


def _overall_severity(issues: list[MaintainabilityIssue]) -> str:
    if any(i.severity == "high" for i in issues):
        return "high"
    if any(i.severity == "medium" for i in issues):
        return "medium"
    return "low"


def _build_result(issues: list[MaintainabilityIssue]) -> MaintainabilityResult:
    long_fn = sum(1 for i in issues if i.kind == "long_function")
    err_gaps = sum(1 for i in issues if i.kind in ("bare_except", "swallowed_exception"))
    score = _compute_score(issues)
    sev   = _overall_severity(issues)
    summary = (
        f"{len(issues)} maintainability issue(s) detected "
        f"(score {score}/100, overall severity: {sev})."
        if issues else "No maintainability issues detected."
    )
    return MaintainabilityResult(
        issues=issues,
        maintainability_score=score,
        long_function_count=long_fn,
        error_handling_gaps=err_gaps,
        overall_severity=sev,
        summary=summary,
    )


# ── Agent ─────────────────────────────────────────────────────────────────────

class MaintainabilityAgent(BaseAgent[MaintainabilityResult]):

    agent_name   = AgentName.MAINTAINABILITY
    output_model = MaintainabilityResult

    system_prompt = (
        "You are a maintainability and clean-code expert for enterprise banking systems. "
        "Analyse the provided code diff and identify code smells, missing error handling, "
        "dead code, excessive complexity, and technical debt. "
        "For each issue provide: kind, severity (low|medium|high), description, "
        "file_path, line number, and a concrete suggestion. "
        "Also compute maintainability_score (0-100), long_function_count, "
        "error_handling_gaps, overall_severity, and a brief summary. "
        "Output ONLY compact JSON."
    )

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> MaintainabilityResult:
        ctx = context or {}

        # ── Static analysis first (always) ────────────────────────────────────
        t0            = time.monotonic()
        static_issues = _run_static(request)

        # ── LLM enrichment ────────────────────────────────────────────────────
        try:
            llm_result = super().run(request, budget, ctx)
            llm_issues = llm_result.issues if not llm_result.fallback_used else []
        except Exception as exc:
            log.warning("MaintainabilityAgent: LLM call failed, using static only: %s", exc)
            llm_issues = []

        # Merge: static + LLM, deduplicate
        all_issues = _dedup(static_issues + llm_issues)
        result = _build_result(all_issues)
        result.duration_s = round(time.monotonic() - t0, 2)
        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        hunks_text = format_hunks_for_prompt(
            request.hunks,
            max_chars_per_hunk=2000,
            focus="general",
        )
        return (
            "Analyse the following code diff for maintainability issues.\n\n"
            f"{hunks_text}"
        )

    def fallback_result(self, request: AnalysisRequest) -> MaintainabilityResult:
        issues = _run_static(request)
        result = _build_result(issues)
        result.fallback_used = True
        return result
