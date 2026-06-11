"""
api/routes/quality.py
----------------------
REST endpoints for Level 2 recall/precision quality metrics.

POST /api/v1/quality/run
  Runs all 8 golden cases through their respective agent's static
  fallback path, computes TP/FP/FN per agent, logs to JSONL, and
  returns a full breakdown.

GET  /api/v1/quality/summary
  Returns the aggregated stats from the latest logged run (or {runs:0}).

GET  /api/v1/quality/trend
  Returns per-run overall_recall and overall_precision for charting.
"""
from __future__ import annotations
import logging
import uuid
from typing import Any

from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["quality"])


# ── Case → (agent_name, file_path, language) mapping ─────────────────────────

_CASE_MAP: dict[str, tuple[str | None, str, str]] = {
    "aws_secret":       ("secrets_entropy", "src/config/aws_client.py",           "python"),
    "drop_table":       ("schema_change",   "db/migrations/V20240601.sql",         "sql"),
    "sql_injection":    ("security",        "src/payments/repository.py",          "python"),
    "removed_endpoint": ("interface",       "api/openapi.yaml",                    "yaml"),
    "open_ingress":     ("iac_analysis",    "infra/security_groups.tf",            "hcl"),
    "high_complexity":  ("ast_analysis",    "src/payments/processor.py",           "python"),
    "taint_flow":       ("taint_analysis",  "src/api/search.py",                   "python"),
    "clean_refactor":   (None,              "src/payments/formatter.py",           "python"),
    "java_sql_injection":      ("security",           "src/main/java/dao/AccountDao.java",      "java"),
    "java_n_plus_1":           ("performance_impact", "src/main/java/svc/OrderService.java",    "java"),
    "java_taint_flow":         ("taint_analysis",     "src/main/java/api/SearchController.java", "java"),
    "java_swallowed_exception": ("maintainability",   "src/main/java/svc/FeeService.java",      "java"),
}

# For the clean-refactor case, run these agents to test for false positives
_CLEAN_AGENTS = ["security", "secrets_entropy", "ast_analysis"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_request(file_path: str, language: str, diff_text: str):
    from core.models import AnalysisRequest, ChangeType, DiffHunk
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/quality/golden-check",
        source_ref="feature",
        target_ref="main",
        hunks=[DiffHunk(
            file_path=file_path,
            language=language,
            additions=diff_text.count("\n+"),
            deletions=diff_text.count("\n-"),
            content=diff_text,
        )],
    )


def _run_agent(agent_name: str, req: Any) -> Any:
    """Run a single agent's fallback_result and return the result object."""
    if agent_name == "secrets_entropy":
        from agents.secrets_entropy_agent import SecretsEntropyAgent
        return SecretsEntropyAgent().fallback_result(req)
    if agent_name == "schema_change":
        from agents.schema_change_agent import SchemaChangeAgent
        return SchemaChangeAgent().fallback_result(req)
    if agent_name == "security":
        from agents.security_agent import SecurityReviewAgent
        return SecurityReviewAgent().fallback_result(req)
    if agent_name == "interface":
        from agents.interface_agent import InterfaceAnalysisAgent
        return InterfaceAnalysisAgent().fallback_result(req)
    if agent_name == "iac_analysis":
        from agents.iac_analysis_agent import IaCAnalysisAgent
        return IaCAnalysisAgent().fallback_result(req)
    if agent_name == "ast_analysis":
        from agents.ast_analysis_agent import ASTAnalysisAgent
        return ASTAnalysisAgent().fallback_result(req)
    if agent_name == "taint_analysis":
        from agents.taint_analysis_agent import TaintAnalysisAgent
        return TaintAnalysisAgent().fallback_result(req)
    if agent_name == "performance_impact":
        from agents.performance_impact_agent import PerformanceImpactAgent
        return PerformanceImpactAgent().fallback_result(req)
    if agent_name == "maintainability":
        from agents.maintainability_agent import MaintainabilityAgent
        return MaintainabilityAgent().fallback_result(req)
    return None


def _build_result_map(case_id: str, diff_text: str, file_path: str, language: str, agent_name: str | None) -> dict:
    req = _build_request(file_path, language, diff_text)
    if agent_name is None:
        # Clean case — run candidate agents to test precision
        result_map = {}
        for name in _CLEAN_AGENTS:
            try:
                result_map[name] = _run_agent(name, req)
            except Exception as exc:
                log.warning("Clean case agent %s failed: %s", name, exc)
        return result_map
    try:
        result = _run_agent(agent_name, req)
        return {agent_name: result}
    except Exception as exc:
        log.error("Quality run: agent %s / case %s failed: %s", agent_name, case_id, exc)
        return {}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/quality/run")
def run_quality_check():
    """
    Run all golden cases through static analysis and record recall/precision.
    Returns a full per-agent and per-case breakdown.
    """
    from evaluation.ground_truth import GOLDEN_CASES
    from evaluation.recall_tracker import RecallTracker

    tracker = RecallTracker()
    result_maps: list[dict] = []

    for case in GOLDEN_CASES:
        info = _CASE_MAP.get(case.case_id)
        if info is None:
            result_maps.append({})
            continue
        agent_name, file_path, language = info
        result_maps.append(
            _build_result_map(case.case_id, case.diff_text, file_path, language, agent_name)
        )

    metrics = tracker.evaluate_all(GOLDEN_CASES, result_maps)
    tracker.log(metrics)

    by_agent = metrics.by_agent()
    return {
        "eval_id":           metrics.eval_id,
        "timestamp":         metrics.timestamp,
        "overall_recall":    metrics.overall_recall,
        "overall_precision": metrics.overall_precision,
        "by_agent": {
            name: {
                "precision": m.precision,
                "recall":    m.recall,
                "f1":        m.f1,
                "tp":        m.true_positives,
                "fp":        m.false_positives,
                "fn":        m.false_negatives,
            }
            for name, m in sorted(by_agent.items())
        },
        "by_case": [
            {
                "case_id":   cm.case_id,
                "recall":    cm.overall_recall,
                "precision": cm.overall_precision,
            }
            for cm in metrics.cases
        ],
    }


@router.get("/quality/summary")
def quality_summary():
    """Return aggregated stats from the most recent logged quality run."""
    from evaluation.recall_tracker import RecallTracker
    return RecallTracker().summary()


@router.get("/quality/trend")
def quality_trend():
    """Return per-run recall/precision for trend charting."""
    from evaluation.recall_tracker import RecallTracker
    return RecallTracker().trend()
