"""
evaluation/models.py
---------------------
Pydantic models for the LLM-as-judge evaluation framework.

Hierarchy:
  JudgeScore      — one LLM judge's rating of one agent's output
  PanelVerdict    — consolidated output from all judges for one agent
  EvaluationReport — full evaluation across multiple agents of a report
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel


class JudgeScore(BaseModel):
    """
    Evaluation produced by a single LLM judge for one agent's output.

    Rubric dimensions (all 1–5):
      completeness      — did the agent find all real issues?
      precision         — are the reported findings actually real (low FP rate)?
      severity_accuracy — are severity ratings correctly calibrated?
      specificity       — are findings actionable with file/line/fix details?
    """
    judge_id:          str   # "provider/model", e.g. "anthropic/claude-sonnet-4-6"
    agent_name:        str

    # Rubric scores (integer 1–5)
    completeness:      int
    precision:         int
    severity_accuracy: int
    specificity:       int

    # Qualitative findings
    missed_issues:   list[str]  # real issues the agent should have found
    false_positives: list[str]  # reported findings that appear incorrect

    verdict:   Literal["PASS", "PARTIAL", "FAIL"]
    reasoning: str   # 2–3 sentence explanation
    key_insight: str = ""  # single most important observation

    # Metadata
    duration_s:    float = 0.0
    fallback_used: bool  = False  # True if the judge couldn't produce valid output

    @property
    def avg_score(self) -> float:
        return (self.completeness + self.precision + self.severity_accuracy + self.specificity) / 4


class PanelVerdict(BaseModel):
    """
    Consolidated verdict produced by aggregating all judges for one agent.

    Scores are averaged across valid (non-fallback) judges.
    Missed issues / false positives are collected with a consensus threshold.
    """
    agent_name:  str
    judge_count: int

    # Averaged rubric scores (1.0 – 5.0)
    avg_completeness:      float
    avg_precision:         float
    avg_severity_accuracy: float
    avg_specificity:       float
    overall_score:         float  # unweighted mean of the four dimensions

    # Consolidated verdict
    verdict:    Literal["PASS", "PARTIAL", "FAIL"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]   # agreement among judges

    # Qualitative findings (union / consensus from all judges)
    all_missed:                list[str]  # flagged by any judge
    consensus_missed:          list[str]  # flagged by >= 50% of judges
    all_false_positives:       list[str]
    consensus_false_positives: list[str]

    # Divergence indicators
    disagreements: list[str]  # dimensions where judges differed by > 1.5 pts
    summary:       str        # human-readable consolidated explanation

    # Raw per-judge data
    judge_scores: list[JudgeScore]


class EvaluationReport(BaseModel):
    """
    Full evaluation of an AnalysisReport — one PanelVerdict per evaluated agent.
    """
    request_id:  str
    repo_url:    str
    source_ref:  str
    target_ref:  str
    panel_size:  int   # number of judges in the panel

    agent_verdicts:  list[PanelVerdict]

    # Roll-up
    overall_verdict: Literal["PASS", "PARTIAL", "FAIL"]
    overall_score:   float   # mean of per-agent overall_score values
    critical_gaps:   list[str]  # consensus-missed items across all agents

    completed_at: str  # ISO-8601 UTC timestamp
