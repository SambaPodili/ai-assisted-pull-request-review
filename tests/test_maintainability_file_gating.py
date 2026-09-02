"""
tests/test_maintainability_file_gating.py
-----------------------------------------
Office-deployment regression: the maintainability agent flagged "Magic number
`3`" and "Nesting depth ~5 (20 spaces)" on `pom.xml` (dependency bumps) and
nesting findings on a `*Test.java`. Its static detectors must only run on real
first-party source now.
"""
from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.maintainability_agent import _run_static


def _req(*hunks):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b", hunks=list(hunks),
    )


def _hunk(path, body, language="java"):
    content = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,10 @@\n{body}"
    return DiffHunk(file_path=path, language=language, additions=5, deletions=0, content=content)


POM_BODY = "\n".join([
    "+    <dependency>",
    "+      <groupId>org.example</groupId>",
    "+      <artifactId>widget</artifactId>",
    "+      <version>3</version>",
    "+                        <scope>test</scope>",   # 24 leading spaces
    "+    </dependency>",
])

TEST_BODY = "\n".join([
    "+    void shouldHandleEnquiry() {",
    "+                                if (x) { if (y) { if (z) { doThing(42); } } }",  # deep + magic
    "+    }",
])

SRC_BODY = "\n".join([
    "+  int computeFee(int amount) {",
    "+      return amount * 7;",   # magic number 7
    "+  }",
])


def test_pom_xml_produces_no_maintainability_issues():
    issues = _run_static(_req(_hunk("cswservice-core/pom.xml", POM_BODY, language="xml")))
    assert issues == []


def test_test_file_produces_no_maintainability_issues():
    path = "cswservice-core/src/test/java/com/x/CrsEnquiryResponseProcessorTest.java"
    issues = _run_static(_req(_hunk(path, TEST_BODY)))
    assert issues == []


def test_real_source_still_flagged():
    issues = _run_static(_req(_hunk("src/main/java/com/x/FeeService.java", SRC_BODY)))
    kinds = {i.kind for i in issues}
    assert "magic_number" in kinds


def test_mixed_diff_only_flags_real_source():
    issues = _run_static(_req(
        _hunk("pom.xml", POM_BODY, language="xml"),
        _hunk("src/test/java/com/x/FooTest.java", TEST_BODY),
        _hunk("src/main/java/com/x/FeeService.java", SRC_BODY),
    ))
    assert issues, "the real source file should still yield findings"
    assert all("FeeService.java" in i.file_path for i in issues)
