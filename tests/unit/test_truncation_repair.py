"""
tests/unit/test_truncation_repair.py
--------------------------------------
Unit tests for the JSON truncation repair logic in base_agent.
Covers the real failure mode: LLM hits max_tokens mid-string.
"""
from __future__ import annotations
import json
import pytest
from agents.base_agent import _repair_truncated_json, _strip_fences


class TestRepairTruncatedJson:

    def test_complete_json_unchanged(self):
        raw      = '{"a": 1, "b": [1, 2, 3]}'
        repaired = _repair_truncated_json(raw)
        assert json.loads(repaired) == {"a": 1, "b": [1, 2, 3]}

    def test_closes_open_string(self):
        raw      = '{"a": "hello world'
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert parsed["a"].startswith("hello world")

    def test_closes_open_array(self):
        raw      = '{"items": [1, 2, 3'
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert parsed["items"] == [1, 2, 3]

    def test_closes_nested_objects(self):
        raw      = '{"outer": {"inner": {"x": 1'
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert parsed["outer"]["inner"]["x"] == 1

    def test_removes_trailing_comma_before_close(self):
        raw      = '{"items": [1, 2,'
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert parsed["items"] == [1, 2]

    def test_truncated_string_in_array(self):
        # Simulates: generated_stubs list where last stub is cut off
        raw = '{"generated_stubs": ["@Test\\nvoid testA() {}","@Test\\nvoid testB() {\\n  // arrange'
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert len(parsed["generated_stubs"]) >= 1
        assert parsed["generated_stubs"][0] == "@Test\nvoid testA() {}"

    def test_escaped_quotes_not_confused(self):
        raw      = '{"msg": "say \\"hello\\""}'
        repaired = _repair_truncated_json(raw)
        assert json.loads(repaired)["msg"] == 'say "hello"'

    def test_real_truncated_test_coverage_result(self):
        # Approximates what the LLM produces when it runs out of tokens
        # mid-stub: a long generated_stubs string cut off
        raw = (
            '{"coverage_delta": -5.0, "uncovered_paths": [], '
            '"regression_risk": "medium", '
            '"generated_stubs": ["@Test\\nvoid testProcess_shouldApplyFee() {'
            '\\n    // Arrange: set up PaymentRequest with fee\\n    // Act: call p'
        )
        repaired = _repair_truncated_json(raw)
        parsed   = json.loads(repaired)
        assert parsed["coverage_delta"] == -5.0
        assert parsed["regression_risk"] == "medium"
        # The truncated stub is still there (partial content)
        assert len(parsed["generated_stubs"]) >= 1

    def test_completely_open_object(self):
        raw      = '{"a":'
        repaired = _repair_truncated_json(raw)
        # Can't be valid JSON — but should not raise
        assert isinstance(repaired, str)

    def test_empty_input(self):
        repaired = _repair_truncated_json("")
        assert isinstance(repaired, str)


class TestStripFences:

    def test_no_fence_unchanged(self):
        from agents.base_agent import _strip_fences
        assert _strip_fences('{"a": 1}') == '{"a": 1}'

    def test_strips_json_fence(self):
        from agents.base_agent import _strip_fences
        raw = "```json\n{\"a\": 1}\n```"
        assert _strip_fences(raw) == '{"a": 1}'

    def test_strips_plain_fence(self):
        from agents.base_agent import _strip_fences
        raw = "```\n{\"a\": 1}\n```"
        assert _strip_fences(raw) == '{"a": 1}'


class TestParseJsonRecovery:
    """
    End-to-end: _parse_json on BaseAgent subclass recovers from truncation.
    """

    def _agent(self):
        from agents.test_coverage_agent import TestCoverageAgent
        return TestCoverageAgent(api_key="test")

    def test_direct_valid_json_parses(self):
        raw = '{"coverage_delta": 0.0, "uncovered_paths": [], "regression_risk": "low", "generated_stubs": []}'
        result = self._agent()._parse_json(raw)
        assert result.regression_risk.value == "low"

    def test_truncated_json_recovers(self):
        # Missing closing brackets — repair should close them
        raw = '{"coverage_delta": -2.5, "uncovered_paths": [], "regression_risk": "medium", "generated_stubs": ["@Test\\nvoid test() {'
        result = self._agent()._parse_json(raw)
        assert result.coverage_delta == -2.5
        assert result.regression_risk.value == "medium"

    def test_preamble_stripped_before_parse(self):
        raw = 'Here is the result:\n{"coverage_delta": 1.0, "uncovered_paths": [], "regression_risk": "low", "generated_stubs": []}\nDone.'
        result = self._agent()._parse_json(raw)
        assert result.coverage_delta == 1.0

    def test_raises_when_unrecoverable(self):
        with pytest.raises(ValueError, match="Could not parse"):
            self._agent()._parse_json("this is not json at all ~~~")
