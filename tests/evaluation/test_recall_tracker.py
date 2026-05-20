"""
tests/evaluation/test_recall_tracker.py
-----------------------------------------
Unit tests for Level 2 recall / precision tracking.

All tests are offline — no LLM calls, no filesystem writes (tracker
is initialised with log_path=None).

Coverage:
  - _covers()           : match hint matching (AND semantics, case-insensitive)
  - _extract_findings() : attribute discovery across different result types
  - AgentMetrics        : precision / recall / F1 arithmetic
  - CaseMetrics         : overall precision / recall rollup
  - EvalMetrics         : by_agent() aggregation, cross-case rollup
  - RecallTracker.evaluate_case() : TP / FP / FN counting
  - RecallTracker.evaluate_all()  : multi-case aggregation
  - Persistence         : log / load_history round-trip
  - Edge cases          : no results, clean case, partial match
"""
from __future__ import annotations
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from evaluation.ground_truth import (
    GroundTruthCase,
    GroundTruthFinding,
    GOLDEN_CASES,
    get_case,
)
from evaluation.metrics import AgentMetrics, CaseMetrics, EvalMetrics
from evaluation.recall_tracker import (
    RecallTracker,
    _covers,
    _extract_findings,
    _finding_to_text,
)


# ── Test helpers ──────────────────────────────────────────────────────────────

def _mock_finding(**fields) -> MagicMock:
    """Build a mock finding object whose model_dump returns fields."""
    m = MagicMock()
    m.model_dump.return_value = fields
    return m


def _simple_case(
    agent: str,
    hints: list[str],
    expected_clean: bool = False,
) -> GroundTruthCase:
    return GroundTruthCase(
        case_id=str(uuid.uuid4()),
        description="test",
        diff_text="",
        expected_findings=[] if expected_clean else [
            GroundTruthFinding(agent=agent, category="cat", match_hints=hints)
        ],
        expected_clean=expected_clean,
    )


def _tracker() -> RecallTracker:
    """Tracker with no log path — no filesystem side-effects."""
    return RecallTracker(log_path=None)


# ═══════════════════════════════════════════════════════════════════════════════
#  _covers / _extract_findings / _finding_to_text
# ═══════════════════════════════════════════════════════════════════════════════

class TestMatchHelpers:

    def test_single_hint_matches(self):
        f = _mock_finding(kind="known_prefix", value="AKIAZ3...")
        truth = GroundTruthFinding(agent="a", category="c", match_hints=["known_prefix"])
        assert _covers(f, truth)

    def test_all_hints_must_match(self):
        f = _mock_finding(kind="known_prefix")
        truth = GroundTruthFinding(agent="a", category="c", match_hints=["known_prefix", "absent_key"])
        assert not _covers(f, truth)

    def test_hints_are_case_insensitive(self):
        f = _mock_finding(description="SQL Injection detected")
        truth = GroundTruthFinding(agent="a", category="c", match_hints=["sql injection"])
        assert _covers(f, truth)

    def test_nested_dict_serialised(self):
        # TaintPath has a nested TaintSink; model_dump returns nested dicts
        f = _mock_finding(sink={"sink": "sql_query", "variable": "q"})
        truth = GroundTruthFinding(agent="a", category="c", match_hints=["sql_query"])
        assert _covers(f, truth)

    def test_no_hints_always_matches(self):
        f = _mock_finding(some="thing")
        truth = GroundTruthFinding(agent="a", category="c", match_hints=[])
        assert _covers(f, truth)

    def test_extract_findings_attr(self):
        result = MagicMock()
        result.findings = [_mock_finding(x=1), _mock_finding(x=2)]
        del result.taint_paths  # ensure attribute error falls through
        result.taint_paths = None
        assert len(_extract_findings(result)) == 2

    def test_extract_taint_paths(self):
        result = MagicMock(spec=["taint_paths"])
        result.taint_paths = [_mock_finding(cwe="CWE-89")]
        assert len(_extract_findings(result)) == 1

    def test_extract_breaking_changes(self):
        result = MagicMock(spec=["breaking_changes"])
        result.breaking_changes = [_mock_finding(break_type="removed")]
        assert len(_extract_findings(result)) == 1

    def test_extract_changes(self):
        result = MagicMock(spec=["changes"])
        result.changes = [_mock_finding(change_type="drop_table")]
        assert len(_extract_findings(result)) == 1

    def test_extract_returns_empty_when_no_known_attr(self):
        result = MagicMock(spec=[])   # no findings-like attr
        assert _extract_findings(result) == []

    def test_finding_to_text_lowercases(self):
        f = _mock_finding(key="AWS_ACCESS_KEY_ID", value="AKIAZ3...")
        text = _finding_to_text(f)
        assert "aws_access_key_id" in text
        assert "akiaz3" in text

    def test_finding_to_text_fallback_on_no_model_dump(self):
        class BadFinding:
            def __str__(self):
                return "PLAIN TEXT"
        text = _finding_to_text(BadFinding())
        assert "plain text" in text


