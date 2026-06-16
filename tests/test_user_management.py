"""
tests/test_user_management.py
------------------------------
UI-managed users with hashed keys + the Super Admin → Admin → Developer/Reviewer
role hierarchy. An Admin must NOT be able to mint another Admin.
"""
import pytest

from governance.rbac import Subject, Role
from governance.user_store import SQLiteUserStore


@pytest.fixture
def store(tmp_path):
    return SQLiteUserStore(str(tmp_path / "users.db"))


# ── Role hierarchy ────────────────────────────────────────────────────────────

def test_super_admin_can_create_admin():
    assert Subject(key_id="1", roles=[Role.SUPER_ADMIN]).can_manage([Role.ADMIN])


def test_admin_cannot_create_admin_or_superadmin():
    admin = Subject(key_id="2", roles=[Role.ADMIN])
    assert not admin.can_manage([Role.ADMIN])
    assert not admin.can_manage([Role.SUPER_ADMIN])


def test_admin_can_create_developer_and_reviewer():
    admin = Subject(key_id="2", roles=[Role.ADMIN])
    assert admin.can_manage([Role.DEVELOPER])
    assert admin.can_manage([Role.REVIEWER])
    assert admin.can_manage([Role.DEVELOPER, Role.REVIEWER])


def test_developer_cannot_manage_anyone():
    assert not Subject(key_id="3", roles=[Role.DEVELOPER]).can_manage([Role.DEVELOPER])


def test_can_manage_rejects_mixed_out_of_tier():
    admin = Subject(key_id="2", roles=[Role.ADMIN])
    # one allowed (developer) + one not (admin) → whole request rejected
    assert not admin.can_manage([Role.DEVELOPER, Role.ADMIN])


# ── Hashed user store ─────────────────────────────────────────────────────────

def test_create_returns_key_once_and_resolves(store):
    key, rec = store.create_user("Alice", "Payments", [Role.REVIEWER], "admin@x")
    assert key.startswith("ciaa_")
    assert rec["roles"] == ["reviewer"] and rec["created_by"] == "admin@x"
    subj = store.resolve(key)
    assert subj is not None and subj.name == "Alice" and Role.REVIEWER in subj.roles


def test_key_is_hashed_not_stored_plaintext(store):
    key, rec = store.create_user("Bob", "", [Role.DEVELOPER], "admin@x")
    raw = open(store._conn.execute("PRAGMA database_list").fetchone()["file"], "rb").read()
    assert key.encode() not in raw          # plaintext key never written to disk
    assert rec["key_prefix"].endswith("…")  # only a prefix is shown


def test_wrong_key_does_not_resolve(store):
    store.create_user("Alice", "", [Role.DEVELOPER], "admin@x")
    assert store.resolve("ciaa_wrong") is None and store.resolve("") is None


def test_revoke_disables_resolution(store):
    key, rec = store.create_user("Carol", "", [Role.DEVELOPER], "admin@x")
    assert store.revoke(rec["id"]) is True
    assert store.resolve(key) is None
    assert rec["id"] not in [u["id"] for u in store.list_users()]


def test_update_roles(store):
    key, rec = store.create_user("Dan", "", [Role.DEVELOPER], "admin@x")
    store.update(rec["id"], roles=[Role.REVIEWER])
    assert Role.REVIEWER in store.resolve(key).roles


def test_user_id_maps_bitbucket_slug(store):
    # name ← displayName, user_id ← slug
    _, rec = store.create_user("Jane Doe", "Payments", [Role.REVIEWER], "admin@x", user_id="jdoe")
    assert rec["name"] == "Jane Doe" and rec["user_id"] == "jdoe"
    assert store.list_users()[0]["user_id"] == "jdoe"
    store.update(rec["id"], user_id="jane.doe")
    assert store.get(rec["id"])["user_id"] == "jane.doe"


def test_user_id_migration_on_old_db(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE users (id TEXT PRIMARY KEY, key_hash TEXT, key_prefix TEXT, "
              "name TEXT, team TEXT, roles TEXT, created_by TEXT, created_at TEXT, active INTEGER DEFAULT 1)")
    c.commit(); c.close()
    st = SQLiteUserStore(p)   # migration must add user_id
    _, rec = st.create_user("Bob", "", [Role.DEVELOPER], "admin@x", user_id="bob")
    assert rec["user_id"] == "bob"


# ── API endpoint hierarchy enforcement ────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.admin as adm
    import governance.user_store as us
    us._store = SQLiteUserStore(str(tmp_path / "u.db"))
    app = FastAPI(); app.include_router(adm.router)
    return TestClient(app), app, adm


def _as(app, adm, role):
    from governance.rbac import Subject, Role as R, get_current_subject
    app.dependency_overrides[get_current_subject] = lambda: Subject(
        key_id="x", roles=[role], name=f"{role.value}@x")


def test_api_admin_can_create_developer(client):
    c, app, adm = client
    _as(app, adm, Role.ADMIN)
    r = c.post("/admin/users", json={"name": "Dev", "roles": ["developer"]})
    assert r.status_code == 200 and r.json()["api_key"].startswith("ciaa_")


def test_api_admin_cannot_create_admin(client):
    c, app, adm = client
    _as(app, adm, Role.ADMIN)
    assert c.post("/admin/users", json={"name": "X", "roles": ["admin"]}).status_code == 403


def test_api_super_admin_can_create_admin(client):
    c, app, adm = client
    _as(app, adm, Role.SUPER_ADMIN)
    assert c.post("/admin/users", json={"name": "A", "roles": ["admin"]}).status_code == 200


def test_api_admin_creatable_roles_excludes_admin(client):
    c, app, adm = client
    _as(app, adm, Role.ADMIN)
    creatable = c.get("/admin/users").json()["creatable_roles"]
    assert "admin" not in creatable and "super_admin" not in creatable
    assert "developer" in creatable and "reviewer" in creatable


# ── Audit trail ───────────────────────────────────────────────────────────────

def test_store_audit_records_newest_first(store):
    _, rec = store.create_user("A", "", [Role.DEVELOPER], "admin@x")
    store.record_event("created", "admin@x", rec["id"], "A", [Role.DEVELOPER])
    store.record_event("revoked", "admin@x", rec["id"], "A", [Role.DEVELOPER])
    ev = store.list_audit()
    assert [e["action"] for e in ev[:2]] == ["revoked", "created"]
    assert ev[0]["actor"] == "admin@x" and ev[0]["target"] == "A"


def test_api_create_and_revoke_appear_in_audit(client):
    c, app, adm = client
    _as(app, adm, Role.SUPER_ADMIN)
    uid = c.post("/admin/users", json={"name": "Eve", "roles": ["admin"]}).json()["user"]["id"]
    c.delete(f"/admin/users/{uid}")
    events = c.get("/admin/users/audit").json()["events"]
    actions = [(e["action"], e["target"]) for e in events]
    assert ("created", "Eve") in actions and ("revoked", "Eve") in actions
    assert all(e["actor"] == "super_admin@x" for e in events)
