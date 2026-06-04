"""
governance/capability_map.py
-----------------------------
Map changed file paths → business capabilities.

Turns "you changed services/payment/refund.py and models/user.py" into
"this affects **Payments / Refunds** (Payments Engineering, critical) and
**Customer Data / Privacy** (Data Governance, high)" — so reviewers and
functional/QA teams see the real-world feature impact, not just file paths.

Config: config/capability_map.json (path globs → capability metadata).
Falls back gracefully to an empty result if the config is missing.
"""
from __future__ import annotations
import fnmatch
import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PATH = "config/capability_map.json"


@lru_cache(maxsize=1)
def _load_map(path: str = _DEFAULT_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        log.debug("capability_map.json not found at %s — capability mapping disabled", p)
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("capabilities", []) if isinstance(data, dict) else []
    except Exception as exc:
        log.warning("Failed to load capability map: %s", exc)
        return []


def reload_map() -> int:
    """Hot-reload the capability map (e.g. after editing the JSON)."""
    _load_map.cache_clear()
    return len(_load_map())


def _matches(file_path: str, pattern: str) -> bool:
    """Glob match supporting ** (treated like *, which already crosses '/')."""
    fp = file_path.lstrip("./").lower()
    pat = pattern.replace("**", "*").lower()
    return fnmatch.fnmatch(fp, pat) or fnmatch.fnmatch(fp, "*/" + pat.lstrip("*/"))


def map_paths(paths: list[str]) -> list[dict]:
    """
    Given a list of changed file paths, return the affected capabilities, each
    with the subset of files that matched. Sorted by criticality (critical first).
    """
    caps = _load_map()
    if not caps or not paths:
        return []

    crit_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    result: list[dict] = []

    for cap in caps:
        patterns = cap.get("paths", [])
        matched = sorted({
            fp for fp in paths
            if fp and any(_matches(fp, pat) for pat in patterns)
        })
        if matched:
            result.append({
                "name":        cap.get("name", "Unknown"),
                "team":        cap.get("team", ""),
                "owners":      cap.get("owners", []),
                "criticality": cap.get("criticality", "medium"),
                "files":       matched[:20],
                "file_count":  len(matched),
            })

    result.sort(key=lambda c: crit_order.get(c["criticality"], 9))
    return result


def _collect_changed_files(report) -> list[str]:
    """
    Gather every file path the report references. AnalysisReport doesn't store
    the raw diff, so we union file paths from reference impact + all findings.
    """
    files: set[str] = set()

    ri = getattr(report, "reference_impact", None)
    if ri:
        for ref in (getattr(ri, "references", None) or []):
            if getattr(ref, "file_path", ""):
                files.add(ref.file_path)
        for fp in (getattr(ri, "high_impact_files", None) or []):
            files.add(fp)
        for sym in (getattr(ri, "changed_symbols", None) or []):
            pass  # symbols aren't paths

    # Findings across agents that carry file_path
    for attr in ("security", "performance_impact", "data_privacy",
                 "maintainability", "observability", "ast_analysis", "secrets_entropy"):
        res = getattr(report, attr, None)
        if not res:
            continue
        for f in (getattr(res, "findings", None) or getattr(res, "pii_findings", []) or
                  getattr(res, "issues", [])):
            fp = getattr(f, "file_path", "") or getattr(f, "file", "")
            if fp:
                files.add(fp)

    # Schema migration files
    sc = getattr(report, "schema_change", None)
    if sc:
        for fp in (getattr(sc, "migration_files", None) or []):
            files.add(fp)

    return sorted(files)


def capabilities_for_report(report) -> list[dict]:
    """Top-level entry: returns the business capabilities a report's change touches."""
    return map_paths(_collect_changed_files(report))
