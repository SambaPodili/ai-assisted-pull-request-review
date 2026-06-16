"""
tests/test_compliance_and_testdetect.py
-----------------------------------------
Compliance mapping (OWASP / PCI-DSS / CWE Top 25) and the hardened test-file
detector.
"""
from __future__ import annotations

from ingestion.test_detect import is_test_file
from governance.compliance import assess
from core.models import (
    AnalysisReport, ChangeType, SecurityResult, SecurityFinding, RiskLevel,
    TaintAnalysisResult, TaintPath, TaintSource, TaintSink,
    DependencyResult, DataPrivacyResult,
)


# ── Test-file detection ───────────────────────────────────────────────────────

def test_detects_real_test_files():
    for p in ["src/test/java/com/x/FooTest.java", "a/FooTests.java", "svc/test_pay.py",
              "svc/pay_test.py", "web/__tests__/a.test.tsx", "pkg/util_test.go", "conftest.py",
              "spec/models/user_spec.rb"]:
        assert is_test_file(p), p


def test_ignores_non_test_files():
    for p in ["src/latest_config.py", "auth/attestation.java", "ui/contestants.js",
              "src/main/java/Foo.java", "lib/protester.py"]:
        assert not is_test_file(p), p


# ── Compliance mapping ────────────────────────────────────────────────────────

def _report(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def test_clean_change_passes():
    c = assess(_report())
    assert c["overall"]["status"] == "PASS" and c["overall"]["fail"] == 0


def test_sqli_maps_to_owasp_a03_and_cwe25():
    rep = _report(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                        cwe_id="CWE-89", description="SQLi", remediation="x")]))
    c = assess(rep)
    owasp = {it["id"]: it["status"] for s in c["standards"] if "OWASP" in s["name"] for it in s["items"]}
    assert owasp["A03"] == "fail"
    cwe = next(s for s in c["standards"] if "CWE Top 25" in s["name"])
    assert any(it["id"] == "CWE-89" and it["status"] == "fail" for it in cwe["items"])


def test_secrets_cve_pii_map_to_pci():
    rep = _report(
        security=SecurityResult(overall_severity=RiskLevel.HIGH, secrets_detected=True, findings=[]),
        dependency=DependencyResult(cve_hits=["CVE-2023-1"]),
        data_privacy=DataPrivacyResult(unencrypted_pii_count=3),
    )
    c = assess(rep)
    pci = {it["id"]: it["status"] for s in c["standards"] if "PCI" in s["name"] for it in s["items"]}
    assert pci["8.3.1"] == "fail"     # secrets
    assert pci["6.3.3"] == "fail"     # CVE
    assert pci["3.4"] == "fail"       # PII


def test_fix1_taint_only_cwe_rolls_up_to_owasp_and_cwe25():
    # SQL injection proven ONLY by the taint agent (security agent found nothing)
    # must still fail OWASP A03 and appear in CWE Top 25.
    rep = _report(taint_analysis=TaintAnalysisResult(taint_paths=[TaintPath(
        source=TaintSource(file_path="Dao.java", line=40, variable="q", source="request_param"),
        sink=TaintSink(file_path="Dao.java", line=42, variable="sql", sink="sql_query"),
        cwe="CWE-89", severity=RiskLevel.HIGH, description="tainted q reaches sql_query")]))
    c = assess(rep)
    owasp = {it["id"]: it["status"] for s in c["standards"] if "OWASP" in s["name"] for it in s["items"]}
    assert owasp["A03"] == "fail"
    cwe = next(s for s in c["standards"] if "CWE Top 25" in s["name"])
    assert any(it["id"] == "CWE-89" and it["status"] == "fail" for it in cwe["items"])


def test_fix2_unmapped_cwe_shows_in_other_bucket():
    # ReDoS (CWE-1333) is real but not in OWASP Top 10 / CWE Top 25 — it must
    # surface in the "Other" section instead of vanishing from compliance.
    rep = _report(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="Re.java", line_range="5", severity=RiskLevel.HIGH,
                        cwe_id="CWE-1333", description="catastrophic backtracking", remediation="x")]))
    c = assess(rep)
    other = next((s for s in c["standards"] if s["name"].startswith("Other")), None)
    assert other is not None, "Other bucket should appear when an unmapped CWE is found"
    assert any(it["id"] == "CWE-1333" and it["status"] == "fail" for it in other["items"])
    assert "ReDoS" in next(it["title"] for it in other["items"] if it["id"] == "CWE-1333")


def test_fix2_other_bucket_absent_when_all_cwes_mapped():
    rep = _report(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="a.py", line_range="1", severity=RiskLevel.HIGH,
                        cwe_id="CWE-89", description="SQLi", remediation="x")]))
    c = assess(rep)
    assert not any(s["name"].startswith("Other") for s in c["standards"])


def test_unverified_security_finding_excluded():
    rep = _report(security=SecurityResult(overall_severity=RiskLevel.CRITICAL, findings=[
        SecurityFinding(file_path="ghost.py", line_range="1", severity=RiskLevel.CRITICAL,
                        cwe_id="CWE-89", description="x", remediation="y", unverified=True)]))
    c = assess(rep)
    owasp = {it["id"]: it["status"] for s in c["standards"] if "OWASP" in s["name"] for it in s["items"]}
    assert owasp["A03"] == "pass"     # unverified finding must not fail compliance
