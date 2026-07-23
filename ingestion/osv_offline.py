"""
ingestion/osv_offline.py
------------------------
Offline OSV snapshot lookup — the LAST-RESORT vulnerability source for
air-gapped networks when both the live sources (OSV API / Xray) are down.

Feed it the official OSV data dumps, downloaded on a machine with internet and
copied into OSV_OFFLINE_DIR:

    curl -LO https://osv-vulnerabilities.storage.googleapis.com/Maven/all.zip
    mv all.zip  $OSV_OFFLINE_DIR/Maven.zip           # or Maven/all.zip
    curl -LO https://osv-vulnerabilities.storage.googleapis.com/NuGet/all.zip
    mv all.zip  $OSV_OFFLINE_DIR/NuGet.zip

Each zip contains one OSV-schema JSON per advisory. On first use the zip is
indexed by package name (kept slim in memory, cached per zip mtime), then
lookups are instant. Version matching prefers the explicit `versions` list OSV
provides for Maven/NuGet, falling back to introduced/fixed range evaluation
with a tolerant version comparator.
"""
from __future__ import annotations

import json
import logging
import os
import re
import zipfile

from ingestion.osv_client import OsvVuln, _severity_from_vuln

log = logging.getLogger(__name__)

# eco → cached index: {"mtime": float, "pkgs": {name_lower: [slim_advisory,...]}}
_INDEX: dict[str, dict] = {}


def _offline_dir() -> str:
    try:
        from config.settings import get_settings
        d = (getattr(get_settings(), "osv_offline_dir", "") or "").strip()
    except Exception:
        d = ""
    return os.getenv("OSV_OFFLINE_DIR", "").strip() or d


def _zip_path(ecosystem: str) -> str:
    """Find the snapshot zip for an ecosystem — tolerant of naming/casing:
    Maven.zip, maven.zip, Maven-all.zip, Maven_all.zip, Maven/all.zip …"""
    base = _offline_dir()
    if not base or not os.path.isdir(base):
        return ""
    eco = ecosystem.lower()
    accepted = {f"{eco}.zip", f"{eco}-all.zip", f"{eco}_all.zip", f"{eco}all.zip"}
    try:
        for entry in os.listdir(base):
            p = os.path.join(base, entry)
            if os.path.isfile(p) and entry.lower() in accepted:
                return p
            # ecosystem subfolder containing all.zip (any casing)
            if os.path.isdir(p) and entry.lower() == eco:
                for sub in os.listdir(p):
                    if sub.lower().endswith(".zip"):
                        return os.path.join(p, sub)
    except OSError:
        return ""
    return ""


def diagnose(ecosystems: set[str]) -> str:
    """Human-readable reason the offline snapshot is (un)usable — surfaced in the
    scan error so a misconfiguration is visible in the UI, not just in logs."""
    base = _offline_dir()
    if not base:
        return "offline snapshot disabled (OSV_OFFLINE_DIR not set)"
    if not os.path.isdir(base):
        return f"offline snapshot dir does not exist: {base}"
    found = {e: os.path.basename(_zip_path(e)) for e in ecosystems if _zip_path(e)}
    if found:
        return "offline snapshot available: " + ", ".join(f"{e}→{f}" for e, f in found.items())
    try:
        listing = ", ".join(sorted(os.listdir(base))[:8]) or "(empty)"
    except OSError as exc:
        listing = f"(unreadable: {exc})"
    ecos = "/".join(sorted(ecosystems))
    return (f"no snapshot for {ecos} in {base} — found [{listing}]. "
            f"Name the file e.g. Maven.zip or NuGet.zip (or Maven/all.zip); "
            f"a bare 'all.zip' is ambiguous and ignored.")


def available(ecosystems: set[str] | None = None) -> bool:
    """True when an offline snapshot exists for at least one requested ecosystem."""
    if not _offline_dir():
        return False
    ecos = ecosystems or {"Maven", "NuGet", "PyPI", "npm"}
    return any(_zip_path(e) for e in ecos)


