"""
governance/rbac.py
-------------------
Role-Based Access Control for the impact analysis framework.

Roles:
  admin      - full access including config changes and gate overrides
  analyst    - can submit analyses, view all reports, override gates
  developer  - can submit analyses, view own reports
  auditor    - read-only access to reports and audit logs
  ci_system  - headless CI/CD integration (submit + read, no admin)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from fastapi import HTTPException


class Permission(str, Enum):
    ANALYSIS_SUBMIT = "analysis:submit"
    ANALYSIS_READ   = "analysis:read"
    ANALYSIS_DELETE = "analysis:delete"
    GATE_OVERRIDE   = "gate:override"
    ADMIN_CONFIG    = "admin:config"
    AUDIT_READ      = "audit:read"
    METRICS_READ    = "metrics:read"


class Role(str, Enum):
    ADMIN     = "admin"
    ANALYST   = "analyst"
    DEVELOPER = "developer"
    AUDITOR   = "auditor"
    CI_SYSTEM = "ci_system"


_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # all permissions

    Role.ANALYST: {
        Permission.ANALYSIS_SUBMIT,
        Permission.ANALYSIS_READ,
        Permission.GATE_OVERRIDE,
        Permission.METRICS_READ,
    },

    Role.DEVELOPER: {
        Permission.ANALYSIS_SUBMIT,
        Permission.ANALYSIS_READ,
        Permission.METRICS_READ,
    },

    Role.AUDITOR: {
        Permission.ANALYSIS_READ,
        Permission.AUDIT_READ,
        Permission.METRICS_READ,
    },

    Role.CI_SYSTEM: {
        Permission.ANALYSIS_SUBMIT,
        Permission.ANALYSIS_READ,
        Permission.METRICS_READ,
    },
}


@dataclass
class Subject:
    key_id:  str
    roles:   list[Role] = field(default_factory=list)
    name:    str = ""
    team:    str = ""

    @property
    def permissions(self) -> set[Permission]:
        perms: set[Permission] = set()
        for role in self.roles:
            perms |= _ROLE_PERMISSIONS.get(role, set())
        return perms

    def has_permission(self, perm: Permission) -> bool:
        return perm in self.permissions

    def require(self, perm: Permission) -> None:
        if not self.has_permission(perm):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: '{perm.value}' required.",
            )


@dataclass
class APIKeyEntry:
    key:     str
    subject: Subject


class APIKeyRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, APIKeyEntry] = {}

    def load_from_settings(self, settings=None) -> None:
        from config.settings import get_settings
        cfg = settings or get_settings()
        if cfg.skip_auth:
            return
        for raw_key in cfg.api_keys:
            if isinstance(raw_key, str):
                subject = Subject(key_id=raw_key, roles=[Role.DEVELOPER])
                self._entries[raw_key] = APIKeyEntry(key=raw_key, subject=subject)
            elif isinstance(raw_key, dict):
                key    = raw_key.get("key", "")
                roles  = [Role(r) for r in raw_key.get("roles", ["developer"])]
                subject = Subject(key_id=key, roles=roles,
                                  name=raw_key.get("name", ""),
                                  team=raw_key.get("team", ""))
                self._entries[key] = APIKeyEntry(key=key, subject=subject)

    def resolve(self, key: str) -> Subject | None:
        entry = self._entries.get(key)
        return entry.subject if entry else None

    def add_key(self, key: str, subject: Subject) -> None:
        self._entries[key] = APIKeyEntry(key=key, subject=subject)

    def revoke_key(self, key: str) -> bool:
        return bool(self._entries.pop(key, None))


_registry: APIKeyRegistry | None = None


def get_registry() -> APIKeyRegistry:
    global _registry
    if _registry is None:
        _registry = APIKeyRegistry()
    return _registry


def resolve_subject(request, skip_auth: bool = False) -> Subject | None:
    if skip_auth:
        return Subject(key_id="__anonymous__", roles=[Role.ADMIN], name="skip_auth")
    key = (
        request.headers.get("X-API-Key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if not key:
        return None
    return get_registry().resolve(key)


@dataclass
class GateOverride:
    request_id:    str
    original_gate: str
    override_to:   str
    reason:        str
    override_by:   str
    override_team: str


class GateOverrideStore:
    def __init__(self) -> None:
        self._overrides: dict[str, GateOverride] = {}

    def record(self, override: GateOverride) -> None:
        self._overrides[override.request_id] = override

    def get(self, request_id: str) -> GateOverride | None:
        return self._overrides.get(request_id)

    def list_all(self) -> list[GateOverride]:
        return list(self._overrides.values())


_gate_override_store = GateOverrideStore()


def get_gate_override_store() -> GateOverrideStore:
    return _gate_override_store
