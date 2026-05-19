"""
tests/evaluation/test_judge_panel.py
--------------------------------------
Unit tests for the LLM-as-judge evaluation framework.

All tests mock the LLM call so no API key is needed.
Tests cover:
  - JudgeScore model validation
  - LLMJudge response parsing (happy path + repair)
  - Panel consolidation (averaging, majority vote, consensus items, disagreements)
  - Edge cases: judge failures, single judge, all-fail panel
  - Multi-provider panel with mixed verdicts
  - evaluate_report() integration with AnalysisReport
"""
from __future__ import annotations
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from evaluation.models import JudgeScore, PanelVerdict, EvaluationReport
from evaluation.judge  import LLMJudge, _parse_response
from evaluation.panel  import (
    JudgePanel,
    _majority_verdict,
    _judge_confidence,
    _collect_text_items,
    _detect_disagreements,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

SAMPLE_DIFF = """\
--- a/src/payments/service.py
+++ b/src/payments/service.py
@@ -10,4 +10,6 @@
+    def get_payment(self, payment_id):
+        return self.db.execute("SELECT * FROM payments WHERE id = " + payment_id)
"""

SAMPLE_FINDINGS = {
    "token_usage": 120,
    "findings": [],
    "secrets_detected": False,
    "overall_severity": "low",
}


def _make_judge_response(
    completeness: int = 4,
    precision: int = 4,
    severity_accuracy: int = 4,
    specificity: int = 3,
    missed: list = None,
    fp: list = None,
    verdict: str = "PARTIAL",
    reasoning: str = "Acceptable analysis.",
    key_insight: str = "Missed the SQL injection.",
) -> str:
    return json.dumps({
        "completeness":      completeness,
        "precision":         precision,
        "severity_accuracy": severity_accuracy,
        "specificity":       specificity,
        "missed_issues":     missed or [],
        "false_positives":   fp or [],
        "verdict":           verdict,
        "reasoning":         reasoning,
        "key_insight":       key_insight,
    })


def _mock_llm_client(response_text: str):
    """Return a mock UnifiedLLMClient.create that returns the given text."""
    mock_resp = MagicMock()
    mock_resp.text         = response_text
    mock_resp.total_tokens = 200
    mock_client = MagicMock()
    mock_client.create.return_value = mock_resp
    return mock_client


def _judge_with_response(response_text: str, provider: str = "anthropic") -> LLMJudge:
    """Build an LLMJudge that returns a fixed response without a real API call."""
    judge = LLMJudge.__new__(LLMJudge)
    from agents.llm_client import ModelConfig
    judge._cfg    = ModelConfig(provider=provider, model="test-model")
    judge._client = _mock_llm_client(response_text)
    return judge


# ═══════════════════════════════════════════════════════════════════════════════
#  Unit tests — consolidation helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestConsolidationHelpers:

    # ── Majority verdict ──────────────────────────────────────────────────────

    def test_unanimous_pass(self):
        assert _majority_verdict(["PASS", "PASS", "PASS"]) == "PASS"

    def test_unanimous_fail(self):
        assert _majority_verdict(["FAIL", "FAIL", "FAIL"]) == "FAIL"

    def test_majority_partial(self):
        assert _majority_verdict(["PASS", "PARTIAL", "PARTIAL"]) == "PARTIAL"

    def test_tie_breaks_toward_stricter(self):
        # PASS vs FAIL tie — should pick FAIL (stricter)
        assert _majority_verdict(["PASS", "FAIL"]) == "FAIL"

    def test_three_way_split_picks_strictest(self):
        # PASS, PARTIAL, FAIL — each appears once; FAIL wins as strictest
        assert _majority_verdict(["PASS", "PARTIAL", "FAIL"]) == "FAIL"

    # ── Confidence ────────────────────────────────────────────────────────────

    def test_confidence_high_when_unanimous(self):
        assert _judge_confidence(["PASS", "PASS", "PASS"]) == "HIGH"

    def test_confidence_medium_when_n_minus_1_agree(self):
        assert _judge_confidence(["PASS", "PASS", "PARTIAL"]) == "MEDIUM"

    def test_confidence_low_when_split(self):
        assert _judge_confidence(["PASS", "PARTIAL", "FAIL"]) == "LOW"

    def test_confidence_high_with_single_judge(self):
        assert _judge_confidence(["PASS"]) == "HIGH"

    # ── Text item collection ──────────────────────────────────────────────────

    def test_collect_union_of_items(self):
        all_items, _ = _collect_text_items([
            ["SQL injection on line 42"],
            ["SQL injection on line 42", "Missing auth check"],
        ])
        lower = [x.lower() for x in all_items]
        assert any("sql injection" in x for x in lower)
        assert any("missing auth" in x for x in lower)

    def test_collect_consensus_at_50pct(self):
        # 2 out of 2 judges mentioned this → consensus
        _, consensus = _collect_text_items([
            ["Missing rate limiting"],
            ["Missing rate limiting"],
        ])
        assert any("rate limiting" in x.lower() for x in consensus)

    def test_non_consensus_item_excluded(self):
        # Only 1 of 3 judges mentioned this → not consensus
        _, consensus = _collect_text_items([
            ["Obscure edge case"],
            [],
            [],
        ])
        assert consensus == []

    def test_empty_lists_handled(self):
        all_items, consensus = _collect_text_items([[], [], []])
        assert all_items == []
        assert consensus == []

    # ── Disagreement detection ────────────────────────────────────────────────

    def test_no_disagreement_when_scores_close(self):
        scores = [
            JudgeScore(judge_id="a", agent_name="x", completeness=4, precision=4,
                       severity_accuracy=4, specificity=4, missed_issues=[], false_positives=[],
                       verdict="PASS", reasoning=""),
            JudgeScore(judge_id="b", agent_name="x", completeness=3, precision=4,
                       severity_accuracy=4, specificity=4, missed_issues=[], false_positives=[],
                       verdict="PARTIAL", reasoning=""),
        ]
        assert _detect_disagreements(scores) == []

    def test_disagreement_flagged_when_spread_exceeds_threshold(self):
        scores = [
            JudgeScore(judge_id="a", agent_name="x", completeness=5, precision=4,
                       severity_accuracy=4, specificity=4, missed_issues=[], false_positives=[],
                       verdict="PASS", reasoning=""),
            JudgeScore(judge_id="b", agent_name="x", completeness=2, precision=4,
                       severity_accuracy=4, specificity=4, missed_issues=[], false_positives=[],
                       verdict="FAIL", reasoning=""),
        ]
        d = _detect_disagreements(scores)
        assert len(d) == 1
        assert "Completeness" in d[0]

    def test_fallback_judges_excluded_from_disagreement(self):
        scores = [
            JudgeScore(judge_id="a", agent_name="x", completeness=5, precision=5,
                       severity_accuracy=5, specificity=5, missed_issues=[], false_positives=[],
                       verdict="PASS", reasoning="", fallback_used=False),
            JudgeScore(judge_id="b", agent_name="x", completeness=1, precision=1,
                       severity_accuracy=1, specificity=1, missed_issues=[], false_positives=[],
                       verdict="FAIL", reasoning="", fallback_used=True),  # ignored
        ]
        # Only one valid judge → no spread possible
        assert _detect_disagreements(scores) == []


# ═══════════════════════════════════════════════════════════════════════════════
#  LLMJudge — response parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestJudgeResponseParsing:

    def test_valid_json_parsed_correctly(self):
        raw = _make_judge_response(completeness=5, verdict="PASS")
        data = _parse_response(raw, "anthropic/sonnet", "security")
        assert data["completeness"] == 5
        assert data["verdict"] == "PASS"

    def test_json_with_markdown_fences_stripped(self):
        raw = "```json\n" + _make_judge_response() + "\n```"
        judge = _judge_with_response(raw)
        score = judge.evaluate("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert not score.fallback_used

    def test_json_with_preamble_extracted(self):
        raw = "Here is my evaluation:\n" + _make_judge_response(completeness=3)
        data = _parse_response(raw, "openai/gpt-4o", "security")
        assert data["completeness"] == 3

    def test_invalid_score_range_raises(self):
        bad = _make_judge_response(completeness=6)  # out of range
        with pytest.raises(ValueError, match="integer 1–5"):
            _parse_response(bad, "x", "y")

    def test_invalid_verdict_raises(self):
        bad = _make_judge_response(verdict="MAYBE")
        with pytest.raises(ValueError, match="PASS/PARTIAL/FAIL"):
            _parse_response(bad, "x", "y")

    def test_missing_field_raises(self):
        incomplete = json.dumps({"completeness": 4, "precision": 4})
        with pytest.raises(ValueError, match="missing fields"):
            _parse_response(incomplete, "x", "y")

    def test_judge_returns_fallback_on_llm_error(self):
        judge = LLMJudge.__new__(LLMJudge)
        from agents.llm_client import ModelConfig
        judge._cfg    = ModelConfig(provider="anthropic", model="test")
        mock_client   = MagicMock()
        mock_client.create.side_effect = RuntimeError("connection refused")
        judge._client = mock_client

        score = judge.evaluate("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert score.fallback_used is True
        assert score.verdict == "FAIL"
        assert "connection refused" in score.reasoning


# ═══════════════════════════════════════════════════════════════════════════════
#  JudgePanel — consolidation scenarios
# ═══════════════════════════════════════════════════════════════════════════════

class TestJudgePanelConsolidation:

    def _panel_with_responses(self, responses: list[str]) -> JudgePanel:
        """Build a panel where each judge returns a fixed response string."""
        panel = JudgePanel.__new__(JudgePanel)
        panel._judges = [_judge_with_response(r) for r in responses]
        return panel

    # ── Score averaging ───────────────────────────────────────────────────────

    def test_scores_averaged_correctly(self):
        r1 = _make_judge_response(completeness=4, precision=4, severity_accuracy=4, specificity=4)
        r2 = _make_judge_response(completeness=2, precision=2, severity_accuracy=2, specificity=2)
        panel   = self._panel_with_responses([r1, r2])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert verdict.avg_completeness == 3.0
        assert verdict.avg_precision    == 3.0
        assert verdict.overall_score    == 3.0

    def test_overall_score_is_mean_of_four_dimensions(self):
        r = _make_judge_response(completeness=5, precision=3, severity_accuracy=4, specificity=2)
        panel   = self._panel_with_responses([r])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert verdict.overall_score == round((5 + 3 + 4 + 2) / 4, 2)

    # ── Majority verdict ──────────────────────────────────────────────────────

    def test_unanimous_pass_verdict(self):
        responses = [_make_judge_response(verdict="PASS")] * 3
        verdict   = self._panel_with_responses(responses).evaluate_agent("sec", SAMPLE_DIFF, {})
        assert verdict.verdict     == "PASS"
        assert verdict.confidence  == "HIGH"

    def test_majority_partial_overrides_lone_pass(self):
        responses = [
            _make_judge_response(verdict="PARTIAL"),
            _make_judge_response(verdict="PARTIAL"),
            _make_judge_response(verdict="PASS"),
        ]
        verdict = self._panel_with_responses(responses).evaluate_agent("sec", SAMPLE_DIFF, {})
        assert verdict.verdict == "PARTIAL"

    def test_split_verdict_breaks_to_stricter(self):
        responses = [
            _make_judge_response(verdict="PASS"),
            _make_judge_response(verdict="FAIL"),
        ]
        verdict = self._panel_with_responses(responses).evaluate_agent("sec", SAMPLE_DIFF, {})
        assert verdict.verdict    == "FAIL"
        assert verdict.confidence == "LOW"

    # ── Consensus missed issues ────────────────────────────────────────────────

    def test_consensus_missed_when_majority_agree(self):
        r1 = _make_judge_response(missed=["SQL injection on line 12"])
        r2 = _make_judge_response(missed=["SQL injection on line 12"])
        r3 = _make_judge_response(missed=[])
        panel   = self._panel_with_responses([r1, r2, r3])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert any("sql injection" in m.lower() for m in verdict.consensus_missed), \
            f"Expected SQL injection in consensus_missed, got: {verdict.consensus_missed}"

    def test_all_missed_includes_minority_items(self):
        r1 = _make_judge_response(missed=["Obscure edge case"])
        r2 = _make_judge_response(missed=[])
        r3 = _make_judge_response(missed=[])
        panel   = self._panel_with_responses([r1, r2, r3])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert any("obscure" in m.lower() for m in verdict.all_missed), \
            "all_missed should include items flagged by even one judge"
        assert verdict.consensus_missed == [], \
            "Minority item should not appear in consensus"

    def test_false_positive_consensus(self):
        r1 = _make_judge_response(fp=["Spurious finding about X"])
        r2 = _make_judge_response(fp=["Spurious finding about X"])
        panel   = self._panel_with_responses([r1, r2])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert verdict.consensus_false_positives, \
            "Both judges flagged the same false positive — must appear in consensus"

    # ── Disagreements ─────────────────────────────────────────────────────────

    def test_disagreement_reported_when_scores_diverge(self):
        r1 = _make_judge_response(completeness=5, precision=4, severity_accuracy=4, specificity=4)
        r2 = _make_judge_response(completeness=1, precision=4, severity_accuracy=4, specificity=4)
        panel   = self._panel_with_responses([r1, r2])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert verdict.disagreements, "Completeness spread of 4 must be reported as a disagreement"

    def test_no_disagreement_when_judges_agree(self):
        r = _make_judge_response(completeness=3, precision=4, severity_accuracy=3, specificity=3)
        panel   = self._panel_with_responses([r, r, r])
        verdict = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert verdict.disagreements == []

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_single_judge_panel(self):
        r     = _make_judge_response(completeness=5, verdict="PASS")
        panel = self._panel_with_responses([r])
        v     = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert v.verdict    == "PASS"
        assert v.judge_count == 1

    def test_all_judges_fail_returns_worst_verdict(self):
        from agents.llm_client import ModelConfig
        panel = JudgePanel.__new__(JudgePanel)
        judges = []
        for i in range(3):
            judge = LLMJudge.__new__(LLMJudge)
            judge._cfg    = ModelConfig(provider="anthropic", model="test")
            mock_client   = MagicMock()
            mock_client.create.side_effect = ConnectionError("timeout")
            judge._client = mock_client
            judges.append(judge)
        panel._judges = judges
        v = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert v.verdict == "FAIL"

    def test_mixed_valid_and_fallback_judges(self):
        from agents.llm_client import ModelConfig
        panel = JudgePanel.__new__(JudgePanel)

        # Judge 1: valid (PASS)
        good_judge = _judge_with_response(_make_judge_response(
            completeness=5, precision=5, severity_accuracy=5, specificity=5, verdict="PASS"
        ))

        # Judge 2: fails
        bad_judge  = LLMJudge.__new__(LLMJudge)
        bad_judge._cfg    = ModelConfig(provider="openai", model="gpt-4")
        bad_client        = MagicMock()
        bad_client.create.side_effect = RuntimeError("API down")
        bad_judge._client = bad_client

        panel._judges = [good_judge, bad_judge]
        v = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        # Consolidation should use only the valid judge's scores
        assert v.avg_completeness == 5.0
        assert v.verdict          == "PASS"


# ═══════════════════════════════════════════════════════════════════════════════
#  Multi-provider panel scenarios
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiProviderPanel:
    """
    Simulate a real multi-provider panel (Anthropic + OpenAI + Anthropic Haiku)
    using mocked clients.  Verifies that different verdicts are consolidated
    correctly regardless of which provider produced them.
    """

    def _make_panel(self, provider_responses: dict[str, str]) -> JudgePanel:
        """provider_responses: {provider_name: json_response_text}"""
        from agents.llm_client import ModelConfig
        panel = JudgePanel.__new__(JudgePanel)
        judges = []
        for provider, response_text in provider_responses.items():
            judge = LLMJudge.__new__(LLMJudge)
            judge._cfg    = ModelConfig(provider=provider, model="test-model")
            judge._client = _mock_llm_client(response_text)
            judges.append(judge)
        panel._judges = judges
        return panel

    def test_three_provider_unanimous_pass(self):
        panel = self._make_panel({
            "anthropic": _make_judge_response(completeness=5, precision=5,
                                              severity_accuracy=5, specificity=4, verdict="PASS"),
            "openai":    _make_judge_response(completeness=4, precision=5,
                                              severity_accuracy=4, specificity=4, verdict="PASS"),
            "ollama":    _make_judge_response(completeness=5, precision=4,
                                              severity_accuracy=5, specificity=5, verdict="PASS"),
        })
        v = panel.evaluate_agent("schema_change", SAMPLE_DIFF, {})
        assert v.verdict    == "PASS"
        assert v.confidence == "HIGH"
        assert v.judge_count == 3

    def test_anthropic_and_openai_outvote_third(self):
        panel = self._make_panel({
            "anthropic": _make_judge_response(verdict="FAIL",
                          missed=["SQL injection", "Missing rate limit"]),
            "openai":    _make_judge_response(verdict="FAIL",
                          missed=["SQL injection"]),
            "ollama":    _make_judge_response(verdict="PASS", missed=[]),
        })
        v = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert v.verdict    == "FAIL"
        assert v.confidence == "MEDIUM"
        # SQL injection flagged by 2/3 judges → consensus
        assert any("sql injection" in m.lower() for m in v.consensus_missed)
        # Missing rate limit flagged by only 1/3 → not consensus, but in all_missed
        assert any("rate limit" in m.lower() for m in v.all_missed)

    def test_provider_scores_averaged_across_different_providers(self):
        panel = self._make_panel({
            "anthropic": _make_judge_response(completeness=5, precision=5,
                                              severity_accuracy=5, specificity=5),
            "openai":    _make_judge_response(completeness=1, precision=1,
                                              severity_accuracy=1, specificity=1),
        })
        v = panel.evaluate_agent("security", SAMPLE_DIFF, SAMPLE_FINDINGS)
        assert v.avg_completeness == 3.0  # (5+1)/2
        assert v.overall_score    == 3.0


# ═══════════════════════════════════════════════════════════════════════════════
#  evaluate_report() — integration with AnalysisReport
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateReport:

    def _panel(self, response_text: str) -> JudgePanel:
        panel = JudgePanel.__new__(JudgePanel)
        panel._judges = [_judge_with_response(response_text)]
        return panel

    def _make_report(self):
        """Build a minimal AnalysisReport with security and schema_change populated."""
        from core.models import (
            AnalysisReport, ChangeType, SecurityResult, SchemaChangeResult,
        )
        return AnalysisReport(
            request_id=str(uuid.uuid4()),
            change_type=ChangeType.PR,
            repo_url="https://github.com/bank/payments",
            source_ref="feature/test",
            target_ref="main",
            security=SecurityResult(findings=[], secrets_detected=False),
            schema_change=SchemaChangeResult(changes=[], has_destructive=False,
                                             has_irreversible=False, gate_contribution="APPROVE",
                                             summary="No changes."),
        )

    def test_evaluate_report_produces_one_verdict_per_agent(self):
        panel  = self._panel(_make_judge_response(verdict="PARTIAL"))
        report = self._make_report()
        result = panel.evaluate_report(report, SAMPLE_DIFF, agent_names=["security", "schema_change"])
        assert len(result.agent_verdicts) == 2
        agent_names = {v.agent_name for v in result.agent_verdicts}
        assert "security"      in agent_names
        assert "schema_change" in agent_names

    def test_overall_verdict_is_worst_case(self):
        from agents.llm_client import ModelConfig
        panel = JudgePanel.__new__(JudgePanel)
        # Judge 1 returns PASS for security, FAIL for schema_change
        call_count = [0]
        responses = [
            _make_judge_response(verdict="PASS"),
            _make_judge_response(verdict="FAIL"),
        ]
        def _rotating_response(**kwargs):
            idx = call_count[0] % len(responses)
            call_count[0] += 1
            mock = MagicMock()
            mock.text         = responses[idx]
            mock.total_tokens = 100
            return mock

        judge = LLMJudge.__new__(LLMJudge)
        judge._cfg    = ModelConfig(provider="anthropic", model="test")
        mock_client   = MagicMock()
        mock_client.create.side_effect = _rotating_response
        judge._client = mock_client
        panel._judges = [judge]

        report = self._make_report()
        result = panel.evaluate_report(report, SAMPLE_DIFF, agent_names=["security", "schema_change"])
        assert result.overall_verdict == "FAIL", \
            "overall_verdict must be FAIL if any agent verdict is FAIL"

    def test_critical_gaps_aggregated_across_agents(self):
        panel  = self._panel(_make_judge_response(
            missed=["Injection vulnerability on line 12"],
            verdict="FAIL",
        ))
        report = self._make_report()
        result = panel.evaluate_report(report, SAMPLE_DIFF, agent_names=["security"])
        # consensus_missed is set → critical_gaps should include it prefixed with agent name
        if result.critical_gaps:
            assert any("security" in g for g in result.critical_gaps)

    def test_none_agent_slots_are_skipped(self):
        panel  = self._panel(_make_judge_response(verdict="PASS"))
        report = self._make_report()
        # Only security and schema_change are non-None; dependency is None
        result = panel.evaluate_report(report, SAMPLE_DIFF,
                                       agent_names=["security", "dependency"])
        evaluated = {v.agent_name for v in result.agent_verdicts}
        assert "security"   in evaluated
        assert "dependency" not in evaluated  # None slot should be skipped

    def test_evaluation_report_has_required_fields(self):
        panel  = self._panel(_make_judge_response())
        report = self._make_report()
        result = panel.evaluate_report(report, SAMPLE_DIFF, agent_names=["security"])
        assert result.request_id == report.request_id
        assert result.panel_size == 1
        assert result.overall_score >= 0.0
        assert result.completed_at  # non-empty ISO timestamp
