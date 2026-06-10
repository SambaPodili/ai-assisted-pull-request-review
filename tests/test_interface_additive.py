"""tests/test_interface_additive.py
The interface agent must surface data-model field additions (additive contract
changes) instead of returning empty — the gap the judge panel flagged on a
POJO-only diff (new fields on data classes, no API spec file changed).
"""
from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.interface_agent import _detect_data_model_changes, InterfaceAnalysisAgent


def _req(hunks):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="b", hunks=hunks)


def test_detects_added_fields_on_data_classes():
    h = DiffHunk(file_path="src/main/java/model/IndividualDropOffApplication.java",
                 language="java", additions=2, deletions=0,
                 content="+    private LocalDate pdfRifCreation;\n+    private LocalDate llpRifCreation;\n")
    notes = _detect_data_model_changes(_req([h]))
    assert any("pdfRifCreation" in n for n in notes)
    assert any("llpRifCreation" in n for n in notes)
    assert all("IndividualDropOffApplication.java" in n for n in notes)


def test_ignores_method_locals_in_service_files():
    h = DiffHunk(file_path="src/main/java/service/FeeService.java", language="java",
                 additions=3, deletions=0,
                 content="+    public int compute() {\n+        int total = amount * 2;\n+        return total;\n+    }\n")
    assert _detect_data_model_changes(_req([h])) == []


def test_agent_run_not_empty_for_pojo_change():
    h = DiffHunk(file_path="src/main/java/dto/ApplicantDetails.java", language="java",
                 additions=1, deletions=0, content="+    private String pdfRifCreation;\n")
    res = InterfaceAnalysisAgent(api_key=None).run(_req([h]), budget=None)
    assert res.additive_changes and res.breaking_changes == []


def test_fallback_includes_additive():
    h = DiffHunk(file_path="src/main/java/model/ApplicantDetails.java", language="java",
                 additions=1, deletions=0, content="+    private String llpRifCreation;\n")
    res = InterfaceAnalysisAgent(api_key=None).fallback_result(_req([h]))
    assert any("llpRifCreation" in n for n in res.additive_changes)


def test_detects_serialization_exclusion_map_edit():
    from agents.interface_agent import _detect_serialization_changes
    h = DiffHunk(file_path="src/main/java/util/JsonWrapperUtils.java", language="java",
                 additions=2, deletions=0,
                 content='+        OBJECT_FIELD_EXCLUSIONS.add("pdfRifCreation");\n'
                         '+        OBJECT_FIELD_EXCLUSIONS.add("llpRifCreation");\n')
    notes = _detect_serialization_changes(_req([h]))
    assert any("pdfRifCreation" in n and "hidden from JSON" in n for n in notes)
    assert any("llpRifCreation" in n for n in notes)


def test_detects_jsonignore_annotation():
    from agents.interface_agent import _detect_serialization_changes
    h = DiffHunk(file_path="src/main/java/dto/Account.java", language="java",
                 additions=1, deletions=0, content="+    @JsonIgnore\n")
    assert any("@JsonIgnore" in n for n in _detect_serialization_changes(_req([h])))


def test_dependency_emits_no_manifest_rationale():
    from agents.dependency_agent import DependencyMappingAgent
    src = DiffHunk(file_path="src/main/java/model/ApplicantDetails.java", language="java",
                   additions=1, deletions=0, content="+    private String pdfRifCreation;\n")
    res = DependencyMappingAgent(api_key=None).run(_req([src]), budget=None)
    assert res.notes and "No dependency manifest" in res.notes[0]


def test_dependency_rationale_blank_when_reach_exists():
    """When declared dependents give real reach, no 'no-impact' rationale is added."""
    from agents.dependency_agent import DependencyMappingAgent
    src = DiffHunk(file_path="src/main/java/model/ApplicantDetails.java", language="java",
                   additions=1, deletions=0, content="+    private String pdfRifCreation;\n")
    req = AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                          source_ref="a", target_ref="b", hunks=[src],
                          metadata={"connected_repos": ["svc-a", "svc-b"]})
    res = DependencyMappingAgent(api_key=None).run(req, budget=None)
    assert res.affected_services and not res.notes   # has reach → no rationale note
