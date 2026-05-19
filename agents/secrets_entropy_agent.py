"""
agents/secrets_entropy_agent.py
---------------------------------
Advanced secret detection using information-theoretic analysis.

Beyond regex patterns (which catch obvious secrets like password="abc123"),
this agent uses Shannon entropy to detect high-randomness strings that are
likely to be cryptographic keys, tokens, or other secrets — even when they
have no recognisable variable name or prefix.

Three detection layers (all run as fallback — zero LLM tokens):

  Layer 1: Known prefixes
    Platform-specific key formats that begin with a recognisable signature:
    sk-, pk-, ghp_, AKIA, xoxb-, Bearer, pat-, etc.

  Layer 2: Shannon entropy scoring
    Measures bits-per-character of string literals. A truly random 32-character
    string has entropy ~5.5 bits/char. English prose is ~4.0 bits/char.
    High-entropy threshold: > 3.7 bits/char for strings >= 20 chars.

  Layer 3: Base64 / hex blob detection
    Long base64 or hex strings embedded in code — often encode keys or
    certificates that were committed by accident.

Banking context: PCI-DSS Req 8.2 — no hardcoded credentials.
                 MAS TRM TRM-G05 — key management controls.
"""
from __future__ import annotations
import math
import re
import base64
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    SecretsEntropyResult, EntropyFinding, RiskLevel,
)
from agents.base_agent import BaseAgent


# ── Entropy calculator ────────────────────────────────────────────────────────

def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    n = len(s)
    return -sum((f / n) * math.log2(f / n) for f in freq.values())


# ── Known key prefixes ────────────────────────────────────────────────────────

_KNOWN_PREFIXES: list[tuple[str, str, RiskLevel]] = [
    # (prefix_pattern, description, severity)
    (r'sk-ant-api\d+-[A-Za-z0-9_\-]{20,}', 'Anthropic API key',        RiskLevel.CRITICAL),
    (r'sk-[A-Za-z0-9]{20,}',               'OpenAI API key',            RiskLevel.CRITICAL),
    (r'AKIA[0-9A-Z]{16}',                  'AWS access key ID',          RiskLevel.CRITICAL),
    (r'ghp_[A-Za-z0-9]{36}',               'GitHub personal access token',RiskLevel.CRITICAL),
    (r'ghs_[A-Za-z0-9]{36}',               'GitHub service token',       RiskLevel.CRITICAL),
    (r'ghr_[A-Za-z0-9]{36}',               'GitHub refresh token',       RiskLevel.CRITICAL),
    (r'xoxb-[0-9A-Za-z\-]{24,}',           'Slack bot token',            RiskLevel.CRITICAL),
    (r'xoxa-[0-9A-Za-z\-]{24,}',           'Slack app token',            RiskLevel.CRITICAL),
    (r'xoxp-[0-9A-Za-z\-]{24,}',           'Slack user token',           RiskLevel.CRITICAL),
    (r'EAAl[A-Za-z0-9]+',                  'Facebook access token',      RiskLevel.HIGH),
    (r'AIza[0-9A-Za-z\-_]{35}',            'Google API key',             RiskLevel.HIGH),
    (r'ya29\.[0-9A-Za-z\-_]+',             'Google OAuth token',          RiskLevel.HIGH),
    (r'pk_live_[A-Za-z0-9]{24,}',          'Stripe live publishable key', RiskLevel.HIGH),
    (r'sk_live_[A-Za-z0-9]{24,}',          'Stripe live secret key',      RiskLevel.CRITICAL),
    (r'rk_live_[A-Za-z0-9]{24,}',          'Stripe restricted key',       RiskLevel.CRITICAL),
    (r'SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}', 'SendGrid API key', RiskLevel.HIGH),
    (r'AC[a-z0-9]{32}',                    'Twilio account SID',          RiskLevel.HIGH),
    (r'SK[a-z0-9]{32}',                    'Twilio auth SID',             RiskLevel.HIGH),
    (r'token[_\-]?[=:]\s*["\'][A-Za-z0-9_\-\.]{20,}["\']', 'Generic token', RiskLevel.MEDIUM),
]

# ── High-entropy patterns (string literals in code) ───────────────────────────

# Match quoted string literals (single or double quotes)
_STRING_LITERAL = re.compile(r'["\']([A-Za-z0-9+/=\-_\.]{16,})["\']')

# Hex strings 32+ chars (often MD5 hashes used as keys, or hex-encoded secrets)
_HEX_BLOB = re.compile(r'["\']([0-9a-fA-F]{32,})["\']')

# Base64 blobs 40+ chars
_B64_BLOB = re.compile(r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']')

# Variable names that suggest secrets (used to contextualise entropy hits)
_SECRET_VARIABLE_NAMES = re.compile(
    r'(?i)(password|passwd|pwd|secret|token|api_key|apikey|auth|credential|'
    r'private_key|access_key|client_secret|app_secret|jwt_secret|signing_key|'
    r'encryption_key|hmac_key|salt|master_key|db_pass|database_password)\s*[=:]'
)

# ── Safe entropy exceptions ────────────────────────────────────────────────────

# High-entropy strings that are NOT secrets (UUIDs, hashes in test fixtures, etc.)
_SAFE_PATTERNS = re.compile(
    r'(?i)(test|mock|example|placeholder|replace_me|your_key|xxx|'
    r'00000000|ffffffff|deadbeef|cafebabe|12345678)'
)

