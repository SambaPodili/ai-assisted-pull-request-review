"""
agents/security_agent.py
-------------------------
Phase 1 agent: scans for security vulnerabilities.

Model: Sonnet (strong) — security findings must be accurate and low false-positive.
Fallback: deterministic regex-based SAST covering the most critical CWEs.

Banking context: MAS TRM, PCI-DSS 4.0, OWASP ASVS Level 2.
"""
from __future__ import annotations
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    SecurityResult, SecurityFinding, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent, format_hunks_for_prompt
from ingestion.language_registry import security_concerns, lang_meta


# ── Regex SAST patterns ───────────────────────────────────────────────────────

_SECRET_PATTERNS: list[tuple[str, str, str, re.Pattern]] = [
    # (label, cwe_id, owasp_cat, pattern)
    (
        "Hardcoded password",
        "CWE-798", "A07:2021",
        re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\'][^"\']{4,}["\']'),
    ),
    (
        "Hardcoded API key",
        "CWE-798", "A07:2021",
        re.compile(r'(?i)(api_key|apikey|secret_key|access_key)\s*[=:]\s*["\'][^"\']{8,}["\']'),
    ),
    (
        "AWS access key",
        "CWE-798", "A07:2021",
        re.compile(r'AKIA[0-9A-Z]{16}'),
    ),
    (
        "Private key material",
        "CWE-321", "A02:2021",
        re.compile(r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----'),
    ),
    (
        "Bearer token in code",
        "CWE-522", "A07:2021",
        re.compile(r'(?i)Bearer\s+[A-Za-z0-9\-._~+/]{20,}'),
    ),
    (
        "Database connection string with credentials",
        "CWE-798", "A07:2021",
        re.compile(r'(?i)(jdbc|mongodb(\+srv)?|redis|postgresql)://[^@\s]+:[^@\s]+@'),
    ),
]

_INJECTION_PATTERNS: list[tuple[str, str, str, re.Pattern]] = [
    (
        "Possible SQL injection via string concatenation",
        "CWE-89", "A03:2021",
        re.compile(r'(?i)(execute|executeQuery|createQuery|nativeQuery|rawQuery|cursor\.execute)\s*\([^)]*\+'),
    ),
    (
        "Possible LDAP injection",
        "CWE-90", "A03:2021",
        re.compile(r'(?i)ldap.*search.*\+'),
    ),
    (
        "Possible command injection",
        "CWE-78", "A03:2021",
        re.compile(r'(?i)(exec|system|popen|subprocess\.call)\s*\([^)]*\+'),
    ),
]

_CRYPTO_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("Weak hash function (MD5)",      "CWE-327", re.compile(r'(?i)MD5\s*[.(]')),
    ("Weak hash function (SHA1)",     "CWE-327", re.compile(r'(?i)(SHA1|SHA-1)\s*[.(]')),
    ("Deprecated cipher (DES/3DES)",  "CWE-327", re.compile(r'(?i)(DES|3DES|TripleDES)\s*[.(]')),
    ("Insecure ECB mode",             "CWE-327", re.compile(r'(?i)(AES|DES).*ECB')),
]

_PAN_PATTERN = re.compile(r'(?<!\d)(?:4[0-9]{12}|5[1-5][0-9]{14}|3[47][0-9]{13})(?!\d)')


