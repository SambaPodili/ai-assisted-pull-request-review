"""
tests/test_sca_fallback.py
--------------------------
The three SCA resilience layers:
  Layer 1 — transient network errors are retried before declaring the source down
  Layer 2 — primary source down → opt-in fallback source (VULN_FALLBACK_SOURCE)
  Layer 3 — all sources down → last successful scan served STALE, clearly labelled
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from ingestion.osv_client import OsvUnavailable, _open_with_retries

POM = ('<project><dependencies><dependency><groupId>g</groupId>'
       '<artifactId>a</artifactId><version>1.0</version></dependency></dependencies></project>')


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVEN_SCAN_TRANSITIVE", "false")
    # isolate the last-known-good cache per test
    import ingestion.sca_cache as sc
    monkeypatch.setattr(sc, "_db_path", lambda: str(tmp_path / "sca_cache.db"))


# ── Layer 1: retries ──────────────────────────────────────────────────────────

def test_retries_recover_from_transient_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}
    def flaky(req, timeout_s):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("transient 502")
        return io.BytesIO(b"{}")
    resp = _open_with_retries(flaky, object(), 5, attempts=(0, 0, 0))
    assert calls["n"] == 3 and resp.read() == b"{}"


def test_retries_exhausted_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    def dead(req, timeout_s):
        raise urllib.error.URLError("down")
    with pytest.raises(urllib.error.URLError):
        _open_with_retries(dead, object(), 5, attempts=(0, 0, 0))


# ── Layer 2: source failover ──────────────────────────────────────────────────

def _cfg(monkeypatch, fallback):
    from types import SimpleNamespace
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: SimpleNamespace(vuln_source="xray", vuln_fallback_source=fallback,
                                                data_dir="data"))


def test_fallback_serves_from_secondary(monkeypatch):
    from ingestion.pom_sca import _vuln_lookup
    _cfg(monkeypatch, fallback="osv")
    monkeypatch.setattr("ingestion.xray_client.query_versioned_xray",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("xray down")))
    monkeypatch.setattr("ingestion.osv_client.query_versioned", lambda *a, **k: {("g:a", "1.0"): []})
    src, hits, note = _vuln_lookup([("g:a", "Maven", "1.0")], 5, source="xray")
    assert src == "osv" and "fallback" in note.lower()


def test_no_fallback_by_default(monkeypatch):
    from ingestion.pom_sca import _vuln_lookup
    _cfg(monkeypatch, fallback="none")
    monkeypatch.setattr("ingestion.xray_client.query_versioned_xray",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("xray down")))
    with pytest.raises(OsvUnavailable):
        _vuln_lookup([("g:a", "Maven", "1.0")], 5, source="xray")


# ── Layer 3: last-known-good stale cache ──────────────────────────────────────

def test_stale_cache_served_when_all_sources_down(monkeypatch):
    from ingestion.pom_sca import scan_pom
    from ingestion.osv_client import OsvVuln

    # 1st scan succeeds → cached
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: {("g:a", "1.0"): [OsvVuln(package="g:a", vuln_id="GHSA-x",
                                                                  summary="s", severity="HIGH",
                                                                  aliases=["CVE-2024-9"])]})
    first = scan_pom(POM, resolve_parents=False)
    assert first["osv_error"] is None and len(first["vulnerabilities"]) == 1

    # 2nd scan: DB down → stale copy served with clear labelling
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("network down")))
    second = scan_pom(POM, resolve_parents=False)
    assert second["stale"] is True and "last successful scan" in second["stale_note"]
    assert second["osv_error"] is None                       # not an empty error page
    assert second["vulnerabilities"][0]["cve"] == "CVE-2024-9"  # findings preserved


def test_no_cache_no_stale_still_honest_error(monkeypatch):
    from ingestion.pom_sca import scan_pom
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("network down")))
    res = scan_pom(POM, resolve_parents=False)
    assert res["osv_error"] and not res.get("stale")          # honest failure preserved


def test_per_request_fallback_overrides_settings(monkeypatch):
    """UI-selected fallback (X-Vuln-Fallback header) works even when
    VULN_FALLBACK_SOURCE is unset in .env."""
    from ingestion.pom_sca import _vuln_lookup
    _cfg(monkeypatch, fallback="none")          # env says NO fallback
    monkeypatch.setattr("ingestion.xray_client.query_versioned_xray",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("xray down")))
    monkeypatch.setattr("ingestion.osv_client.query_versioned", lambda *a, **k: {("g:a", "1.0"): []})
    src, hits, note = _vuln_lookup([("g:a", "Maven", "1.0")], 5, source="xray", fallback="osv")
    assert src == "osv" and "fallback" in note.lower()


def test_offline_selected_as_explicit_fallback(monkeypatch, tmp_path):
    """fallback='offline' goes straight to the local snapshot when primary dies."""
    import json, zipfile
    from ingestion.pom_sca import _vuln_lookup
    adv = {"id": "GHSA-loc", "summary": "s", "aliases": ["CVE-7"],
           "database_specific": {"severity": "HIGH"},
           "affected": [{"package": {"ecosystem": "Maven", "name": "g:a"}, "versions": ["1.0"]}]}
    with zipfile.ZipFile(tmp_path / "Maven.zip", "w") as z:
        z.writestr("a.json", json.dumps(adv))
    import ingestion.osv_offline as off
    off._INDEX.clear()
    monkeypatch.setenv("OSV_OFFLINE_DIR", str(tmp_path))
    _cfg(monkeypatch, fallback="none")             # env none — UI picks offline
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("osv down")))
    src, hits, note = _vuln_lookup([("g:a", "Maven", "1.0")], 5, source="osv", fallback="offline")
    assert src == "osv-offline" and "OFFLINE OSV snapshot" in note
    assert hits[("g:a", "1.0")][0].vuln_id == "GHSA-loc"
