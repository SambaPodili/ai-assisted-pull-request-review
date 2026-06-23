"""
tests/test_nuget_sca.py
-----------------------
.NET / NuGet SCA (Path A): dependency parsing across .csproj (SDK-style),
packages.config (legacy) and Directory.Packages.props (CPM), plus scan/endpoint
with OSV mocked — the C# counterpart to test_pom_sca.py.
"""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ingestion.nuget_sca import parse_nuget_dependencies, scan_nuget
from ingestion.osv_client import OsvVuln

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
    <PackageReference Include="Serilog" Version="2.10.0" />
    <PackageReference Include="MyInternalLib" />
    <PackageReference Include="System.Net.Http" Version="4.3.*" />
  </ItemGroup>
</Project>"""

PKGCONFIG = '<packages><package id="jQuery" version="3.4.1" targetFramework="net48" /></packages>'
CPM = '<Project><ItemGroup><PackageVersion Include="System.Text.Json" Version="6.0.0" /></ItemGroup></Project>'


def test_parse_csproj_packagereference():
    deps = {d["package"]: d for d in parse_nuget_dependencies(CSPROJ)}
    assert deps["Newtonsoft.Json"]["version"] == "13.0.1" and deps["Newtonsoft.Json"]["resolved"]
    assert deps["Serilog"]["resolved"]
    assert deps["MyInternalLib"]["resolved"] is False        # no version (central / CPM)
    assert deps["System.Net.Http"]["resolved"] is False      # floating 4.3.* not concrete


def test_parse_packages_config_and_cpm():
    assert parse_nuget_dependencies(PKGCONFIG)[0] == {"package": "jQuery", "version": "3.4.1", "resolved": True}
    assert parse_nuget_dependencies(CPM)[0]["package"] == "System.Text.Json"


def test_empty_manifest_raises():
    with pytest.raises(ValueError):
        scan_nuget("<Project></Project>")


def test_scan_reports_cve_and_unresolved():
    def fake_qv(items, timeout_s=15, raise_on_error=False):
        return {("Newtonsoft.Json", "13.0.1"): [
            OsvVuln(package="Newtonsoft.Json", vuln_id="GHSA-5crp",
                    summary="Improper handling of exceptional conditions",
                    severity="HIGH", aliases=["CVE-2024-21907"])]}
    with patch("ingestion.osv_client.query_versioned", fake_qv):
        res = scan_nuget(CSPROJ)
    assert res["ecosystem"] == "NuGet"
    assert res["dependencies_scanned"] == 2                   # Newtonsoft.Json + Serilog resolved
    assert "MyInternalLib" in res["unresolved"]
    v = res["vulnerabilities"]
    assert len(v) == 1 and v[0]["cve"] == "CVE-2024-21907" and v[0]["severity"] == "HIGH"
    assert v[0]["depth"] == "direct"


@pytest.fixture
def client():
    from api.app import create_app
    return TestClient(create_app())


def test_endpoint_scans_nuget(client):
    with patch("ingestion.osv_client.query_versioned", lambda items, timeout_s=15, raise_on_error=False: {}):
        r = client.post("/api/v1/sca/nuget", content=CSPROJ.encode())
    assert r.status_code == 200
    body = r.json()
    assert body["ecosystem"] == "NuGet" and body["dependencies_scanned"] == 2


def test_osv_unavailable_is_surfaced_not_masked():
    # When OSV can't be reached, the scan must report it — NOT "0 vulnerabilities".
    from ingestion.osv_client import OsvUnavailable
    def boom(items, timeout_s=15, raise_on_error=False):
        raise OsvUnavailable("SSL: CERTIFICATE_VERIFY_FAILED")
    with patch("ingestion.osv_client.query_versioned", boom):
        res = scan_nuget(CSPROJ)
    assert res["osv_error"] and "CERTIFICATE" in res["osv_error"]
    assert "Could not reach" in res["summary"]
    assert res["vulnerabilities"] == []
