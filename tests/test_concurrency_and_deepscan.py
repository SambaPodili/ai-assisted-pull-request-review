"""
tests/test_concurrency_and_deepscan.py
----------------------------------------
#1 Admission control (concurrency cap + bounded FIFO queue) and
#4 Deep-scan batching/merge for large PRs.
"""
from __future__ import annotations
import asyncio
import pytest

from core.concurrency import AnalysisAdmission
from core.deep_scan import batch_hunks, merge_security, merge_code
from core.models import (
    DiffHunk, SecurityResult, SecurityFinding, CodeAnalysisResult, RiskLevel,
)


# ── #1 Admission control ──────────────────────────────────────────────────────

def test_admission_caps_and_queues():
    async def run():
        adm = AnalysisAdmission(max_concurrent=2, max_queued=2)
        await adm.acquire("a"); await adm.acquire("b")     # 2 running
        assert adm.running == 2 and adm.queued == 0
        t_c = asyncio.create_task(adm.acquire("c"))
        t_d = asyncio.create_task(adm.acquire("d"))
        await asyncio.sleep(0.02)
        assert adm.queued == 2
        assert adm.position("c") == 1 and adm.position("d") == 2
        assert not adm.can_admit()                          # full (2 + 2)
        adm.release()                                       # frees one -> c promoted
        await asyncio.sleep(0.02)
        assert t_c.done() and adm.position("d") == 1
        adm.release(); await asyncio.sleep(0.02)
        assert t_d.done()
    asyncio.run(run())


def test_admission_position_zero_when_running():
    async def run():
        adm = AnalysisAdmission(max_concurrent=1, max_queued=5)
        await adm.acquire("x")
        assert adm.position("x") == 0       # running, not waiting
    asyncio.run(run())


# ── #4 Deep-scan batching + merge ─────────────────────────────────────────────

def _hunks(n, size=2000):
    return [DiffHunk(file_path=f"f{i}.py", language="python", additions=3, deletions=1,
                     content="x"*size) for i in range(n)]


def test_batch_hunks_covers_all_files():
    hunks = _hunks(20)
    batches = batch_hunks(hunks, max_chars=12000, max_batches=10)
    assert sum(len(b) for b in batches) == 20          # nothing dropped
    assert len(batches) > 1                              # actually split


def test_batch_hunks_respects_max_batches():
    hunks = _hunks(100)
    batches = batch_hunks(hunks, max_chars=4000, max_batches=5)
    assert len(batches) <= 5
    assert sum(len(b) for b in batches) == 100          # remainder rides last batch


def test_merge_security_dedups_and_rolls_up_severity():
    r1 = SecurityResult(findings=[SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                                                  cwe_id="CWE-89", description="SQLi", remediation="x")],
                        overall_severity=RiskLevel.HIGH, token_usage=100)
    r2 = SecurityResult(findings=[
            SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH, cwe_id="CWE-89",
                            description="SQLi", remediation="x"),   # duplicate
            SecurityFinding(file_path="b.py", line_range="9", severity=RiskLevel.CRITICAL, cwe_id="CWE-79",
                            description="XSS", remediation="y"),
         ], secrets_detected=True, overall_severity=RiskLevel.CRITICAL, token_usage=120)
    m = merge_security([r1, r2])
    assert len(m.findings) == 2                          # duplicate collapsed
    assert m.secrets_detected is True
    assert m.overall_severity == RiskLevel.CRITICAL      # rolled up
    assert m.token_usage == 220                          # summed


def test_merge_code_combines_metrics():
    c1 = CodeAnalysisResult(summary="Refactored auth.", change_type="refactor", complexity_delta=3, token_usage=50)
    c2 = CodeAnalysisResult(summary="Added endpoint.", change_type="feature", complexity_delta=5, token_usage=70)
    m = merge_code([c1, c2])
    assert m.complexity_delta == 8
    assert m.change_type == "mixed"                      # differing types
    assert "Refactored auth." in m.summary and "Added endpoint." in m.summary
    assert m.token_usage == 120
