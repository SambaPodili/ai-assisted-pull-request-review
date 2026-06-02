"""
core/progress.py
-----------------
Thread-safe per-agent progress tracking for active analysis runs.
Each agent reports started / done so the API can stream live status to the UI.
Entries are kept in memory with a 1-hour TTL — only needed while a run is live.
"""
from __future__ import annotations
import threading
import time


# ── Per-agent entry ────────────────────────────────────────────────────────────

class _AgentEntry:
    __slots__ = ("agent", "status", "started_at", "completed_at",
                 "tokens", "duration_s", "model", "fallback")

    def __init__(self, agent: str) -> None:
        self.agent        = agent
        self.status       = "pending"   # pending | running | done | fallback | skipped
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.tokens       = 0
        self.duration_s   = 0.0
        self.model        = ""
        self.fallback     = False

    def to_dict(self) -> dict:
        now     = time.monotonic()
        elapsed = 0.0
        if self.started_at is not None:
            elapsed = round((self.completed_at or now) - self.started_at, 2)
        return {
            "agent":      self.agent,
            "status":     self.status,
            "tokens":     self.tokens,
            "duration_s": self.duration_s,
            "elapsed_s":  elapsed,
            "model":      self.model,
            "fallback":   self.fallback,
        }


# ── Per-run progress ───────────────────────────────────────────────────────────

class RunProgress:
    def __init__(self) -> None:
        self._agents: dict[str, _AgentEntry] = {}
        self._lock    = threading.Lock()
        self._created = time.monotonic()

    @staticmethod
    def _key(agent) -> str:
        """Normalise agent identifiers to their string value.
        Accepts AgentName enums or plain strings so progress keys are always
        the canonical string the frontend matches on (e.g. 'ast_analysis')."""
        return getattr(agent, "value", agent)

    def agent_started(self, agent: str) -> None:
        agent = self._key(agent)
        with self._lock:
            if agent not in self._agents:
                self._agents[agent] = _AgentEntry(agent)
            e = self._agents[agent]
            e.status     = "running"
            e.started_at = time.monotonic()

    def agent_done(self, agent: str, tokens: int, duration_s: float,
                   model: str, fallback: bool) -> None:
        agent = self._key(agent)
        with self._lock:
            if agent not in self._agents:
                self._agents[agent] = _AgentEntry(agent)
            e = self._agents[agent]
            e.status       = "fallback" if fallback else "done"
            e.completed_at = time.monotonic()
            e.tokens       = tokens
            e.duration_s   = round(duration_s, 2)
            e.model        = model
            e.fallback     = fallback

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [v.to_dict() for v in self._agents.values()]

    @property
    def age_s(self) -> float:
        return time.monotonic() - self._created


# ── Process-level singleton ───────────────────────────────────────────────────

class ProgressStore:
    _TTL = 3600
    _GC_INTERVAL = 300

    def __init__(self) -> None:
        self._runs:    dict[str, RunProgress] = {}
        self._lock     = threading.Lock()
        self._last_gc  = time.monotonic()

    def get_or_create(self, request_id: str) -> RunProgress:
        with self._lock:
            if request_id not in self._runs:
                self._runs[request_id] = RunProgress()
            self._maybe_gc()
            return self._runs[request_id]

    def get(self, request_id: str) -> RunProgress | None:
        with self._lock:
            return self._runs.get(request_id)

    def _maybe_gc(self) -> None:
        now = time.monotonic()
        if now - self._last_gc < self._GC_INTERVAL:
            return
        self._last_gc = now
        expired = [k for k, v in self._runs.items() if v.age_s > self._TTL]
        for k in expired:
            del self._runs[k]


_store: ProgressStore | None = None


def get_progress_store() -> ProgressStore:
    global _store
    if _store is None:
        _store = ProgressStore()
    return _store
