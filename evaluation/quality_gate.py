"""
evaluation/quality_gate.py
---------------------------
Continuous-eval quality gate: runs every golden case through the agents'
deterministic (static-fallback) paths and FAILS when detection quality regresses
against a checked-in baseline.

This is what turns the golden cases + recall tracker from a dashboard into a
regression gate: any change to an agent, prompt-adjacent static rule, or parser
that lowers recall/precision breaks CI instead of silently shipping.

Deterministic by design — no LLM call, no API key needed — so it runs anywhere
(developer laptop, locked-down CI runner).

Baseline file: evaluation/data/quality_baseline.json
  {
    "overall_recall": 1.0, "overall_precision": 0.92,
    "by_agent": {"security": {"recall": 1.0, "precision": 1.0}, ...}
  }

Usage:
  from evaluation.quality_gate import run_gate
  verdict = run_gate()           # -> GateVerdict(passed=..., failures=[...])

  python scripts/quality_gate.py                   # CI: exit 1 on regression
  python scripts/quality_gate.py --update-baseline # accept current as baseline
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent / "data" / "quality_baseline.json"

# Tolerance keeps the gate from flapping on float jitter while still catching
# any real regression (one lost TP on this corpus moves recall by >= ~0.04).
_TOLERANCE = 0.005


@dataclass
class GateVerdict:
    passed:            bool
    overall_recall:    float
    overall_precision: float
    by_agent:          dict
    failures:          list[str] = field(default_factory=list)
    baseline:          dict | None = None


def _current_metrics() -> tuple[float, float, dict]:
    """Run all golden cases through the static-fallback paths and score them."""
    from evaluation.ground_truth import GOLDEN_CASES
    from evaluation.recall_tracker import RecallTracker
    from api.routes.quality import _CASE_MAP, _build_result_map

    tracker = RecallTracker(log_path=None)   # no persistence — pure evaluation
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
    by_agent = {
        name: {"recall": round(m.recall, 4), "precision": round(m.precision, 4),
               "tp": m.true_positives, "fp": m.false_positives, "fn": m.false_negatives}
        for name, m in sorted(metrics.by_agent().items())
    }
    return round(metrics.overall_recall, 4), round(metrics.overall_precision, 4), by_agent


def load_baseline(path: Path | str = BASELINE_PATH) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError) as exc:
        log.warning("Quality baseline unreadable (%s) — gate will only enforce floors", exc)
        return None


def write_baseline(path: Path | str = BASELINE_PATH) -> dict:
    """Snapshot current metrics as the new accepted baseline."""
    recall, precision, by_agent = _current_metrics()
    data = {"overall_recall": recall, "overall_precision": precision, "by_agent": by_agent}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
    return data


def run_gate(baseline_path: Path | str = BASELINE_PATH,
             min_recall: float = 0.7, min_precision: float = 0.7) -> GateVerdict:
    """Evaluate current quality vs the baseline (and absolute floors).

    Fails when:
      • overall recall/precision drops below the baseline (beyond tolerance), or
      • any agent present in the baseline regresses on recall, or
      • metrics fall under the absolute floors (safety net if baseline is missing).
    """
    recall, precision, by_agent = _current_metrics()
    baseline = load_baseline(baseline_path)
    failures: list[str] = []

    if recall < min_recall:
        failures.append(f"overall recall {recall:.3f} below absolute floor {min_recall}")
    if precision < min_precision:
        failures.append(f"overall precision {precision:.3f} below absolute floor {min_precision}")

    if baseline:
        if recall + _TOLERANCE < baseline.get("overall_recall", 0):
            failures.append(
                f"overall recall regressed: {recall:.3f} < baseline {baseline['overall_recall']:.3f}")
        if precision + _TOLERANCE < baseline.get("overall_precision", 0):
            failures.append(
                f"overall precision regressed: {precision:.3f} < baseline {baseline['overall_precision']:.3f}")
        for name, base in (baseline.get("by_agent") or {}).items():
            cur = by_agent.get(name)
            if cur is None:
                failures.append(f"agent '{name}' produced no metrics (was in baseline)")
                continue
            if cur["recall"] + _TOLERANCE < base.get("recall", 0):
                failures.append(
                    f"{name}: recall regressed {cur['recall']:.3f} < baseline {base['recall']:.3f}")

    return GateVerdict(
        passed=not failures,
        overall_recall=recall,
        overall_precision=precision,
        by_agent=by_agent,
        failures=failures,
        baseline=baseline,
    )
