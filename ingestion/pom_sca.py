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
import re
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


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


def scan_pom(pom_text: str, timeout_s: int = 15) -> dict:
    """Parse pom.xml, query OSV for resolved deps, return a structured SCA result."""
    deps = parse_pom_dependencies(pom_text)
    resolved = [d for d in deps if d["resolved"]]
    unresolved = [d for d in deps if not d["resolved"]]

    from ingestion.osv_client import query_versioned
    items = [(f"{d['group']}:{d['artifact']}", "Maven", d["version"]) for d in resolved]
    hits = query_versioned(items, timeout_s=timeout_s) if items else {}

    vulns: list[dict] = []
    for d in resolved:
        name = f"{d['group']}:{d['artifact']}"
        for v in hits.get((name, d["version"]), []):
            cve = next((a for a in v.aliases if a.startswith("CVE-")), v.vuln_id)
            vulns.append({
                "package": name, "version": d["version"], "scope": d["scope"],
                "vuln_id": v.vuln_id, "cve": cve, "severity": v.severity or "UNKNOWN",
                "summary": v.summary, "depth": "direct",
            })

    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    vulns.sort(key=lambda x: sev_rank.get(x["severity"], 4))
    return {
        "ecosystem": "Maven",
        "depth": "direct",
        "dependencies_scanned": len(resolved),
        "unresolved": [f"{d['group']}:{d['artifact']}" for d in unresolved],
        "vulnerabilities": vulns,
        "summary": (f"{len(vulns)} vulnerabilit{'y' if len(vulns)==1 else 'ies'} in "
                    f"{len(resolved)} direct dependenc{'y' if len(resolved)==1 else 'ies'}"
                    + (f"; {len(unresolved)} version(s) unresolved (managed externally)" if unresolved else "")),
    }
