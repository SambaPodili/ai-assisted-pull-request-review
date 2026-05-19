"""
evaluation/panel.py
--------------------
Multi-judge panel: runs N independent LLM judges in parallel and
consolidates their individual JudgeScores into a single PanelVerdict.

Consolidation rules
-------------------
  Scores        : arithmetic mean across valid (non-fallback) judges
  Verdict       : majority vote; ties broken toward the stricter verdict
  Confidence    : HIGH if all judges agree, MEDIUM if n-1 agree, LOW otherwise
  Missed issues : collected from all judges; consensus = flagged by >= 50 % of judges
  False positives: same threshold
  Disagreements : any rubric dimension where max − min > 1.5 across judges
"""
from __future__ import annotations
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from evaluation.judge  import LLMJudge
from evaluation.models import EvaluationReport, JudgeScore, PanelVerdict

log = logging.getLogger(__name__)

_VERDICT_RANK = {"FAIL": 0, "PARTIAL": 1, "PASS": 2}


# ── Consolidation helpers ─────────────────────────────────────────────────────

def _majority_verdict(verdicts: list[str]) -> str:
    """Return the most-common verdict; ties break toward the stricter outcome."""
    counts = Counter(verdicts)
    max_count = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_count]
    # Among ties, pick the stricter verdict (lowest rank)
    return min(candidates, key=lambda v: _VERDICT_RANK[v])


def _judge_confidence(verdicts: list[str]) -> str:
    unique = set(verdicts)
    n = len(verdicts)
    if len(unique) == 1:
        return "HIGH"
    top_count = Counter(verdicts).most_common(1)[0][1]
    if n >= 3 and top_count >= n - 1:
        return "MEDIUM"
    return "LOW"


def _collect_text_items(
    lists: list[list[str]],
    threshold: float = 0.5,
) -> tuple[list[str], list[str]]:
    """
    Given lists of strings from each judge, produce:
      - all_items     : deduplicated union (case-insensitive)
      - consensus     : items mentioned by >= threshold fraction of judges
    """
    counts: dict[str, tuple[int, str]] = {}  # normalised_key → (count, canonical_text)
    for lst in lists:
        for item in lst:
            key = item.strip().lower()
            if not key:
                continue
            prev_count, prev_text = counts.get(key, (0, item.strip()))
            counts[key] = (prev_count + 1, prev_text)

    n         = len(lists)
    all_items = [text for _, text in counts.values()]
    consensus = [
        text for (cnt, text) in counts.values()
        if cnt / n >= threshold
    ]
    return all_items, consensus


def _detect_disagreements(scores: list[JudgeScore]) -> list[str]:
    """Return a description for each rubric dimension with spread > 1.5."""
    dims = [
        ("completeness",      "Completeness"),
        ("precision",         "Precision"),
        ("severity_accuracy", "Severity accuracy"),
        ("specificity",       "Specificity"),
    ]
    disagreements = []
    for attr, label in dims:
        values = [getattr(s, attr) for s in scores if not s.fallback_used]
        if len(values) < 2:
            continue
        lo, hi = min(values), max(values)
        if hi - lo > 1.5:
            disagreements.append(
                f"{label}: judges rated {lo}–{hi} (spread = {hi - lo})"
            )
    return disagreements


def _build_summary(
    verdict:       str,
    scores:        list[JudgeScore],
    disagreements: list[str],
) -> str:
    valid = [s for s in scores if not s.fallback_used]
    parts = [f"Panel of {len(valid)} judge(s) → verdict: {verdict}."]

    insights = [s.key_insight for s in valid if s.key_insight]
    if insights:
        parts.append("Key insights: " + " | ".join(insights))

    if disagreements:
        parts.append("Divergences: " + "; ".join(disagreements))

    return "  ".join(parts)


# ── JudgePanel ────────────────────────────────────────────────────────────────

