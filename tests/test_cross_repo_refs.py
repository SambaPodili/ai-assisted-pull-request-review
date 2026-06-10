"""tests/test_cross_repo_refs.py
Cross-repo reference tracing: external_references injected by the frontend
(from the git provider's code search) must fold into reference impact and
flow through to consumer impact with the originating repo.
"""
from agents.reference_impact_agent import _external_references, _static_analysis
from core.models import (AnalysisRequest, ChangeType, DiffHunk, InterfaceResult,
                         ContractBreak, ReferenceImpactResult, AnalysisReport)
from governance.consumer_impact import trace_consumer_impacts


def _req(external, hunks=None):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="main",
                           hunks=hunks or [],
                           metadata={"external_references": external})


def test_external_refs_parsed_and_repo_filtered():
    ext = [
        {"symbol": "computeFee", "file_path": "svc/Bill.java", "line": 42, "context": "computeFee(x)", "repo": "billing"},
        {"symbol": "unrelated",  "file_path": "svc/Other.java", "line": 1, "repo": "billing"},  # filtered: not a changed symbol
        {"file_path": "", "repo": "x"},  # filtered: no path
    ]
    refs, repos = _external_references(_req(ext), symbols=["computeFee"])
    assert len(refs) == 1
    assert refs[0].repo == "billing" and refs[0].line == 42 and refs[0].depth == 1
    assert repos == {"billing"}


def test_external_refs_fold_into_static_analysis():
    hunk = DiffHunk(file_path="src/Fee.java", language="java", additions=2, deletions=0,
                    content="+    public int computeFee(int a) { return a; }")
    ext = [{"symbol": "computeFee", "file_path": "billing/Bill.java", "line": 10, "repo": "billing-svc"}]
    res = _static_analysis(_req(ext, hunks=[hunk]))
    assert res.total_references >= 1
    assert any(r.repo == "billing-svc" for r in res.references)
    assert "provider_search" in res.search_backend
    assert "cross-repo" in res.summary


def test_cross_repo_refs_reach_consumer_impact():
    """A breaking change + a cross-repo caller ref → a ConsumerImpact carrying the repo."""
    rep = AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        interface=InterfaceResult(breaking_changes=[
            ContractBreak(interface_type="REST", path="/v1/computeFee", break_type="removed")]),
        reference_impact=ReferenceImpactResult(references=[
            __import__("core.models", fromlist=["SymbolReference"]).SymbolReference(
                symbol="computeFee", file_path="billing/Bill.java", line=10, repo="billing-svc")]),
    )
    impacts = trace_consumer_impacts(rep)
    assert any(ci.repo == "billing-svc" and ci.file_path == "billing/Bill.java" for ci in impacts)


def test_clone_url_builders_inject_auth():
    from api.routes.git_proxy import _clone_url, GitConfig
    bb = GitConfig(provider="bitbucket_server", base_url="https://bb.x.com", token="TKN", username="unc")
    url, secret = _clone_url(bb, "PROJ/repo")
    assert url == "https://unc:TKN@bb.x.com/scm/PROJ/repo.git" and secret == "TKN"
    gh, _ = _clone_url(GitConfig(provider="github", token="t"), "o/r")
    assert gh == "https://x-token-auth:t@github.com/o/r.git"
    bc, _ = _clone_url(GitConfig(provider="bitbucket_cloud", username="m", password="p"), "ws/r")
    assert bc == "https://m:p@bitbucket.org/ws/r.git"
    assert _clone_url(GitConfig(provider="weird"), "a/b")[0] == ""


def test_xref_clone_fallback_when_search_empty(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.git_proxy as gp
    import ingestion.reference_finder as rf
    from core.models import SymbolReference

    monkeypatch.setattr(gp, "_search_bb_server", lambda cfg, h, slug, sym: [])  # search disabled
    monkeypatch.setattr(rf, "_has_git", lambda: True)
    monkeypatch.setattr(rf, "clone_and_grep", lambda url, symbols, ref="", repo_label="", secret="", timeout_s=60:
                        [SymbolReference(symbol=symbols[0], file_path="src/C.java", line=9, repo=repo_label)])

    app = FastAPI(); app.include_router(gp.router)
    r = TestClient(app).post("/api/v1/git/xref", json={
        "cfg": {"provider": "bitbucket_server", "base_url": "https://bb.x.com", "token": "T", "username": "u"},
        "repo_slugs": ["P/billing"], "symbols": ["computeFee"], "ref": "main"})
    d = r.json()
    assert d["backend"] == "clone_grep" and len(d["references"]) == 1
    assert d["references"][0]["repo"] == "billing"
