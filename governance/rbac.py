"""
governance/rbac.py
-------------------
Role-Based Access Control for the impact analysis framework.

Roles (in ascending privilege order):
  developer  - submit analyses, view own reports — NO gate override, NO PR comments
  reviewer   - everything developer can + post PR comments + approve/block gate
  analyst    - everything reviewer can + delete reports + metrics
  ci_system  - headless CI/CD: submit + read only, no human actions
  auditor    - read-only: reports + audit logs
  admin      - full access including config changes

Separation of developer vs reviewer:
  Developers run the analysis to get feedback on their own code.
  Reviewers are the people who approve merges — they alone can:
    • Post findings as PR comments / reviews on GitHub or Bitbucket
    • Override the gate decision (approve / block a PR)
  This mirrors real-world code-review governance where developers cannot
  self-approve their own pull requests.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from fastapi import HTTPException, Request


class Permission(str, Enum):
    ANALYSIS_SUBMIT = "analysis:submit"
    ANALYSIS_READ   = "analysis:read"
    ANALYSIS_DELETE = "analysis:delete"
    GATE_OVERRIDE   = "gate:override"     # approve / block a PR
    PR_COMMENT      = "pr:comment"        # post findings to GitHub / Bitbucket PR
    ADMIN_CONFIG    = "admin:config"
    USER_MANAGE     = "user:manage"       # create/revoke users (hierarchy-limited)
    AUDIT_READ      = "audit:read"
    METRICS_READ    = "metrics:read"


class Role(str, Enum):
    SUPER_ADMIN = "super_admin"   # top tier — manages admins
    ADMIN     = "admin"
    ANALYST   = "analyst"
    REVIEWER  = "reviewer"    # ← code reviewers who approve PRs
    DEVELOPER = "developer"
    AUDITOR   = "auditor"
    CI_SYSTEM = "ci_system"


# Which roles each role is allowed to CREATE/MANAGE. A manager can never mint a
# peer or higher role — so an Admin physically cannot create another Admin.
MANAGEABLE_ROLES: dict[Role, set[Role]] = {
    Role.SUPER_ADMIN: {Role.ADMIN, Role.ANALYST, Role.REVIEWER, Role.DEVELOPER,
                       Role.AUDITOR, Role.CI_SYSTEM},
    Role.ADMIN:       {Role.REVIEWER, Role.DEVELOPER, Role.AUDITOR, Role.CI_SYSTEM},
}


_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.SUPER_ADMIN: set(Permission),  # all permissions
    Role.ADMIN: set(Permission),  # all permissions

    Role.ANALYST: {
        Permission.ANALYSIS_SUBMIT,
        Permission.ANALYSIS_READ,
        Permission.ANALYSIS_DELETE,
        Permission.GATE_OVERRIDE,
        Permission.PR_COMMENT,
        Permission.METRICS_READ,
        Permission.AUDIT_READ,
    },

    # Reviewer: full read + gate actions + PR posting — but no admin/delete
    Role.REVIEWER: {
        Permission.ANALYSIS_SUBMIT,
        Permission.ANALYSIS_READ,
        Permission.GATE_OVERRIDE,
        Permission.PR_COMMENT,
        Permission.METRICS_READ,
    },

    # Developer: submit + read only — cannot post to PRs or override gates
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


# ── Human-readable role metadata (used by /me endpoint and UI) ───────────────

ROLE_META: dict[Role, dict] = {
    Role.SUPER_ADMIN: {
        "label":       "Super Admin",
        "description": "Top tier — manages admins and everyone below",
        "color":       "#4c1d95",
        "can_comment": True,
        "can_override": True,
    },
    Role.ADMIN: {
        "label":       "Admin",
        "description": "Full access — manages developers/reviewers, config, overrides",
        "color":       "#7c3aed",
        "can_comment": True,
        "can_override": True,
    },
    Role.ANALYST: {
        "label":       "Analyst",
        "description": "Submit, view, override gates, post PR comments",
        "color":       "#0ea5e9",
        "can_comment": True,
        "can_override": True,
    },
    Role.REVIEWER: {
        "label":       "Reviewer",
        "description": "View results, post PR comments, approve or block PRs",
        "color":       "#059669",
        "can_comment": True,
        "can_override": True,
    },
    Role.DEVELOPER: {
        "label":       "Developer",
        "description": "Submit analyses and view results — read-only on reviews",
        "color":       "#1a56db",
        "can_comment": False,
        "can_override": False,
    },
    Role.AUDITOR: {
        "label":       "Auditor",
        "description": "Read-only access to reports and audit logs",
        "color":       "#9fadbf",
        "can_comment": False,
        "can_override": False,
    },
    Role.CI_SYSTEM: {
        "label":       "CI System",
        "description": "Headless: submit + read only",
        "color":       "#f59e0b",
        "can_comment": False,
        "can_override": False,
    },
}


@dataclass
class Subject:
    key_id:  str
    roles:   list[Role] = field(default_factory=list)
    name:    str = ""
    team:    str = ""
    user_id: str = ""        # external identity (e.g. Bitbucket slug like "uncs16")

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

    def manageable_roles(self) -> set[Role]:
        """The set of roles this subject may create/assign to others."""
        out: set[Role] = set()
        for role in self.roles:
            out |= MANAGEABLE_ROLES.get(role, set())
        return out

    def can_manage(self, roles) -> bool:
        """True only if EVERY target role is within this subject's tier."""
        allowed = self.manageable_roles()
        targets = roles if isinstance(roles, (list, set, tuple)) else [roles]
        return bool(targets) and all(r in allowed for r in targets)