class JudgePanel:
    """
    Runs multiple independent LLM judges in parallel and consolidates results.

    Minimum: 1 judge (trivial consolidation).
    Recommended: 3 judges for robust majority voting.

    Parameters
    ----------
    judges : list of config dicts, each with at least:
               - provider  : "anthropic" | "openai" | "azure_openai" | "ollama"
               - model     : model identifier (e.g. "claude-sonnet-4-6", "gpt-4o")
               - api_key   : API key for the provider (omit to read from env)
               - base_url  : optional, for Azure / Ollama

    Example
    -------
    panel = JudgePanel(judges=[
        {"provider": "anthropic", "model": "claude-sonnet-4-6",        "api_key": "sk-ant-..."},
        {"provider": "openai",    "model": "gpt-4o",                    "api_key": "sk-oai-..."},
        {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "api_key": "sk-ant-..."},
    ])
    verdict = panel.evaluate_agent("security", diff_text, security_result.model_dump())
    full    = panel.evaluate_report(report, diff_text)
    """

    def __init__(self, judges: list[dict[str, Any]]) -> None:
        if not judges:
            raise ValueError("JudgePanel requires at least one judge configuration")
        self._judges = [LLMJudge(cfg) for cfg in judges]

    @property
    def judge_ids(self) -> list[str]:
        return [j.judge_id for j in self._judges]

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate_agent(
        self,
        agent_name:    str,
        diff_text:     str,
        findings_json: dict[str, Any],
        max_tokens:    int = 1500,
    ) -> PanelVerdict:
        """
        Run all judges on a single agent's output and return a consolidated verdict.

        Parameters
        ----------
        agent_name    : agent identifier, e.g. "security"
        diff_text     : the original code diff
        findings_json : agent_result.model_dump()
        max_tokens    : token cap per judge response
        """
        scores = self._run_judges(agent_name, diff_text, findings_json, max_tokens)
        return self._consolidate(agent_name, scores)

    def evaluate_report(
        self,
        report,                            # core.models.AnalysisReport
        diff_text:   str,
        agent_names: list[str] | None = None,
    ) -> EvaluationReport:
        """
        Evaluate multiple agents from a completed AnalysisReport in parallel.

        Parameters
        ----------
        report      : completed AnalysisReport
        diff_text   : the original diff (not stored in AnalysisReport)
        agent_names : which agents to judge; defaults to all with non-None results
        """
        slot_map = {
            "code_analysis":   report.code_analysis,
            "security":        report.security,
            "ast_analysis":    report.ast_analysis,
            "secrets_entropy": report.secrets_entropy,
            "taint_analysis":  report.taint_analysis,
            "iac_analysis":    report.iac_analysis,
            "temporal_risk":   report.temporal_risk,
            "schema_change":   report.schema_change,
            "dependency":      report.dependency,
            "test_coverage":   report.test_coverage,
            "interface":       report.interface,
            "risk":            report.risk,
            "remediation":     report.remediation,
        }
        targets = agent_names or [k for k, v in slot_map.items() if v is not None]

        # Evaluate each agent — run agent evaluations in parallel too
        verdicts: list[PanelVerdict] = []
        with ThreadPoolExecutor(max_workers=min(len(targets), 6)) as pool:
            futures = {}
            for name in targets:
                result = slot_map.get(name)
                if result is None:
                    continue
                try:
                    findings = result.model_dump()
                except Exception:
                    continue
                futures[pool.submit(self.evaluate_agent, name, diff_text, findings)] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    verdicts.append(future.result())
                except Exception as exc:
                    log.error("[EvalPanel] Agent %s evaluation crashed: %s", name, exc)

        # Roll-up across agents
        scores        = [v.overall_score for v in verdicts]
        overall_score = round(sum(scores) / len(scores), 2) if scores else 0.0

        verdict_list = [v.verdict for v in verdicts]
        if "FAIL" in verdict_list:
            overall_verdict = "FAIL"
        elif "PARTIAL" in verdict_list:
            overall_verdict = "PARTIAL"
        else:
            overall_verdict = "PASS"

        critical_gaps = [
            f"[{v.agent_name}] {item}"
            for v in verdicts
            for item in v.consensus_missed
        ]

        return EvaluationReport(
            request_id=report.request_id,
            repo_url=report.repo_url,
            source_ref=report.source_ref,
            target_ref=report.target_ref,
            panel_size=len(self._judges),
            agent_verdicts=verdicts,
            overall_verdict=overall_verdict,
            overall_score=overall_score,
            critical_gaps=critical_gaps,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_judges(
        self,
        agent_name:    str,
        diff_text:     str,
        findings_json: dict[str, Any],
        max_tokens:    int,
    ) -> list[JudgeScore]:
        scores: list[JudgeScore] = []
        with ThreadPoolExecutor(max_workers=len(self._judges)) as pool:
            futures = {
                pool.submit(judge.evaluate, agent_name, diff_text, findings_json, max_tokens): judge
                for judge in self._judges
            }
            for future in as_completed(futures):
                judge = futures[future]
                try:
                    scores.append(future.result())
                except Exception as exc:
                    log.error("[Judge %s] Unhandled crash: %s", judge.judge_id, exc)
                    scores.append(JudgeScore(
                        judge_id=judge.judge_id,
                        agent_name=agent_name,
                        completeness=1, precision=1, severity_accuracy=1, specificity=1,
                        missed_issues=[], false_positives=[],
                        verdict="FAIL",
                        reasoning=f"Judge process crashed: {exc}",
                        fallback_used=True,
                    ))
        return scores

    def _consolidate(self, agent_name: str, scores: list[JudgeScore]) -> PanelVerdict:
        # Prefer valid scores; fall back to all scores if everything failed
        valid = [s for s in scores if not s.fallback_used] or scores

        def _avg(attr: str) -> float:
            vals = [getattr(s, attr) for s in valid]
            return round(sum(vals) / len(vals), 2) if vals else 0.0

        c  = _avg("completeness")
        p  = _avg("precision")
        sa = _avg("severity_accuracy")
        sp = _avg("specificity")
        overall = round((c + p + sa + sp) / 4, 2)

        verdicts   = [s.verdict for s in valid]
        verdict    = _majority_verdict(verdicts)
        confidence = _judge_confidence(verdicts)

        all_missed,  consensus_missed  = _collect_text_items([s.missed_issues   for s in valid])
        all_fp,      consensus_fp      = _collect_text_items([s.false_positives  for s in valid])
        disagreements = _detect_disagreements(valid)
        summary       = _build_summary(verdict, valid, disagreements)

        return PanelVerdict(
            agent_name=agent_name,
            judge_count=len(scores),
            avg_completeness=c,
            avg_precision=p,
            avg_severity_accuracy=sa,
            avg_specificity=sp,
            overall_score=overall,
            verdict=verdict,
            confidence=confidence,
            all_missed=all_missed,
            consensus_missed=consensus_missed,
            all_false_positives=all_fp,
            consensus_false_positives=consensus_fp,
            disagreements=disagreements,
            summary=summary,
            judge_scores=scores,
        )
