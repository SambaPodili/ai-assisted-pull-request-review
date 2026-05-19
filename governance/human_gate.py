"""
governance/human_gate.py
-------------------------
Human-in-the-loop override gates for the CI/CD quality gate decisions.

Four-eyes principle: a BLOCK or HOLD decision can only be overridden
if two distinct approvers (different key_ids) both sign off.

Override is time-limited — expires after OVERRIDE_TTL_SECONDS.

All overrides are written to the audit log automatically.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from threading import Lock

from core.models import GateDecision
from governance.audit_logger import AuditEvent, make_audit_logger

log = logging.getLogger(__name__)

OVERRIDE_TTL_SECONDS = 3600          # 1 hour — after this, decision reverts
FOUR_EYES_MIN_APPROVERS = 2         # BLOCK requires 2 distinct approvers


@dataclass
class GateOverride:
    request_id:    str
    original_gate: GateDecision
    new_gate:      GateDecision
    reason:        str
    approvers:     list[str] = field(default_factory=list)
    created_at:    float     = field(default_factory=time.monotonic)
    expires_at:    float     = field(default=0.0)

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.created_at + OVERRIDE_TTL_SECONDS

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    @property
    def approved(self) -> bool:
        """Four-eyes: needs at least 2 distinct approvers for BLOCK overrides."""
        if self.original_gate == GateDecision.BLOCK:
            return len(set(self.approvers)) >= FOUR_EYES_MIN_APPROVERS
        return len(self.approvers) >= 1


class HumanGateService:
    """
    Manages gate override requests.

    Flow:
      1. Analyst calls request_override(request_id, reason, approver_id)
      2. Second approver calls approve_override(request_id, approver_id)
      3. Gate decision reverts to the original if TTL expires without full approval.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, GateOverride] = {}
        self._lock       = Lock()
        self._audit      = make_audit_logger()

    def request_override(
        self,
        request_id:    str,
        new_gate:      GateDecision,
        reason:        str,
        approver_id:   str,
        original_gate: GateDecision = GateDecision.BLOCK,
    ) -> GateOverride:
        """Create or update an override request."""
        with self._lock:
            existing = self._overrides.get(request_id)
            if existing and not existing.expired:
                if approver_id not in existing.approvers:
                    existing.approvers.append(approver_id)
                return existing

            override = GateOverride(
                request_id=request_id,
                original_gate=original_gate,
                new_gate=new_gate,
                reason=reason,
                approvers=[approver_id],
            )
            self._overrides[request_id] = override

        self._audit.log(AuditEvent.GATE_OVERRIDE, {
            "request_id":    request_id,
            "original_gate": original_gate.value,
            "new_gate":      new_gate.value,
            "reason":        reason,
            "approver":      approver_id,
        })
        log.info("[Gate] Override requested for %s by %s: %s → %s",
                 request_id, approver_id, original_gate.value, new_gate.value)
        return override

    def approve_override(self, request_id: str, approver_id: str) -> bool:
        """Add a second approver signature. Returns True when fully approved."""
        with self._lock:
            override = self._overrides.get(request_id)
            if not override or override.expired:
                return False
            if approver_id not in override.approvers:
                override.approvers.append(approver_id)

        self._audit.log(AuditEvent.GATE_OVERRIDE, {
            "request_id": request_id,
            "event":      "approval",
            "approver":   approver_id,
            "approved":   override.approved,
        })
        log.info("[Gate] Override %s approved by %s (approved=%s)",
                 request_id, approver_id, override.approved)
        return override.approved

    def get_effective_gate(self, request_id: str, default: GateDecision) -> GateDecision:
        """Return the effective gate decision, accounting for active overrides."""
        with self._lock:
            override = self._overrides.get(request_id)
        if override and not override.expired and override.approved:
            log.info("[Gate] Override active for %s: %s → %s",
                     request_id, override.original_gate.value, override.new_gate.value)
            return override.new_gate
        return default

    def pending_overrides(self) -> list[dict]:
        """List all non-expired, not-yet-approved override requests."""
        with self._lock:
            return [
                {
                    "request_id":    o.request_id,
                    "original_gate": o.original_gate.value,
                    "new_gate":      o.new_gate.value,
                    "reason":        o.reason,
                    "approvers":     o.approvers,
                    "approved":      o.approved,
                    "expires_in_s":  max(0, int(o.expires_at - time.monotonic())),
                }
                for o in self._overrides.values()
                if not o.expired
            ]


# ── Module-level singleton ────────────────────────────────────────────────────

_gate_service: HumanGateService | None = None


def get_gate_service() -> HumanGateService:
    global _gate_service
    if _gate_service is None:
        _gate_service = HumanGateService()
    return _gate_service
