"""
tests/test_cross_repo_impact.py
--------------------------------
Deep analysis of declared downstream repos: for each call-site of a changed
symbol in a dependent repo, decide whether the change breaks the caller.

Covers the deterministic pass (zero tokens) — the LLM pass only sharpens text.
"""
from core.models import AnalysisRequest, ChangeType, DiffHunk, RiskLevel
from agents.cross_repo_impact_agent import (
    classify_symbol_changes, _static_impacts, _arity, CrossRepoImpactAgent,
)
from core.token_manager import TokenBudgetManager


def _req(diff: str, lang="java", refs=None):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b",
        hunks=[DiffHunk(file_path="Svc.java", language=lang, additions=1, deletions=1, content=diff)],
        metadata={"external_references": refs or []},
    )


# ── arity helper ──────────────────────────────────────────────────────────────

def test_arity_counts_top_level_params():
    assert _arity("void f()") == 0
    assert _arity("void f(int a)") == 1
    assert _arity("void f(int a, String b)") == 2
    assert _arity("void f(Map<K,V> m, int b)") == 2   # generic comma not counted
    assert _arity("no parens here") == -1


# ── symbol-change classification ──────────────────────────────────────────────

def test_signature_change_detected_with_arity_delta():
    diff = ("@@ -10,1 +10,1 @@\n"
            "-    public void updateRp(RelatedParty rp) {\n"
            "+    public void updateRp(RelatedParty rp, String rif) {\n")
    ch = classify_symbol_changes(_req(diff))
    assert ch["updateRp"]["change_kind"] == "signature_changed"
    assert ch["updateRp"]["old_arity"] == 1 and ch["updateRp"]["new_arity"] == 2


def test_removed_symbol_detected():
    diff = ("@@ -10,1 +10,0 @@\n"
            "-    public void oldApi(int a) {\n")
    ch = classify_symbol_changes(_req(diff))
    assert ch["oldApi"]["change_kind"] == "removed"


# ── end-to-end deterministic impacts ──────────────────────────────────────────

def test_downstream_callsite_of_signature_change_is_breaking():
    diff = ("@@ -10,1 +10,1 @@\n"
            "-    public void updateRp(RelatedParty rp) {\n"
            "+    public void updateRp(RelatedParty rp, String rif) {\n")
    refs = [{"symbol": "updateRp", "file_path": "client/Caller.java", "line": 88,
             "context": "svc.updateRp(rp);", "repo": "SCV/billing"}]
    res = _static_impacts(_req(diff, refs=refs))
    assert res.analysed and res.total_call_sites == 1
    imp = res.impacts[0]
    assert imp.repo == "SCV/billing" and imp.impact == "likely"
    assert imp.severity == RiskLevel.HIGH
    assert "1 to 2" in imp.reason
    assert res.breaking_count == 1 and res.overall_risk == RiskLevel.HIGH


def test_removed_symbol_callsite_breaks():
    diff = "@@ -10,1 +10,0 @@\n-    public void oldApi(int a) {\n"
    refs = [{"symbol": "oldApi", "file_path": "client/X.java", "line": 5,
             "context": "svc.oldApi(1);", "repo": "team/consumer"}]
    res = _static_impacts(_req(diff, refs=refs))
    assert res.impacts[0].impact == "breaks"
    assert res.impacts[0].severity == RiskLevel.HIGH


def test_no_downstream_repos_is_noop():
    diff = "@@ -10,1 +10,1 @@\n-old\n+new\n"
    res = _static_impacts(_req(diff, refs=[]))
    assert res.analysed is False and res.total_call_sites == 0


def test_references_require_a_repo_label():
    # same-repo refs (no repo) must NOT be treated as downstream impacts
    diff = ("@@ -10,1 +10,1 @@\n"
            "-    public void updateRp(RelatedParty rp) {\n"
            "+    public void updateRp(RelatedParty rp, String rif) {\n")
    refs = [{"symbol": "updateRp", "file_path": "same/Repo.java", "line": 3,
             "context": "updateRp(rp)", "repo": ""}]
    res = _static_impacts(_req(diff, refs=refs))
    assert res.analysed is False


def test_agent_run_noop_without_refs_does_not_call_llm():
    diff = "@@ -10,1 +10,1 @@\n-old\n+new\n"
    agent = CrossRepoImpactAgent(api_key="x")
    budget = TokenBudgetManager("t", None)
    res = agent.run(_req(diff, refs=[]), budget, {})
    assert res.analysed is False
