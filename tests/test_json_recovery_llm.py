"""
tests/test_json_recovery_llm.py
--------------------------------
Self-hosted models (Llama / Mistral / vLLM) emit *almost*-valid JSON that the
strict parser rejected → every agent fell back to static rules. These pin the
no-dependency recovery: Python literals, trailing commas, // and /* */ comments,
and prose wrappers must parse without the json-repair library installed.
"""
import json
import pytest

from agents.base_agent import _lenient_json_repair, _json_candidates, _strip_reasoning
from agents.code_analysis_agent import CodeAnalysisAgent


# ── reasoning-model <think> stripping (Qwen/QwQ/R1) ───────────────────────────

def test_strip_reasoning_closed_block():
    out = _strip_reasoning('<think>let me reason about this diff…</think>\n{"change_type":"feature"}')
    assert out == '{"change_type":"feature"}'


def test_strip_reasoning_truncated_block_keeps_json():
    out = _strip_reasoning('<think>partial reasoning {"change_type":"fix"} trailing')
    assert out.startswith('{"change_type":"fix"}')


def test_strip_reasoning_leaves_plain_untouched():
    assert _strip_reasoning('{"change_type":"feature"}') == '{"change_type":"feature"}'


def test_agent_parses_qwen_reasoning_output():
    a = CodeAnalysisAgent(api_key="x")
    raw = ('<think>The diff adds a field; low risk.</think>\n'
           '{"summary":"adds field","change_type":"feature","complexity_delta":0,"findings":[]}')
    res = a._parse_json(raw)
    assert res.change_type == "feature"


# ── lenient repair (no deps) ──────────────────────────────────────────────────

def test_python_literals_normalised():
    fixed = _lenient_json_repair('{"a": True, "b": False, "c": None}')
    assert json.loads(fixed) == {"a": True, "b": False, "c": None}


def test_trailing_commas_dropped():
    fixed = _lenient_json_repair('{"x": [1, 2, 3,], "y": 4,}')
    assert json.loads(fixed) == {"x": [1, 2, 3], "y": 4}


def test_comments_stripped():
    fixed = _lenient_json_repair('{\n // hi\n "a": 1, /* block */ "b": 2\n}')
    assert json.loads(fixed) == {"a": 1, "b": 2}


def test_raw_control_chars_in_strings_escaped():
    # multi-line description with raw newline + tab (the #1 self-hosted-LLM bug)
    bad = '{"summary": "line one\nline two\twith tab", "n": 1}'
    out = json.loads(_lenient_json_repair(bad))
    assert out["summary"] == "line one\nline two\twith tab" and out["n"] == 1


def test_url_and_string_slashes_preserved():
    # // inside a string (URL / path) must NOT be treated as a comment
    s = '{"url": "https://api.x.com/v1", "path": "a//b", "note": "True story"}'
    out = json.loads(_lenient_json_repair(s))
    assert out["url"] == "https://api.x.com/v1"
    assert out["path"] == "a//b"
    assert out["note"] == "True story"   # literal inside string untouched


def test_candidates_yield_parseable_for_malformed():
    bad = 'prose {\n "fallback_used": False,\n "items": [1, 2,],\n}'
    assert any(_is_json(c) for c in _json_candidates(bad))


# ── full agent parse (end-to-end) ─────────────────────────────────────────────

def test_agent_parses_llama_style_output():
    a = CodeAnalysisAgent(api_key="x")
    bad = (
        "Here is the analysis:\n"
        "{\n"
        "  // code review\n"
        '  "summary": "Adds fields",\n'
        '  "change_type": "feature",\n'
        '  "complexity_delta": 1,\n'
        '  "fallback_used": False,\n'
        '  "findings": [\n'
        '    {"file_path": "src/A.java", "line_range": "78", "severity": "low",'
        '     "category": "SQL", "description": "x", "suggestion": ""},\n'
        "  ],\n"
        "}\n"
        "That concludes the review."
    )
    res = a._parse_json(bad)
    assert res.change_type == "feature" and len(res.findings) == 1


def test_unescaped_inner_quotes_via_json_repair_if_available():
    # this case needs the optional json-repair lib (lenient repair can't fix it)
    pytest.importorskip("json_repair")
    a = CodeAnalysisAgent(api_key="x")
    bad = '{"summary": "the field "amount" changed", "change_type": "feature", "findings": []}'
    res = a._parse_json(bad)
    assert res.change_type == "feature"


def _is_json(s):
    try:
        json.loads(s); return True
    except Exception:
        return False