# ═══════════════════════════════════════════════════════════════════════════════
#  AgentMetrics arithmetic
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentMetrics:

    def test_perfect_precision_and_recall(self):
        m = AgentMetrics(agent_name="x", true_positives=3, false_positives=0, false_negatives=0)
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.f1 == 1.0

    def test_zero_precision_when_all_false_positives(self):
        m = AgentMetrics(agent_name="x", true_positives=0, false_positives=3, false_negatives=0)
        assert m.precision == 0.0
        assert m.recall == 1.0   # no FN, nothing expected
        assert m.f1 == 0.0

    def test_zero_recall_when_all_false_negatives(self):
        m = AgentMetrics(agent_name="x", true_positives=0, false_positives=0, false_negatives=2)
        assert m.precision == 1.0
        assert m.recall == 0.0
        assert m.f1 == 0.0

    def test_partial_precision_recall(self):
        # TP=2, FP=2, FN=1 → precision=0.5, recall=0.667
        m = AgentMetrics(agent_name="x", true_positives=2, false_positives=2, false_negatives=1)
        assert m.precision == 0.5
        assert abs(m.recall - round(2/3, 4)) < 1e-3
        assert 0 < m.f1 < 1

    def test_no_findings_no_expected_gives_perfect_scores(self):
        m = AgentMetrics(agent_name="x")
        assert m.precision == 1.0
        assert m.recall == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
#  CaseMetrics rollup
# ═══════════════════════════════════════════════════════════════════════════════

class TestCaseMetrics:

    def test_overall_recall_across_agents(self):
        # Two agents: one perfect, one total miss
        c = CaseMetrics(case_id="x", agent_metrics=[
            AgentMetrics(agent_name="a", true_positives=1, false_positives=0, false_negatives=0),
            AgentMetrics(agent_name="b", true_positives=0, false_positives=0, false_negatives=1),
        ])
        # 1 TP, 1 FN → recall = 0.5
        assert c.overall_recall == 0.5

    def test_overall_precision_across_agents(self):
        c = CaseMetrics(case_id="x", agent_metrics=[
            AgentMetrics(agent_name="a", true_positives=2, false_positives=1, false_negatives=0),
        ])
        assert c.overall_precision == round(2/3, 4)

    def test_get_agent_found(self):
        m = AgentMetrics(agent_name="security", true_positives=1)
        c = CaseMetrics(case_id="x", agent_metrics=[m])
        assert c.get_agent("security") is m

    def test_get_agent_missing_returns_none(self):
        c = CaseMetrics(case_id="x")
        assert c.get_agent("missing") is None


# ═══════════════════════════════════════════════════════════════════════════════
#  EvalMetrics aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvalMetrics:

    def _make_eval(self) -> EvalMetrics:
        return EvalMetrics(cases=[
            CaseMetrics(case_id="c1", agent_metrics=[
                AgentMetrics(agent_name="security", true_positives=2, false_positives=1, false_negatives=0),
                AgentMetrics(agent_name="ast_analysis", true_positives=1, false_positives=0, false_negatives=1),
            ]),
            CaseMetrics(case_id="c2", agent_metrics=[
                AgentMetrics(agent_name="security", true_positives=1, false_positives=0, false_negatives=0),
            ]),
        ])

    def test_by_agent_aggregates_tp_fp_fn(self):
        e = self._make_eval()
        by = e.by_agent()
        sec = by["security"]
        assert sec.true_positives == 3
        assert sec.false_positives == 1
        assert sec.false_negatives == 0

    def test_by_agent_separate_agents(self):
        e = self._make_eval()
        by = e.by_agent()
        assert "ast_analysis" in by
        assert by["ast_analysis"].false_negatives == 1

    def test_overall_recall_cross_case(self):
        e = self._make_eval()
        # total TP=4, FN=1 → recall = 4/5
        assert e.overall_recall == round(4/5, 4)

    def test_overall_precision_cross_case(self):
        e = self._make_eval()
        # total TP=4, FP=1 → precision = 4/5
        assert e.overall_precision == round(4/5, 4)

    def test_eval_id_and_timestamp_auto_populated(self):
        e = EvalMetrics()
        assert e.eval_id
        assert e.timestamp

    def test_get_case_found(self):
        e = self._make_eval()
        assert e.get_case("c1") is not None

    def test_get_case_missing(self):
        e = self._make_eval()
        assert e.get_case("nope") is None


