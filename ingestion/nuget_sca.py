"""
ingestion/nuget_sca.py
----------------------
Software Composition Analysis (SCA) for .NET / NuGet — the C# counterpart to
pom_sca.py. Parses the DIRECT dependencies declared across the common NuGet
manifest formats and matches each package@version against OSV (ecosystem "NuGet"):

  • SDK-style  .csproj                 <PackageReference Include="X" Version="Y" />
  • Legacy     packages.config         <package id="X" version="Y" />
  • Central Package Management (CPM)    Directory.Packages.props
                                        <PackageVersion Include="X" Version="Y" />

Direct deps only (no transitive graph) — findings are depth="direct". A
PackageReference with no version (version centralised via CPM, resolved in
another file) is reported as "unresolved" rather than silently dropped, the same
way pom_sca handles parent-POM-managed versions.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />   (Version as attribute)
_PKGREF_ATTR = re.compile(r'<PackageReference\b[^>]*?\bInclude="([^"]+)"[^>]*?\bVersion="([^"]+)"', re.I)
# <PackageReference Include="X">  …  (no Version attribute — central/child version)
_PKGREF_NOVER = re.compile(r'<PackageReference\b(?![^>]*\bVersion=)[^>]*?\bInclude="([^"]+)"', re.I)
# <package id="X" version="Y" targetFramework="…" />               (packages.config)
_PKGCONFIG = re.compile(r'<package\b[^>]*?\bid="([^"]+)"[^>]*?\bversion="([^"]+)"', re.I)
# <PackageVersion Include="X" Version="Y" />                        (Directory.Packages.props, CPM)
_PKGVERSION = re.compile(r'<PackageVersion\b[^>]*?\bInclude="([^"]+)"[^>]*?\bVersion="([^"]+)"', re.I)


def _concrete(version: str) -> bool:
    """A single, pinned version — not a NuGet range/float ([1,2) , 1.0.* , $(Var))."""
    v = (version or "").strip()
    return bool(v) and not any(c in v for c in "[](),*$") and not v.endswith("-")


def parse_nuget_dependencies(text: str) -> list[dict]:
    """Return [{package, version, resolved}] for direct NuGet deps, de-duplicated
    by package id (case-insensitive — NuGet ids are case-insensitive)."""
    found: dict[str, str] = {}     # lower(id) -> version
    names: dict[str, str] = {}     # lower(id) -> original-case id

    def add(name: str, version: str) -> None:
        k = (name or "").strip().lower()
        if not k:
            return
        names.setdefault(k, name.strip())
        # keep the first version seen, but upgrade "" / range → a concrete version
        if k not in found or (not _concrete(found[k]) and _concrete(version)):
            found[k] = (version or "").strip()

    for n, v in _PKGREF_ATTR.findall(text):  add(n, v)
    for n, v in _PKGCONFIG.findall(text):    add(n, v)
    for n, v in _PKGVERSION.findall(text):   add(n, v)
    for n in _PKGREF_NOVER.findall(text):    add(n, "")   # version managed elsewhere (CPM)

    return [{"package": names[k], "version": v, "resolved": _concrete(v)} for k, v in found.items()]


def scan_nuget(text: str, timeout_s: int = 15,
               vuln_source: str = "", xray_url: str = "", xray_auth: str = "") -> dict:
    """Parse a NuGet manifest, query the selected vulnerability source (OSV or
    Xray) for the resolved deps, and return a structured SCA result identical in
    shape to pom_sca.scan_pom."""
    deps = parse_nuget_dependencies(text)
    if not deps:
        raise ValueError("No NuGet dependencies found — expected <PackageReference>, "
                         "<package> (packages.config), or <PackageVersion> entries.")

    resolved = [d for d in deps if d["resolved"]]
    unresolved = [d for d in deps if not d["resolved"]]

    from ingestion.osv_client import OsvUnavailable
    from ingestion.pom_sca import _vuln_lookup
    from ingestion import sca_cache
    items = [(d["package"], "NuGet", d["version"]) for d in resolved]
    _ck = sca_cache.cache_key(text, "NuGet", vuln_source)
    osv_error, used_source, fb_note = "", "osv", ""
    try:
        used_source, hits, fb_note = _vuln_lookup(items, timeout_s, vuln_source, xray_url, xray_auth) if items else ("osv", {}, "")
    except OsvUnavailable as exc:
        cached, ts = sca_cache.load(_ck)
        if cached:
            return {**cached, "stale": True, "stale_from": ts, "osv_error": None,
                    "stale_note": (f"Vulnerability database unreachable — showing the last successful "
                                   f"scan from {sca_cache.age_label(ts)}. New CVEs since then are NOT "
                                   f"reflected. ({exc})")}
        hits, osv_error = {}, str(exc)

    vulns: list[dict] = []
    for d in resolved:
        for v in hits.get((d["package"], d["version"]), []):
            cve = next((a for a in v.aliases if a.startswith("CVE-")), v.vuln_id)
            vulns.append({
                "package": d["package"], "version": d["version"], "scope": "compile",
                "vuln_id": v.vuln_id, "cve": cve, "severity": v.severity or "UNKNOWN",
                "summary": v.summary, "depth": "direct",
            })

    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    vulns.sort(key=lambda x: sev_rank.get(x["severity"], 4))
    if osv_error:
        summary = ("Could not reach the OSV vulnerability database — scan incomplete. "
                   "Check network access, or set OSV_CA_BUNDLE / OSV_VERIFY_SSL / OSV_BASE_URL.")
    else:
        summary = (f"{len(vulns)} vulnerabilit{'y' if len(vulns) == 1 else 'ies'} in "
                   f"{len(resolved)} direct dependenc{'y' if len(resolved) == 1 else 'ies'}"
                   + (f"; {len(unresolved)} version(s) unresolved (central package management)" if unresolved else ""))
    result = {
        "ecosystem": "NuGet",
        "depth": "direct",
        "dependencies_scanned": len(resolved),
        "unresolved": [d["package"] for d in unresolved],
        "vulnerabilities": vulns,
        "osv_error": osv_error or None,
        "vuln_source": used_source,
        "fallback_note": fb_note or None,
        "summary": summary,
    }
    if not osv_error:
        sca_cache.save(_ck, result)   # last-known-good for Layer-3 fallback
    return result
