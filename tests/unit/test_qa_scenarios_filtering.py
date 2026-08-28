"""
tests/unit/test_qa_scenarios_filtering.py
--------------------------------------------
Regression test for a live-usage bug: the qa_scenarios agent's fallback path
was suggesting nonsensical "write a unit test for this" scenarios for build
config files (pom.xml — an "orphan" language id with no LangMeta entry, so
the old `language == "unknown"` check missed it) and for files that are
already test classes themselves (recommending a test for a test).
"""
from __future__ import annotations
import uuid

from core.models import AnalysisRequest, ChangeType, DiffHunk
from ingestion.language_registry import detect_language
from agents.qa_scenarios_agent import _build_fallback_scenarios, _is_scenario_worthy


def make_req(*hunks: DiffHunk) -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/test",
        source_ref="feature", target_ref="main",
        hunks=list(hunks),
    )


def hunk(file_path: str, content: str) -> DiffHunk:
    added = sum(1 for l in content.splitlines() if l.startswith("+"))
    return DiffHunk(file_path=file_path, language=detect_language(file_path), additions=added, deletions=0, content=content)


def test_is_scenario_worthy_rejects_orphan_xml_language():
    # pom.xml maps to the "xml" language id, which has no LangMeta entry —
    # the exact bug: lang_meta("xml") silently falls back to UNKNOWN_LANG's
    # metadata, but the raw string "xml" != "unknown" was slipping past the
    # old check.
    assert detect_language("pom.xml") == "xml"
    assert not _is_scenario_worthy("xml")


def test_is_scenario_worthy_rejects_infra_languages():
    assert not _is_scenario_worthy("yaml")
    assert not _is_scenario_worthy("json")
    assert not _is_scenario_worthy("dockerfile")


def test_is_scenario_worthy_accepts_real_source():
    assert _is_scenario_worthy("java")
    assert _is_scenario_worthy("python")


def test_pom_xml_change_produces_no_scenario():
    req = make_req(hunk("pom.xml", "+<version>2.0</version>\n"))
    scenarios = _build_fallback_scenarios(req)
    assert scenarios == []


def test_changing_a_test_file_produces_no_scenario():
    req = make_req(hunk(
        "src/test/java/com/uob/CustomerDetailCheckV2ValidatorTest.java",
        "+    @Test\n+    void testSomething() {}\n",
    ))
    scenarios = _build_fallback_scenarios(req)
    assert scenarios == []


def test_real_source_change_still_produces_scenarios():
    req = make_req(hunk(
        "src/main/java/com/uob/CustomerDetailCheckV2Validator.java",
        "+    public boolean validate(String x) { return x != null; }\n",
    ))
    scenarios = _build_fallback_scenarios(req)
    assert len(scenarios) > 0
    assert all(f != "pom.xml" for s in scenarios for f in s.affected_files)


def test_mixed_diff_only_flags_the_real_source_file():
    req = make_req(
        hunk("pom.xml", "+<version>2.0</version>\n"),
        hunk(
            "src/test/java/com/uob/BlacklistCheckRuleTest.java",
            "+    @Test\n+    void t() {}\n",
        ),
        hunk(
            "src/main/java/com/uob/BlacklistCheckRule.java",
            "+    public boolean check(String x) { return true; }\n",
        ),
    )
    scenarios = _build_fallback_scenarios(req)
    affected = {f for s in scenarios for f in s.affected_files}
    assert "pom.xml" not in affected
    assert "src/test/java/com/uob/BlacklistCheckRuleTest.java" not in affected
    assert "src/main/java/com/uob/BlacklistCheckRule.java" in affected
