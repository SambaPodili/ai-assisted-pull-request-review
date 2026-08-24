"""
governance/prompt_guard.py
----------------------------
This is the AUTHORITATIVE, enforced copy of the rule set — the server always
runs this before accepting user_instructions. frontend/src/promptGuard.js and
vscode-extension/src/promptGuard.ts are hand-maintained PARALLEL copies for
real-time UX only (advisory, not enforced) — keep all three in sync if you
edit rules here.

Deterministic (non-LLM, zero-token) scanner for the free-text
`user_instructions` field a submitter can attach to an analysis to steer
prioritization (e.g. "focus on security in the payment module").

This is NOT the safety guarantee for that feature — it's a speed bump and an
audit trail. The real guarantee is structural, enforced elsewhere:
  - `user_instructions` lives only on AnalysisRequest, never on
    AnalysisReport, so governance/gate_policy.py's evaluate_policy(report)
    cannot see it (it doesn't take a request at all).
  - governance/rationale.py's build_rationale(report) deterministically
    overwrites the primary displayed gate rationale from final metrics —
    also report-only, no request, no LLM text.
  - Every agent that reads user_instructions wraps it via
    agents.base_agent.format_user_priorities(), which explicitly tells the
    model it is untrusted context, not an instruction, and that security/
    secrets/critical findings must still be reported at full severity.

Given those structural guarantees hold regardless of what this module
catches, the rule set below is intentionally a first pass, not exhaustive —
its job is to reject the crudest, most common injection/manipulation
attempts before they cost tokens or reach a model's context window at all.

Three categories:
  override         — impersonating a system/developer instruction, or trying
                      to make the model discard its existing rules.
  gate_manipulation — trying to force a specific gate outcome or suppress a
                      whole category of findings (secrets, security, ...).
  exfiltration      — trying to get a model to reveal its system prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    category: str
    phrase: str        # the human-readable rule label, not the matched text
    pattern: str        # the regex that matched, for logging/debugging


_RULES: list[tuple[str, str, re.Pattern]] = []


def _add(category: str, phrase: str, pattern: str) -> None:
    _RULES.append((category, phrase, re.compile(pattern, re.IGNORECASE | re.MULTILINE)))


# ── override: system/developer impersonation, instruction override ────────────
_add("override", "ignore previous instructions",
     r"\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|prompt|guidance|directions?)\b")
_add("override", "disregard the rules above",
     r"\bdisregard\s+(the\s+)?(rules?|instructions?|policy|guidelines?)\s+(above|before)\b")
_add("override", "you are now",
     r"\byou\s+are\s+now\b")
_add("override", "system/developer role header",
     r"^\s*(system|developer)\s*:")
_add("override", "new system prompt",
     r"\bnew\s+system\s+prompt\b")
_add("override", "override your instructions",
     r"\boverride\s+(your|the)\s+(instructions?|rules?|guidelines?)\b")
_add("override", "act as a different/unrestricted assistant",
     r"\bact\s+as\s+(if\s+you\s+are\s+)?(a\s+)?(different|new|unrestricted)\b")

# ── gate_manipulation: forcing outcomes / suppressing finding categories ───────
_add("gate_manipulation", "always approve",
     r"\balways\s+(approve|pass)\b")
_add("gate_manipulation", "never block/hold",
     r"\bnever\s+(block|hold|fail)\b")
_add("gate_manipulation", "mark everything as passing",
     r"\bmark\s+(everything|all\s+findings?|this)\s+as\s+(low|passing|approved)\b")
_add("gate_manipulation", "suppress security/secrets findings",
     r"\b(ignore|skip|suppress|hide|don'?t\s+report|do\s+not\s+report)\s+(all\s+|any\s+)?(security|secrets?|vulnerabilit\w*|findings?)\b")
_add("gate_manipulation", "skip the security agent",
     r"\bskip\s+the\s+(security|secrets?)\s+agent\b")
_add("gate_manipulation", "force approve/gate",
     r"\bforce\s+(approve|gate)\b")
_add("gate_manipulation", "set gate decision directly",
     r"\bset\s+gate\s*(decision)?\s*=?\s*(approve|hold|block)\b")
_add("gate_manipulation", "downgrade severity",
     r"\bdowngrade\s+(all\s+)?(severity|findings?)\b")

# ── exfiltration: system prompt extraction ─────────────────────────────────────
_add("exfiltration", "reveal your system prompt",
     r"\b(repeat|print|reveal|show|output|dump)\s+(your\s+)?(system\s+prompt|instructions)\b")
_add("exfiltration", "what is your system prompt",
     r"\bwhat\s+(is|are)\s+your\s+(system\s+prompt|instructions)\b")


def scan(text: str) -> list[Violation]:
    """Return every rule that matched. Empty list = clean."""
    if not text:
        return []
    return [Violation(category, phrase, pattern.pattern)
            for category, phrase, pattern in _RULES if pattern.search(text)]


def is_safe(text: str) -> bool:
    return not scan(text)
