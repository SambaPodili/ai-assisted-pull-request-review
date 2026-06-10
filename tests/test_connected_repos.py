"""tests/test_connected_repos.py
Reviewer-declared dependent repos (UI "connected repos") must drive
affected_services and the blast radius, and amplify with breaking changes.
"""
from agents.dependency_agent import _merge_declared_dependents, _declared_dependents
from core.models import (AnalysisRequest, AnalysisReport, ChangeType, DependencyResult,
                         InterfaceResult, ContractBreak)
from governance.blast_radius import derive_blast_radius


def _req(connected):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="main", hunks=[],
                           metadata={"connected_repos": connected})


def test_declared_dependents_parses_strings_and_dicts():
    req = _req(["svc-a", {"name": "svc-b"}, {"slug": "svc-c"}, "svc-a"])
    assert _declared_dependents(req) == ["svc-a", "svc-b", "svc-c"]


def test_merge_sets_affected_services_and_baseline_blast():
    res = DependencyResult(blast_radius_score=0, affected_services=[])
    out = _merge_declared_dependents(res, _req(["svc-a", "svc-b", "svc-c"]))
    assert set(out.affected_services) == {"svc-a", "svc-b", "svc-c"}
    assert out.blast_radius_score == 42          # 3 * 14
    assert all(n.critical for n in out.dependency_nodes)


def test_merge_never_lowers_existing_score():
    res = DependencyResult(blast_radius_score=80, affected_services=["x"])
    out = _merge_declared_dependents(res, _req(["svc-a"]))
    assert out.blast_radius_score == 80          # 1*14 < 80 → keep
    assert "svc-a" in out.affected_services


def test_no_connected_repos_is_noop():
    res = DependencyResult(blast_radius_score=0)
    out = _merge_declared_dependents(res, _req([]))
    assert out.blast_radius_score == 0 and out.affected_services == []


def test_breaking_changes_amplify_connected_repo_baseline():
    # 3 dependents → baseline 42; one breaking change should push it higher.
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="r",
                         source_ref="a", target_ref="b",
                         dependency=DependencyResult(blast_radius_score=42,
                                                     affected_services=["a", "b", "c"]),
                         interface=InterfaceResult(breaking_changes=[
                             ContractBreak(interface_type="REST", path="/x", break_type="removed")]))
    derived = derive_blast_radius(rep)
    assert derived == 54                          # 1*18 + 3*12 = 54 > 42
