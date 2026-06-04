"""
tests/test_frontend_contract.py
--------------------------------
Frontend ↔ backend field contract.

The single-page UI (frontend/index.html → normalizeReport + the per-tab
renderers) reads specific fields off each agent-result model. When a backend
field is renamed without updating the frontend, the affected tab silently
shows empty or wrong data — exactly the class of bug found across the
Privacy, Timings, Performance, Quality and Checklist tabs.

This test pins the contract: for every model the frontend consumes, the
fields it depends on MUST exist on the Pydantic model. Rename a backend field
and forget the UI, and this test fails immediately with a clear message.

It also greps frontend/index.html to make sure the contracted field names are
actually referenced there (guards against the contract drifting from the UI).
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest

from core import models as m


# ── The contract ──────────────────────────────────────────────────────────────
# model class name -> the backend fields the frontend normalizer/tab reads.
# Keep this in sync with normalizeReport() and renderResultTab() in index.html.
CONTRACT: dict[str, list[str]] = {
    # Gate / risk
    "RiskResult":            ["overall_risk", "risk_score", "gate_decision",
                              "rationale", "deployment_guidance", "rollback_feasibility"],
    "RemediationResult":     ["fix_suggestions", "validation_checklist",
                              "deployment_strategy", "executive_summary"],

    # Phase 1
    "CodeAnalysisResult":    ["summary", "change_type", "complexity_delta", "findings"],
    "CodeFinding":           ["severity", "description", "file_path", "line_range"],
    "SecurityResult":        ["findings", "secrets_detected", "overall_severity"],
    "SecurityFinding":       ["cwe_id", "severity", "description", "file_path", "line_range"],

    # Phase 2
    "DependencyResult":      ["blast_radius_score", "affected_services",
                              "changed_packages", "cve_hits"],
    "TestCoverageResult":    ["coverage_delta", "regression_risk", "uncovered_paths"],
    "InterfaceResult":       ["breaking_changes"],
    "SchemaChangeResult":    ["changes", "has_destructive", "has_irreversible"],

    # Quality group
    "PerformanceImpactResult": ["findings", "has_db_risk",
                                "has_complexity_regression", "overall_severity", "summary"],
    "PerformanceFinding":    ["category", "severity", "description", "file_path", "line", "suggestion"],
    "DataPrivacyResult":     ["pii_findings", "logging_violations",
                              "unencrypted_pii_count", "gdpr_risk", "summary"],
    "PIIFinding":            ["pii_type", "risk_level", "description", "file_path",
                              "line", "is_encrypted", "is_logged"],
    "MaintainabilityResult": ["issues", "maintainability_score", "summary"],
    "MaintainabilityIssue":  ["kind", "severity", "description", "file_path", "line", "suggestion"],
    "LicenseComplianceResult": ["findings", "has_copyleft", "has_license_conflict", "summary"],
    "LicenseFinding":        ["package", "detected_license", "risk_level", "description", "file_path"],
    "ObservabilityResult":   ["findings", "logs_removed", "metrics_removed",
                              "unobserved_branches", "summary"],
    "ObservabilityFinding":  ["kind", "severity", "description", "file_path", "line", "suggestion"],

    # QA + references
    "QAScenariosResult":     ["scenarios", "total_scenarios"],
    "ReferenceImpactResult": ["references", "total_references", "high_impact_files",
                              "intra_project_risk", "search_backend", "changed_symbols"],

    # Timings (per-agent)
    "AgentTokenUsage":       ["agent", "tokens_used", "model", "duration_s"],

    # Top-level report sections the UI dereferences
    "AnalysisReport":        ["code_analysis", "security", "dependency", "test_coverage",
                              "interface", "schema_change", "qa_scenarios", "reference_impact",
                              "performance_impact", "data_privacy", "maintainability",
                              "license_compliance", "observability", "risk", "remediation",
                              "token_usage", "errors"],
}

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def _frontend_source() -> str:
    """
    Concatenate all frontend source the UI is built from. Supports both layouts:
      • Vite/React app  → frontend/src/**/*.{js,jsx,ts,tsx}
      • legacy monolith → frontend/index.html (or index_1.html backup)
    """
    chunks: list[str] = []
    src_dir = _FRONTEND / "src"
    if src_dir.is_dir():
        for ext in ("*.js", "*.jsx", "*.ts", "*.tsx"):
            for f in src_dir.rglob(ext):
                chunks.append(f.read_text(errors="ignore"))
    for fallback in ("index.html", "index_1.html"):
        fp = _FRONTEND / fallback
        if fp.exists():
            chunks.append(fp.read_text(errors="ignore"))
    return "\n".join(chunks)


_INDEX_HTML = _FRONTEND / "index.html"   # retained for the presence check


@pytest.mark.parametrize("model_name,fields", CONTRACT.items())
def test_backend_fields_exist(model_name: str, fields: list[str]):
    """Every field the frontend depends on must exist on the Pydantic model."""
    cls = getattr(m, model_name, None)
    assert cls is not None, f"Model '{model_name}' no longer exists in core.models"
    assert hasattr(cls, "model_fields"), f"'{model_name}' is not a Pydantic model"
    model_fields = set(cls.model_fields.keys())
    missing = [f for f in fields if f not in model_fields]
    assert not missing, (
        f"{model_name} is missing field(s) {missing} that the frontend "
        f"(normalizeReport / renderResultTab) reads. Either restore the field "
        f"or update frontend/index.html AND this contract. "
        f"Available fields: {sorted(model_fields)}"
    )


def test_index_html_present():
    assert _INDEX_HTML.exists(), f"frontend not found at {_INDEX_HTML}"


@pytest.mark.parametrize("model_name,fields", CONTRACT.items())
def test_contract_fields_referenced_in_frontend(model_name: str, fields: list[str]):
    """
    Guard the other direction: each contracted field should appear somewhere in
    index.html, so the contract can't silently drift away from what the UI uses.
    (A loose check — just that the identifier appears as a property access.)
    """
    html = _frontend_source()
    # Fields that are intentionally read via fallback aliases or only in
    # backend-shaped payloads; skip the few that the UI accesses indirectly.
    SKIP = {("AnalysisReport", "errors")}  # errors handled via normalizeReport default
    not_found = []
    for f in fields:
        if (model_name, f) in SKIP:
            continue
        # property access like `.field` or `"field"` (object key)
        if not re.search(rf"[.\"']{re.escape(f)}\b", html):
            not_found.append(f)
    assert not not_found, (
        f"Fields {not_found} are in the {model_name} contract but never "
        f"referenced in frontend/index.html — contract is stale."
    )
