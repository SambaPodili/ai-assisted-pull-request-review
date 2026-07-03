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
import math
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Base URL is overridable for an internal OSV mirror (corporate / air-gapped).
_OSV_BASE = (os.getenv("OSV_BASE_URL", "") or "https://api.osv.dev").rstrip("/")
OSV_BATCH_URL = f"{_OSV_BASE}/v1/querybatch"
OSV_QUERY_URL = f"{_OSV_BASE}/v1/query"


class OsvUnavailable(Exception):
    """OSV could not be reached (network / TLS). Lets callers distinguish
    'no vulnerabilities' from 'the scan never ran'."""


def _ssl_context() -> ssl.SSLContext:
    """TLS context honouring corporate CA bundles / verification opt-out:
      OSV_CA_BUNDLE=/path/ca.pem  → trust a corporate (TLS-intercepting) CA
      OSV_VERIFY_SSL=false        → INSECURE: skip verification (last resort)
    Otherwise falls back to certifi's CA bundle, which fixes the common
    'unable to get local issuer certificate' error on macOS / minimal images."""
    # Read from Settings FIRST (honours .env, which pydantic does NOT push into
    # os.environ), then fall back to os.getenv for plain exported env vars.
    verify, ca = True, ""
    try:
        from config.settings import get_settings
        _s = get_settings()
        verify = bool(getattr(_s, "osv_verify_ssl", True))
        ca = (getattr(_s, "osv_ca_bundle", "") or "").strip()
    except Exception:
        pass
    if os.getenv("OSV_VERIFY_SSL", "").strip().lower() in ("false", "0", "no"):
        verify = False
    ca = (os.getenv("OSV_CA_BUNDLE", "").strip() or ca)

    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca:
        return ssl.create_default_context(cafile=ca)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _open_with_retries(open_fn, req, timeout_s, attempts=(0, 1.0, 3.0), label="vuln-db"):
    """Layer-1 fallback: retry a network call briefly before declaring the source
    down. Corporate proxies / Xray under load throw transient 5xx/timeouts all
    the time — two quick retries eliminate most false 'outages' without hiding a
    real one. Retries NETWORK errors only (URLError/OSError/timeouts)."""
    import time as _t
    last = None
    for i, delay in enumerate(attempts):
        if delay:
            _t.sleep(delay)
        try:
            return open_fn(req, timeout_s)
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if i < len(attempts) - 1:
                log.warning("%s request failed (attempt %d/%d): %s — retrying",
                            label, i + 1, len(attempts), exc)
    raise last


def _osv_urlopen(req, timeout_s):
    """urlopen for OSV requests, routed through OSV_PROXY_URL when configured
    (corporate forward proxy, e.g. http://proxy.bank.com:8080). Scoped to OSV
    ONLY — internal Artifactory/Xray traffic must never go via the proxy.
    Reads Settings first (honours .env), then the env var; blank → direct."""
    proxy = ""
    try:
        from config.settings import get_settings
        proxy = (getattr(get_settings(), "osv_proxy_url", "") or "").strip()
    except Exception:
        pass
    proxy = os.getenv("OSV_PROXY_URL", "").strip() or proxy
    ctx = _ssl_context()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        return opener.open(req, timeout=timeout_s)
    return urllib.request.urlopen(req, timeout=timeout_s, context=ctx)


_LANG_TO_ECOSYSTEM: dict[str, str] = {
    "python":     "PyPI",
    "javascript": "npm",
    "typescript": "npm",
    "java":       "Maven",
    "kotlin":     "Maven",
    "csharp":     "NuGet",
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


_SEV_LABELS = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MODERATE": "MEDIUM",
               "MEDIUM": "MEDIUM", "LOW": "LOW"}