@dataclass
class APIKeyEntry:
    key:     str
    subject: Subject


class APIKeyRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, APIKeyEntry] = {}

    # ── Loading ───────────────────────────────────────────────────────────────

    def load_from_settings(self, settings=None) -> None:
        """Load keys from settings (API_KEYS env var) and/or API_KEYS_FILE."""
        import json, logging
        from pathlib import Path
        log = logging.getLogger(__name__)

        from config.settings import get_settings
        cfg = settings or get_settings()
        if cfg.skip_auth:
            return

        all_raw: list = list(cfg.api_keys)

        # Also load from the dedicated keys file if configured
        keys_file = getattr(cfg, "api_keys_file", "")
        if keys_file:
            path = Path(keys_file)
            if path.exists():
                try:
                    extra = json.loads(path.read_text())
                    if isinstance(extra, list):
                        all_raw.extend(extra)
                        log.info("APIKeyRegistry: loaded %d entries from %s", len(extra), path)
                    else:
                        log.warning("API_KEYS_FILE must contain a JSON array — skipping %s", path)
                except Exception as exc:
                    log.error("Failed to load API_KEYS_FILE %s: %s", path, exc)
            else:
                log.warning("API_KEYS_FILE %s not found", path)

        self._entries.clear()
        for raw_key in all_raw:
            self._load_one(raw_key)

        log.info("APIKeyRegistry: %d key(s) loaded (%d developer, %d reviewer, %d admin+)",
                 len(self._entries),
                 sum(1 for e in self._entries.values() if Role.DEVELOPER in e.subject.roles),
                 sum(1 for e in self._entries.values() if Role.REVIEWER  in e.subject.roles),
                 sum(1 for e in self._entries.values() if Role.ADMIN in e.subject.roles
                                                       or Role.ANALYST in e.subject.roles))

    def _load_one(self, raw_key) -> None:
        if isinstance(raw_key, str):
            subject = Subject(key_id=raw_key, roles=[Role.DEVELOPER])
            self._entries[raw_key] = APIKeyEntry(key=raw_key, subject=subject)
        elif isinstance(raw_key, dict):
            key = raw_key.get("key", "")
            if not key:
                return
            try:
                roles = [Role(r) for r in raw_key.get("roles", ["developer"])]
            except ValueError as exc:
                import logging
                logging.getLogger(__name__).warning("Invalid role in key entry %s: %s", key[:8], exc)
                roles = [Role.DEVELOPER]
            subject = Subject(
                key_id = key,
                roles  = roles,
                name   = raw_key.get("name", ""),
                team   = raw_key.get("team", ""),
                user_id = raw_key.get("user_id", ""),
            )
            self._entries[key] = APIKeyEntry(key=key, subject=subject)

    def reload(self, settings=None) -> int:
        """Hot-reload all keys without restarting the server. Returns new count."""
        self.load_from_settings(settings)
        return len(self._entries)

    # ── Lookup ────────────────────────────────────────────────────────────────

    def resolve(self, key: str) -> Subject | None:
        entry = self._entries.get(key)
        if entry:
            return entry.subject
        # Fall through to the UI-managed user store (hashed keys in SQLite).
        try:
            from governance.user_store import get_user_store
            return get_user_store().resolve(key)
        except Exception:
            return None

    def list_subjects(self) -> list[dict]:
        """Return a redacted summary of all loaded keys (for admin UI)."""
        return [
            {
                "key_prefix":   e.subject.key_id[:10] + "…",
                "name":         e.subject.name,
                "team":         e.subject.team,
                "roles":        [r.value for r in e.subject.roles],
                "can_comment":  ROLE_META.get(e.subject.roles[0], {}).get("can_comment", False)
                                if e.subject.roles else False,
                "can_override": ROLE_META.get(e.subject.roles[0], {}).get("can_override", False)
                                if e.subject.roles else False,
            }
            for e in self._entries.values()
        ]

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


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def get_current_subject(request: Request) -> Subject:
    """FastAPI dependency: resolve the calling subject or raise 401."""
    from config.settings import get_settings
    cfg = get_settings()
    subject = resolve_subject(request, skip_auth=getattr(cfg, "skip_auth", False))
    if subject is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return subject


def require_permission(permission: Permission):
    """
    FastAPI dependency factory.  Checks that the calling subject holds a
    specific permission; raises 403 if not.

    Usage:
        @router.post("/some-endpoint")
        def my_route(subject: Subject = Depends(require_permission(Permission.PR_COMMENT))):
            ...
    """
    from fastapi import Depends

    def _check(subject: Subject = Depends(get_current_subject)) -> Subject:
        subject.require(permission)
        return subject

    return Depends(_check)


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
