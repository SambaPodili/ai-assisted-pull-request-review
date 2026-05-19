"""
analysis/entropy.py
--------------------
Shannon entropy-based secret detection.

Secrets in source code have characteristically HIGH entropy:
  - English prose:          ~3.5 bits/char
  - Variable names/paths:   ~3.8 bits/char
  - Base64 API keys/tokens: ~5.8 bits/char
  - Hex digests/secrets:    ~4.0 bits/char
  - UUID-format tokens:     ~3.9 bits/char (after removing dashes)
  - Random alphanumeric:    ~5.4 bits/char

The key insight: real secrets are derived from cryptographically random
sources and therefore have entropy significantly above normal code strings.
A 40-char string with entropy > 4.5 is almost certainly a secret.

This catches secrets that regex SAST misses:
  - Non-patterned API keys (not starting with sk-, ghp_, etc.)
  - Base64-encoded credentials
  - Raw hex secrets
  - JWT tokens in non-standard locations
"""
from __future__ import annotations
import math
import re
from collections import Counter
from dataclasses import dataclass

# ── Thresholds ────────────────────────────────────────────────────────────────

# Minimum string length to evaluate (short strings have unreliable entropy)
MIN_LENGTH = 16

# Entropy thresholds (bits per character)
ENTROPY_HIGH    = 4.5   # Almost certainly a secret
ENTROPY_MEDIUM  = 3.8   # Suspicious — worth flagging with lower severity
ENTROPY_CHARSET_BONUS = 0.3   # Added when charset looks like token chars

# Character sets used by common secret formats
BASE64_CHARS  = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
HEX_CHARS     = set("0123456789abcdefABCDEF")
ALPHANUM_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")

# Known non-secret high-entropy patterns to suppress (false positives)
WHITELIST_PATTERNS = [
    re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I),  # UUID
    re.compile(r'^[\w./+-]+==$'),                 # base64 padding only
    re.compile(r'^\d+\.\d+\.\d+(\.\d+)?$'),       # version numbers
    re.compile(r'^[A-Z][A-Z0-9_]{2,}$'),           # constants/enum values
    re.compile(r'^\$\{[^}]+\}$'),                  # template variables ${VAR}
    re.compile(r'^<%[^%]+%>$'),                    # template tags
    re.compile(r'^https?://'),                     # URLs
    re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z]{2,}$', re.I),  # emails
]

# Context keywords that increase confidence this is a secret
SECRET_CONTEXT_KEYWORDS = {
    'key', 'secret', 'password', 'passwd', 'pwd', 'token', 'auth',
    'credential', 'cred', 'api', 'access', 'private', 'jwt', 'session',
    'hmac', 'hash', 'salt', 'nonce', 'cipher', 'encrypt',
}


@dataclass
class EntropyHit:
    """A string literal with suspiciously high entropy."""
    value:         str         # full original value
    preview:       str         # first 6 chars + *** (for display)
    entropy:       float       # Shannon entropy in bits/char
    length:        int
    charset:       str         # "base64" | "hex" | "alphanum" | "mixed"
    likely_type:   str         # "api_key" | "token" | "password" | "unknown"
    confidence:    str         # "high" | "medium"
    line_content:  str         # surrounding line for context
    line_number:   int
    file_path:     str
    context_word:  str         # nearby variable/key name that hints at secret type


def shannon_entropy(text: str) -> float:
    """
    Compute Shannon entropy in bits per character.
    H = -Σ p(x) * log₂(p(x))
    """
    if not text:
        return 0.0
    freq = Counter(text)
    length = len(text)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in freq.values()
    )


def classify_charset(value: str) -> str:
    chars = set(value)
    if chars <= HEX_CHARS:
        return "hex"
    if chars <= BASE64_CHARS:
        return "base64"
    if chars <= ALPHANUM_CHARS:
        return "alphanum"
    return "mixed"


def is_whitelisted(value: str) -> bool:
    return any(pat.match(value) for pat in WHITELIST_PATTERNS)


