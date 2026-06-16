"""tests/test_functional_validation.py
FSD tracking & functional impact: requirement extraction, keyword validation
against the diff, no-FSD note path, gate hold on contradiction, orchestrator wiring.
"""
from core.models import (AnalysisRequest, AnalysisReport, ChangeType, DiffHunk,
                         RiskLevel, RiskResult, GateDecision,
                         FunctionalValidationResult, FSDRequirement)
from agents.functional_validation_agent import (
    FunctionalValidationAgent, _extract_requirements, _static_validate)

FSD = """\
Fee Calculation Module — Functional Specification

3.1 The system shall calculate the processing fee using the applicant tier.
3.2 The system must store the pdfRifCreation date when a PDF RIF is generated.
3.3 The application should display a confirmation screen after submission.
Some descriptive text that is not a requirement and has no modal verb keywords.
"""

DIFF = """+    public void onPdfGenerated(Applicant a) {
+        a.setPdfRifCreation(LocalDate.now());
+        repository.save(a);
+    }"""


def _req(metadata=None, hunks=None):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="b",
                           hunks=hunks if hunks is not None else
                           [DiffHunk(file_path="src/main/java/svc/RifService.java",
                                     language="java", additions=4, deletions=0, content=DIFF)],
                           metadata=metadata or {})


def test_requirement_extraction_numbered_and_modal():
    reqs = _extract_requirements([{"name": "fee_fsd.docx", "text": FSD}])
    ids = [r.req_id for r in reqs]
    assert "3.1" in ids and "3.2" in ids and "3.3" in ids
    assert all(r.source_doc == "fee_fsd.docx" for r in reqs)
    # the non-requirement sentence is not extracted
    assert not any("descriptive text" in r.text for r in reqs)


def test_static_validation_matches_requirement_to_diff():
    res = _static_validate(_req(metadata={
        "functional_docs": [{"name": "fee_fsd.docx", "text": FSD}],
        "connected_repos": ["billing-svc"]}))
    by_id = {r.req_id: r for r in res.requirements}
    assert by_id["3.2"].status == "partial"            # pdfRifCreation keywords hit
    assert "RifService.java" in by_id["3.2"].evidence
    assert by_id["3.3"].status == "not_addressed"      # UI requirement, not in diff
    assert res.coverage_pct > 0
    assert res.impacts and res.impacts[0].affected_repos == ["billing-svc"]
    # deterministic pass never claims contradiction
    assert res.has_contradiction is False


def test_no_fsd_note_path_runs_without_llm():
    agent = FunctionalValidationAgent(api_key=None)
    res = agent.run(_req(metadata={}), budget=None)
    assert res.requirements == []
    assert any("No Functional Specification" in n for n in res.notes)


def test_gate_holds_on_contradiction():
    from governance.gate_policy import evaluate_policy
    rep = AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=5, gate_decision=GateDecision.APPROVE),
        functional_validation=FunctionalValidationResult(
            has_contradiction=True,
            requirements=[FSDRequirement(req_id="3.2", text="must store date",
                                         status="contradicted", evidence="RifService.java")]))
    res = evaluate_policy(rep)
    assert res.gate == GateDecision.HOLD
    assert any("contradicts FSD" in r and "3.2" in r for r in res.reasons)


def test_orchestrator_registers_agent():
    from core.orchestrator import ImpactAnalysisOrchestrator
    from core.models import AgentName
    o = ImpactAnalysisOrchestrator(api_key=None, phase=2)
    assert o._func.agent_name == AgentName.FUNCTIONAL_VALIDATION
