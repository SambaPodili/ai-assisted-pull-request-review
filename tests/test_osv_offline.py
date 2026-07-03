"""
tests/test_osv_offline.py
-------------------------
Offline OSV snapshot (pre-downloaded all.zip) as the LAST-RESORT vulnerability
source when every live source (OSV API / Xray) is down.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from ingestion.osv_client import OsvUnavailable


ADV_EXPLICIT = {  # matches via the explicit versions list (typical for Maven)
    "id": "GHSA-off-1", "summary": "SSRF in spring-web",
    "aliases": ["CVE-2024-1111"],
    "database_specific": {"severity": "HIGH"},
    "affected": [{"package": {"ecosystem": "Maven", "name": "org.springframework:spring-web"},
                  "versions": ["5.3.21", "5.3.22"]}],
}
ADV_RANGE = {     # matches via introduced/fixed range events
    "id": "GHSA-off-2", "summary": "DoS in snakeyaml",
    "aliases": ["CVE-2024-2222"],
    "database_specific": {"severity": "CRITICAL"},
    "affected": [{"package": {"ecosystem": "Maven", "name": "org.yaml:snakeyaml"},
                  "ranges": [{"type": "ECOSYSTEM",
                              "events": [{"introduced": "0"}, {"fixed": "1.33"}]}]}],
}


@pytest.fixture()
def snapshot_dir(tmp_path, monkeypatch):
    with zipfile.ZipFile(tmp_path / "Maven.zip", "w") as zf:
        zf.writestr("GHSA-off-1.json", json.dumps(ADV_EXPLICIT))
        zf.writestr("GHSA-off-2.json", json.dumps(ADV_RANGE))
    monkeypatch.setenv("OSV_OFFLINE_DIR", str(tmp_path))
    import ingestion.osv_offline as off
    off._INDEX.clear()
    return tmp_path


def test_explicit_version_match(snapshot_dir):
    from ingestion.osv_offline import query_versioned_offline
    hits = query_versioned_offline([("org.springframework:spring-web", "Maven", "5.3.22")])
    v = hits[("org.springframework:spring-web", "5.3.22")][0]
    assert v.vuln_id == "GHSA-off-1" and v.severity == "HIGH" and v.aliases == ["CVE-2024-1111"]
    # a version NOT in the list doesn't match
    assert ("org.springframework:spring-web", "6.0.0") not in \
        query_versioned_offline([("org.springframework:spring-web", "Maven", "6.0.0")])


def test_range_match_introduced_fixed(snapshot_dir):
    from ingestion.osv_offline import query_versioned_offline
    assert query_versioned_offline([("org.yaml:snakeyaml", "Maven", "1.30")])   # < fixed → hit
    assert not query_versioned_offline([("org.yaml:snakeyaml", "Maven", "1.33")])  # == fixed → clean
    assert not query_versioned_offline([("org.yaml:snakeyaml", "Maven", "2.0")])


def test_unavailable_without_dir(monkeypatch):
    monkeypatch.delenv("OSV_OFFLINE_DIR", raising=False)
    monkeypatch.setattr("ingestion.osv_offline._offline_dir", lambda: "")
    from ingestion.osv_offline import available
    assert available({"Maven"}) is False


def test_vuln_lookup_falls_back_to_offline_snapshot(snapshot_dir, monkeypatch):
    """Both live sources down → snapshot serves the result, clearly labelled."""
    from ingestion.pom_sca import _vuln_lookup
    from types import SimpleNamespace
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: SimpleNamespace(vuln_source="xray", vuln_fallback_source="osv",
                                                data_dir="data", osv_offline_dir=str(snapshot_dir)))
    monkeypatch.setattr("ingestion.xray_client.query_versioned_xray",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("xray down")))
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("osv down")))
    src, hits, note = _vuln_lookup([("org.springframework:spring-web", "Maven", "5.3.22")], 5,
                                   source="xray")
    assert src == "osv-offline"
    assert "OFFLINE OSV snapshot" in note
    assert hits[("org.springframework:spring-web", "5.3.22")][0].vuln_id == "GHSA-off-1"


def test_honest_error_when_no_snapshot(monkeypatch, tmp_path):
    """No offline dir either → the original honest OsvUnavailable still raises."""
    from ingestion.pom_sca import _vuln_lookup
    from types import SimpleNamespace
    monkeypatch.delenv("OSV_OFFLINE_DIR", raising=False)
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: SimpleNamespace(vuln_source="osv", vuln_fallback_source="none",
                                                data_dir="data", osv_offline_dir=""))
    monkeypatch.setattr("ingestion.osv_client.query_versioned",
                        lambda *a, **k: (_ for _ in ()).throw(OsvUnavailable("osv down")))
    with pytest.raises(OsvUnavailable):
        _vuln_lookup([("g:a", "Maven", "1.0")], 5, source="osv")
