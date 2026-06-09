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
    TaintAnalysisResult, DependencyResult, DataPrivacyResult,
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


def test_unverified_security_finding_excluded():
    rep = _report(security=SecurityResult(overall_severity=RiskLevel.CRITICAL, findings=[
        SecurityFinding(file_path="ghost.py", line_range="1", severity=RiskLevel.CRITICAL,
                        cwe_id="CWE-89", description="x", remediation="y", unverified=True)]))
    c = assess(rep)
    owasp = {it["id"]: it["status"] for s in c["standards"] if "OWASP" in s["name"] for it in s["items"]}
    assert owasp["A03"] == "pass"     # unverified finding must not fail compliance