def infer_type(context_word: str, charset: str, length: int) -> str:
    cw = context_word.lower()
    if any(k in cw for k in ('password', 'passwd', 'pwd')):
        return "password"
    if any(k in cw for k in ('token', 'jwt', 'session', 'csrf')):
        return "token"
    if any(k in cw for k in ('key', 'api', 'apikey', 'secret')):
        return "api_key"
    if charset == "hex" and length in (32, 40, 64):
        return "hash/secret"
    if charset == "base64":
        return "token/credential"
    return "unknown_secret"


def extract_context_word(line: str, value_pos: int) -> str:
    """Extract the variable name or key closest to the secret value."""
    before = line[:value_pos]
    m = re.search(r'[\w_]+\s*$', before)
    if m:
        return m.group().strip()
    return ""


# ── Main scanner ──────────────────────────────────────────────────────────────

# Extract string literals from source code lines
_STRING_LITERAL = re.compile(
    r'(?:"([^"\\]{16,})"'    # double-quoted
    r"|'([^'\\]{16,})')"     # single-quoted
)


def scan_diff_for_entropy(diff_text: str, file_path: str = "") -> list[EntropyHit]:
    """
    Scan a unified diff for high-entropy string literals.

    Only examines ADDED lines (starting with +) — we don't want to flag
    lines that were already in the codebase and are being removed.
    """
    hits: list[EntropyHit] = []
    line_number = 0

    for raw_line in diff_text.splitlines():
        # Track line numbers from @@ headers
        hunk_header = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)', raw_line)
        if hunk_header:
            line_number = int(hunk_header.group(1)) - 1
            continue

        if raw_line.startswith('+') and not raw_line.startswith('+++'):
            line_number += 1
            line_content = raw_line[1:]  # strip leading +

            for m in _STRING_LITERAL.finditer(line_content):
                value = m.group(1) or m.group(2)
                if not value or len(value) < MIN_LENGTH:
                    continue
                if is_whitelisted(value):
                    continue

                entropy = shannon_entropy(value)
                charset = classify_charset(value)

                # Charset-aware threshold adjustment
                threshold = ENTROPY_HIGH
                if charset in ("base64", "alphanum"):
                    threshold -= ENTROPY_CHARSET_BONUS

                if entropy < ENTROPY_MEDIUM:
                    continue

                confidence = "high" if entropy >= threshold else "medium"
                ctx_word = extract_context_word(line_content, m.start())

                # Extra boost if nearby context word is secret-related
                if any(k in ctx_word.lower() for k in SECRET_CONTEXT_KEYWORDS):
                    confidence = "high"

                preview = value[:6] + "***" if len(value) > 9 else "***"

                hits.append(EntropyHit(
                    value=value,
                    preview=preview,
                    entropy=round(entropy, 3),
                    length=len(value),
                    charset=charset,
                    likely_type=infer_type(ctx_word, charset, len(value)),
                    confidence=confidence,
                    line_content=line_content.strip(),
                    line_number=line_number,
                    file_path=file_path,
                    context_word=ctx_word,
                ))
        elif not raw_line.startswith('-'):
            line_number += 1

    # Deduplicate by value preview (same secret appearing in multiple lines)
    seen: set[str] = set()
    unique: list[EntropyHit] = []
    for hit in hits:
        if hit.preview not in seen:
            seen.add(hit.preview)
            unique.append(hit)

    return sorted(unique, key=lambda h: h.entropy, reverse=True)


def scan_multiple_hunks(hunks: list, file_path: str = "") -> list[EntropyHit]:
    """Scan a list of DiffHunk objects."""
    all_hits: list[EntropyHit] = []
    for hunk in hunks:
        fp = getattr(hunk, 'file_path', file_path)
        content = getattr(hunk, 'content', str(hunk))
        all_hits.extend(scan_diff_for_entropy(content, fp))
    return all_hits
