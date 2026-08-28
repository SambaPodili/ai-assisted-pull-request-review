"""
tests/test_license_real_lookup.py
------------------------------------
license_compliance_agent previously classified risk purely from KEYWORDS IN
THE PACKAGE NAME — missing real copyleft packages whose name doesn't say so,
and flagging every MIT/Apache package not on a small hardcoded safe-list as
"unknown, needs review". These tests cover the deps.dev-backed real-license
path added to fix both, and confirm it falls back to the old heuristic
cleanly when deps.dev is disabled or unreachable.
"""
from __future__ import annotations
from unittest.mock import patch

from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.license_compliance_agent import (
    _scan, _classify_spdx, _classify_spdx_single,
)


def _req(file_path, content, language="text"):
    return AnalysisRequest(
        request_id="t", change_type=ChangeType.PR, repo_url="r",
        source_ref="a", target_ref="b",
        hunks=[DiffHunk(file_path=file_path, language=language,
                        additions=1, deletions=0, content=content)],
    )


# ── _classify_spdx ──────────────────────────────────────────────────────────

def test_classify_spdx_simple_permissive():
    assert _classify_spdx("MIT") == ("safe", "MIT")
    assert _classify_spdx("Apache-2.0")[0] == "safe"


def test_classify_spdx_gpl_is_critical_even_without_gpl_in_name():
    # This is the real-world case a name-only heuristic misses: a package
    # whose license is GPL but whose name never says "gpl".
    risk, name = _classify_spdx("GPL-3.0-only")
    assert risk == "critical"


def test_classify_spdx_lgpl_is_medium_not_critical():
    risk, _ = _classify_spdx("LGPL-2.1-only")
    assert risk == "medium"


def test_classify_spdx_or_expression_takes_permissive_escape_hatch():
    # Dual-licensed "GPL OR MIT" lets the licensee pick MIT — not critical.
    risk, name = _classify_spdx("GPL-3.0-only OR MIT")
    assert risk == "safe"
    assert name == "MIT"


def test_classify_spdx_and_expression_takes_worst_component():
    risk, _ = _classify_spdx("MIT AND GPL-3.0-only")
    assert risk == "critical"


def test_classify_spdx_classpath_exception_is_safe():
    # GPL-2.0 WITH Classpath-exception specifically permits proprietary
    # linking — common across the Java ecosystem (OpenJDK-adjacent libs).
    risk, _ = _classify_spdx_single("GPL-2.0-only WITH Classpath-exception-2.0")
    assert risk == "safe"


def test_classify_spdx_empty_returns_unknown():
    assert _classify_spdx("") == ("", "")


# ── _scan() end-to-end with a mocked real-license lookup ───────────────────

def test_real_license_overrides_false_positive_unknown():
    """A package not on the hardcoded safe-list, but whose REAL license is
    MIT, must not be reported as "unknown, needs review" once deps.dev
    resolves it — this was the tool's main noise source."""
    req = _req("requirements.txt", '@@ -1 +1 @@\n+some-uncommon-pkg==1.0.0\n')
    with patch("agents.license_compliance_agent._lookup_real_licenses",
               return_value={("some-uncommon-pkg", "requirements.txt"): "MIT"}):
        result = _scan(req)
    assert result.findings == []
    assert result.has_copyleft is False


def test_real_license_catches_copyleft_with_safe_looking_name():
    """A package whose NAME has no copyleft keyword, but whose real license
    is GPL, must be caught — this was the tool's main false-negative."""
    req = _req("requirements.txt", '@@ -1 +1 @@\n+totally-innocuous-name==2.0.0\n')
    with patch("agents.license_compliance_agent._lookup_real_licenses",
               return_value={("totally-innocuous-name", "requirements.txt"): "GPL-3.0-only"}):
        result = _scan(req)
    assert result.has_copyleft is True
    assert any(f.risk_level == "critical" for f in result.findings)


def test_falls_back_to_heuristic_when_deps_dev_returns_nothing():
    """deps.dev disabled/unreachable — a package WITH "gpl" in its name must
    still be caught via the pre-existing name heuristic, unchanged."""
    req = _req("requirements.txt", '@@ -1 +1 @@\n+some-gpl-licensed-tool==1.0.0\n')
    with patch("agents.license_compliance_agent._lookup_real_licenses", return_value={}):
        result = _scan(req)
    assert result.has_copyleft is True


def test_maven_manifest_unaffected_no_groupid_no_lookup():
    """pom.xml isn't eligible for real-license lookup (artifactId alone isn't
    enough to query a registry) — must still use the heuristic, unchanged."""
    req = _req("pom.xml",
               "@@ -1 +1 @@\n+    <artifactId>some-gpl-lib</artifactId>\n")
    with patch("agents.license_compliance_agent._lookup_real_licenses") as mock_lookup:
        mock_lookup.return_value = {}
        result = _scan(req)
        # Called (so candidates were still collected) but pom.xml packages
        # never resolve to a system, so the heuristic path is exercised.
        assert result.has_copyleft is True
