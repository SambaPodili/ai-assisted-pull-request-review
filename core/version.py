"""
core/version.py
----------------
Single source of truth for the application's build version.

Bump BUILD_VERSION whenever you ship a meaningful set of changes so operators
can instantly confirm a running backend/frontend is on the latest build
(surfaced via /live, /health, and the dashboard footer).

BUILD_DATE is informational; BUILD_VERSION is the value to compare.
"""
from __future__ import annotations

BUILD_VERSION = "2.4.0"
BUILD_DATE = "2026-06-04"

# Short, human-readable summary of what this build includes — shown in /health.
BUILD_FEATURES = [
    "deterministic-gate-policy",
    "business-capability-mapping",
    "reviewer-feedback-loop",
    "concrete-fix-diffs",
    "consumer-impact-tracing",
    "insights-dashboards",
    "phantom-finding-filter",
]


def version_info() -> dict:
    return {
        "version":  BUILD_VERSION,
        "built":    BUILD_DATE,
        "features": BUILD_FEATURES,
    }
