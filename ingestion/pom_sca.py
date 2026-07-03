"""
ingestion/pom_sca.py
---------------------
Software Composition Analysis (SCA) for Maven `pom.xml` — Path A.

Maven has no lockfile by default, so this scans the DIRECT dependencies declared
in pom.xml (resolving same-file ${properties} and <dependencyManagement> versions)
and matches each concrete group:artifact@version against OSV.

Works on any branch with no CI and no lockfile. It does NOT see transitive deps —
findings are labelled depth="direct" so confidence is explicit. Versions that
can't be resolved from the pom alone (managed by a parent POM / external BOM) are
reported as "unresolved" rather than silently dropped.
"""
from __future__ import annotations
import logging
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

# Maven repo for resolving parent/BOM POMs (Spring Boot etc. manage every version
# in a parent). Point MAVEN_REPO_URL at your internal Artifactory/Nexus; set
# MAVEN_REPO_AUTH (e.g. "Bearer xxx" / "Basic xxx") if it needs credentials.
_DEFAULT_MAVEN_REPO = "https://repo1.maven.org/maven2"


def test_connection(repo: str = "", auth: str = "", timeout_s: int = 10) -> dict:
    """Probe the configured Maven repo (Artifactory/Nexus) — verifies the URL is
    reachable and the auth + TLS work, BEFORE a real scan needs it. Returns
    {ok, status, message}. Mirrors how _fetch_pom connects (same auth header +
    TLS context honouring OSV_CA_BUNDLE / OSV_VERIFY_SSL)."""
    repo = (repo or os.getenv("MAVEN_REPO_URL", "") or _DEFAULT_MAVEN_REPO).rstrip("/")
    auth = (auth if auth else os.getenv("MAVEN_REPO_AUTH", "")).strip()
    from ingestion.osv_client import _ssl_context
    headers = {"Authorization": auth} if auth else {}
    req = urllib.request.Request(repo + "/", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context()) as r:
            return {"ok": True, "status": r.status,
                    "message": f"Connected — HTTP {r.status} from {repo}"}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "status": exc.code,
                    "message": f"Reachable, but authentication failed (HTTP {exc.code}) — check the token/credentials."}
        if exc.code in (404, 405):
            return {"ok": True, "status": exc.code,
                    "message": f"Host reachable (HTTP {exc.code} on base path) — auth/TLS OK; verify the exact repo path."}
        return {"ok": False, "status": exc.code, "message": f"HTTP {exc.code} from {repo}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "status": 0,
                "message": (f"Could not reach {repo}: {exc}. Check the URL (incl. https://), "
                            f"network egress, and TLS/CA (OSV_CA_BUNDLE / OSV_VERIFY_SSL).")}


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]   # strip XML namespace


def _find(el, name):
    for c in el:
        if _localname(c.tag) == name:
            return c
    return None


def _findall_deep(root, name):
    return [e for e in root.iter() if _localname(e.tag) == name]


def _text(el, name, default="") -> str:
    c = _find(el, name)
    return (c.text or "").strip() if c is not None and c.text else default


def _collect_properties(root) -> dict[str, str]:
    props: dict[str, str] = {}
    for pblock in _findall_deep(root, "properties"):
        for c in pblock:
            props[_localname(c.tag)] = (c.text or "").strip()
    # common built-ins
    version = _text(root, "version") or ""
    parent = _find(root, "parent")
    if not version and parent is not None:
        version = _text(parent, "version")
    if version:
        props.setdefault("project.version", version)
        props.setdefault("version", version)
    return props


_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve(value: str, props: dict[str, str], depth: int = 0) -> str:
    if not value or depth > 5:
        return value
    def repl(m):
        return props.get(m.group(1), m.group(0))
    out = _PROP_RE.sub(repl, value)
    return _resolve(out, props, depth + 1) if (out != value and "${" in out) else out


def parse_pom_dependencies(pom_text: str) -> list[dict]:
    """Return [{group, artifact, version, scope, resolved}] for direct deps."""
    try:
        root = ET.fromstring(pom_text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid pom.xml: {exc}")

    props = _collect_properties(root)

    # Versions declared in <dependencyManagement> (used when a direct dep omits version)
    managed: dict[str, str] = {}
    for dm in _findall_deep(root, "dependencyManagement"):
        for deps in (d for d in dm.iter() if _localname(d.tag) == "dependency"):
            g, a = _text(deps, "groupId"), _text(deps, "artifactId")
            v = _resolve(_text(deps, "version"), props)
            if g and a and v:
                managed[f"{g}:{a}"] = v

    # Direct deps = <dependency> NOT inside dependencyManagement / plugins
    managed_deps = set()
    for dm in _findall_deep(root, "dependencyManagement"):
        for d in (x for x in dm.iter() if _localname(x.tag) == "dependency"):
            managed_deps.add(id(d))

    out: list[dict] = []
    seen = set()
    for dep in _findall_deep(root, "dependency"):
        if id(dep) in managed_deps:
            continue
        g, a = _text(dep, "groupId"), _text(dep, "artifactId")
        if not g or not a:
            continue
        v = _resolve(_text(dep, "version"), props)
        if not v:
            v = managed.get(f"{g}:{a}", "")
        scope = _text(dep, "scope") or "compile"
        key = f"{g}:{a}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "group": g, "artifact": a, "version": v, "scope": scope,
            "resolved": bool(v and "${" not in v),
        })
    return out


