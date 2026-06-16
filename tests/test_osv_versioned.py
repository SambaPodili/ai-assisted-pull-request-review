"""
tests/test_osv_versioned.py
----------------------------
OSV CVE lookup must query with the PINNED version (query_versioned) so it only
reports CVEs that actually affect the version in the manifest — not every CVE
ever filed against the package (which over-reports fixed vulns).
"""
from unittest.mock import patch

from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.dependency_agent import _extract_versioned_packages, _osv_lookup, _clean_ver
from ingestion.osv_client import OsvVuln


def _req(file_path, language, content):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b",
        hunks=[DiffHunk(file_path=file_path, language=language, additions=1, deletions=0,
                        content=content)],
    )


def test_clean_ver_strips_range_operators():
    assert _clean_ver("^1.2.3") == "1.2.3"
    assert _clean_ver(">= 2.0.0") == "2.0.0"
    assert _clean_ver('"3.1.0"') == "3.1.0"


def test_extract_npm_versioned():
    req = _req("package.json", "javascript",
               '@@ -1 +1 @@\n+    "lodash": "^4.17.19",\n')
    versioned, name_only = _extract_versioned_packages(req)
    assert ("lodash", "npm", "4.17.19") in versioned
    assert name_only == []


def test_extract_pip_versioned():
    req = _req("requirements.txt", "python",
               "@@ -1 +1 @@\n+requests==2.19.0\n+urllib3>=1.24.1\n")
    versioned, _ = _extract_versioned_packages(req)
    names = {(n, v) for (n, e, v) in versioned}
    assert ("requests", "2.19.0") in names
    assert ("urllib3", "1.24.1") in names


def test_extract_maven_adjacent_lines():
    req = _req("pom.xml", "java",
               "@@ -1 +1 @@\n+    <artifactId>jackson-databind</artifactId>\n"
               "+    <version>2.9.8</version>\n")
    versioned, _ = _extract_versioned_packages(req)
    assert ("jackson-databind", "Maven", "2.9.8") in versioned


def test_osv_lookup_uses_versioned_query():
    req = _req("requirements.txt", "python", "@@ -1 +1 @@\n+requests==2.19.0\n")
    captured = {}

    def fake_versioned(items, timeout_s=15):
        captured["items"] = items
        return {("requests", "2.19.0"): [OsvVuln(
            package="requests", vuln_id="GHSA-x", summary="bad", severity="HIGH",
            aliases=["CVE-2018-18074"])]}

    with patch("config.settings.get_settings") as gs, \
         patch("ingestion.osv_client.query_versioned", fake_versioned), \
         patch("ingestion.osv_client.query_batch", return_value={}):
        gs.return_value.osv_enabled = True
        cves = _osv_lookup(["requests"], req)

    # the pinned version was passed through to OSV
    assert ("requests", "PyPI", "2.19.0") in captured["items"]
    assert cves == ["CVE-2018-18074"]


def test_osv_lookup_disabled_returns_empty():
    req = _req("requirements.txt", "python", "@@ -1 +1 @@\n+requests==2.19.0\n")
    with patch("config.settings.get_settings") as gs:
        gs.return_value.osv_enabled = False
        assert _osv_lookup(["requests"], req) == []
