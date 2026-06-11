"""tests/test_quality_gate_ci.py
Continuous-eval regression gate: golden-case detection quality must never drop
below the checked-in baseline. Runs the deterministic (static) agent paths only,
so it needs no LLM key and is safe in any CI runner.
"""
from evaluation.quality_gate import run_gate, load_baseline, BASELINE_PATH


def test_baseline_exists_and_is_current_format():
    base = load_baseline()
    assert base is not None, f"missing {BASELINE_PATH} — run scripts/quality_gate.py --update-baseline"
    assert "overall_recall" in base and "by_agent" in base


def test_detection_quality_has_not_regressed():
    v = run_gate()
    assert v.passed, "Quality regression vs baseline:\n  " + "\n  ".join(v.failures)


def test_gate_detects_a_synthetic_regression(tmp_path):
    """The gate itself must fail when the baseline demands more than reality."""
    import json
    fake = tmp_path / "baseline.json"
    fake.write_text(json.dumps({
        "overall_recall": 1.0, "overall_precision": 1.0,
        "by_agent": {"security": {"recall": 1.0, "precision": 1.0},
                      "nonexistent_agent": {"recall": 1.0, "precision": 1.0}},
    }))
    v = run_gate(baseline_path=fake)
    assert not v.passed
    assert any("nonexistent_agent" in f for f in v.failures)
