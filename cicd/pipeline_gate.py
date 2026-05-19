"""
cicd/pipeline_gate.py
----------------------
CI/CD pipeline integration — enforces analysis gate decisions.

Supports:
  • Jenkins    — POST to Jenkins API to set build status / abort build
  • GitHub Actions  — update commit status via GitHub API
  • GitLab CI  — update pipeline via GitLab API
  • Generic    — exit code + JSON artifact for any CI system

Usage in CI:
    python -m cicd.pipeline_gate --request-id <id> --api-url http://localhost:8080

Exit codes:
  0 = APPROVE
  1 = HOLD (warning, pipeline continues)
  2 = BLOCK (fatal, pipeline fails)
  3 = error (could not retrieve result)
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)


# ── Gate result ───────────────────────────────────────────────────────────────

class GateResult:
    APPROVE = 0
    HOLD    = 1
    BLOCK   = 2
    ERROR   = 3


# ── Main gate check ───────────────────────────────────────────────────────────

def check_gate(
    request_id: str,
    api_url:    str,
    api_key:    str = "",
    poll_secs:  int = 5,
    timeout:    int = 300,
    output_file: str = "",
) -> int:
    """
    Poll the analysis API until the report is ready, then return the exit code.
    """
    url     = f"{api_url.rstrip('/')}/api/v1/report/{request_id}"
    headers = {"X-API-Key": api_key} if api_key else {}
    elapsed = 0

    while elapsed < timeout:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                report = resp.json()
                return _handle_result(report, output_file)
            if resp.status_code != 404:
                log.error("Unexpected status %d from analysis API", resp.status_code)
                return GateResult.ERROR
        except requests.RequestException as exc:
            log.warning("Poll attempt failed: %s", exc)

        time.sleep(poll_secs)
        elapsed += poll_secs

    log.error("Timeout waiting for report %s after %ds", request_id, timeout)
    return GateResult.ERROR


def _handle_result(report: dict, output_file: str) -> int:
    gate  = report.get("gate", "HOLD")
    risk  = report.get("risk", "unknown")
    score = report.get("risk_score", 0)

    print(f"\n{'='*60}")
    print(f"  AI Impact Analysis — {gate}")
    print(f"  Risk: {risk.upper()} (score: {score}/100)")
    print(f"  Repository: {report.get('repo', '')}")
    print(f"  Comparison: {report.get('comparison', '')}")
    print(f"  Tokens used: {report.get('total_tokens', 0)}")

    metrics = report.get("metrics", {})
    if metrics:
        print(f"\n  Security findings: {metrics.get('security_findings', 0)}")
        print(f"  Secrets detected: {metrics.get('secrets_detected', False)}")
        print(f"  Breaking changes: {metrics.get('breaking_changes', 0)}")
        print(f"  Blast radius: {metrics.get('blast_radius', 0)}/100")
        print(f"  Coverage delta: {metrics.get('coverage_delta', 0):+.1f}%")
    print('='*60)

    if output_file:
        Path(output_file).write_text(json.dumps(report, indent=2))
        print(f"\n  Full report saved to: {output_file}")

    if gate == "APPROVE":
        print("\n  ✅ Gate: APPROVE — safe to proceed")
        return GateResult.APPROVE
    elif gate == "HOLD":
        print("\n  ⚠️  Gate: HOLD — review required before proceeding")
        return GateResult.HOLD
    else:
        print("\n  🚫 Gate: BLOCK — pipeline must not proceed")
        return GateResult.BLOCK


# ── GitHub Actions status update ──────────────────────────────────────────────

def update_github_commit_status(
    repo:       str,
    sha:        str,
    gate:       str,
    report_url: str,
    gh_token:   str,
    gh_api_url: str = "https://api.github.com",
) -> bool:
    state_map = {"APPROVE": "success", "HOLD": "pending", "BLOCK": "failure"}
    desc_map  = {
        "APPROVE": "AI Impact Analysis: APPROVE",
        "HOLD":    "AI Impact Analysis: HOLD — review required",
        "BLOCK":   "AI Impact Analysis: BLOCK — deployment prevented",
    }

    payload = {
        "state":       state_map.get(gate, "error"),
        "target_url":  report_url,
        "description": desc_map.get(gate, "Unknown gate"),
        "context":     "ai-impact-analysis/gate",
    }

    try:
        resp = requests.post(
            f"{gh_api_url}/repos/{repo}/statuses/{sha}",
            json=payload,
            headers={
                "Authorization":        f"token {gh_token}",
                "Accept":               "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("GitHub commit status updated: %s → %s", sha[:8], state_map.get(gate))
        return True
    except Exception as exc:
        log.error("GitHub status update failed: %s", exc)
        return False


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Check AI Impact Analysis gate and exit with appropriate code for CI/CD"
    )
    parser.add_argument("--request-id",  required=True,  help="Analysis request ID")
    parser.add_argument("--api-url",     required=True,  help="Impact Analyzer API base URL")
    parser.add_argument("--api-key",     default="",     help="API authentication key")
    parser.add_argument("--timeout",     type=int, default=300, help="Max wait seconds (default: 300)")
    parser.add_argument("--poll",        type=int, default=5,   help="Poll interval seconds (default: 5)")
    parser.add_argument("--output",      default="",     help="Write full JSON report to this file")
    parser.add_argument("--fail-on-hold", action="store_true",  help="Treat HOLD as failure (exit 2)")

    args   = parser.parse_args()
    code   = check_gate(
        request_id=args.request_id,
        api_url=args.api_url,
        api_key=args.api_key,
        poll_secs=args.poll,
        timeout=args.timeout,
        output_file=args.output,
    )

    if args.fail_on_hold and code == GateResult.HOLD:
        code = GateResult.BLOCK

    sys.exit(code)


if __name__ == "__main__":
    main()
