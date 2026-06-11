"""
evaluation/recall_tracker.py
-----------------------------
Level 2 — Recall / Precision tracking.

RecallTracker scores agent results against GroundTruthCase definitions,
producing AgentMetrics (TP / FP / FN → precision / recall / F1) and
persisting results to a JSONL log so trends can be observed over time.

Matching rule
-------------
A finding "covers" a GroundTruthFinding when ALL of the truth's
match_hints appear (case-insensitive) inside the JSON-serialised text
of the finding.  This is deliberately strict — a single match_hint
like "open_ingress" is unambiguous enough; you add more only to
disambiguate within a noisy result set.

Finding extraction
------------------
The tracker tries the following attribute names in order:
  findings, taint_paths, breaking_changes, changes
If none are present the result is treated as having no findings.

Usage
-----
    tracker = RecallTracker()

    # Evaluate a single case against agent outputs
    result_map = {
        "secrets_entropy": agent.fallback_result(request),
        "security":        security_agent.fallback_result(request),
    }
    case_m = tracker.evaluate_case(case, result_map)
    print(case_m.overall_recall, case_m.overall_precision)

    # Evaluate all golden cases and persist
    metrics = tracker.evaluate_all(GOLDEN_CASES, result_maps)
    tracker.log(metrics)
    print(tracker.summary())
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Any

from evaluation.ground_truth import GroundTruthCase, GroundTruthFinding
from evaluation.metrics import AgentMetrics, CaseMetrics, EvalMetrics

log = logging.getLogger(__name__)

# Ordered list of attribute names to try when extracting findings from a result
_FINDING_ATTRS = ("findings", "taint_paths", "breaking_changes", "changes", "issues", "pii_findings")

_DEFAULT_LOG = Path(__file__).parent / "data" / "metrics_log.jsonl"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_findings(result: Any) -> list[Any]:
    """Return a flat list of findings from any agent result object."""
    for attr in _FINDING_ATTRS:
        val = getattr(result, attr, None)
        if val is not None:
            return list(val)
    return []


def _finding_to_text(finding: Any) -> str:
    """Serialise a finding to lowercase JSON for hint matching."""
    try:
        return json.dumps(finding.model_dump(mode="json"), ensure_ascii=False).lower()
    except Exception:
        return str(finding).lower()


def _covers(finding: Any, truth: GroundTruthFinding) -> bool:
    """Return True if all match_hints appear in the serialised finding text."""
    text = _finding_to_text(finding)
    return all(hint.lower() in text for hint in truth.match_hints)


# ── RecallTracker ─────────────────────────────────────────────────────────────

class RecallTracker:
    """
    Scores agent output against ground-truth cases and persists metrics.

    Parameters
    ----------
    log_path : path to the JSONL metrics log.
               Defaults to evaluation/data/metrics_log.jsonl.
               Pass None to disable persistence (useful in tests).
    """

    def __init__(self, log_path: Path | str | None = _DEFAULT_LOG) -> None:
        self._log_path: Path | None = Path(log_path) if log_path else None
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Core evaluation ───────────────────────────────────────────────────────

    def evaluate_case(
        self,
        case:       GroundTruthCase,
        result_map: dict[str, Any],
    ) -> CaseMetrics:
        """
        Score one case.

        Parameters
        ----------
        case       : the ground-truth specification
        result_map : {agent_name: result_object | None}
                     Missing keys are treated the same as None.
        """
        # Group expected findings by agent
        by_agent: dict[str, list[GroundTruthFinding]] = {}
        for gf in case.expected_findings:
            by_agent.setdefault(gf.agent, []).append(gf)

        agent_metrics: list[AgentMetrics] = []

        # --- Score agents that have at least one expected finding ---
        for agent_name, truths in by_agent.items():
            result = result_map.get(agent_name)
            actual = _extract_findings(result) if result is not None else []

            tp = sum(1 for t in truths if any(_covers(a, t) for a in actual))
            fn = len(truths) - tp
            # FP: actual findings that don't cover any expected finding
            fp = sum(1 for a in actual if not any(_covers(a, t) for t in truths))

            agent_metrics.append(AgentMetrics(
                agent_name=agent_name,
                true_positives=tp,
                false_positives=fp,
                false_negatives=fn,
            ))

        # --- For clean cases, count findings from all agents as FP ---
        if case.expected_clean:
            expected_agents = set(by_agent.keys())
            for agent_name, result in result_map.items():
                if agent_name in expected_agents or result is None:
                    continue
                actual = _extract_findings(result)
                if actual:
                    agent_metrics.append(AgentMetrics(
                        agent_name=agent_name,
                        true_positives=0,
                        false_positives=len(actual),
                        false_negatives=0,
                    ))

        return CaseMetrics(case_id=case.case_id, agent_metrics=agent_metrics)

    def evaluate_all(
        self,
        cases:       list[GroundTruthCase],
        result_maps: list[dict[str, Any]],
    ) -> EvalMetrics:
        """
        Evaluate a list of cases.

        result_maps[i] must correspond to cases[i].
        """
        if len(cases) != len(result_maps):
            raise ValueError(
                f"cases ({len(cases)}) and result_maps ({len(result_maps)}) must have the same length"
            )
        case_metrics = [
            self.evaluate_case(c, rm)
            for c, rm in zip(cases, result_maps)
        ]
        return EvalMetrics(cases=case_metrics)

    # ── Persistence ───────────────────────────────────────────────────────────

    def log(self, metrics: EvalMetrics) -> None:
        """Append an EvalMetrics run to the JSONL log file and check alert threshold."""
        if self._log_path is None:
            return
        with self._log_path.open("a") as f:
            f.write(metrics.model_dump_json() + "\n")
        self._check_alert(metrics)

    def _check_alert(self, metrics: EvalMetrics) -> None:
        """Emit warning + optional Slack alert when recall drops below the configured threshold."""
        try:
            from config.settings import get_settings
            threshold = get_settings().quality_recall_alert_threshold
        except Exception:
            return
        if threshold <= 0.0:
            return
        if metrics.overall_recall < threshold:
            log.warning(
                "QUALITY ALERT: overall_recall %.3f is below threshold %.3f  (eval_id=%s)",
                metrics.overall_recall, threshold, metrics.eval_id,
            )
            self._send_slack_alert(metrics, threshold)

    def _send_slack_alert(self, metrics: EvalMetrics, threshold: float) -> None:
        try:
            from config.settings import get_settings
            slack_url = get_settings().slack_webhook_url
        except Exception:
            return
        if not slack_url:
            return
        import urllib.request, json as _json
        body = _json.dumps({
            "text": (
                f":rotating_light: *Quality Alert* — recall *{metrics.overall_recall:.1%}* "
                f"is below threshold *{threshold:.1%}*\n"
                f"eval_id: `{metrics.eval_id}`  timestamp: `{metrics.timestamp}`"
            )
        }).encode()
        try:
            req = urllib.request.Request(
                slack_url, data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            log.warning("Failed to send Slack quality alert: %s", exc)

    def load_history(self) -> list[EvalMetrics]:
        """Load all previously persisted EvalMetrics runs in insertion order."""
        if self._log_path is None or not self._log_path.exists():
            return []
        runs: list[EvalMetrics] = []
        for line in self._log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(EvalMetrics.model_validate_json(line))
            except Exception as exc:
                log.warning("Skipping malformed metrics entry: %s", exc)
        return runs

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """
        Return a summary dict from the most recent logged run.

        Keys: runs, latest_eval_id, latest_timestamp,
              overall_recall, overall_precision,
              by_agent: {agent → {precision, recall, f1, tp, fp, fn}}
        """
        history = self.load_history()
        if not history:
            return {"runs": 0}
        latest = history[-1]
        by_agent = latest.by_agent()
        return {
            "runs":              len(history),
            "latest_eval_id":    latest.eval_id,
            "latest_timestamp":  latest.timestamp,
            "overall_recall":    latest.overall_recall,
            "overall_precision": latest.overall_precision,
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
        }

    def trend(self) -> list[dict]:
        """
        Return per-run overall_recall and overall_precision for charting.

        Each entry: {eval_id, timestamp, overall_recall, overall_precision}
        """
        return [
            {
                "eval_id":           r.eval_id,
                "timestamp":         r.timestamp,
                "overall_recall":    r.overall_recall,
                "overall_precision": r.overall_precision,
            }
            for r in self.load_history()
        ]
