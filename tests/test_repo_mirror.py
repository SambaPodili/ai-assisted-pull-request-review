"""tests/test_repo_mirror.py — warm local mirror for instant cross-repo xref."""
import os
from pathlib import Path
import pytest
from ingestion import repo_mirror as rm


def _make_repo(root: Path, slug_dir: str, rel_file: str, content: str) -> Path:
    d = root / slug_dir
    (d / Path(rel_file).parent).mkdir(parents=True, exist_ok=True)
    (d / rel_file).write_text(content)
    return d


def test_resolve_repo_dir_variants(tmp_path):
    _make_repo(tmp_path, "BANK__billing", "src/A.java", "x")
    assert rm.resolve_repo_dir("BANK/billing", str(tmp_path))   # flattened
    assert rm.resolve_repo_dir("billing", str(tmp_path))         # last segment
    assert rm.resolve_repo_dir("BANK/missing", str(tmp_path)) is None
    assert rm.resolve_repo_dir("anything", "") is None           # no root


def test_local_xref_greps_mirror(tmp_path):
    _make_repo(tmp_path, "BANK__billing", "src/Caller.java",
               "public void run(){ int x = computeFee(10); }\n")
    refs = rm.local_xref("BANK/billing", ["computeFee"], root=str(tmp_path))
    assert any(r.symbol == "computeFee" and r.repo == "billing" for r in refs)
    assert all(r.line >= 1 for r in refs)
    # File path is relative to the repo dir (not absolute)
    assert any(r.file_path.endswith("Caller.java") and not os.path.isabs(r.file_path) for r in refs)


def test_local_xref_empty_when_not_mirrored(tmp_path):
    assert rm.local_xref("X/none", ["foo"], root=str(tmp_path)) == []


def test_xref_endpoint_prefers_mirror(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.git_proxy as gp
    _make_repo(tmp_path, "BANK__billing", "src/Caller.java", "computeFee(1);\n")
    monkeypatch.setattr(rm, "mirror_root", lambda: str(tmp_path))
    # Search must NOT be called for a mirrored repo
    def _boom(*a, **k):
        raise AssertionError("provider search should be skipped for mirrored repos")
    monkeypatch.setattr(gp, "_search_bb_server", _boom)

    app = FastAPI(); app.include_router(gp.router)
    r = TestClient(app).post("/api/v1/git/xref", json={
        "cfg": {"provider": "bitbucket_server", "base_url": "https://bb.x", "token": "T", "username": "u"},
        "repo_slugs": ["BANK/billing"], "symbols": ["computeFee"]})
    d = r.json()
    assert d["backend"] == "local_mirror"
    assert d["mirrored_repos"] == ["BANK/billing"]
    assert any(x["repo"] == "billing" for x in d["references"])


def test_mirror_status_and_sync_guard(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.git_proxy as gp
    _make_repo(tmp_path, "BANK__billing", "a.txt", "x")
    monkeypatch.setattr(rm, "mirror_root", lambda: str(tmp_path))
    app = FastAPI(); app.include_router(gp.router); c = TestClient(app)
    cfg = {"provider": "bitbucket_server", "base_url": "https://bb.x", "token": "T", "username": "u"}
    st = c.post("/api/v1/git/mirror/status", json={"cfg": cfg, "repo_slugs": ["BANK/billing", "BANK/none"]}).json()
    assert st["enabled"] is True
    flags = {r["slug"]: r["mirrored"] for r in st["repos"]}
    assert flags == {"BANK/billing": True, "BANK/none": False}

    # sync with no REPOS_ROOT → 400
    monkeypatch.setattr(rm, "mirror_root", lambda: "")
    bad = c.post("/api/v1/git/mirror/sync", json={"cfg": cfg, "repo_slugs": ["BANK/billing"]})
    assert bad.status_code == 400


def test_split_slug_refs_precedence():
    from api.routes.git_proxy import _split_slug_refs
    slugs = ["SCV/billing@release/2.1", "SCV/ledger", "SCV/core"]
    clean, refs = _split_slug_refs(slugs, repo_refs={"SCV/core": "hotfix/9"}, default_ref="main")
    assert clean == ["SCV/billing", "SCV/ledger", "SCV/core"]
    assert refs["SCV/billing"] == "release/2.1"   # embedded @ref wins over default
    assert refs["SCV/ledger"] == "main"           # falls back to default
    assert refs["SCV/core"] == "hotfix/9"          # repo_refs map wins


def test_mirror_sync_uses_per_repo_ref(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api.routes.git_proxy as gp
    monkeypatch.setattr(rm, "mirror_root", lambda: str(tmp_path))
    captured = {}
    def fake_sync(url, slug, ref="", secret="", root=None):
        captured[slug] = ref
        return {"slug": slug, "ok": True, "action": "cloned"}
    monkeypatch.setattr(rm, "sync_repo", fake_sync)

    app = FastAPI(); app.include_router(gp.router)
    r = TestClient(app).post("/api/v1/git/mirror/sync", json={
        "cfg": {"provider": "bitbucket_server", "base_url": "https://bb.x", "token": "T", "username": "u"},
        "repo_slugs": ["SCV/billing@release/2.1", "SCV/ledger"], "ref": "main"})
    d = r.json()
    assert d["synced"] == 2
    assert captured["SCV/billing"] == "release/2.1"
    assert captured["SCV/ledger"] == "main"