# ── Parent / BOM resolution (Spring Boot et al. manage versions in a parent) ──

def _pom_url(repo: str, group: str, artifact: str, version: str) -> str:
    return f"{repo.rstrip('/')}/{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"


def _fetch_pom(repo, group, artifact, version, auth, timeout, cache) -> str | None:
    if not (group and artifact and version) or "${" in (version or ""):
        return None
    url = _pom_url(repo, group, artifact, version)
    if url in cache:
        return cache[url]
    cache[url] = None
    try:
        from ingestion.osv_client import _ssl_context
        headers = {"Authorization": auth} if auth else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            cache[url] = r.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Maven POM fetch failed (%s): %s", url, exc)
    return cache[url]


def _collect_managed(text, repo, auth, timeout, props, managed_raw, cache, depth=0) -> None:
    """Recurse the parent chain + <scope>import</scope> BOMs, accumulating
    properties and <dependencyManagement> versions (first-seen wins → child
    overrides parent). Versions stay raw (${…}) and are resolved afterwards."""
    if not text or depth > 8:
        return
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return
    for pblock in _findall_deep(root, "properties"):
        for c in pblock:
            props.setdefault(_localname(c.tag), (c.text or "").strip())
    for dm in _findall_deep(root, "dependencyManagement"):
        for dep in (d for d in dm.iter() if _localname(d.tag) == "dependency"):
            g, a, v = _text(dep, "groupId"), _text(dep, "artifactId"), _text(dep, "version")
            scope, typ = _text(dep, "scope"), _text(dep, "type")
            if scope == "import" and typ == "pom":
                bom = _fetch_pom(repo, g, a, _resolve(v, props), auth, timeout, cache)
                _collect_managed(bom, repo, auth, timeout, props, managed_raw, cache, depth + 1)
            elif g and a and v:
                managed_raw.setdefault(f"{g}:{a}", v)
    parent = _find(root, "parent")
    if parent is not None:
        ptext = _fetch_pom(repo, _text(parent, "groupId"), _text(parent, "artifactId"),
                           _resolve(_text(parent, "version"), props), auth, timeout, cache)
        _collect_managed(ptext, repo, auth, timeout, props, managed_raw, cache, depth + 1)


def resolve_managed_versions(pom_text, repo, auth="", timeout=15) -> dict[str, str]:
    """{group:artifact -> concrete version} for everything the parent/BOM chain
    manages (so a Spring Boot pom that declares no versions can still be scanned)."""
    props, managed_raw, cache = {}, {}, {}
    _collect_managed(pom_text, repo, auth, timeout, props, managed_raw, cache)
    out = {}
    for k, v in managed_raw.items():
        rv = _resolve(v, props)
        if rv and "${" not in rv:
            out[k] = rv
    return out


