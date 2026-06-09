"""
tests/test_pom_sca.py
----------------------
Maven pom.xml SCA (Path A): dependency parsing (properties, dependencyManagement,
unresolved versions) and the scan/endpoint with OSV mocked.
"""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ingestion.pom_sca import parse_pom_dependencies, scan_pom
from ingestion.osv_client import OsvVuln

POM = """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties><jackson.version>2.9.8</jackson.version></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId><version>5.3.0</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId><version>${jackson.version}</version></dependency>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId></dependency>
    <dependency><groupId>com.acme</groupId><artifactId>internal</artifactId><version>${unset}</version></dependency>
  </dependencies>
</project>"""


def test_parse_resolves_properties_and_managed_versions():
    deps = {f"{d['group']}:{d['artifact']}": d for d in parse_pom_dependencies(POM)}
    assert deps["com.fasterxml.jackson.core:jackson-databind"]["version"] == "2.9.8"   # ${prop}
    assert deps["org.springframework:spring-core"]["version"] == "5.3.0"               # from <dependencyManagement>
    assert deps["com.acme:internal"]["resolved"] is False                              # unresolved ${unset}


def test_invalid_pom_raises():
    with pytest.raises(ValueError):
        parse_pom_dependencies("not xml")


def test_scan_reports_cve_and_unresolved():
    def fake_qv(items, timeout_s=15):
        return {("com.fasterxml.jackson.core:jackson-databind", "2.9.8"): [
            OsvVuln(package="com.fasterxml.jackson.core:jackson-databind", vuln_id="GHSA-x",
                    summary="Deserialization RCE", severity="CRITICAL", aliases=["CVE-2019-12384"])]}
    with patch("ingestion.osv_client.query_versioned", fake_qv):
        res = scan_pom(POM)
    assert res["dependencies_scanned"] == 2          # 2 resolved (jackson + spring)
    assert "com.acme:internal" in res["unresolved"]   # unresolved reported, not scanned
    v = res["vulnerabilities"]
    assert len(v) == 1 and v[0]["cve"] == "CVE-2019-12384" and v[0]["severity"] == "CRITICAL"
    assert v[0]["depth"] == "direct"


@pytest.fixture
def client():
    from api.app import create_app
    return TestClient(create_app())


def test_endpoint_scans_pom(client):
    def fake_qv(items, timeout_s=15):
        return {}
    with patch("ingestion.osv_client.query_versioned", fake_qv):
        r = client.post("/api/v1/sca/pom", content=POM.encode())
    assert r.status_code == 200
    body = r.json()
    assert body["ecosystem"] == "Maven" and body["dependencies_scanned"] == 2


def test_endpoint_rejects_empty(client):
    assert client.post("/api/v1/sca/pom", content=b"").status_code == 400
