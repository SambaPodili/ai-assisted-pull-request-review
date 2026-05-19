"""
governance/compliance_checker.py
----------------------------------
Maps security findings to specific regulatory controls.

Supported frameworks:
  • MAS TRM (Monetary Authority of Singapore Technology Risk Management)
  • PCI-DSS 4.0
  • OWASP ASVS Level 2
"""
from __future__ import annotations
from dataclasses import dataclass
from core.models import SecurityResult, AnalysisReport, GateDecision, RiskLevel


@dataclass
class ComplianceViolation:
    framework:   str    # "MAS TRM" | "PCI-DSS 4.0" | "OWASP ASVS L2"
    control_ref: str    # e.g. "Req 6.2.4" or "TRM-G05"
    description: str
    severity:    str    # "must_block" | "must_hold" | "advisory"
    cwe_ids:     list[str]


# ── Control mapping ───────────────────────────────────────────────────────────

_CONTROL_MAPPING: list[ComplianceViolation] = [
    # MAS TRM
    ComplianceViolation("MAS TRM", "TRM-G05", "Hardcoded credential — key management violation", "must_block", ["CWE-798", "CWE-321", "CWE-522"]),
    ComplianceViolation("MAS TRM", "TRM-G07", "Sensitive financial data exposure (PAN/account)", "must_block", ["CWE-312", "CWE-313"]),
    ComplianceViolation("MAS TRM", "TRM-G11", "Insufficient access control on new endpoint",    "must_hold",  ["CWE-862", "CWE-863"]),
    ComplianceViolation("MAS TRM", "TRM-G04", "Weak cryptographic primitive in financial code",  "must_hold",  ["CWE-327", "CWE-328"]),

    # PCI-DSS 4.0
    ComplianceViolation("PCI-DSS 4.0", "Req 3.3", "PAN stored in plaintext or logs",                "must_block", ["CWE-312", "CWE-313"]),
    ComplianceViolation("PCI-DSS 4.0", "Req 6.2",  "Injection vulnerability in payment code",        "must_block", ["CWE-89", "CWE-78", "CWE-90"]),
    ComplianceViolation("PCI-DSS 4.0", "Req 8.2",  "Hardcoded credentials in application code",      "must_block", ["CWE-798"]),
    ComplianceViolation("PCI-DSS 4.0", "Req 4.2",  "Weak TLS or crypto algorithm used",              "must_hold",  ["CWE-326", "CWE-327"]),
    ComplianceViolation("PCI-DSS 4.0", "Req 6.4",  "Missing input validation (SSRF risk)",           "must_hold",  ["CWE-918"]),

    # OWASP ASVS L2
    ComplianceViolation("OWASP ASVS L2", "V2.1", "Credentials exposed in source code",             "must_block", ["CWE-798"]),
    ComplianceViolation("OWASP ASVS L2", "V5.3", "SQL or injection flaw detected",                  "must_block", ["CWE-89", "CWE-78"]),
    ComplianceViolation("OWASP ASVS L2", "V6.2", "Weak cryptographic algorithm in use",             "must_hold",  ["CWE-327", "CWE-328"]),
    ComplianceViolation("OWASP ASVS L2", "V4.1", "Access control missing on new functionality",     "must_hold",  ["CWE-862"]),
    ComplianceViolation("OWASP ASVS L2", "V10.3", "Malicious code or dangerous function usage",     "must_hold",  ["CWE-676"]),
]


class ComplianceChecker:
    """
    Evaluates security findings against regulatory controls.

    Usage:
        checker    = ComplianceChecker()
        violations = checker.check(security_result)
        if checker.must_block(violations):
            gate = GateDecision.BLOCK
    """

    def __init__(self, frameworks: list[str] | None = None) -> None:
        from config.settings import get_settings
        self._frameworks = frameworks or get_settings().compliance_frameworks
        self._controls   = [c for c in _CONTROL_MAPPING if c.framework in self._frameworks]

    def check(self, security: SecurityResult) -> list[ComplianceViolation]:
        """Return all control violations triggered by the security findings."""
        finding_cwes = {f.cwe_id for f in security.findings if f.cwe_id}
        violations:  list[ComplianceViolation] = []

        for control in self._controls:
            if finding_cwes & set(control.cwe_ids):
                violations.append(control)

        return violations

    def check_report(self, report: AnalysisReport) -> list[ComplianceViolation]:
        """Full report check: security + API breaking changes."""
        violations: list[ComplianceViolation] = []
        if report.security:
            violations.extend(self.check(report.security))
        # Breaking API changes without consumers identified is a TRM-G11 risk
        if report.interface and report.interface.breaking_changes:
            violations.append(ComplianceViolation(
                framework="MAS TRM", control_ref="TRM-G11",
                description="Breaking API change detected — downstream impact unverified",
                severity="must_hold", cwe_ids=[],
            ))
        return violations

    @staticmethod
    def must_block(violations: list[ComplianceViolation]) -> bool:
        return any(v.severity == "must_block" for v in violations)

    @staticmethod
    def must_hold(violations: list[ComplianceViolation]) -> bool:
        return any(v.severity in ("must_block", "must_hold") for v in violations)

    def derive_gate(self, violations: list[ComplianceViolation]) -> GateDecision:
        if self.must_block(violations):
            return GateDecision.BLOCK
        if self.must_hold(violations):
            return GateDecision.HOLD
        return GateDecision.APPROVE

    def format_flags(self, violations: list[ComplianceViolation]) -> list[str]:
        return [
            f"{v.framework} {v.control_ref}: {v.description}"
            for v in violations
        ]
