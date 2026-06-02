"""
ingestion/osv_client.py
------------------------
Query the OSV.dev vulnerability database for known CVEs in changed packages.

OSV.dev is free, open-source, and requires no API key.
Docs: https://google.github.io/osv.dev/api/

Supported ecosystems (mapped from language detected in diff):
  python      → PyPI
  javascript  → npm
  typescript  → npm
  java        → Maven
  kotlin      → Maven
  scala       → Maven
  go          → Go
  rust        → crates.io
  ruby        → RubyGems
  php         → Packagist
  dart        → Pub
  swift       → SwiftURL
  r           → CRAN

Other languages: query is attempted with ecosystem="" (OSV ignores unknown ones).
"""
from __future__ import annotations
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_QUERY_URL = "https://api.osv.dev/v1/query"

_LANG_TO_ECOSYSTEM: dict[str, str] = {
    "python":     "PyPI",
    "javascript": "npm",
    "typescript": "npm",
    "java":       "Maven",
    "kotlin":     "Maven",
    "scala":      "Maven",
    "groovy":     "Maven",
    "go":         "Go",
    "rust":       "crates.io",
    "ruby":       "RubyGems",
    "php":        "Packagist",
    "dart":       "Pub",
    "swift":      "SwiftURL",
    "r":          "CRAN",
    "elixir":     "Hex",
    "erlang":     "Hex",
}


@dataclass
class OsvVuln:
    package:   str
    vuln_id:   str          # e.g. "GHSA-xxxx" or "CVE-2024-1234"
    summary:   str
    severity:  str = ""     # CRITICAL | HIGH | MEDIUM | LOW | ""
    aliases:   list[str] = field(default_factory=list)  # cross-refs (CVE IDs)


def _severity_from_vuln(vuln: dict) -> str:
    """Extract highest severity label from OSV vuln object."""
    for sev in vuln.get("severity", []):
        score = sev.get("score", "")
        if "CRITICAL" in score.upper():
            return "CRITICAL"
        if "HIGH" in score.upper():
            return "HIGH"
        if "MEDIUM" in score.upper():
            return "MEDIUM"
        if "LOW" in score.upper():
            return "LOW"
    # Fall back to CVSS numeric score in database_specific
    for affected in vuln.get("affected", []):
        for r in affected.get("ranges", []):
            for evt in r.get("events", []):
                pass   # structure varies — omit deep parsing
    return ""


def query_batch(
    packages:  list[tuple[str, str]],   # [(name, ecosystem), ...]
    timeout_s: int = 15,
) -> dict[str, list[OsvVuln]]:
    """
    Batch-query OSV for multiple packages in a single HTTP request.

    Returns {package_name: [OsvVuln, ...]}
    """
    if not packages:
        return {}

    queries = [{"package": {"name": name, "ecosystem": eco}} for name, eco in packages]
    payload = json.dumps({"queries": queries}).encode()

    req = urllib.request.Request(
        OSV_BATCH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("OSV batch query failed: %s", exc)
        return {}

    results: dict[str, list[OsvVuln]] = {}
    for (name, _eco), result in zip(packages, data.get("results", [])):
        vulns: list[OsvVuln] = []
        for v in result.get("vulns", []):
            vulns.append(OsvVuln(
                package=name,
                vuln_id=v.get("id", ""),
                summary=v.get("summary", "")[:200],
                severity=_severity_from_vuln(v),
                aliases=[a for a in v.get("aliases", []) if a.startswith("CVE-")],
            ))
        if vulns:
            results[name] = vulns
    return results


def lookup_packages(
    package_names: list[str],
    language:      str = "python",
    timeout_s:     int = 15,
) -> list[OsvVuln]:
    """
    Query OSV for a list of packages in the ecosystem matching *language*.
    Returns a flat list of all vulnerabilities found.
    """
    ecosystem = _LANG_TO_ECOSYSTEM.get(language, "")
    packages  = [(name, ecosystem) for name in package_names if name]

    if not packages:
        return []

    results = query_batch(packages, timeout_s=timeout_s)
    vulns: list[OsvVuln] = []
    for pkg_vulns in results.values():
        vulns.extend(pkg_vulns)

    if vulns:
        log.info("OSV: found %d vulnerabilities across %d packages", len(vulns), len(results))
    return vulns


def lookup_multi_ecosystem(
    pkg_by_language: dict[str, list[str]],   # {language: [pkg_name, ...]}
    timeout_s: int = 15,
) -> list[OsvVuln]:
    """
    Look up packages grouped by language in a single batch call.
    Ideal when a diff touches manifests from multiple ecosystems.
    """
    packages: list[tuple[str, str]] = []
    for lang, names in pkg_by_language.items():
        eco = _LANG_TO_ECOSYSTEM.get(lang, "")
        for name in names:
            if name:
                packages.append((name, eco))

    if not packages:
        return []

    results = query_batch(packages, timeout_s=timeout_s)
    return [v for vulns in results.values() for v in vulns]


def cve_ids(vulns: list[OsvVuln]) -> list[str]:
    """Collect all CVE IDs (from vuln_id and aliases) deduped."""
    ids: list[str] = []
    seen: set[str] = set()
    for v in vulns:
        candidates = [v.vuln_id] + v.aliases
        for c in candidates:
            if c.startswith("CVE-") and c not in seen:
                seen.add(c)
                ids.append(c)
    return ids
