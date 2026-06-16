"""
tests/test_llm_coverage_indicator.py
-------------------------------------
Large-PR LLM coverage indicator: tells reviewers when the model reviewed only a
budget-capped slice (partial) vs every file (full / deep-scan batched), so a
partial pass is never mistaken for full coverage.
"""
from core.models import DiffHunk, AnalysisRequest, ChangeType
from agents.base_agent import count_files_in_llm_budget
from core.orchestrator import ImpactAnalysisOrchestrator


def _hunks(n, content_lines=5, prefix="f", ext="py"):
    return [DiffHunk(file_path=f"{prefix}{i}.{ext}", language="python",
                     additions=content_lines, deletions=0, content="+x\n" * content_lines)
            for i in range(n)]


def _req(hunks, deep_scan=False):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="b", hunks=hunks, deep_scan=deep_scan)


# ── budget counting ───────────────────────────────────────────────────────────

def test_small_pr_all_files_fit():
    c = count_files_in_llm_budget(_hunks(3))
    assert c["reviewed"] == c["signal"] == 3 and c["skipped_noise"] == 0


def test_large_pr_is_budget_capped():
    big = [DiffHunk(file_path=f"f{i}.java", language="java", additions=50, deletions=0,
                    content="+line\n" * 350) for i in range(60)]   # ~2KB each
    c = count_files_in_llm_budget(big)
    assert c["total"] == 60 and 0 < c["reviewed"] < 60   # only a slice fits


def test_noise_files_excluded():
    hunks = _hunks(3) + [
        DiffHunk(file_path="pkg/app.min.js", language="javascript", additions=1, deletions=0, content="+x"),
        DiffHunk(file_path="go.sum", language="", additions=1, deletions=0, content="+x"),
    ]
    c = count_files_in_llm_budget(hunks)
    assert c["skipped_noise"] == 2 and c["signal"] == 3


# ── orchestrator coverage modes ───────────────────────────────────────────────

def test_coverage_mode_full_for_small_pr():
    cov = ImpactAnalysisOrchestrator._llm_coverage(_req(_hunks(4)), deep_scan_used=False)
    assert cov["mode"] == "full" and cov["reviewed_by_llm"] == 4 and cov["deep_scan"] is False


def test_coverage_mode_partial_for_large_pr():
    big = [DiffHunk(file_path=f"f{i}.java", language="java", additions=50, deletions=0,
                    content="+line\n" * 350) for i in range(60)]
    cov = ImpactAnalysisOrchestrator._llm_coverage(_req(big), deep_scan_used=False)
    assert cov["mode"] == "partial" and cov["reviewed_by_llm"] < cov["total_files"]


def test_coverage_mode_batched_when_deep_scan():
    big = [DiffHunk(file_path=f"f{i}.java", language="java", additions=50, deletions=0,
                    content="+line\n" * 350) for i in range(60)]
    cov = ImpactAnalysisOrchestrator._llm_coverage(_req(big, deep_scan=True), deep_scan_used=True)
    assert cov["mode"] == "batched" and cov["reviewed_by_llm"] == cov["total_files"] == 60
    assert cov["deep_scan"] is True
