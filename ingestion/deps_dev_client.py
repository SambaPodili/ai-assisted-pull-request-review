"""
ingestion/deps_dev_client.py
------------------------------
Query deps.dev — Google/OpenSSF's Open Source Insights API — for the REAL,
authoritative SPDX license of a package/version.

Free, no API key. Docs: https://docs.deps.dev/api/v3/

license_compliance_agent.py previously classified license risk purely from
KEYWORDS IN THE PACKAGE NAME (e.g. "gpl" appearing in the string "my-gpl-fork"),
which both misses real copyleft packages whose name doesn't say so (e.g.
mysql-connector-java is GPL-licensed) and flags ordinary MIT/Apache packages
that just aren't on a small hardcoded safe-list as "unknown, needs review".
This module supplies the real license so that heuristic is a fallback, not
the only source of truth — the same role OSV.dev plays for CVEs in
ingestion/osv_client.py.
"""
from __future__ import annotations
import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_BASE = (os.getenv("DEPS_DEV_BASE_URL", "") or "https://api.deps.dev").rstrip("/")

def _ssl_context() -> ssl.SSLContext:
    verify, ca = True, ""
    try:
        from config.settings import get_settings
        _s = get_settings()
        verify = bool(getattr(_s, "deps_dev_verify_ssl", True))
        ca = (getattr(_s, "deps_dev_ca_bundle", "") or "").strip()
    except Exception:
        pass
    if os.getenv("DEPS_DEV_VERIFY_SSL", "").strip().lower() in ("false", "0", "no"):
        verify = False
    ca = (os.getenv("DEPS_DEV_CA_BUNDLE", "").strip() or ca)

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


def _urlopen(req, timeout_s):
    proxy = ""
    try:
        from config.settings import get_settings
        proxy = (getattr(get_settings(), "deps_dev_proxy_url", "") or "").strip()
    except Exception:
        pass
    proxy = os.getenv("DEPS_DEV_PROXY_URL", "").strip() or proxy
    ctx = _ssl_context()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        return opener.open(req, timeout=timeout_s)
    return urllib.request.urlopen(req, timeout=timeout_s, context=ctx)


def _get_json(url: str, timeout_s: int) -> dict:
    req = urllib.request.Request(url)
    with _urlopen(req, timeout_s) as resp:
        return json.loads(resp.read())


def lookup_license(name: str, system: str, version: str = "", timeout_s: int = 10) -> str:
    """Return the package's SPDX license expression (e.g. "MIT", "GPL-3.0-only",
    "Apache-2.0 OR MIT"), or "" when unknown/unreachable/not the default
    version. "" must be treated as "couldn't determine" by callers, never as
    "confirmed no license" — fall back to the existing heuristic instead of
    treating it as safe.
    """
    if not name or not system:
        return ""
    encoded = urllib.parse.quote(name, safe="")
    try:
        if not version:
            listing = _get_json(f"{_BASE}/v3/systems/{system}/packages/{encoded}", timeout_s)
            for v in listing.get("versions", []) or []:
                if v.get("isDefault"):
                    version = (v.get("versionKey") or {}).get("version", "")
                    break
            if not version:
                return ""
        enc_version = urllib.parse.quote(version, safe="")
        data = _get_json(
            f"{_BASE}/v3/systems/{system}/packages/{encoded}/versions/{enc_version}", timeout_s
        )
        licenses = data.get("licenses") or []
        return licenses[0] if licenses else ""
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        log.debug("deps.dev lookup failed for %s/%s@%s: %s", system, name, version, exc)
        return ""


def lookup_licenses_batch(
    items: list[tuple[str, str, str]],   # [(name, system, version), ...] — version may be ""
    timeout_s: int = 10,
    max_workers: int = 8,
) -> dict[tuple[str, str, str], str]:
    """Parallel lookup_license over multiple packages. deps.dev's v3 API has
    no batch endpoint, so this fans out individual requests concurrently —
    manifest diffs are typically a handful of changed packages, not hundreds.
    Returns {(name, system, version): spdx_license} for keys with a result."""
    items = [(n, s, v) for (n, s, v) in dict.fromkeys(items) if n and s]
    if not items:
        return {}
    from concurrent.futures import ThreadPoolExecutor

    def _one(item):
        n, s, v = item
        return item, lookup_license(n, s, v, timeout_s)

    out: dict[tuple[str, str, str], str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for item, lic in pool.map(_one, items):
            if lic:
                out[item] = lic
    return out
