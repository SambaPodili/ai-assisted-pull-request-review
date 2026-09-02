"""
tests/test_test_and_remediation_noise.py
----------------------------------------
Office-deployment regressions:
  - test_coverage agent generated "write a unit test" stubs for methods that
    are themselves tests.
  - remediation fallback emitted fixed boilerplate ("ensure 80% coverage",
    "validate API contract with consuming teams", "rollback with DBA") on a
    PR that touches none of those concerns.
"""
from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.test_coverage_agent import _extract_new_methods, TestCoverageAgent as CoverageAgent
from agents.remediation_agent import RemediationAgent


def _req(*hunks):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b", hunks=list(hunks),
    )


def _hunk(path, body, language="java"):
    return DiffHunk(file_path=path, language=language, additions=3, deletions=0,
                    content=f"--- a/{path}\n+++ b/{path}\n@@ -1 +1,5 @@\n{body}")


TEST_METHODS = "\n".join([
    "+    public void shouldReturnAccount() {",
    "+        assertThat(svc.get()).isNotNull();",
    "+    }",
])
REAL_METHOD = "+    public String buildAccountKey(String id) {\n+        return PREFIX + id;\n+    }"


def test_extract_new_methods_skips_test_files():
    r = _req(_hunk("src/test/java/com/x/AccountServiceTest.java", TEST_METHODS))
    assert _extract_new_methods(r) == []


def test_extract_new_methods_still_finds_real_methods():
    r = _req(_hunk("src/main/java/com/x/AccountService.java", REAL_METHOD))
    assert "buildAccountKey" in _extract_new_methods(r)


def test_test_coverage_fallback_has_no_stub_for_test_only_diff():
    r = _req(_hunk("src/test/java/com/x/AccountServiceTest.java", TEST_METHODS))
    res = CoverageAgent().fallback_result(r)
    assert res.generated_stubs == []
    assert res.uncovered_paths == []


def test_remediation_fallback_is_diff_aware_for_deps_and_tests_pr():
    # A PR that only bumps a pom and adds a test — no API, no SQL, no untested src.
    r = _req(
        _hunk("pom.xml", "+      <version>2.5.1</version>", language="xml"),
        _hunk("src/test/java/com/x/FooTest.java", TEST_METHODS),
    )
    res = RemediationAgent().fallback_result(r)
    joined = " ".join(res.fix_suggestions).lower()
    assert "80%" not in joined
    assert "api contract" not in joined
    assert "dba" not in " ".join(res.validation_checklist).lower()
    assert any("dependency" in s.lower() or "cve" in s.lower() for s in res.fix_suggestions)


def test_remediation_fallback_keeps_relevant_lines():
    r = _req(
        _hunk("src/main/java/com/x/PayController.java",
              "+  @PostMapping(\"/pay\")\n+  public Resp pay() { return svc.pay(); }"),
        _hunk("db/migration/V4__add_idx.sql", "+CREATE INDEX idx ON acct(id);", language="sql"),
    )
    res = RemediationAgent().fallback_result(r)
    joined = " ".join(res.fix_suggestions).lower()
    assert "api contract" in joined
    assert "rollback" in joined or "schema" in joined
    assert any("untested" in s.lower() or "no paired test" in s.lower() for s in res.fix_suggestions)
