"""
governance/circuit_breaker.py
------------------------------
Agent-level circuit breakers.

States:
  CLOSED    — normal operation; failures counted
  OPEN      — agent blocked; all calls return fallback immediately
  HALF_OPEN — one trial call allowed; success → CLOSED, failure → OPEN

The breaker protects the system when an agent is consistently failing
(LLM timeouts, quota exhaustion, parsing errors) so that one bad agent
doesn't cascade into a full pipeline failure.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock

log = logging.getLogger(__name__)


class BreakerState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    Per-agent circuit breaker.

    Parameters
    ----------
    agent            : name used for logging
    failure_threshold: consecutive failures before opening
    recovery_timeout : seconds in OPEN before trying HALF_OPEN
    """
    agent:             str
    failure_threshold: int   = 3
    recovery_timeout:  float = 60.0

    _state:         BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures:      int          = field(default=0, init=False)
    _opened_at:     float        = field(default=0.0, init=False)
    _lock:          Lock         = field(default_factory=Lock, init=False)

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow_request(self) -> bool:
        """Return True if the agent should be called; False if blocked."""
        with self._lock:
            if self._state == BreakerState.CLOSED:
                return True

            if self._state == BreakerState.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    self._state = BreakerState.HALF_OPEN
                    log.info("[CircuitBreaker] %s → HALF_OPEN", self.agent)
                    return True
                return False

            # HALF_OPEN: allow exactly one trial
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state in (BreakerState.HALF_OPEN, BreakerState.OPEN):
                log.info("[CircuitBreaker] %s → CLOSED (recovered)", self.agent)
            self._state    = BreakerState.CLOSED
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == BreakerState.HALF_OPEN:
                # Trial failed — stay OPEN
                self._state    = BreakerState.OPEN
                self._opened_at = time.monotonic()
                log.warning("[CircuitBreaker] %s trial failed → OPEN", self.agent)
            elif self._failures >= self.failure_threshold:
                self._state    = BreakerState.OPEN
                self._opened_at = time.monotonic()
                log.warning(
                    "[CircuitBreaker] %s → OPEN after %d failures",
                    self.agent, self._failures,
                )

    def metrics(self) -> dict:
        with self._lock:
            return {
                "agent":    self.agent,
                "state":    self._state.value,
                "failures": self._failures,
            }


class CircuitBreakerRegistry:
    """Singleton registry: one breaker per agent, shared across threads."""

    def __init__(
        self,
        failure_threshold: int   = 3,
        recovery_timeout:  float = 60.0,
    ) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._threshold  = failure_threshold
        self._timeout    = recovery_timeout
        self._lock       = Lock()

    def get(self, agent: str) -> CircuitBreaker:
        with self._lock:
            if agent not in self._breakers:
                self._breakers[agent] = CircuitBreaker(
                    agent=agent,
                    failure_threshold=self._threshold,
                    recovery_timeout=self._timeout,
                )
            return self._breakers[agent]

    def all_metrics(self) -> list[dict]:
        with self._lock:
            return [b.metrics() for b in self._breakers.values()]


# ── Module-level singleton ────────────────────────────────────────────────────

_breaker_registry: CircuitBreakerRegistry | None = None


def get_breaker_registry() -> CircuitBreakerRegistry:
    global _breaker_registry
    if _breaker_registry is None:
        _breaker_registry = CircuitBreakerRegistry()
    return _breaker_registry
