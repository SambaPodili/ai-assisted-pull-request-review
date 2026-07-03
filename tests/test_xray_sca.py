"""
tests/test_xray_sca.py
----------------------
JFrog Xray as a selectable SCA vulnerability source (alternative to OSV).
HTTP fully mocked.
"""
from __future__ import annotations

import io
import json

import pytest

from ingestion.osv_client import OsvUnavailable
from ingestion.xray_client import _component_id, _severity, query_versioned_xray


class _Resp(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _mock_urlopen(monkeypatch, response: dict, capture: list):
    import ingestion.xray_client as m
    def fake(req, timeout=None, context=None):
        capture.append((req.full_url, req.headers, json.loads(req.data) if req.data else None))
        return _Resp(json.dumps(response).encode())
    monkeypatch.setattr(m.urllib.request, "urlopen", fake)


def test_component_ids():
    assert _component_id("org.springframework:spring-web", "Maven", "5.3.22") == \
        "gav://org.springframework:spring-web:5.3.22"
    assert _component_id("Newtonsoft.Json", "NuGet", "12.0.1") == "nuget://Newtonsoft.Json:12.0.1"


def test_severity_label_and_cvss_fallback():
    assert _severity({"severity": "Critical"}) == "CRITICAL"
    assert _severity({"severity": "Unknown", "cves": [{"cve": "CVE-1", "cvss_v3": "9.8/CVSS:3.1/AV:N"}]}) == "CRITICAL"
    assert _severity({"cves": [{"cvss_v3": "5.3/CVSS:3.1/AV:N"}]}) == "MEDIUM"


def test_query_parses_summary_response(monkeypatch):
    calls = []
    _mock_urlopen(monkeypatch, {
        "artifacts": [{
            "general": {"component_id": "gav://org.springframework:spring-web:5.3.22"},
            "issues": [
                {"issue_id": "XRAY-1", "severity": "High", "issue_type": "security",
                 "summary": "SSRF in spring-web", "cves": [{"cve": "CVE-2024-1"}]},
                {"issue_id": "XRAY-2", "severity": "Low", "issue_type": "license", "summary": "GPL"},
            ],
        }],
    }, calls)
    hits = query_versioned_xray([("org.springframework:spring-web", "Maven", "5.3.22")],
                                base_url="https://xray.local", auth="tok123")
    vulns = hits[("org.springframework:spring-web", "5.3.22")]
    assert len(vulns) == 1                                   # license issue skipped
    assert vulns[0].severity == "HIGH" and vulns[0].aliases == ["CVE-2024-1"]
    url, headers, body = calls[0]
    assert url == "https://xray.local/api/v1/summary/component"
    assert headers.get("Authorization") == "Bearer tok123"   # raw token gets Bearer prefix
    assert body["component_details"][0]["component_id"] == "gav://org.springframework:spring-web:5.3.22"


def test_unconfigured_xray_raises_unavailable(monkeypatch):
    monkeypatch.delenv("XRAY_BASE_URL", raising=False)
    with pytest.raises(OsvUnavailable):
        query_versioned_xray([("a:b", "Maven", "1")], raise_on_error=True, base_url="")


def test_scan_pom_dispatches_to_xray(monkeypatch):
    import ingestion.pom_sca as pom
    monkeypatch.setenv("MAVEN_SCAN_TRANSITIVE", "false")
    seen = {}
    def fake_xray(items, timeout_s=20, raise_on_error=False, base_url="", auth=""):
        seen["items"] = items; seen["base"] = base_url
        return {}
    monkeypatch.setattr("ingestion.xray_client.query_versioned_xray", fake_xray)
    pom_xml = ('<project><dependencies><dependency><groupId>g</groupId>'
               '<artifactId>a</artifactId><version>1.0</version></dependency></dependencies></project>')
    res = pom.scan_pom(pom_xml, resolve_parents=False, vuln_source="xray", xray_url="https://xr.local")
    assert res["vuln_source"] == "xray"
    assert seen["items"] == [("g:a", "Maven", "1.0")] and seen["base"] == "https://xr.local"
