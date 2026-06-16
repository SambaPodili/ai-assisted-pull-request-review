"""
governance/rationale.py
------------------------
Deterministic gate rationale built from the FINAL report values.

The risk agent's LLM rationale is generated mid-pipeline — before blast radius is
derived in _finalize and while metrics we've since dropped (e.g. coverage delta)
were still in scope. That makes the headline drift from the displayed numbers
("blast radius is 0" while the tile shows 14/100). This rebuilds a concise,
accurate summary from the final report so the headline always matches the metrics.
"""
from __future__ import annotations


def build_rationale(report) -> str:
    sec = getattr(report, "security", None)
    sec_findings = [f for f in (getattr(sec, "findings", None) or [])
                    if not getattr(f, "unverified", False)]
    secrets = bool(getattr(sec, "secrets_detected", False)) or bool(
        getattr(getattr(report, "secrets_entropy", None), "findings", None))

    iface = getattr(report, "interface", None)
    breaking = len(getattr(iface, "breaking_changes", None) or [])

    dep = getattr(report, "dependency", None)
    blast = int(getattr(dep, "blast_radius_score", 0) or 0)
    services = len(getattr(dep, "affected_services", None) or [])
    cves = len(getattr(dep, "cve_hits", None) or [])

    tc = getattr(report, "test_coverage", None)
    gaps = len(getattr(tc, "uncovered_paths", None) or [])

    schema = len(getattr(getattr(report, "schema_change", None), "changes", None) or [])

    ca = getattr(report, "code_analysis", None)
    cx = int(getattr(ca, "complexity_delta", 0) or 0)

    bits: list[str] = []
    bits.append(f"{len(sec_findings)} security finding(s)" if sec_findings else "no security findings")
    bits.append("hardcoded secrets present" if secrets else "no secrets")
    bits.append(f"{breaking} breaking change(s)" if breaking else "no breaking changes")

    blast_txt = f"blast radius {blast}/100"
    blast_txt += (f" across {services} downstream service(s)" if services
                  else " (no declared downstream services)")
    bits.append(blast_txt)

    if cves:
        bits.append(f"{cves} known CVE(s) in dependencies")
    if schema:
        bits.append(f"{schema} schema change(s)")
    bits.append(f"{gaps} changed file(s) without tests" if gaps else "tests present for changed files")
    if cx:
        bits.append(f"complexity {cx:+d}")

    return "Key signals — " + ", ".join(bits) + "."
