"""
governance/blast_radius.py
---------------------------
Derive a blast-radius score from real impact signals when the service-dependency
graph isn't available (graph score would be 0). Uses reference-impact + interface
signals so the metric reflects actual downstream risk. Never lowers a real score.
"""
from __future__ import annotations


def derive_blast_radius(report) -> int | None:
    """Return a derived 0-100 score if it should replace a missing/near-zero
    graph score, else None (leave the existing value)."""
    dep = getattr(report, "dependency", None)
    if dep is None:
        return None
    current = getattr(dep, "blast_radius_score", 0) or 0

    ri = getattr(report, "reference_impact", None)
    iface = getattr(report, "interface", None)
    refs     = (getattr(ri, "total_references", 0) or 0) if ri else 0
    hi_files = len(getattr(ri, "high_impact_files", []) or []) if ri else 0
    shared   = len(getattr(ri, "shared_lib_breaks", []) or []) if ri else 0
    breaking = len(getattr(iface, "breaking_changes", []) or []) if iface else 0
    # Downstream services the reviewer explicitly declared as dependents
    # (selected "connected repos" → DependencyResult.affected_services).
    services = len(getattr(dep, "affected_services", []) or [])

    derived = min(100, refs * 2 + hi_files * 8 + breaking * 18 + shared * 15 + services * 12)
    # Never lower an existing score (real graph result OR a connected-repo
    # baseline) — only raise it when impact signals justify more.
    return derived if derived > current else None
