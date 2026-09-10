"""
tests/test_missing_return_tracking.py
---------------------------------------
Regression tests for agents/maintainability_agent.py's "missing_return"
static check's function-scope tracking (in_ret_func/ret_func_*). A real bug:
the tracker only recognized a NEW function boundary when the new `def` line
had a return-type annotation (_FUNC_DEF_TYPED) — an untyped `def` in between
was invisible to it, so tracking stayed open across the function boundary
and the untyped function's own `return <value>` got misattributed to the
PRIOR (typed) function, silently suppressing a real missing-return finding.
"""
from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.maintainability_agent import _run_static


def _req(*hunks):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b", hunks=list(hunks),
    )


def _hunk(path, body):
    content = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,10 @@\n{body}"
    return DiffHunk(file_path=path, language="python", additions=8, deletions=0, content=content)


def test_untyped_function_after_typed_one_does_not_hide_missing_return():
    body = "\n".join([
        " def calc_total(items) -> int:",
        "+    total = 0",
        "+    for i in items:",
        "+        total += i",
        "+",
        "+def helper():",
        "+    log.debug('done')",
        "+    return None",
    ])
    issues = _run_static(_req(_hunk("calc.py", body)))
    missing_return = [i for i in issues if i.kind == "missing_return"]
    assert len(missing_return) == 1
    assert "int" in missing_return[0].description
    assert missing_return[0].line == 1


def test_second_typed_function_still_tracked_independently():
    """Two typed functions back to back — each must be judged on its OWN
    body, not have the second's return leak backward into the first."""
    body = "\n".join([
        "+def broken(x) -> int:",
        "+    y = x + 1",
        "+",
        "+def fixed(x) -> int:",
        "+    return x + 1",
    ])
    issues = _run_static(_req(_hunk("calc.py", body)))
    missing_return = [i for i in issues if i.kind == "missing_return"]
    assert len(missing_return) == 1
    assert missing_return[0].line == 1  # flags `broken`, not `fixed`


def test_function_with_real_return_after_untyped_helper_is_not_flagged():
    body = "\n".join([
        "+def helper():",
        "+    pass",
        "+",
        "+def calc_total(items) -> int:",
        "+    return sum(items)",
    ])
    issues = _run_static(_req(_hunk("calc.py", body)))
    assert [i for i in issues if i.kind == "missing_return"] == []