def _deps_of_pom(text: str, managed: dict[str, str]) -> list[tuple[str, str, str]]:
    """Direct compile/runtime dependencies declared in a POM (skipping test/
    provided/optional and dependencyManagement). Versions are resolved from the
    POM's own properties, then the BOM-managed map (so omitted versions resolve)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    props: dict[str, str] = {}
    for pblock in _findall_deep(root, "properties"):
        for c in pblock:
            props.setdefault(_localname(c.tag), (c.text or "").strip())
    mgmt_ids = {id(d) for dm in _findall_deep(root, "dependencyManagement")
                for d in dm.iter() if _localname(d.tag) == "dependency"}
    out: list[tuple[str, str, str]] = []
    for dep in _findall_deep(root, "dependency"):
        if id(dep) in mgmt_ids:
            continue
        if (_text(dep, "scope") or "compile") in ("test", "provided", "system"):
            continue
        if _text(dep, "optional") == "true":
            continue
        g, a = _text(dep, "groupId"), _text(dep, "artifactId")
        if not g or not a:
            continue
        v = _resolve(_text(dep, "version"), props) or managed.get(f"{g}:{a}", "")
        if v and "${" not in v:
            out.append((g, a, v))
    return out


def _transitive_closure(direct, managed, repo, auth, timeout, cap: int = 800) -> dict[str, str]:
    """BFS the ACTUAL transitive deps the declared dependencies pull (fetching each
    POM and resolving versions via the BOM-managed map) — so we scan what the app
    really uses, not the whole BOM. Returns {group:artifact -> version}."""
    cache: dict[str, str | None] = {}
    closure: dict[str, str] = {}
    queue = [(d["group"], d["artifact"], d["version"]) for d in direct]
    while queue and len(closure) < cap:
        g, a, v = queue.pop()
        key = f"{g}:{a}"
        if key in closure:
            continue
        closure[key] = v
        pom = _fetch_pom(repo, g, a, v, auth, timeout, cache)
        if not pom:
            continue
        for cg, ca, cv in _deps_of_pom(pom, managed):
            if f"{cg}:{ca}" not in closure:
                queue.append((cg, ca, cv))
    return closure


def _query_one_source(src, items, timeout_s, xray_url="", xray_auth=""):
    if src == "xray":
        from ingestion.xray_client import query_versioned_xray
        return query_versioned_xray(items, timeout_s=timeout_s, raise_on_error=True,
                                    base_url=xray_url, auth=xray_auth)
    from ingestion.osv_client import query_versioned
    return query_versioned(items, timeout_s=timeout_s, raise_on_error=True)


def _vuln_lookup(items, timeout_s, source="", xray_url="", xray_auth=""):
    """Query the SELECTED vulnerability source ('osv' public / 'xray' in-house).
    Layer-2 fallback: when the primary is down (after retries), try the
    VULN_FALLBACK_SOURCE if configured (opt-in, default none). Returns
    (source_used, hits, note) — note is set when a fallback served the result.
    Raises OsvUnavailable when every configured source failed."""
    from config.settings import get_settings
    from ingestion.osv_client import OsvUnavailable
    cfg = get_settings()
    src = (source or getattr(cfg, "vuln_source", "osv") or "osv").strip().lower()
    def _offline_last_resort(err_msg):
        """Final fallback: pre-downloaded OSV snapshot zips (OSV_OFFLINE_DIR)."""
        from ingestion import osv_offline
        ecos = {e for (_n, e, _v) in items}
        if not osv_offline.available(ecos):
            return None
        try:
            hits = osv_offline.query_versioned_offline(items)
        except Exception as off_exc:
            log.warning("Offline OSV snapshot lookup failed: %s", off_exc)
            return None
        age = max((osv_offline.snapshot_age_days(e) for e in ecos), default=-1)
        note = (f"Live vulnerability sources unreachable ({err_msg}) — results served from "
                f"the OFFLINE OSV snapshot"
                + (f" downloaded {age} day(s) ago" if age >= 0 else "")
                + ". CVEs published since the snapshot are not reflected.")
        return "osv-offline", hits, note

    try:
        return src, _query_one_source(src, items, timeout_s, xray_url, xray_auth), ""
    except OsvUnavailable as primary_exc:
        fb = (getattr(cfg, "vuln_fallback_source", "none") or "none").strip().lower()
        if fb in ("none", "", src):
            off = _offline_last_resort(str(primary_exc))
            if off:
                return off
            raise
        log.warning("Primary vuln source '%s' unavailable (%s) — falling back to '%s'",
                    src, primary_exc, fb)
        try:
            hits = _query_one_source(fb, items, timeout_s, xray_url, xray_auth)
        except OsvUnavailable as fb_exc:
            off = _offline_last_resort(f"{src}: {primary_exc}; {fb}: {fb_exc}")
            if off:
                return off
            raise OsvUnavailable(f"{src}: {primary_exc}; fallback {fb}: {fb_exc}") from fb_exc
        note = (f"Primary vulnerability source '{src}' was unreachable — results served "
                f"from fallback '{fb}'. Coverage may differ between sources.")
        return fb, hits, note


def scan_pom(pom_text: str, timeout_s: int = 15, resolve_parents: bool = True,
             repo: str | None = None, auth: str | None = None,
             vuln_source: str = "", xray_url: str = "", xray_auth: str = "") -> dict:
    """Parse pom.xml, resolve parent/BOM-managed versions (Spring Boot etc.), query
    the selected vulnerability source (OSV or Xray) for the resolved deps, and
    return a structured SCA result. `repo`/`auth` (e.g. from the UI) override the
    MAVEN_REPO_URL / MAVEN_REPO_AUTH env."""
    deps = parse_pom_dependencies(pom_text)
    direct_keys = {f"{d['group']}:{d['artifact']}" for d in deps}

    # Resolve versions managed by a parent POM / imported BOM via the Maven repo.
    repo = (repo or os.getenv("MAVEN_REPO_URL", "") or _DEFAULT_MAVEN_REPO)
    auth = (auth if auth is not None else os.getenv("MAVEN_REPO_AUTH", "")).strip()
    deep = os.getenv("MAVEN_SCAN_TRANSITIVE", "true").strip().lower() not in ("false", "0", "no")
    managed_ext: dict[str, str] = {}
    needs_resolve = ("<parent>" in pom_text) or any(not d["resolved"] for d in deps)
    if resolve_parents and needs_resolve:
        try:
            managed_ext = resolve_managed_versions(pom_text, repo, auth, timeout_s)
        except Exception as exc:
            log.warning("Parent POM resolution failed: %s", exc)
    # Fill the versions our pom omitted from the parent/BOM chain.
    for d in deps:
        if not d["resolved"] and managed_ext.get(f"{d['group']}:{d['artifact']}"):
            d["version"], d["resolved"] = managed_ext[f"{d['group']}:{d['artifact']}"], True

    resolved = [d for d in deps if d["resolved"]]
    unresolved = [d for d in deps if not d["resolved"]]
    # Transitive deps the declared dependencies ACTUALLY pull (real closure, not
    # the whole BOM) — that's where most Spring Boot CVEs live (spring-core,
    # snakeyaml, tomcat…). Versions resolved via the BOM-managed map.
    transitive = []
    if resolve_parents and deep and resolved:
        try:
            closure = _transitive_closure(resolved, managed_ext, repo, auth, timeout_s)
            for k, v in closure.items():
                if k not in direct_keys:
                    g, a = k.split(":", 1)
                    transitive.append({"group": g, "artifact": a, "version": v, "scope": "transitive", "resolved": True})
        except Exception as exc:
            log.warning("Transitive closure failed: %s", exc)

    scan_set = resolved + transitive
    from ingestion.osv_client import OsvUnavailable
    from ingestion import sca_cache
    items = [(f"{d['group']}:{d['artifact']}", "Maven", d["version"]) for d in scan_set]
    _ck = sca_cache.cache_key(pom_text, "Maven", vuln_source)
    osv_error, used_source, fb_note = "", "osv", ""
    try:
        used_source, hits, fb_note = _vuln_lookup(items, timeout_s, vuln_source, xray_url, xray_auth) if items else ("osv", {}, "")
    except OsvUnavailable as exc:
        # Layer-3 fallback: serve the LAST SUCCESSFUL scan of this exact manifest,
        # clearly labelled stale, instead of an empty error.
        cached, ts = sca_cache.load(_ck)
        if cached:
            return {**cached, "stale": True, "stale_from": ts, "osv_error": None,
                    "stale_note": (f"Vulnerability database unreachable — showing the last successful "
                                   f"scan from {sca_cache.age_label(ts)}. New CVEs since then are NOT "
                                   f"reflected. ({exc})")}
        hits, osv_error = {}, str(exc)

    vulns: list[dict] = []
    for d in scan_set:
        name = f"{d['group']}:{d['artifact']}"
        is_direct = name in direct_keys
        for v in hits.get((name, d["version"]), []):
            cve = next((a for a in v.aliases if a.startswith("CVE-")), v.vuln_id)
            vulns.append({
                "package": name, "version": d["version"], "scope": d["scope"],
                "vuln_id": v.vuln_id, "cve": cve, "severity": v.severity or "UNKNOWN",
                "summary": v.summary, "depth": "direct" if is_direct else "transitive",
            })

    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    # direct findings first, then by severity
    vulns.sort(key=lambda x: (x["depth"] != "direct", sev_rank.get(x["severity"], 4)))
    n_direct = sum(1 for v in vulns if v["depth"] == "direct")
    n_trans = len(vulns) - n_direct
    if osv_error:
        summary = ("Could not reach the OSV vulnerability database — scan incomplete. "
                   "Check network access, or set OSV_CA_BUNDLE / OSV_VERIFY_SSL / OSV_BASE_URL.")
    else:
        summary = (f"{len(vulns)} vulnerabilit{'y' if len(vulns)==1 else 'ies'} "
                   f"({n_direct} in direct deps"
                   + (f", {n_trans} in BOM-managed/transitive" if n_trans else "")
                   + f") across {len(scan_set)} dependenc{'y' if len(scan_set)==1 else 'ies'} scanned"
                   + (f"; {len(unresolved)} still unresolved" if unresolved else ""))
    result = {
        "ecosystem": "Maven",
        "depth": "direct+transitive" if transitive else "direct",
        "dependencies_scanned": len(scan_set),
        "direct_scanned": len(resolved),
        "unresolved": [f"{d['group']}:{d['artifact']}" for d in unresolved],
        "vulnerabilities": vulns,
        "osv_error": osv_error or None,
        "vuln_source": used_source,
        "fallback_note": fb_note or None,
        "summary": summary,
    }
    if not osv_error:
        sca_cache.save(_ck, result)   # last-known-good for Layer-3 fallback
    return result
