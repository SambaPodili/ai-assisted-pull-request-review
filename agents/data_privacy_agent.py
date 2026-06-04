"""
agents/data_privacy_agent.py
------------------------------
Data privacy impact analysis: static PII detection (zero tokens) + optional LLM
enhancement for banking systems subject to GDPR, PCI-DSS, and PDPA.

Static pass detects:
  - PII field names introduced in added code
  - PII values being logged or printed
  - Hardcoded PII literals (email addresses, phone numbers)
  - PII fields stored without encryption/hashing
  - Data structures holding PII without TTL/expiry hints
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.models import (
    AgentName, AgentResultBase, AnalysisRequest, DiffHunk,
    PIIFinding, DataPrivacyResult,
)
from agents.base_agent import BaseAgent, format_hunks_for_prompt

log = logging.getLogger(__name__)

# ── AgentName resolution ──────────────────────────────────────────────────────
try:
    _AGENT_NAME: AgentName = AgentName.DATA_PRIVACY  # type: ignore[attr-defined]
except AttributeError:
    _AGENT_NAME = "data_privacy"  # type: ignore[assignment]


# ═════════════════════════════════════════════════════════════════════════════
#  Static-detection patterns
# ═════════════════════════════════════════════════════════════════════════════

# PII field-name patterns mapped to their canonical pii_type
_PII_FIELD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\bemail\b',                    re.IGNORECASE), "email"),
    (re.compile(r'\bphone\b|\bmobile\b',         re.IGNORECASE), "phone"),
    (re.compile(r'\bssn\b|\bsocial_security\b',  re.IGNORECASE), "ssn"),
    (re.compile(r'\bcredit_card\b|\bcard_number\b|\bcvv\b|\bpan\b',
                                                  re.IGNORECASE), "credit_card"),
    (re.compile(r'\bdob\b|\bdate_of_birth\b',    re.IGNORECASE), "dob"),
    (re.compile(r'\bpassport\b',                 re.IGNORECASE), "passport"),
    (re.compile(r'\baddress\b|\bpostcode\b|\bzip_code\b',
                                                  re.IGNORECASE), "address"),
    (re.compile(r'\bfull_name\b|\bfirst_name\b|\blast_name\b',
                                                  re.IGNORECASE), "name"),
    (re.compile(r'\bnational_id\b',              re.IGNORECASE), "generic_pii"),
    (re.compile(r'\bip_address\b',               re.IGNORECASE), "ip"),
]

# Logging / print call patterns
_LOG_RE = re.compile(
    r'\b(?:log|logger|logging)\s*\.\s*(?:debug|info|warning|error|critical|exception|fatal)\s*\(|'
    r'\bprint\s*\(',
    re.IGNORECASE,
)

# Sensitive terms that must NOT appear inside log/print calls
_LOG_SENSITIVE_RE = re.compile(
    r'password|secret|token|credit_card|ssn|email|card_number|cvv|pan',
    re.IGNORECASE,
)

# Hardcoded email address in a string literal
_HARDCODED_EMAIL_RE = re.compile(
    r'["\'][\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}["\']'
)

# Hardcoded phone number in a string literal (simple international format)
_HARDCODED_PHONE_RE = re.compile(
    r'["\'][\+]?[\d\s\-\(\)]{7,15}["\']'
)

# Encryption / hashing / masking guard patterns
_ENCRYPT_RE = re.compile(
    r'\bencrypt\b|\bencrypted\b|\bhash\b|\bhashed\b|\bmask\b|\bmasked\b|'
    r'\bfernet\b|\baes\b|\brsa\b|\bbcrypt\b|\bpbkdf2\b|\bscrypt\b|\bargon\b',
    re.IGNORECASE,
)

# TTL / expiry patterns
_TTL_RE = re.compile(
    r'\bttl\b|\bexpiry\b|\bexpires\b|\bexpire_at\b|\bexpired_at\b|\bmax_age\b',
    re.IGNORECASE,
)

# Model / schema definition keywords — signs that a field is being declared
_MODEL_FIELD_RE = re.compile(
    r'=\s*(?:models\.|fields\.|Column\s*\(|Field\s*\(|CharField|IntegerField|'
    r'EmailField|TextField|DateField|property\s)',
    re.IGNORECASE,
)

_RISK_ORDER = ["low", "medium", "high", "critical"]


def _max_risk(risk_list: list[str]) -> str:
    if not risk_list:
        return "low"
    return max(risk_list, key=lambda r: _RISK_ORDER.index(r) if r in _RISK_ORDER else 0)


# ═════════════════════════════════════════════════════════════════════════════
#  Agent
# ═════════════════════════════════════════════════════════════════════════════

class DataPrivacyAgent(BaseAgent[DataPrivacyResult]):

    agent_name   = _AGENT_NAME
    output_model = DataPrivacyResult

    system_prompt = (
        "You are a data privacy and compliance expert specialising in banking systems "
        "subject to GDPR, PCI-DSS, and PDPA.\n"
        "Analyse the code diff for privacy and data protection risks:\n"
        "  • PII fields (email, phone, SSN, credit card, passport, etc.) added without "
        "encryption or hashing\n"
        "  • PII values written to logs or print statements\n"
        "  • Hardcoded PII literals in source code\n"
        "  • Data structures holding PII without TTL/expiry controls\n"
        "  • Data residency risks (cross-border storage or transfer of personal data)\n"
        "  • GDPR/PCI-DSS Article references where applicable\n\n"
        "For each finding: pii_type, description, file_path, line, context snippet, "
        "is_logged, is_encrypted, risk_level (low/medium/high/critical).\n"
        "List logging_violations as plain strings.\n"
        "gdpr_risk and data_exposure_risk = worst risk level observed.\n"
        "unencrypted_pii_count = number of unencrypted PII fields found.\n"
        "Output ONLY compact JSON matching the DataPrivacyResult schema."
    )

    # ── Public interface ──────────────────────────────────────────────────────

    def run(
        self,
        request: AnalysisRequest,
        budget: Any,
        context: dict[str, Any] | None = None,
    ) -> DataPrivacyResult:
        ctx = context or {}
        t0  = time.monotonic()

        # Step 1: deterministic static pass (zero tokens)
        static_pii, static_log_violations = self.detect_static_issues(request)

        # Step 2: attempt LLM enhancement if budget permits
        agent_key = self.agent_name if isinstance(self.agent_name, str) else self.agent_name.value
        remaining = budget.get_remaining(agent_key) if hasattr(budget, "get_remaining") else 0
        llm_result: DataPrivacyResult | None = None

        llm_attempted = remaining > 1500
        if llm_attempted:
            try:
                # Pass static results via context so build_user_prompt reuses them
                # instead of running detect_static_issues a second time.
                ctx["_static_pii"]            = static_pii
                ctx["_static_log_violations"] = static_log_violations
                llm_result = super().run(request, budget, ctx)
            except Exception as exc:
                log.warning("[%s] DataPrivacyAgent LLM error: %s", request.request_id, exc)

        # Step 3: merge static + LLM findings
        if llm_result is not None:
            seen = {(f.file_path, f.line, f.pii_type) for f in static_pii}
            for f in llm_result.pii_findings:
                if (f.file_path, f.line, f.pii_type) not in seen:
                    static_pii.append(f)
            # Merge logging violations (deduplicate)
            merged_violations = list(dict.fromkeys(static_log_violations + llm_result.logging_violations))
            result = llm_result
            result.pii_findings      = static_pii
            result.logging_violations = merged_violations
        else:
            result = self._build_result_from_static(static_pii, static_log_violations)
            result.fallback_used = True
            result.duration_s    = round(time.monotonic() - t0, 2)

        # Ensure derived fields are consistent
        result.unencrypted_pii_count = sum(
            1 for f in result.pii_findings if not f.is_encrypted
        )
        all_risks = [f.risk_level for f in result.pii_findings]
        result.gdpr_risk          = _max_risk(all_risks)
        result.data_exposure_risk = _max_risk(all_risks)

        if not llm_attempted:
            self.report_static_progress(request, result.duration_s)

        return result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        # Reuse static results computed in run() if available — avoids double hunk scan
        if "_static_pii" in context:
            static_pii      = context["_static_pii"]
            log_violations  = context["_static_log_violations"]
        else:
            static_pii, log_violations = self.detect_static_issues(request)

        pii_str = "\n".join(
            f"  [{f.risk_level.upper()}] {f.file_path}:{f.line} — {f.pii_type}: {f.description}"
            for f in static_pii
        ) or "  (none detected by static scan)"

        log_str = "\n".join(f"  {v}" for v in log_violations) or "  (none)"

        diff_block = format_hunks_for_prompt(
            request.hunks,
            max_chars_per_hunk=2000,
            focus="security",
        )
        return (
            f"Repository: {request.repo_url}\n"
            f"Changed files: {request.changed_files}\n\n"
            f"Static PII findings:\n{pii_str}\n\n"
            f"Logging violations detected:\n{log_str}\n\n"
            f"Diff:\n{diff_block}"
        )

    def fallback_result(self, request: AnalysisRequest) -> DataPrivacyResult:
        pii_findings, log_violations = self.detect_static_issues(request)
        return self._build_result_from_static(pii_findings, log_violations)

    # ── Static detection ──────────────────────────────────────────────────────

    def detect_static_issues(
        self, request: AnalysisRequest
    ) -> tuple[list[PIIFinding], list[str]]:
        """
        Scan all diff hunks for PII patterns.
        Returns (pii_findings, logging_violations).
        """
        all_pii:  list[PIIFinding] = []
        all_logs: list[str]        = []

        for hunk in (request.hunks or []):
            try:
                pii, logs = self._scan_hunk(hunk)
                all_pii.extend(pii)
                all_logs.extend(logs)
            except Exception as exc:
                log.debug("DataPrivacyAgent static scan error on %s: %s", hunk.file_path, exc)

        return all_pii, all_logs

    def _scan_hunk(self, hunk: DiffHunk) -> tuple[list[PIIFinding], list[str]]:
        pii_findings:      list[PIIFinding] = []
        log_violations:    list[str]        = []

        if not hunk or not hunk.content:
            return pii_findings, log_violations

        # Real source line numbers via @@ headers (not diff offsets)
        from ingestion.diff_parser import iter_added_lines
        added_lines: list[tuple[int, str]] = list(iter_added_lines(hunk.content))

        if not added_lines:
            return pii_findings, log_violations

        added_contents = [content for _, content in added_lines]

        for pos, (hunk_lineno, content) in enumerate(added_lines):
            if not content.strip():
                continue

            # ── PII field name detection ──────────────────────────────────────
            for pattern, pii_type in _PII_FIELD_PATTERNS:
                if pattern.search(content):
                    # Check if it looks like a field/variable declaration
                    is_field = bool(_MODEL_FIELD_RE.search(content)) or (
                        "=" in content or ":" in content
                    )
                    if not is_field:
                        continue  # skip mere references in comments etc.

                    # Check for encryption guard in ±10 lines window
                    window_start = max(0, pos - 10)
                    window_end   = min(len(added_contents), pos + 11)
                    window       = added_contents[window_start:window_end]
                    is_encrypted = any(_ENCRYPT_RE.search(w) for w in window)

                    # Check for TTL hint in ±10 lines
                    has_ttl = any(_TTL_RE.search(w) for w in window)

                    risk = "high" if pii_type in ("ssn", "credit_card", "passport") else "medium"
                    if is_encrypted:
                        risk = "low"
                    if not is_encrypted and pii_type in ("ssn", "credit_card"):
                        risk = "critical"

                    desc = f"PII field '{pii_type}' introduced"
                    if not is_encrypted:
                        desc += " without apparent encryption/hashing"
                    if not has_ttl:
                        desc += "; no TTL/expiry detected"

                    pii_findings.append(PIIFinding(
                        pii_type=pii_type,
                        description=desc,
                        file_path=hunk.file_path,
                        line=hunk_lineno,
                        context=content.strip()[:120],
                        is_logged=False,
                        is_encrypted=is_encrypted,
                        risk_level=risk,
                    ))
                    break  # one finding per line is enough

            # ── Logging PII ───────────────────────────────────────────────────
            if _LOG_RE.search(content) and _LOG_SENSITIVE_RE.search(content):
                violation = (
                    f"{hunk.file_path}:{hunk_lineno} — sensitive field in log/print: "
                    f"{content.strip()[:100]}"
                )
                log_violations.append(violation)

                # Also emit a PIIFinding for this
                matched_type = "generic_pii"
                for pattern, pii_type in _PII_FIELD_PATTERNS:
                    if pattern.search(content):
                        matched_type = pii_type
                        break
                pii_findings.append(PIIFinding(
                    pii_type=matched_type,
                    description=(
                        f"PII/sensitive data may be written to logs at line {hunk_lineno}."
                    ),
                    file_path=hunk.file_path,
                    line=hunk_lineno,
                    context=content.strip()[:120],
                    is_logged=True,
                    is_encrypted=False,
                    risk_level="high",
                ))

            # ── Hardcoded PII literals ────────────────────────────────────────
            if _HARDCODED_EMAIL_RE.search(content):
                pii_findings.append(PIIFinding(
                    pii_type="email",
                    description=(
                        f"Hardcoded email address literal in source at line {hunk_lineno}."
                    ),
                    file_path=hunk.file_path,
                    line=hunk_lineno,
                    context=content.strip()[:120],
                    is_logged=False,
                    is_encrypted=False,
                    risk_level="medium",
                ))

            if _HARDCODED_PHONE_RE.search(content):
                # Quick sanity: require at least one digit group of 4+
                m = _HARDCODED_PHONE_RE.search(content)
                if m and len(re.sub(r'\D', '', m.group())) >= 7:
                    pii_findings.append(PIIFinding(
                        pii_type="phone",
                        description=(
                            f"Hardcoded phone number literal in source at line {hunk_lineno}."
                        ),
                        file_path=hunk.file_path,
                        line=hunk_lineno,
                        context=content.strip()[:120],
                        is_logged=False,
                        is_encrypted=False,
                        risk_level="medium",
                    ))

        return pii_findings, log_violations

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _build_result_from_static(
        pii_findings: list[PIIFinding],
        log_violations: list[str],
    ) -> DataPrivacyResult:
        all_risks  = [f.risk_level for f in pii_findings]
        gdpr_risk  = _max_risk(all_risks) if all_risks else "low"
        unenc      = sum(1 for f in pii_findings if not f.is_encrypted)

        if pii_findings or log_violations:
            summary = (
                f"{len(pii_findings)} PII finding(s) and {len(log_violations)} "
                f"logging violation(s) detected by static analysis. "
                f"GDPR risk: {gdpr_risk}. Unencrypted PII fields: {unenc}."
            )
        else:
            summary = "No PII or data privacy issues detected by static analysis."

        return DataPrivacyResult(
            pii_findings=pii_findings,
            logging_violations=log_violations,
            gdpr_risk=gdpr_risk,
            data_exposure_risk=gdpr_risk,
            unencrypted_pii_count=unenc,
            summary=summary,
            fallback_used=True,
        )
