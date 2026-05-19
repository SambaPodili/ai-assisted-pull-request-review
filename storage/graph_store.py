"""
storage/graph_store.py
-----------------------
Service dependency graph storage with two backends:
  • NetworkX  — in-process (Phase 2, development, testing)
  • Neo4j     — production-grade (Phase 3)

Both implement the GraphStore protocol so agents are backend-agnostic.
"""
from __future__ import annotations
import logging
from typing import Protocol, runtime_checkable, Any

log = logging.getLogger(__name__)

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False

try:
    from neo4j import GraphDatabase
    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False


@runtime_checkable
class GraphStore(Protocol):
    def add_node(self, name: str, node_type: str, metadata: dict) -> None: ...
    def add_edge(self, source: str, target: str, edge_type: str) -> None: ...
    def get_descendants(self, node: str) -> list[str]: ...
    def get_predecessors(self, node: str) -> list[str]: ...
    def node_count(self) -> int: ...
    def node_metadata(self, name: str) -> dict: ...


# ── NetworkX backend ──────────────────────────────────────────────────────────

class NetworkXGraphStore:
    """In-process directed graph. Suitable for Phase 2 and testing."""

    def __init__(self) -> None:
        if not HAS_NX:
            raise ImportError("pip install networkx to enable graph store")
        self._g: Any = nx.DiGraph()

    def add_node(self, name: str, node_type: str = "service", metadata: dict | None = None) -> None:
        self._g.add_node(name, type=node_type, **(metadata or {}))

    def add_edge(self, source: str, target: str, edge_type: str = "depends_on") -> None:
        """source depends on target  →  edge: source → target"""
        self._g.add_edge(source, target, type=edge_type)

    def get_descendants(self, node: str) -> list[str]:
        """All nodes reachable from *node* (i.e. everything that depends on it)."""
        if node not in self._g:
            return []
        # Reverse graph: edge source→target means source depends on target.
        # Descendants in the reverse graph = things that depend on `node`.
        return list(nx.descendants(self._g.reverse(), node))

    def get_predecessors(self, node: str) -> list[str]:
        """Direct dependencies of node."""
        return list(self._g.predecessors(node)) if node in self._g else []

    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def node_metadata(self, name: str) -> dict:
        return dict(self._g.nodes.get(name, {}))

    @property
    def raw_graph(self) -> Any:
        """Access underlying NetworkX graph for advanced operations."""
        return self._g


# ── Neo4j backend ─────────────────────────────────────────────────────────────

class Neo4jGraphStore:
    """Production graph store backed by Neo4j. Phase 3+."""

    def __init__(self, url: str, user: str, password: str) -> None:
        if not HAS_NEO4J:
            raise ImportError("pip install neo4j to enable Neo4j graph store")
        self._driver = GraphDatabase.driver(url, auth=(user, password))

    def add_node(self, name: str, node_type: str = "Service", metadata: dict | None = None) -> None:
        props = {"name": name, **(metadata or {})}
        with self._driver.session() as s:
            s.run(
                f"MERGE (n:{node_type} {{name: $name}}) SET n += $props",
                name=name, props=props,
            )

    def add_edge(self, source: str, target: str, edge_type: str = "DEPENDS_ON") -> None:
        with self._driver.session() as s:
            s.run(
                f"MATCH (a {{name: $src}}), (b {{name: $tgt}}) "
                f"MERGE (a)-[:{edge_type}]->(b)",
                src=source, tgt=target,
            )

    def get_descendants(self, node: str) -> list[str]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (n {name: $name})<-[:DEPENDS_ON*]-(m) RETURN DISTINCT m.name AS name",
                name=node,
            )
            return [r["name"] for r in result]

    def get_predecessors(self, node: str) -> list[str]:
        with self._driver.session() as s:
            result = s.run(
                "MATCH (n {name: $name})-[:DEPENDS_ON]->(m) RETURN m.name AS name",
                name=node,
            )
            return [r["name"] for r in result]

    def node_count(self) -> int:
        with self._driver.session() as s:
            result = s.run("MATCH (n) RETURN count(n) AS cnt")
            return result.single()["cnt"]

    def node_metadata(self, name: str) -> dict:
        with self._driver.session() as s:
            result = s.run("MATCH (n {name: $name}) RETURN properties(n) AS props", name=name)
            record = result.single()
            return dict(record["props"]) if record else {}

    def close(self) -> None:
        self._driver.close()


# ── Factory ────────────────────────────────────────────────────────────────────

def make_graph_store(settings=None) -> GraphStore:
    from config.settings import get_settings
    cfg = settings or get_settings()
    if cfg.neo4j_url and HAS_NEO4J:
        try:
            return Neo4jGraphStore(cfg.neo4j_url, cfg.neo4j_user, cfg.neo4j_pass)
        except Exception as e:
            log.warning("Neo4j connection failed (%s) — using NetworkX", e)
    if HAS_NX:
        return NetworkXGraphStore()
    log.warning("No graph store available (networkx not installed)")
    return _NullGraphStore()


class _NullGraphStore:
    """No-op fallback when no graph backend is available."""
    def add_node(self, *a, **kw):   pass
    def add_edge(self, *a, **kw):   pass
    def get_descendants(self, *a):  return []
    def get_predecessors(self, *a): return []
    def node_count(self):           return 0
    def node_metadata(self, *a):    return {}


# ── Loader utility ────────────────────────────────────────────────────────────

def load_graph_from_dict(manifest: dict[str, list[str]], store: GraphStore | None = None) -> GraphStore:
    """
    Populate a graph store from a simple manifest dict.

    manifest format: {service: [list_of_dependencies_it_uses]}
    e.g. {"payments-service": ["auth-lib", "accounts-core"]}
    """
    g = store or make_graph_store()
    for service, deps in manifest.items():
        g.add_node(service, node_type="Service")
        for dep in deps:
            g.add_node(dep, node_type="Library")
            g.add_edge(service, dep)
    return g
