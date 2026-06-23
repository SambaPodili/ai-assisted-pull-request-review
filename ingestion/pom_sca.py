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


def scan_pom(pom_text: str, timeout_s: int = 15, resolve_parents: bool = True,
             repo: str | None = None, auth: str | None = None) -> dict:
    """Parse pom.xml, resolve parent/BOM-managed versions (Spring Boot etc.), query
    OSV for the resolved deps, and return a structured SCA result. `repo`/`auth`
    (e.g. from the UI) override the MAVEN_REPO_URL / MAVEN_REPO_AUTH env."""
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
    from ingestion.osv_client import query_versioned, OsvUnavailable
    items = [(f"{d['group']}:{d['artifact']}", "Maven", d["version"]) for d in scan_set]
    osv_error = ""
    try:
        hits = query_versioned(items, timeout_s=timeout_s, raise_on_error=True) if items else {}
    except OsvUnavailable as exc:
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
    return {
        "ecosystem": "Maven",
        "depth": "direct+transitive" if transitive else "direct",
        "dependencies_scanned": len(scan_set),
        "direct_scanned": len(resolved),
        "unresolved": [f"{d['group']}:{d['artifact']}" for d in unresolved],
        "vulnerabilities": vulns,
        "osv_error": osv_error or None,
        "summary": summary,
    }