# ═══════════════════════════════════════════════════════════════════════════════
#  RecallTracker.evaluate_case()
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateCase:

    def test_true_positive_counted(self):
        case = _simple_case("security", ["sql_injection"])
        finding = _mock_finding(description="SQL_injection in execute()")
        result_map = {"security": MagicMock(findings=[finding])}
        cm = _tracker().evaluate_case(case, result_map)
        m = cm.get_agent("security")
        assert m.true_positives == 1
        assert m.false_negatives == 0

    def test_false_negative_when_no_result(self):
        case = _simple_case("security", ["sql_injection"])
        cm = _tracker().evaluate_case(case, {})
        m = cm.get_agent("security")
        assert m.false_negatives == 1
        assert m.true_positives == 0

    def test_false_negative_when_result_is_none(self):
        case = _simple_case("security", ["cwe-89"])
        cm = _tracker().evaluate_case(case, {"security": None})
        m = cm.get_agent("security")
        assert m.false_negatives == 1

    def test_false_positive_for_unmatched_finding(self):
        case = _simple_case("security", ["cwe-89"])
        # Finding doesn't contain the hint
        bad_finding = _mock_finding(description="unrelated noise")
        result = MagicMock(findings=[bad_finding])
        cm = _tracker().evaluate_case(case, {"security": result})
        m = cm.get_agent("security")
        assert m.false_positives == 1
        assert m.false_negatives == 1   # ground truth not matched

    def test_multiple_findings_one_match(self):
        case = _simple_case("security", ["cwe-89"])
        matched   = _mock_finding(cwe_id="CWE-89", description="SQL injection")
        unmatched = _mock_finding(description="unrelated info message")
        result = MagicMock(findings=[matched, unmatched])
        cm = _tracker().evaluate_case(case, {"security": result})
        m = cm.get_agent("security")
        assert m.true_positives == 1
        assert m.false_positives == 1
        assert m.false_negatives == 0

    def test_clean_case_any_finding_is_fp(self):
        case = _simple_case("security", [], expected_clean=True)
        noise = _mock_finding(description="noise")
        result = MagicMock(findings=[noise])
        cm = _tracker().evaluate_case(case, {"security": result})
        m = cm.get_agent("security")
        assert m.false_positives == 1
        assert m.true_positives == 0

    def test_clean_case_no_findings_means_no_metrics(self):
        case = _simple_case("security", [], expected_clean=True)
        result = MagicMock(findings=[])
        cm = _tracker().evaluate_case(case, {"security": result})
        # No agent metric created when there are no findings on a clean case
        assert cm.get_agent("security") is None

    def test_perfect_recall_and_precision(self):
        case = _simple_case("iac_analysis", ["open_ingress"])
        f = _mock_finding(kind="open_ingress", severity="critical")
        result = MagicMock(findings=[f])
        cm = _tracker().evaluate_case(case, {"iac_analysis": result})
        m = cm.get_agent("iac_analysis")
        assert m.recall == 1.0
        assert m.precision == 1.0
        assert m.f1 == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
#  RecallTracker.evaluate_all()
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateAll:

    def test_produces_one_case_per_input(self):
        c1 = _simple_case("security", ["x"])
        c2 = _simple_case("ast_analysis", ["y"])
        tracker = _tracker()
        metrics = tracker.evaluate_all([c1, c2], [{}, {}])
        assert len(metrics.cases) == 2

    def test_raises_on_mismatched_lengths(self):
        with pytest.raises(ValueError, match="same length"):
            _tracker().evaluate_all([_simple_case("a", [])], [{}, {}])

    def test_by_agent_aggregates_across_cases(self):
        c1 = _simple_case("security", ["cwe-89"])
        c2 = _simple_case("security", ["drop_table"])
        matched = _mock_finding(cwe_id="CWE-89")
        not_matched = _mock_finding(kind="drop_table")
        r1 = {"security": MagicMock(findings=[matched])}
        r2 = {"security": MagicMock(findings=[not_matched])}
        metrics = _tracker().evaluate_all([c1, c2], [r1, r2])
        by = metrics.by_agent()
        assert by["security"].true_positives == 2


