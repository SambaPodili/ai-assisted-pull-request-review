"""
tests/test_unit_test_validation.py
------------------------------------
Method-level unit-test scenario validation: detects changed methods, the
scenarios each should have, and which the PR's tests actually cover.
"""
from __future__ import annotations

from core.models import AnalysisRequest, ChangeType, DiffHunk
from ingestion.unit_test_validation import (
    validate, hollow_tests, HAPPY, INVALID, NULL, BOUNDARY, ERROR, REGRESSION,
    SECURITY, CONCURRENCY, DATA, BACKCOMPAT,
)


def _req(hunks, ref="feature/x"):
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref=ref, target_ref="main", hunks=hunks)


def test_detects_method_and_required_scenarios():
    src = DiffHunk(file_path="src/Fee.java", language="java", additions=4, deletions=0,
                   content='+    public int computeFee(int amount, String tier) throws BadTierException {\n'
                           '+        if (amount < 0) throw new IllegalArgumentException();\n'
                           '+        return amount * 2;\n+    }')
    methods, summary = validate(_req([src], ref="fix/fee-bug"))
    names = {m.method for m in methods}
    assert "computeFee" in names
    assert "IllegalArgumentException" not in names      # thrown type, not a method
    m = next(x for x in methods if x.method == "computeFee")
    # has args → invalid+null; throws → error; numeric → boundary; bugfix branch → regression
    for cat in (HAPPY, INVALID, NULL, ERROR, REGRESSION):
        assert cat in m.required_scenarios
    assert "not yet covered" in summary


def test_marks_covered_scenarios_from_tests():
    src = DiffHunk(file_path="src/Fee.java", language="java", additions=3, deletions=0,
                   content='+    public int computeFee(int amount, String tier) throws BadTierException {\n+        return amount * 2;\n+    }')
    test = DiffHunk(file_path="src/test/FeeTest.java", language="java", additions=4, deletions=0,
                    content='+    @Test void computeFee_valid() { assertEquals(20, svc.computeFee(10, "g")); }\n'
                            '+    @Test void computeFee_nullTier() { assertThrows(NPE.class, () -> svc.computeFee(0, null)); }')
    methods, _ = validate(_req([src, test]))
    m = next(x for x in methods if x.method == "computeFee")
    assert m.has_test is True
    assert HAPPY in m.covered_scenarios          # assertEquals present
    assert NULL in m.covered_scenarios           # null test present
    assert ERROR in m.covered_scenarios          # assertThrows present
    assert HAPPY not in m.missing_scenarios


def test_no_test_file_means_all_missing():
    src = DiffHunk(file_path="svc/pay.py", language="python", additions=3, deletions=0,
                   content='+def charge(amount, account):\n+    return amount\n')
    methods, _ = validate(_req([src]))
    m = next(x for x in methods if x.method == "charge")
    assert m.has_test is False
    assert m.missing_scenarios == m.required_scenarios   # nothing covered
    assert HAPPY in m.missing_scenarios


def test_no_methods_returns_empty():
    doc = DiffHunk(file_path="README.md", language="markdown", additions=1, deletions=0,
                   content="+Some docs change")
    methods, summary = validate(_req([doc]))
    assert methods == [] and summary == ""


def test_security_and_backcompat_categories():
    src = DiffHunk(file_path="src/api/AuthController.java", language="java", additions=3, deletions=0,
                   content='+    public Token authenticate(String user, String password) {\n'
                           '+        return jwt.sign(user);\n+    }')
    methods, _ = validate(_req([src]))
    m = methods[0]
    assert SECURITY in m.required_scenarios       # auth/token path
    assert BACKCOMPAT in m.required_scenarios      # controller/api surface


def test_hollow_test_detection():
    test = DiffHunk(file_path="src/test/PayTest.java", language="java", additions=4, deletions=0,
                    content='+    @Test void charge_ok() {\n+        svc.charge(10);\n+    }\n'
                            '+    @Test void refund_ok() { assertEquals(5, svc.refund(5)); }')
    hollow = hollow_tests(_req([test]))
    assert any("charge_ok" in h for h in hollow)    # no assertion → flagged
    assert not any("refund_ok" in h for h in hollow)  # has assertEquals → fine


def test_gate_only_holds_untested_NEW_security_method():
    """A modified (not new) untested security method must NOT block — its tests
    may exist in the repo outside this PR. A new one with no test should HOLD."""
    from core.models import (AnalysisReport, RiskResult, RiskLevel, GateDecision,
                             TestCoverageResult, MethodTestCoverage)
    from governance.gate_policy import evaluate_policy

    def report(is_new):
        m = MethodTestCoverage(method="authenticate", file_path="Auth.java", is_new=is_new,
                               has_test=False, required_scenarios=["happy path", "security (authz / injection)"])
        return AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="r",
                              source_ref="a", target_ref="b",
                              risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=GateDecision.APPROVE),
                              test_coverage=TestCoverageResult(method_coverage=[m]))
    assert evaluate_policy(report(is_new=False)).gate == GateDecision.APPROVE   # modified → no false hold
    assert evaluate_policy(report(is_new=True)).gate == GateDecision.HOLD       # new + untested → hold


def test_gate_holds_on_hollow_tests():
    from core.models import (AnalysisReport, RiskResult, RiskLevel, GateDecision, TestCoverageResult)
    from governance.gate_policy import evaluate_policy
    rep = AnalysisReport(request_id="t", change_type=ChangeType.PR, repo_url="r",
                         source_ref="a", target_ref="b",
                         risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10, gate_decision=GateDecision.APPROVE),
                         test_coverage=TestCoverageResult(hollow_tests=["FooTest.java::bar_works"]))
    assert evaluate_policy(rep).gate == GateDecision.HOLD
