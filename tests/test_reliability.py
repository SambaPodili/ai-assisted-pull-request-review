"""
tests/test_reliability.py
--------------------------
Quality & reliability guards:
  • LLM calls are deterministic by default (temperature 0).
  • The async-eval result store is bounded (no unbounded memory growth).
"""
from __future__ import annotations


def test_temperature_defaults_to_zero():
    from config.settings import get_settings
    assert get_settings().llm_temperature == 0.0


def test_eval_result_store_is_bounded():
    import api.routes.evaluate as e
    orig = e._MAX_EVAL_RESULTS
    try:
        e._MAX_EVAL_RESULTS = 5
        e._eval_results.clear()
        for i in range(20):
            e._store_eval(f"job-{i}", {"status": "done"})
        assert len(e._eval_results) <= 5
        assert "job-0" not in e._eval_results      # oldest evicted
        assert "job-19" in e._eval_results          # newest kept
    finally:
        e._MAX_EVAL_RESULTS = orig
        e._eval_results.clear()