# ═══════════════════════════════════════════════════════════════════════════════
#  Persistence: log / load_history
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistence:

    @pytest.fixture
    def tmp_tracker(self, tmp_path):
        return RecallTracker(log_path=tmp_path / "metrics.jsonl")

    def test_log_creates_file(self, tmp_tracker, tmp_path):
        tmp_tracker.log(EvalMetrics())
        assert (tmp_path / "metrics.jsonl").exists()

    def test_load_history_empty_when_no_file(self, tmp_path):
        tracker = RecallTracker(log_path=tmp_path / "nonexistent.jsonl")
        assert tracker.load_history() == []

    def test_round_trip_single_run(self, tmp_tracker):
        original = EvalMetrics(cases=[
            CaseMetrics(case_id="x", agent_metrics=[
                AgentMetrics(agent_name="security", true_positives=1)
            ])
        ])
        tmp_tracker.log(original)
        history = tmp_tracker.load_history()
        assert len(history) == 1
        loaded = history[0]
        assert loaded.eval_id == original.eval_id
        assert loaded.cases[0].case_id == "x"
        assert loaded.cases[0].agent_metrics[0].true_positives == 1

    def test_multiple_runs_appended(self, tmp_tracker):
        tmp_tracker.log(EvalMetrics())
        tmp_tracker.log(EvalMetrics())
        assert len(tmp_tracker.load_history()) == 2

    def test_summary_returns_latest_run(self, tmp_tracker):
        run1 = EvalMetrics(cases=[CaseMetrics(case_id="c", agent_metrics=[
            AgentMetrics(agent_name="a", true_positives=1, false_positives=1)
        ])])
        run2 = EvalMetrics(cases=[CaseMetrics(case_id="c", agent_metrics=[
            AgentMetrics(agent_name="a", true_positives=2, false_positives=0)
        ])])
        tmp_tracker.log(run1)
        tmp_tracker.log(run2)
        s = tmp_tracker.summary()
        assert s["runs"] == 2
        assert s["latest_eval_id"] == run2.eval_id
        # run2: TP=2, FP=0 → precision=1.0
        assert s["overall_precision"] == 1.0

    def test_trend_returns_per_run_scores(self, tmp_tracker):
        tmp_tracker.log(EvalMetrics())
        tmp_tracker.log(EvalMetrics())
        trend = tmp_tracker.trend()
        assert len(trend) == 2
        assert all("overall_recall" in t for t in trend)
        assert all("overall_precision" in t for t in trend)

    def test_log_none_path_does_not_raise(self):
        tracker = RecallTracker(log_path=None)
        tracker.log(EvalMetrics())   # should silently no-op


# ═══════════════════════════════════════════════════════════════════════════════
#  Ground-truth catalogue sanity checks
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroundTruthCatalogue:

    def test_golden_cases_has_eight_entries(self):
        assert len(GOLDEN_CASES) == 8

    def test_all_case_ids_are_unique(self):
        ids = [c.case_id for c in GOLDEN_CASES]
        assert len(ids) == len(set(ids))

    def test_get_case_returns_correct_entry(self):
        c = get_case("sql_injection")
        assert c is not None
        assert any(gf.agent == "security" for gf in c.expected_findings)

    def test_get_case_returns_none_for_unknown(self):
        assert get_case("nonexistent") is None

    def test_clean_refactor_has_no_expected_findings(self):
        c = get_case("clean_refactor")
        assert c.expected_clean is True
        assert c.expected_findings == []

    def test_all_non_clean_cases_have_at_least_one_finding(self):
        for c in GOLDEN_CASES:
            if not c.expected_clean:
                assert c.expected_findings, f"{c.case_id} has no expected findings"

    def test_all_diff_texts_are_non_empty(self):
        for c in GOLDEN_CASES:
            assert c.diff_text.strip(), f"{c.case_id} has empty diff_text"
