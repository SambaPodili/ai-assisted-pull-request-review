#!/usr/bin/env python3
"""
scripts/quality_gate.py — CI quality regression gate.

Runs every golden case through the agents' deterministic paths and fails
(exit 1) if recall/precision regressed vs evaluation/data/quality_baseline.json.
No LLM key required.

  python scripts/quality_gate.py                    # gate (CI)
  python scripts/quality_gate.py --update-baseline  # accept current as baseline
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.quality_gate import run_gate, write_baseline  # noqa: E402


def main() -> int:
    if "--update-baseline" in sys.argv:
        data = write_baseline()
        print(f"Baseline updated: recall={data['overall_recall']} precision={data['overall_precision']}")
        return 0

    v = run_gate()
    print(f"Quality gate — recall={v.overall_recall:.3f} precision={v.overall_precision:.3f}")
    for name, m in v.by_agent.items():
        print(f"  {name:18s} R={m['recall']:.2f} P={m['precision']:.2f} (tp={m['tp']} fp={m['fp']} fn={m['fn']})")
    if v.passed:
        print("PASS — no quality regression.")
        return 0
    print("FAIL — quality regressed:")
    for f in v.failures:
        print(f"  ✗ {f}")
    print("If the change is intentional, run: python scripts/quality_gate.py --update-baseline")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
