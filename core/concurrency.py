"""
core/concurrency.py
--------------------
Admission control for analyses — a fair, in-process queue that caps how many
analyses run concurrently and bounds how many may wait.

Why: when many developers submit at once, running every analysis immediately
spawns a thread + a burst of LLM calls per request, overloading the backend and
tripping the model provider's rate limits. This adds back-pressure: excess
analyses wait in a FIFO queue; beyond the queue limit the server sheds load with
a clear "busy, retry" response.

IMPORTANT: this only affects SCHEDULING (how many run at once) — it never changes
how an individual analysis runs, so results are byte-for-byte identical. The
analysis timeout starts only once a slot is acquired (queue wait is not counted).

Single-process only. To coordinate across multiple worker processes, back this
with Redis (a later step).
"""
from __future__ import annotations
import asyncio
import logging

log = logging.getLogger(__name__)


class AnalysisAdmission:
    def __init__(self, max_concurrent: int, max_queued: int):
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_queued = max(0, int(max_queued))
        self._running = 0
        self._waiters: list[tuple[str, asyncio.Event]] = []

    # ── stats ────────────────────────────────────────────────────────────────
    @property
    def running(self) -> int:
        return self._running

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def position(self, request_id: str) -> int:
        """1-based queue position, or 0 if running / not waiting."""
        for i, (rid, _) in enumerate(self._waiters):
            if rid == request_id:
                return i + 1
        return 0

    def can_admit(self) -> bool:
        """Room to accept another analysis (a free slot OR queue space)?"""
        return (self._running + len(self._waiters)) < (self.max_concurrent + self.max_queued)

    # ── slot lifecycle (all called on the event loop, so no locks needed) ──────
    async def acquire(self, request_id: str) -> None:
        """Block until a run slot is free. Returns at once if capacity is free."""
        if self._running < self.max_concurrent and not self._waiters:
            self._running += 1
            return
        ev = asyncio.Event()
        self._waiters.append((request_id, ev))
        try:
            await ev.wait()
        except asyncio.CancelledError:
            # Abandoned while queued — drop from the queue and, if we had already
            # been granted a slot, hand it to the next waiter.
            still_waiting = any(r == request_id for r, _ in self._waiters)
            self._waiters = [(r, e) for (r, e) in self._waiters if r != request_id]
            if not still_waiting:
                self.release()
            raise

    def release(self) -> None:
        """Free a slot and promote the next waiter (if any)."""
        if self._running > 0:
            self._running -= 1
        if self._waiters and self._running < self.max_concurrent:
            _rid, ev = self._waiters.pop(0)
            self._running += 1
            ev.set()


_admission: AnalysisAdmission | None = None


def get_admission() -> AnalysisAdmission:
    global _admission
    if _admission is None:
        from config.settings import get_settings
        cfg = get_settings()
        _admission = AnalysisAdmission(
            max_concurrent=getattr(cfg, "max_concurrent_analyses", 8),
            max_queued=getattr(cfg, "max_queued_analyses", 50),
        )
        log.info("Admission control: max_concurrent=%d max_queued=%d",
                 _admission.max_concurrent, _admission.max_queued)
    return _admission


def reset_admission() -> None:
    """Test helper — drop the singleton so settings are re-read."""
    global _admission
    _admission = None
