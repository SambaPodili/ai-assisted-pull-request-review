"""tests/test_blast_radius.py — derived blast radius when no service graph."""
from governance.blast_radius import derive_blast_radius
from core.models import (AnalysisReport, ChangeType, DependencyResult, InterfaceResult,
                         ContractBreak, ReferenceImpactResult, RiskLevel)


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b")
    base.update(kw); return AnalysisReport(**base)


def test_derives_from_breaking_changes_when_graph_zero():
    r = _rep(dependency=DependencyResult(blast_radius_score=0),
             interface=InterfaceResult(breaking_changes=[ContractBreak(interface_type="REST", path="/x", break_type="removed")]),
             reference_impact=ReferenceImpactResult(total_references=6, high_impact_files=["a.js", "b.js"],
                                                    shared_lib_breaks=["src/shared/utils.js"]))
    assert derive_blast_radius(r) == 61   # 6*2 + 2*8 + 1*18 + 1*15


def test_does_not_lower_a_real_graph_score():
    r = _rep(dependency=DependencyResult(blast_radius_score=80),
             interface=InterfaceResult(breaking_changes=[ContractBreak(interface_type="REST", path="/x", break_type="removed")]))
    assert derive_blast_radius(r) is None   # real graph score preserved


def test_clean_change_no_derivation():
    r = _rep(dependency=DependencyResult(blast_radius_score=0))
    assert derive_blast_radius(r) is None   # nothing to derive → leave 0
