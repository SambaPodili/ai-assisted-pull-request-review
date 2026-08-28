"""
core/token_manager.py
---------------------
Central token budget management for a single analysis run.

Five cost-reduction strategies applied simultaneously:
  1. Model tiering   – Haiku for structural/simple agents; Sonnet for complex reasoning
  2. Prompt caching  – cache_control=ephemeral marks system prompts for server-side caching
  3. Diff chunking   – split large diffs; send only the highest-signal chunk to LLM
  4. Budget slicing  – each agent gets a hard allocation; exhaustion → deterministic fallback
  5. Budget donation – redistribute leftover tokens at runtime between agents
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ── Model constants ───────────────────────────────────────────────────────────
MODEL_FAST   = "claude-haiku-4-5-20251001"   # dep scan, test stubs, entropy/IaC/observability
MODEL_STRONG = "claude-sonnet-4-6"           # security, risk, interface, remediation,
                                              # code_analysis, maintainability, ast_analysis

# Which agents use the strong model. code_analysis/maintainability moved here
# deliberately (previously MODEL_FAST) — general bug-hunting and code-quality
# review benefit from stronger reasoning the same way security/risk do; the
# original "classification doesn't need deep reasoning" framing undersold
# what these two agents are actually asked to find (logic flaws, technical
# debt, not just a change-type label). ast_analysis's OWN docstring already
# claimed "LLM enhancement (Sonnet)" — it was never actually wired to
# MODEL_STRONG; adding it here makes the code match what it already
# documented, not a new claim.
_STRONG_AGENTS = {"security", "risk", "interface", "remediation",
                  "code_analysis", "maintainability", "ast_analysis"}

# Minimum remaining tokens below which we treat the budget as exhausted
_SAFETY_MARGIN = 200


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AgentBudget:
    agent:     str
    allocated: int
    used:      int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.allocated - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining < _SAFETY_MARGIN


# ── Budget manager ────────────────────────────────────────────────────────────

class TokenBudgetManager:
    """
    Thread-safe per-run token budget tracker.

    Usage (inside an agent):
        model  = budget.select_model("security")
        ok     = budget.check_and_reserve("security", estimated_tokens)
        if not ok:
            return fallback_result()
        # … call LLM …
        budget.record_usage("security", actual_tokens, model)
    """

    def __init__(
        self,
        request_id:     str,
        custom_budgets: dict[str, int] | None = None,
    ) -> None:
        from config.settings import get_settings
        budgets = custom_budgets or get_settings().agent_budgets
        self.request_id = request_id
        self._agents: dict[str, AgentBudget] = {
            name: AgentBudget(agent=name, allocated=tokens)
            for name, tokens in budgets.items()
        }
        self._lock       = Lock()
        self._usage_log: list[dict] = []

    # ── Model selection ───────────────────────────────────────────────────────

    def select_model(self, agent: str) -> str:
        return MODEL_STRONG if agent in _STRONG_AGENTS else MODEL_FAST

    # ── Budget operations ─────────────────────────────────────────────────────

    def get_remaining(self, agent: str) -> int:
        with self._lock:
            return self._agents.get(agent, self._agents["_reserve"]).remaining

    def check_and_reserve(self, agent: str, needed: int) -> bool:
        """
        Atomically reserve *needed* tokens.
        Returns False if the budget cannot accommodate the request.
        """
        with self._lock:
            slot = self._agents.get(agent, self._agents["_reserve"])
            if slot.remaining < max(needed, _SAFETY_MARGIN):
                log.warning(
                    "[%s] Budget exhausted for '%s': remaining=%d, needed=%d",
                    self.request_id, agent, slot.remaining, needed,
                )
                return False
            slot.used += needed
            return True

    def record_usage(self, agent: str, tokens_used: int, model: str) -> None:
        """Reconcile actual vs reserved token spend."""
        with self._lock:
            slot = self._agents.get(agent, self._agents["_reserve"])
            slot.used = max(slot.used, tokens_used)
            self._usage_log.append({
                "agent":  agent,
                "tokens": tokens_used,
                "model":  model,
            })
            log.debug("[%s] %s used %d tokens (%s)", self.request_id, agent, tokens_used, model)

    def donate_unused(self, from_agent: str, to_agent: str, keep: int = 200) -> int:
        """Transfer surplus tokens from one agent to another. Returns amount donated."""
        with self._lock:
            src = self._agents.get(from_agent)
            dst = self._agents.get(to_agent)
            if not src or not dst:
                return 0
            transferable = max(0, src.remaining - keep)
            if transferable > 0:
                dst.allocated += transferable
                src.allocated -= transferable
                log.info("[%s] Donated %d tokens: %s → %s", self.request_id, transferable, from_agent, to_agent)
            return transferable

    def summary(self) -> dict:
        with self._lock:
            return {
                "request_id":      self.request_id,
                "total_allocated": sum(a.allocated for a in self._agents.values()),
                "total_used":      sum(a.used      for a in self._agents.values()),
                "per_agent":       {
                    name: {"allocated": a.allocated, "used": a.used, "remaining": a.remaining}
                    for name, a in self._agents.items()
                },
                "usage_log": self._usage_log,
            }


# ── Diff chunking utilities ───────────────────────────────────────────────────

def chunk_diff(diff_text: str, max_lines: int = 200) -> list[str]:
    """
    Split a unified diff into chunks of at most *max_lines* lines.
    Each chunk starts with the file header so context is preserved.
    """
    if not diff_text.strip():
        return []

    lines   = diff_text.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    file_hdr = ""

    for line in lines:
        if line.startswith("diff --git") or line.startswith("--- "):
            file_hdr = line
        if line.startswith("@@") and len(current) >= max_lines:
            chunks.append("".join(current))
            current = [file_hdr, line]
            continue
        current.append(line)

    if current:
        chunks.append("".join(current))
    return chunks


def select_hottest_chunk(chunks: list[str]) -> str:
    """Return the chunk with the most added/removed lines (highest signal)."""
    if not chunks:
        return ""
    def _score(chunk: str) -> int:
        return sum(
            1 for line in chunk.splitlines()
            if line and line[0] in ("+", "-") and not line.startswith(("+++", "---"))
        )
    return max(chunks, key=_score)


def trim_diff_for_budget(diff: str, max_tokens_approx: int = 2000) -> str:
    """
    Trim a diff to fit within an approximate token budget.
    Prefers the highest-churn chunk when truncation is needed.
    Rule of thumb: 1 token ≈ 4 characters.
    """
    max_chars = max_tokens_approx * 4
    if len(diff) <= max_chars:
        return diff
    chunks = chunk_diff(diff)
    hot    = select_hottest_chunk(chunks)
    return hot[:max_chars]


def estimate_tokens(text: str) -> int:
    """Rough token estimate (1 token ≈ 4 chars for code)."""
    return max(1, len(text) // 4)
