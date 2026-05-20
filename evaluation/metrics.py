"""
evaluation/metrics.py
----------------------
Pydantic models for Level 2 recall / precision tracking.

Hierarchy:
  AgentMetrics   — TP/FP/FN for one agent in one case
  CaseMetrics    — all AgentMetrics for one GroundTruthCase run
  EvalMetrics    — a full evaluation run across all cases (persisted to log)
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel


class AgentMetrics(BaseModel):
    """Counts and derived metrics for one agent in one case."""
    agent_name:      str
    true_positives:  int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return round(self.true_positives / denom, 4) if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return round(self.true_positives / denom, 4) if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0


class CaseMetrics(BaseModel):
    """Aggregated metrics for a single GroundTruthCase evaluation."""
    case_id:       str
    agent_metrics: list[AgentMetrics] = []

    @property
    def overall_recall(self) -> float:
        tp = sum(m.true_positives  for m in self.agent_metrics)
        fn = sum(m.false_negatives for m in self.agent_metrics)
        return round(tp / (tp + fn), 4) if (tp + fn) else 1.0

    @property
    def overall_precision(self) -> float:
        tp = sum(m.true_positives  for m in self.agent_metrics)
        fp = sum(m.false_positives for m in self.agent_metrics)
        return round(tp / (tp + fp), 4) if (tp + fp) else 1.0

    def get_agent(self, agent_name: str) -> AgentMetrics | None:
        return next((m for m in self.agent_metrics if m.agent_name == agent_name), None)


class EvalMetrics(BaseModel):
    """
    One complete evaluation run — persisted as a single line in the JSONL log.

    Covers all cases passed to RecallTracker.evaluate_all().
    """
    eval_id:   str = ""
    timestamp: str = ""
    cases:     list[CaseMetrics] = []

    def model_post_init(self, __context) -> None:
        if not self.eval_id:
            self.eval_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    # ── Aggregate properties ──────────────────────────────────────────────────

    @property
    def overall_recall(self) -> float:
        tp = sum(sum(m.true_positives  for m in c.agent_metrics) for c in self.cases)
        fn = sum(sum(m.false_negatives for m in c.agent_metrics) for c in self.cases)
        return round(tp / (tp + fn), 4) if (tp + fn) else 1.0

    @property
    def overall_precision(self) -> float:
        tp = sum(sum(m.true_positives  for m in c.agent_metrics) for c in self.cases)
        fp = sum(sum(m.false_positives for m in c.agent_metrics) for c in self.cases)
        return round(tp / (tp + fp), 4) if (tp + fp) else 1.0

    def by_agent(self) -> dict[str, AgentMetrics]:
        """
        Aggregate TP/FP/FN per agent across all cases.
        Returns a dict keyed by agent_name.
        """
        totals: dict[str, AgentMetrics] = {}
        for case in self.cases:
            for m in case.agent_metrics:
                if m.agent_name not in totals:
                    totals[m.agent_name] = AgentMetrics(agent_name=m.agent_name)
                agg = totals[m.agent_name]
                agg.true_positives  += m.true_positives
                agg.false_positives += m.false_positives
                agg.false_negatives += m.false_negatives
        return totals

    def get_case(self, case_id: str) -> CaseMetrics | None:
        return next((c for c in self.cases if c.case_id == case_id), None)