class SecurityReviewAgent(BaseAgent[SecurityResult]):

    agent_name   = AgentName.SECURITY
    output_model = SecurityResult

    system_prompt = (
        "You are a senior application security engineer specialising in enterprise banking systems.\n"
        "Analyse the provided unified diff for security vulnerabilities:\n"
        "  1. Hardcoded secrets, API keys, credentials, tokens\n"
        "  2. Injection flaws: SQL, LDAP, command, template injection\n"
        "  3. Insecure deserialization and dangerous object construction\n"
        "  4. Missing authentication / authorisation on new endpoints\n"
        "  5. Weak cryptographic algorithms (MD5, SHA1, DES, RC4, ECB mode)\n"
        "  6. SSRF (Server-Side Request Forgery) patterns\n"
        "  7. PCI-DSS 4.0: PAN/CVV exposure, logging of card data\n"
        "  8. MAS TRM: data classification violations, key management issues\n"
        "  9. OWASP ASVS L2: session management, input validation gaps\n\n"
        "For each finding: file_path, line_range, severity (critical/high/medium/low), "
        "cwe_id, owasp_cat, mas_trm_ref, description, remediation.\n"
        "Output ONLY compact JSON. No prose, no preamble."
    )

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        frameworks = context.get("compliance_frameworks", ["MAS TRM", "PCI-DSS 4.0"])

        # Collect language-specific security concerns across all changed files
        lang_concerns: list[str] = []
        seen_langs: set[str] = set()
        for h in request.hunks:
            if h.language not in seen_langs:
                seen_langs.add(h.language)
                concerns = security_concerns(h.language)
                if concerns:
                    meta = lang_meta(h.language)
                    lang_concerns.append(f"  {meta.display}: {', '.join(concerns[:4])}")

        lang_block = ""
        if lang_concerns:
            lang_block = "Language-specific risks to check:\n" + "\n".join(lang_concerns) + "\n\n"

        diff_block = format_hunks_for_prompt(request.hunks, max_chars_per_hunk=3000, focus="security")
        return (
            f"Repository: {request.repo_url}\n"
            f"Compliance scope: {', '.join(frameworks)}\n"
            f"Languages detected: {', '.join(sorted(seen_langs))}\n\n"
            f"{lang_block}"
            f"DIFF:\n{diff_block}"
        )

    def fallback_result(self, request: AnalysisRequest) -> SecurityResult:
        """Full regex-based SAST scan — no LLM tokens consumed."""
        combined = "\n".join(h.content for h in request.hunks)
        findings: list[SecurityFinding] = []

        # Secrets
        for label, cwe, owasp, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(combined):
                findings.append(_make_finding(
                    combined, m, request,
                    severity=RiskLevel.CRITICAL,
                    cwe_id=cwe, owasp_cat=owasp,
                    description=f"{label} detected in code.",
                    remediation="Store in secrets vault (CyberArk / HashiCorp Vault). Never commit credentials.",
                    mas_trm_ref="TRM-G05",
                ))

        # Injection
        for label, cwe, owasp, pattern in _INJECTION_PATTERNS:
            for m in pattern.finditer(combined):
                findings.append(_make_finding(
                    combined, m, request,
                    severity=RiskLevel.HIGH,
                    cwe_id=cwe, owasp_cat=owasp,
                    description=label,
                    remediation="Use parameterised queries / prepared statements. Never concatenate user input.",
                ))

        # Weak crypto
        for label, cwe, pattern in _CRYPTO_PATTERNS:
            for m in pattern.finditer(combined):
                findings.append(_make_finding(
                    combined, m, request,
                    severity=RiskLevel.HIGH,
                    cwe_id=cwe, owasp_cat="A02:2021",
                    description=label,
                    remediation="Replace with AES-256-GCM or SHA-256+. Use BCrypt/Argon2 for passwords.",
                ))

        # PAN exposure
        for m in _PAN_PATTERN.finditer(combined):
            findings.append(_make_finding(
                combined, m, request,
                severity=RiskLevel.CRITICAL,
                cwe_id="CWE-312", owasp_cat="A02:2021",
                description="Possible PAN (payment card number) exposed in code.",
                remediation="Remove card data from source. Use tokenisation. PCI-DSS Req 3.3.",
                mas_trm_ref="TRM-G07",
            ))

        worst = _max_severity(findings)
        secrets_found = any(f.cwe_id in {"CWE-798", "CWE-321", "CWE-522"} for f in findings)

        return SecurityResult(
            findings=findings,
            secrets_detected=secrets_found,
            compliance_flags=_derive_flags(findings),
            overall_severity=worst,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_finding(
    combined: str,
    match:    re.Match,
    request:  AnalysisRequest,
    severity: RiskLevel,
    cwe_id:   str,
    owasp_cat: str,
    description: str,
    remediation: str,
    mas_trm_ref: str = "",
) -> SecurityFinding:
    line_num = combined[: match.start()].count("\n") + 1
    return SecurityFinding(
        file_path=_guess_file(combined, match.start(), request),
        line_range=str(line_num),
        severity=severity,
        cwe_id=cwe_id,
        owasp_cat=owasp_cat,
        mas_trm_ref=mas_trm_ref,
        description=description,
        remediation=remediation,
    )


def _guess_file(combined: str, pos: int, request: AnalysisRequest) -> str:
    before = combined[:pos]
    for line in reversed(before.splitlines()):
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/"):
            return line[6:].strip()
    return request.hunks[0].file_path if request.hunks else "unknown"


def _max_severity(findings: list[SecurityFinding]) -> RiskLevel:
    order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    if not findings:
        return RiskLevel.LOW
    return max(findings, key=lambda f: order.index(f.severity)).severity


def _derive_flags(findings: list[SecurityFinding]) -> list[str]:
    flags = []
    cwes  = {f.cwe_id for f in findings}
    if {"CWE-798", "CWE-321"} & cwes:
        flags.append("PCI-DSS 4.0 Req 8: Hardcoded credential detected")
        flags.append("MAS TRM TRM-G05: Credential management violation")
    if "CWE-312" in cwes:
        flags.append("PCI-DSS 4.0 Req 3: Cardholder data storage violation")
        flags.append("MAS TRM TRM-G07: Data classification — sensitive financial data exposed")
    if "CWE-89" in cwes:
        flags.append("OWASP ASVS L2 V5: SQL injection risk")
    if "CWE-327" in cwes:
        flags.append("OWASP ASVS L2 V6: Cryptographic controls violation")
    return flags
