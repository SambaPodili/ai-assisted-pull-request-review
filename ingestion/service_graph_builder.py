"""
ingestion/service_graph_builder.py
------------------------------------
Auto-construct the service dependency graph that DependencyMappingAgent uses
for blast-radius calculation.

Three data sources (tried in priority order):

  1. SERVICE_MAP_PATH — path to a JSON file you maintain manually or export
     from Backstage / ServiceNow / Apicurio.
     Format: {"service-a": ["lib-x", "lib-y"], "service-b": ["lib-x"]}

  2. REPOS_ROOT — path to a local directory containing clones of all your
     microservice repos.  We scan each repo's manifests, extract dependencies,
     and build edges automatically.

  3. Inline dict — call build_from_dict() programmatically (useful in tests
     or when graph data comes from another system).

The resulting NetworkX DiGraph has:
  • Nodes: services and libraries, with attrs {type, team, version}
  • Edges: dependency → service  (A → B means "B depends on A")
  • nx.descendants(G, pkg) gives all services transitively impacted by pkg.

Usage:
    from ingestion.service_graph_builder import load_service_graph
    graph = load_service_graph()          # reads settings automatically
    orchestrator = Orchestrator(..., graph_store=graph)
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import networkx as nx
    _HAS_NX = True
except ImportError:
    _HAS_NX = False
    log.info("networkx not installed — service graph disabled (pip install networkx)")


# ── Manifest extractors (reuse patterns from dependency_agent.py) ─────────────

_MANIFEST_NAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts",
    "package.json", "requirements.txt", "Pipfile",
    "go.mod", "Cargo.toml", "Gemfile", "composer.json",
    "pubspec.yaml", "mix.exs", "build.sbt",
}

def _extract_deps_from_manifest(path: Path) -> list[str]:
    """Best-effort extraction of dependency names from a manifest file."""
    deps: list[str] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return deps

    name = path.name.lower()

    if name == "package.json":
        try:
            data = json.loads(text)
            for section in ("dependencies", "devDependencies", "peerDependencies"):
                deps.extend(data.get(section, {}).keys())
        except json.JSONDecodeError:
            pass

    elif name in ("requirements.txt", "pipfile"):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                m = re.match(r'^([A-Za-z0-9_.-]+)', line)
                if m:
                    deps.append(m.group(1))

    elif name == "go.mod":
        for line in text.splitlines():
            m = re.match(r'^\s+([^\s]+)\s+v', line)
            if m:
                # Keep only the module base name
                deps.append(m.group(1).split("/")[-1])

    elif name == "cargo.toml":
        for line in text.splitlines():
            m = re.match(r'^(\w[\w-]*)\s*=', line)
            if m and m.group(1) not in ("package", "profile", "features", "workspace"):
                deps.append(m.group(1))

    elif name == "pom.xml":
        for m in re.finditer(r'<artifactId>([^<]+)</artifactId>', text):
            deps.append(m.group(1))

    elif name in ("build.gradle", "build.gradle.kts"):
        for m in re.finditer(r"['\"]([a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+):[^'\"]+['\"]", text):
            deps.append(m.group(1).split(":")[-1])

    elif name == "gemfile":
        for m in re.finditer(r"gem\s+['\"]([^'\"]+)['\"]", text):
            deps.append(m.group(1))

    elif name == "composer.json":
        try:
            data = json.loads(text)
            for section in ("require", "require-dev"):
                deps.extend(data.get(section, {}).keys())
        except json.JSONDecodeError:
            pass

    elif name == "pubspec.yaml":
        in_deps = False
        for line in text.splitlines():
            if re.match(r'^dependencies:', line):
                in_deps = True
                continue
            if in_deps:
                if re.match(r'^\S', line) and not line.startswith(" "):
                    in_deps = False
                    continue
                m = re.match(r'^\s+(\w[\w-]*):', line)
                if m and m.group(1) != "flutter":
                    deps.append(m.group(1))

    return list(dict.fromkeys(deps))   # deduplicate


def _service_name_from_path(repo_dir: Path) -> str:
    """Derive a service name from the repo directory name."""
    return repo_dir.name.lower().replace("_", "-").replace(" ", "-")


def _scan_repos_root(repos_root: str) -> dict[str, list[str]]:
    """
    Scan a directory of repo clones.
    Returns {service_name: [dependency_names]}.
    """
    root = Path(repos_root)
    if not root.is_dir():
        log.warning("REPOS_ROOT '%s' is not a directory", repos_root)
        return {}

    service_map: dict[str, list[str]] = {}
    for repo_dir in sorted(root.iterdir()):
        if not repo_dir.is_dir() or repo_dir.name.startswith("."):
            continue
        service = _service_name_from_path(repo_dir)
        deps: list[str] = []
        for manifest_name in _MANIFEST_NAMES:
            manifest = repo_dir / manifest_name
            if manifest.exists():
                deps.extend(_extract_deps_from_manifest(manifest))
        if deps:
            service_map[service] = list(dict.fromkeys(deps))
            log.debug("Scanned %s → %d deps", service, len(deps))

    log.info("Service graph scan: %d services discovered in %s", len(service_map), repos_root)
    return service_map


# ── Graph builders ────────────────────────────────────────────────────────────

def build_from_dict(service_map: dict[str, list[str]], meta: dict[str, dict] | None = None) -> Any:
    """
    Build a NetworkX DiGraph from a service → [deps] mapping.

    meta: optional {node_name: {team, version, type}} attribute dict.
    Edges: dep → service  (nx.descendants(G, dep) = all services that use dep).
    """
    if not _HAS_NX:
        raise ImportError("pip install networkx to enable service graph analysis")

    G   = nx.DiGraph()
    meta = meta or {}

    for service, deps in service_map.items():
        attrs = {"type": "service", **meta.get(service, {})}
        G.add_node(service, **attrs)
        for dep in deps:
            if not G.has_node(dep):
                G.add_node(dep, type="library", **meta.get(dep, {}))
            G.add_edge(dep, service)

    log.info(
        "Service graph built: %d nodes, %d edges",
        G.number_of_nodes(), G.number_of_edges(),
    )
    return G


def build_from_json_file(path: str) -> Any:
    """Load a service map JSON file and build the graph."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot load service map from '%s': %s", path, exc)
        return None

    if not isinstance(data, dict):
        log.error("Service map JSON must be a dict, got %s", type(data).__name__)
        return None

    # Support optional metadata section: {"_meta": {node: {team, version}}, ...services...}
    meta = data.pop("_meta", {})
    return build_from_dict(data, meta)


def load_service_graph() -> Any | None:
    """
    Load the service graph using the best available source.
    Called once at orchestrator startup.
    Returns a NetworkX DiGraph or None if nothing is configured.
    """
    if not _HAS_NX:
        return None

    try:
        from config.settings import get_settings
        cfg = get_settings()
    except Exception:
        return None

    # Priority 1: explicit JSON file
    map_path = getattr(cfg, "service_map_path", "")
    if map_path:
        graph = build_from_json_file(map_path)
        if graph is not None:
            log.info("Service graph loaded from %s (%d nodes)", map_path, graph.number_of_nodes())
            return graph

    # Priority 2: scan repos root
    repos_root = getattr(cfg, "repos_root", "")
    if repos_root:
        service_map = _scan_repos_root(repos_root)
        if service_map:
            return build_from_dict(service_map)

    return None