def _band(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return ""


def _cvss_band(score_str: str) -> str:
    """Severity band from an OSV severity score — a plain number OR a CVSS v3
    vector (which is what GHSA usually provides), computed via the CVSS 3.1 base
    formula."""
    s = (score_str or "").strip()
    try:
        return _band(float(s))
    except ValueError:
        pass
    if not s.upper().startswith("CVSS:3"):
        return ""
    try:
        m = dict(p.split(":", 1) for p in s.split("/")[1:] if ":" in p)
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[m["AV"]]
        ac = {"L": 0.77, "H": 0.44}[m["AC"]]
        ui = {"N": 0.85, "R": 0.62}[m["UI"]]
        changed = m["S"] == "C"
        pr = ({"N": 0.85, "L": 0.68, "H": 0.5} if changed
              else {"N": 0.85, "L": 0.62, "H": 0.27})[m["PR"]]
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        iss = 1 - (1 - cia[m["C"]]) * (1 - cia[m["I"]]) * (1 - cia[m["A"]])
        impact = (7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15) if changed else 6.42 * iss
        if impact <= 0:
            return ""
        expl = 8.22 * av * ac * pr * ui
        raw = min((1.08 if changed else 1.0) * (impact + expl), 10.0)
        return _band(math.ceil(raw * 10) / 10)
    except (KeyError, ValueError):
        return ""


def _severity_from_vuln(vuln: dict) -> str:
    """Highest severity for an OSV vuln. Prefers the GHSA text label
    (database_specific.severity), then computes a band from the CVSS vector."""
    label = ((vuln.get("database_specific") or {}).get("severity") or "").upper()
    if label in _SEV_LABELS:
        return _SEV_LABELS[label]
    for aff in vuln.get("affected", []):
        l2 = ((aff.get("database_specific") or {}).get("severity") or "").upper()
        if l2 in _SEV_LABELS:
            return _SEV_LABELS[l2]
    for sev in vuln.get("severity", []):
        band = _cvss_band(sev.get("score", ""))
        if band:
            return band
    return ""


_vuln_cache: dict[str, dict] = {}   # vuln_id -> full OSV vuln object (process-wide)


def _fetch_vuln_details(ids, timeout_s: int = 15) -> dict[str, dict]:
    """Fetch full OSV vuln objects (with severity/summary) for a set of vuln IDs,
    in parallel and cached. /querybatch only returns IDs, so this back-fills the
    detail needed to classify severity."""
    ids = [i for i in ids if i and i not in _vuln_cache]
    if ids:
        from concurrent.futures import ThreadPoolExecutor

        def _one(vid):
            try:
                req = urllib.request.Request(f"{_OSV_BASE}/v1/vulns/{vid}")
                with _osv_urlopen(req, timeout_s) as r:
                    _vuln_cache[vid] = json.loads(r.read())
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                log.debug("OSV vuln detail fetch failed (%s): %s", vid, exc)
                _vuln_cache[vid] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_one, ids))
    return {i: _vuln_cache[i] for i in set(ids) | set(_vuln_cache) if _vuln_cache.get(i)}


def query_versioned(
    items:     list[tuple[str, str, str]],   # [(name, ecosystem, version), ...]
    timeout_s: int = 15,
    raise_on_error: bool = False,
) -> dict[tuple[str, str], list[OsvVuln]]:
    """Batch-query OSV with EXACT versions so only vulns affecting that version
    are returned. Returns {(name, version): [OsvVuln, ...]}. When raise_on_error
    is set, a network/TLS failure raises OsvUnavailable instead of returning {}
    (so an SCA scan can report 'couldn't reach OSV' rather than 'no vulns')."""
    items = [(n, e, v) for (n, e, v) in items if n and v]
    if not items:
        return {}
    queries = [{"package": {"name": n, "ecosystem": e}, "version": v} for n, e, v in items]
    req = urllib.request.Request(
        OSV_BATCH_URL, data=json.dumps({"queries": queries}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _open_with_retries(_osv_urlopen, req, timeout_s, label="OSV") as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("OSV versioned query failed (after retries): %s", exc)
        if raise_on_error:
            raise OsvUnavailable(str(exc)) from exc
        return {}
    # /querybatch returns only vuln IDs (no severity/summary). Fetch the unique
    # vuln details (parallel + cached) so severities aren't all "UNKNOWN".
    id_set = {v.get("id") for result in data.get("results", []) or []
              for v in (result.get("vulns") or []) if v.get("id")}
    details = _fetch_vuln_details(id_set, timeout_s)

    out: dict[tuple[str, str], list[OsvVuln]] = {}
    for (name, _eco, version), result in zip(items, data.get("results", [])):
        vulns = []
        for stub in result.get("vulns", []) or []:
            v = details.get(stub.get("id"), stub)   # full detail when available
            vulns.append(OsvVuln(
                package=name, vuln_id=v.get("id", ""),
                summary=(v.get("summary") or v.get("details", "") or "")[:200],
                severity=_severity_from_vuln(v),
                aliases=[a for a in v.get("aliases", []) if a.startswith("CVE-")],
            ))
        if vulns:
            out[(name, version)] = vulns
    return out


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
        with _osv_urlopen(req, timeout_s) as resp:
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


def fixed_version_for(
    package:   str,
    ecosystem: str = "PyPI",
    timeout_s: int = 15,
) -> tuple[str, str]:
    """
    Query OSV for a single package and return (safe_version, cve_or_vuln_id).

    Walks the affected[].ranges[].events[] structure to find the first
    'fixed' event — the minimum version that resolves the vulnerability.
    Returns ("", "") if no fix is published or the package is not vulnerable.
    """
    payload = json.dumps({"package": {"name": package, "ecosystem": ecosystem}}).encode()
    req = urllib.request.Request(
        OSV_QUERY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with _osv_urlopen(req, timeout_s) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("OSV fixed-version query failed for %s: %s", package, exc)
        return "", ""

    for vuln in data.get("vulns", []):
        vuln_id = vuln.get("id", "")
        # Prefer a CVE alias for display
        cve = next((a for a in vuln.get("aliases", []) if a.startswith("CVE-")), vuln_id)
        for affected in vuln.get("affected", []):
            for rng in affected.get("ranges", []):
                for evt in rng.get("events", []):
                    fixed = evt.get("fixed")
                    if fixed:
                        return fixed, cve
    return "", ""