ENTROPY_THRESHOLD     = 3.7   # bits/char — above this = high entropy
MIN_SECRET_LENGTH     = 20    # minimum string length to flag
HIGH_SEVERITY_ENTROPY = 4.5   # above this = CRITICAL


class SecretsEntropyAgent(BaseAgent[SecretsEntropyResult]):

    agent_name   = AgentName.SECRETS_ENTROPY
    output_model = SecretsEntropyResult

    system_prompt = (
        "You are an advanced secret detection specialist for enterprise banking systems.\n"
        "Analyse the provided code diff for secrets using entropy analysis and pattern recognition.\n"
        "For each suspicious string found:\n"
        "  • entropy: Shannon entropy value (bits per character)\n"
        "  • kind: high_entropy | known_prefix | base64_blob | hex_key\n"
        "  • severity: critical | high | medium | low\n"
        "  • variable: the variable name the secret is assigned to\n"
        "  • value: first 6 chars only (do NOT include the full secret)\n"
        "Focus on: long random strings, known API key prefixes, base64-encoded keys, hex-encoded secrets.\n"
        "Output ONLY compact JSON."
    )

    # ── Always use deterministic fallback (no LLM for secrets) ────────────────

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> SecretsEntropyResult:
        """Always runs the deterministic multi-layer scanner. No LLM needed."""
        return self.fallback_result(request)

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        return "\n\n".join(h.content for h in request.hunks)

    def fallback_result(self, request: AnalysisRequest) -> SecretsEntropyResult:
        """
        Three-layer secret detection. Zero LLM tokens consumed.
        """
        findings:       list[EntropyFinding] = []
        known_prefix_count = 0
        high_entropy_count = 0

        combined = "\n".join(h.content for h in request.hunks)
        lines    = combined.splitlines()

        for line_no, line in enumerate(lines, 1):
            # Only examine added lines (not context or removed lines)
            if not line.startswith("+") or line.startswith("+++"):
                continue
            content = line[1:]

            # ── Layer 1: Known key prefixes ───────────────────────────────────
            for pattern, description, severity in _KNOWN_PREFIXES:
                for m in re.finditer(pattern, content):
                    matched = m.group()
                    if _SAFE_PATTERNS.search(matched):
                        continue
                    e = shannon_entropy(matched)
                    findings.append(EntropyFinding(
                        file_path=_guess_file(lines, line_no),
                        line=line_no,
                        value=matched[:6] + "...",
                        entropy=round(e, 2),
                        kind="known_prefix",
                        severity=severity,
                        variable=_extract_variable(content),
                    ))
                    known_prefix_count += 1

            # ── Layer 2: Shannon entropy on string literals ───────────────────
            for m in _STRING_LITERAL.finditer(content):
                s = m.group(1)
                if len(s) < MIN_SECRET_LENGTH:
                    continue
                if _SAFE_PATTERNS.search(s):
                    continue
                e = shannon_entropy(s)
                if e < ENTROPY_THRESHOLD:
                    continue

                # Is this a secret variable? Boost severity
                is_secret_var = bool(_SECRET_VARIABLE_NAMES.search(content))
                severity = (
                    RiskLevel.CRITICAL if e >= HIGH_SEVERITY_ENTROPY or is_secret_var
                    else RiskLevel.HIGH if e >= 4.0
                    else RiskLevel.MEDIUM
                )
                findings.append(EntropyFinding(
                    file_path=_guess_file(lines, line_no),
                    line=line_no,
                    value=s[:6] + "...",
                    entropy=round(e, 2),
                    kind="high_entropy",
                    severity=severity,
                    variable=_extract_variable(content),
                ))
                high_entropy_count += 1

            # ── Layer 3: Base64 / hex blobs ───────────────────────────────────
            for pattern, kind in [(_B64_BLOB, "base64_blob"), (_HEX_BLOB, "hex_key")]:
                for m in pattern.finditer(content):
                    s = m.group(1)
                    e = shannon_entropy(s)
                    if e < 3.2 or _SAFE_PATTERNS.search(s):
                        continue
                    # Verify it's actually valid base64 for base64 blobs
                    if kind == "base64_blob":
                        try:
                            base64.b64decode(s + "==")
                        except Exception:
                            continue
                    # Skip if already caught by known prefix layer
                    if any(f.line == line_no and f.kind == "known_prefix" for f in findings):
                        continue
                    findings.append(EntropyFinding(
                        file_path=_guess_file(lines, line_no),
                        line=line_no,
                        value=s[:6] + "...",
                        entropy=round(e, 2),
                        kind=kind,
                        severity=RiskLevel.HIGH,
                        variable=_extract_variable(content),
                    ))

        # Deduplicate by (file, line, kind)
        seen: set[tuple] = set()
        unique: list[EntropyFinding] = []
        for f in findings:
            key = (f.file_path, f.line, f.kind)
            if key not in seen:
                seen.add(key)
                unique.append(f)

        worst = _max_severity(unique)
        return SecretsEntropyResult(
            findings=unique,
            high_entropy_count=high_entropy_count,
            known_prefix_count=known_prefix_count,
            overall_severity=worst,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guess_file(lines: list[str], current_line: int) -> str:
    for line in reversed(lines[:current_line]):
        if line.startswith("+++ b/"):
            return line[6:].strip()
    return "unknown"


def _extract_variable(line: str) -> str:
    m = re.search(r'(\w+)\s*[=:]', line)
    return m.group(1) if m else ""


def _max_severity(findings: list[EntropyFinding]) -> RiskLevel:
    order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    if not findings:
        return RiskLevel.LOW
    return max(findings, key=lambda f: order.index(f.severity)).severity
