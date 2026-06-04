"""
agents/fix_generator.py
------------------------
Deterministic, high-confidence code-fix generator.

Produces concrete before/after patches (and unified diffs) for well-known,
unambiguous anti-patterns found in the changed (added) lines — no LLM, so the
suggestions are exact and safe to copy. The remediation agent merges these with
any LLM-suggested fixes.

Each rule is intentionally conservative: it only fires when the fix is
mechanical and correct, so a reviewer can apply it with confidence.
"""
from __future__ import annotations
import re

from core.models import AnalysisRequest, CodeFix
from ingestion.diff_parser import iter_added_lines


# ── Rules: (name, category, severity, matcher, fixer, explanation) ────────────
# matcher: regex on the stripped line. fixer: (line) -> replacement line or None.

_SECRET_ASSIGN = re.compile(
    r'^(\s*)([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|API_KEY))\s*=\s*["\'][^"\']{6,}["\']',
    re.IGNORECASE,
)
_MD5  = re.compile(r'hashlib\.md5\s*\(')
_SHA1 = re.compile(r'hashlib\.sha1\s*\(')
_BARE_EXCEPT = re.compile(r'^(\s*)except\s*:\s*$')
_VERIFY_FALSE = re.compile(r'verify\s*=\s*False')
_DEBUG_TRUE   = re.compile(r'^(\s*)DEBUG\s*=\s*True\s*$')
_SELECT_STAR  = re.compile(r'SELECT\s+\*\s+FROM', re.IGNORECASE)
_YAML_LOAD    = re.compile(r'yaml\.load\s*\(([^,)]*)\)')   # without Loader=


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _diff(file_path: str, line_no: int, before: str, after: str) -> str:
    return (
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"@@ line {line_no} @@\n"
        f"-{before}\n"
        f"+{after}"
    )


def _make(file_path, line_no, title, category, severity, before, after, explanation, confidence="high") -> CodeFix:
    return CodeFix(
        title=title, file_path=file_path, category=category, severity=severity,
        before=before, after=after, diff=_diff(file_path, line_no, before, after),
        explanation=explanation, confidence=confidence,
    )


def generate_fixes(request: AnalysisRequest, max_fixes: int = 12) -> list[CodeFix]:
    fixes: list[CodeFix] = []
    seen: set[tuple] = set()

    for hunk in request.hunks:
        fp = hunk.file_path
        for line_no, raw in iter_added_lines(hunk.content):
            code = raw.rstrip()
            stripped = code.strip()
            if not stripped:
                continue
            ind = _indent(code)

            # 1) Hardcoded secret → environment variable
            m = _SECRET_ASSIGN.match(code)
            if m:
                var = m.group(2)
                after = f'{m.group(1)}{var} = os.environ["{var.upper()}"]'
                key = (fp, line_no, "secret")
                if key not in seen:
                    seen.add(key)
                    fixes.append(_make(
                        fp, line_no, f"Move hardcoded secret `{var}` to an env var",
                        "security", "critical", code, after,
                        "Never commit credentials. Read from the environment (or a secrets manager) "
                        "and rotate the exposed value immediately.",
                    ))
                continue

            # 2) Weak hash → SHA-256
            if _MD5.search(code) or _SHA1.search(code):
                after = _SHA1.sub("hashlib.sha256(", _MD5.sub("hashlib.sha256(", code))
                fixes.append(_make(
                    fp, line_no, "Replace weak hash (MD5/SHA-1) with SHA-256",
                    "security", "high", code, after,
                    "MD5 and SHA-1 are cryptographically broken. Use SHA-256 (or bcrypt/argon2 for passwords).",
                ))
                continue

            # 3) Bare except → except Exception
            be = _BARE_EXCEPT.match(code)
            if be:
                after = f"{be.group(1)}except Exception as exc:"
                fixes.append(_make(
                    fp, line_no, "Replace bare `except:` with `except Exception`",
                    "quality", "high", code, after,
                    "A bare except also swallows KeyboardInterrupt/SystemExit. Catch Exception and log it.",
                ))
                continue

            # 4) TLS verification disabled
            if _VERIFY_FALSE.search(code):
                after = _VERIFY_FALSE.sub("verify=True", code)
                fixes.append(_make(
                    fp, line_no, "Re-enable TLS certificate verification",
                    "security", "high", code, after,
                    "verify=False disables certificate validation, exposing the call to MITM attacks.",
                ))
                continue

            # 5) DEBUG=True in committed code
            dt = _DEBUG_TRUE.match(code)
            if dt:
                after = f"{dt.group(1)}DEBUG = False"
                fixes.append(_make(
                    fp, line_no, "Disable DEBUG before production",
                    "security", "medium", code, after,
                    "DEBUG mode leaks stack traces and config. Drive it from an env var, defaulting to False.",
                ))
                continue

            # 6) SELECT * → name columns
            if _SELECT_STAR.search(code):
                after = _SELECT_STAR.sub("SELECT <explicit, needed columns> FROM", code)
                fixes.append(_make(
                    fp, line_no, "Avoid SELECT * — name the columns",
                    "performance", "medium", code, after,
                    "SELECT * fetches unused columns (more I/O) and breaks on schema changes. List only what you use.",
                    confidence="medium",
                ))
                continue

            # 7) yaml.load without a safe Loader
            ym = _YAML_LOAD.search(code)
            if ym and "Loader" not in code:
                after = code.replace("yaml.load(", "yaml.safe_load(")
                fixes.append(_make(
                    fp, line_no, "Use yaml.safe_load to prevent arbitrary object construction",
                    "security", "high", code, after,
                    "yaml.load without SafeLoader can execute arbitrary Python on untrusted input.",
                ))
                continue

            if len(fixes) >= max_fixes:
                return fixes

    return fixes
