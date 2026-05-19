"""
governance/audit_logger.py
---------------------------
Append-only JSONL audit trail satisfying:
  • MAS TRM G05: Change Management — all changes to production-affecting systems logged
  • MAS TRM G06: Incident Management — anomalous gate decisions captured
  • PCI-DSS 4.0 Req 10: Audit log of all system access and analysis events

Each log entry is a single JSON line with a UTC timestamp.
The log file is never overwritten — only appended to.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


class AuditEvent(str, Enum):
    ANALYSIS_STARTED   = "analysis_started"
    ANALYSIS_COMPLETED = "analysis_completed"
    GATE_DECISION      = "gate_decision"
    GATE_OVERRIDE      = "gate_override"
    WEBHOOK_RECEIVED   = "webhook_received"
    AGENT_ERROR        = "agent_error"
    API_ACCESS         = "api_access"
    SECRET_DETECTED    = "secret_detected"
    COMPLIANCE_VIOLATION = "compliance_violation"


@runtime_checkable
class AuditLogger(Protocol):
    def log(self, event: AuditEvent, data: dict[str, Any]) -> None: ...


# ── File-backed audit logger ──────────────────────────────────────────────────

class FileAuditLogger:
    """
    Thread-safe append-only JSONL logger.
    Never raises — a failed audit write is logged to stderr but does not
    interrupt the analysis pipeline.
    """

    def __init__(self, log_path: str = "logs/audit.jsonl") -> None:
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def log(self, event: AuditEvent, data: dict[str, Any]) -> None:
        entry = {
            "ts":    datetime.utcnow().isoformat() + "Z",
            "event": event.value,
            **data,
        }
        try:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            log.error("Audit write failed: %s — entry: %s", exc, entry)

    # ── Convenience methods ────────────────────────────────────────────────────

    def log_analysis_started(self, request_id: str, repo_url: str, source_ref: str, target_ref: str) -> None:
        self.log(AuditEvent.ANALYSIS_STARTED, {
            "request_id": request_id,
            "repo_url":   repo_url,
            "source_ref": source_ref,
            "target_ref": target_ref,
        })

    def log_analysis_completed(self, request_id: str, gate: str, risk: str, tokens: int) -> None:
        self.log(AuditEvent.ANALYSIS_COMPLETED, {
            "request_id": request_id,
            "gate":       gate,
            "risk":       risk,
            "tokens":     tokens,
        })

    def log_gate_decision(self, request_id: str, gate: str, rationale: str) -> None:
        self.log(AuditEvent.GATE_DECISION, {
            "request_id": request_id,
            "gate":       gate,
            "rationale":  rationale,
        })

    def log_secret_detected(self, request_id: str, file_path: str, cwe_id: str) -> None:
        self.log(AuditEvent.SECRET_DETECTED, {
            "request_id": request_id,
            "file_path":  file_path,
            "cwe_id":     cwe_id,
        })

    def log_webhook(self, provider: str, event: str, request_id: str) -> None:
        self.log(AuditEvent.WEBHOOK_RECEIVED, {
            "provider":   provider,
            "event":      event,
            "request_id": request_id,
        })

    def log_agent_error(self, request_id: str, agent: str, error: str) -> None:
        self.log(AuditEvent.AGENT_ERROR, {
            "request_id": request_id,
            "agent":      agent,
            "error":      error,
        })


# ── Null logger (testing) ─────────────────────────────────────────────────────

class NullAuditLogger:
    def log(self, event: AuditEvent, data: dict) -> None:
        pass
    def log_analysis_started(self, *a, **kw):   pass
    def log_analysis_completed(self, *a, **kw): pass
    def log_gate_decision(self, *a, **kw):      pass
    def log_secret_detected(self, *a, **kw):    pass
    def log_webhook(self, *a, **kw):            pass
    def log_agent_error(self, *a, **kw):        pass


# ── Factory ────────────────────────────────────────────────────────────────────

def make_audit_logger(settings=None) -> FileAuditLogger | NullAuditLogger:
    from config.settings import get_settings
    cfg = settings or get_settings()
    if cfg.audit_log_path:
        return FileAuditLogger(cfg.audit_log_path)
    return NullAuditLogger()