def snapshot_age_days(ecosystem: str) -> int:
    p = _zip_path(ecosystem)
    if not p:
        return -1
    import time
    return int((time.time() - os.path.getmtime(p)) / 86400)


# ── tolerant version comparison (Maven/NuGet versions are not strict semver) ──

def _vkey(v: str):
    parts = re.split(r"[.\-_+]", (v or "").strip())
    key = []
    for p in parts:
        key.append((0, int(p)) if p.isdigit() else (1, p.lower()))
    return key


def _version_affected(version: str, affected: dict) -> bool:
    # 1. Explicit enumeration (OSV provides this for Maven/NuGet) — exact match.
    versions = affected.get("versions") or []
    if versions:
        return version in versions
    # 2. Range evaluation: introduced ≤ v < fixed (last event wins per range).
    for rng in affected.get("ranges") or []:
        if rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
            continue
        introduced, fixed, last_affected = None, None, None
        for ev in rng.get("events") or []:
            if "introduced" in ev:    introduced = ev["introduced"]
            if "fixed" in ev:         fixed = ev["fixed"]
            if "last_affected" in ev: last_affected = ev["last_affected"]
        try:
            vk = _vkey(version)
            if introduced not in (None, "0") and vk < _vkey(introduced):
                continue
            if fixed and vk >= _vkey(fixed):
                continue
            if last_affected and vk > _vkey(last_affected):
                continue
            return True
        except TypeError:
            continue                       # incomparable version tokens — skip range
    return False


def _index_for(ecosystem: str) -> dict:
    """Package-name index for an ecosystem's snapshot, built once per zip mtime."""
    path = _zip_path(ecosystem)
    if not path:
        return {}
    mtime = os.path.getmtime(path)
    cached = _INDEX.get(ecosystem)
    if cached and cached["mtime"] == mtime:
        return cached["pkgs"]

    log.info("Indexing offline OSV snapshot %s (first use — one-off per file)…", path)
    pkgs: dict[str, list[dict]] = {}
    count = 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith(".json"):
                continue
            try:
                adv = json.loads(zf.read(name))
            except Exception:
                continue
            slim_affected = []
            for aff in adv.get("affected") or []:
                pkg = (aff.get("package") or {})
                if (pkg.get("ecosystem") or "") != ecosystem:
                    continue
                pname = (pkg.get("name") or "").lower()
                if not pname:
                    continue
                slim_affected.append((pname, {"versions": aff.get("versions"),
                                              "ranges": aff.get("ranges")}))
            if not slim_affected:
                continue
            slim = {
                "id":       adv.get("id", ""),
                "summary":  (adv.get("summary") or adv.get("details") or "")[:200],
                "severity": _severity_from_vuln(adv),
                "aliases":  [a for a in (adv.get("aliases") or []) if a.startswith("CVE-")],
            }
            for pname, aff in slim_affected:
                pkgs.setdefault(pname, []).append({**slim, "affected": aff})
            count += 1
    _INDEX[ecosystem] = {"mtime": mtime, "pkgs": pkgs}
    log.info("Offline OSV snapshot for %s indexed: %d advisories, %d packages.",
             ecosystem, count, len(pkgs))
    return pkgs


def query_versioned_offline(items: list[tuple[str, str, str]],
                            timeout_s: int = 0, raise_on_error: bool = False,
                            ) -> dict[tuple[str, str], list[OsvVuln]]:
    """Same shape as osv_client.query_versioned, served from the local snapshot."""
    out: dict[tuple[str, str], list[OsvVuln]] = {}
    for name, eco, version in items:
        if not (name and version):
            continue
        idx = _index_for(eco)
        vulns = []
        for adv in idx.get(name.lower(), []):
            if _version_affected(version, adv["affected"]):
                vulns.append(OsvVuln(package=name, vuln_id=adv["id"], summary=adv["summary"],
                                     severity=adv["severity"], aliases=adv["aliases"]))
        if vulns:
            out[(name, version)] = vulns
    return out
