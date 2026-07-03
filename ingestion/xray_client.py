"""
ingestion/xray_client.py
------------------------
JFrog Xray as an ALTERNATIVE vulnerability source to OSV — for air-gapped
deployments where Artifactory+Xray is the in-house SCA authority and
api.osv.dev is unreachable.

Uses Xray's component-summary API:
    POST {XRAY_BASE_URL}/api/v1/summary/component
    {"component_details": [{"component_id": "gav://group:artifact:version"}]}

Returns the same shape as osv_client.query_versioned so pom_sca / nuget_sca can
swap sources transparently: {(name, version): [OsvVuln, ...]}.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ingestion.osv_client import OsvUnavailable, OsvVuln, _open_with_retries, _ssl_context

log = logging.getLogger(__name__)

# OSV ecosystem name → Xray component-id prefix
_ECO_PREFIX = {"Maven": "gav", "NuGet": "nuget", "PyPI": "pypi", "npm": "npm", "Go": "go"}

_SEV = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MEDIUM": "MEDIUM", "LOW": "LOW",
        "INFORMATION": "LOW", "UNKNOWN": ""}


def _component_id(name: str, ecosystem: str, version: str) -> str:
    return f"{_ECO_PREFIX.get(ecosystem, 'generic')}://{name}:{version}"


def _severity(issue: dict) -> str:
    sev = _SEV.get(str(issue.get("severity", "")).upper(), "")
    if sev:
        return sev
    # Fall back to the CVSS v3 score Xray attaches to each CVE ("9.8/CVSS:3.1/…")
    for c in issue.get("cves", []) or []:
        raw = str(c.get("cvss_v3") or c.get("cvss_v2") or "").split("/")[0]
        try:
            score = float(raw)
        except ValueError:
            continue
        return ("CRITICAL" if score >= 9 else "HIGH" if score >= 7
                else "MEDIUM" if score >= 4 else "LOW")
    return ""


def query_versioned_xray(
    items: list[tuple[str, str, str]],      # [(name, ecosystem, version), ...]
    timeout_s: int = 20,
    raise_on_error: bool = False,
    base_url: str = "",
    auth: str = "",
) -> dict[tuple[str, str], list[OsvVuln]]:
    """Look up known vulnerabilities for exact package versions in JFrog Xray.
    base_url/auth fall back to XRAY_BASE_URL / XRAY_AUTH settings. Raises
    OsvUnavailable (shared with the OSV client) on network/auth failure when
    raise_on_error is set, so callers surface 'couldn't reach the vulnerability
    database' instead of 'no vulnerabilities'."""
    from config.settings import get_settings
    cfg = get_settings()
    base = (base_url or getattr(cfg, "xray_base_url", "") or "").rstrip("/")
    tok  = (auth or getattr(cfg, "xray_auth", "") or "").strip()
    if not base:
        msg = "Xray selected but XRAY_BASE_URL is not configured."
        if raise_on_error:
            raise OsvUnavailable(msg)
        log.warning(msg)
        return {}

    items = [(n, e, v) for (n, e, v) in items if n and v]
    if not items:
        return {}
    body = {"component_details": [{"component_id": _component_id(n, e, v)} for n, e, v in items]}
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = tok if tok.lower().startswith(("bearer ", "basic ")) else f"Bearer {tok}"
    req = urllib.request.Request(f"{base}/api/v1/summary/component",
                                 data=json.dumps(body).encode(), headers=headers, method="POST")
    def _open(r, t):
        return urllib.request.urlopen(r, timeout=t, context=_ssl_context())
    try:
        with _open_with_retries(_open, req, timeout_s, label="Xray") as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning("Xray component-summary query failed (after retries): %s", exc)
        if raise_on_error:
            raise OsvUnavailable(f"Xray: {exc}") from exc
        return {}

    # Index results by component_id → map back to (name, version)
    by_cid: dict[str, dict] = {}
    for art in data.get("artifacts", []) or []:
        cid = ((art.get("general") or {}).get("component_id") or "").strip()
        if cid:
            by_cid[cid] = art

    out: dict[tuple[str, str], list[OsvVuln]] = {}
    for name, eco, version in items:
        art = by_cid.get(_component_id(name, eco, version))
        if not art:
            continue
        vulns = []
        for issue in art.get("issues", []) or []:
            if str(issue.get("issue_type", "security")).lower() not in ("security", ""):
                continue                      # skip license/operational issues
            cves = [c.get("cve") for c in (issue.get("cves") or []) if c.get("cve")]
            vulns.append(OsvVuln(
                package=name,
                vuln_id=issue.get("issue_id") or (cves[0] if cves else "XRAY"),
                summary=(issue.get("summary") or issue.get("description") or "")[:200],
                severity=_severity(issue),
                aliases=cves,
            ))
        if vulns:
            out[(name, version)] = vulns
    return out


def test_connection(base_url: str = "", auth: str = "", timeout_s: int = 10) -> dict:
    """Probe Xray (system/ping) — verifies URL, auth and TLS before a real scan."""
    from config.settings import get_settings
    cfg = get_settings()
    base = (base_url or getattr(cfg, "xray_base_url", "") or "").rstrip("/")
    tok  = (auth or getattr(cfg, "xray_auth", "") or "").strip()
    if not base:
        return {"ok": False, "status": 0, "message": "No Xray URL configured — set it here or XRAY_BASE_URL in .env."}
    headers = {}
    if tok:
        headers["Authorization"] = tok if tok.lower().startswith(("bearer ", "basic ")) else f"Bearer {tok}"
    req = urllib.request.Request(f"{base}/api/v1/system/ping", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context()) as r:
            return {"ok": True, "status": r.status, "message": f"Connected — Xray responded HTTP {r.status} at {base}"}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "status": exc.code,
                    "message": f"Xray reachable but authentication failed (HTTP {exc.code}) — check the token."}
        return {"ok": False, "status": exc.code, "message": f"HTTP {exc.code} from {base}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "status": 0,
                "message": f"Could not reach {base}: {exc}. Check the URL, network and TLS/CA (OSV_CA_BUNDLE applies)."}
