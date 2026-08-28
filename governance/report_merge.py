"""
governance/report_merge.py
-----------------------------
merge_reports(old, new_partial) — combines a PR's prior full analysis with a
partial analysis of just the new commits into one report, as part of "true
incremental re-review" (api/routes/webhooks.py::_run_with_diff).

Only merges RAW typed per-agent-result fields — derived fields (top_issues,
gate_decision, suppressed_notes, gate_policy_reasons...) are NOT recomputed
here. The caller is responsible for re-running the same finalize pipeline
(governance/finding_quality.py::correct_findings, governance/
false_positive_guard.py::guard_false_positives, governance/evidence.py::
filter_all_unsubstantiated, governance/suppression.py::apply_suppressions,
governance/correlation.py::correlate_findings, governance/gate_policy.py::
evaluate_policy) against the merged report to regenerate those — all four
verification passes are stateless/idempotent, and evaluate_policy is purely
finding-driven, so this is safe.

IMPORTANT — this covers more than governance/correlation.py::_collect reads.
gate_policy.py's BLOCK/HOLD rules read several scalar/boolean SIGNAL fields
(secrets_detected, has_injection, has_destructive, ...) that are NOT lists
and don't get "merged" by simple concatenation — an old report with
secrets_detected=False and a new partial with secrets_detected=True must
merge to secrets_detected=True (OR, not "keep old's value"), or a real
hardcoded secret in the new commits would silently fail to BLOCK the merged
report. gate_policy.py also reads report.dependency/license_compliance/
test_coverage/functional_validation directly, none of which
governance/correlation.py touches at all — all four need explicit merge
handling here too, for the same reason.

Trust boundary: operates purely on two already-finalized AnalysisReport
objects' typed result fields. Introduces no new path for untrusted text to
reach governance.gate_policy.evaluate_policy or governance.rationale.build_
rationale — both still take only the (now-merged) report, by construction.
"""
from __future__ import annotations

from core.models import AnalysisReport

# Per-agent-result-type list attribute to concatenate — NOT uniform across
# types (confirmed against governance/correlation.py::_collect, which reads
# every one of these under this exact attribute name).
_MERGE_LIST_ATTR: dict[str, str] = {
    "security":            "findings",
    "secrets_entropy":      "findings",
    "code_analysis":        "findings",
    "ast_analysis":         "findings",
    "iac_analysis":         "findings",
    "performance_impact":   "findings",
    "observability":        "findings",
    "taint_analysis":       "taint_paths",
    "data_privacy":         "pii_findings",
    "maintainability":      "issues",
    "schema_change":        "changes",
    "interface":            "breaking_changes",
}

# Boolean SIGNAL fields gate_policy.py reads directly (not via findings
# lists) — must be OR'd: true in EITHER report means true in the merged one.
_MERGE_BOOL_OR: dict[str, tuple[str, ...]] = {
    "security":             ("secrets_detected",),
    "taint_analysis":       ("has_injection", "has_ssrf", "has_path_traversal"),
    "schema_change":        ("has_destructive", "has_irreversible"),
    "license_compliance":   ("has_copyleft", "has_license_conflict"),
    "functional_validation": ("has_contradiction",),
}


def _merge_result(merged_field, old_result, new_result, list_attrs: tuple[str, ...] = (),
                   bool_or_attrs: tuple[str, ...] = ()):
    """In-place merge of `new_result`'s list/bool-signal attributes into
    `old_result` (already a deep copy, safe to mutate) — shared by every
    per-type branch below."""
    for attr in list_attrs:
        old_list = getattr(old_result, attr, None)
        new_list = getattr(new_result, attr, None)
        if old_list is None and new_list is None:
            continue
        setattr(old_result, attr, [*(old_list or []), *(new_list or [])])
    for attr in bool_or_attrs:
        setattr(old_result, attr, bool(getattr(old_result, attr, False)) or bool(getattr(new_result, attr, False)))
    return old_result


def merge_reports(old: AnalysisReport, new_partial: AnalysisReport) -> AnalysisReport:
    """Returns a NEW AnalysisReport — never mutates `old` or `new_partial`."""
    merged = old.model_copy(deep=True)

    # Identity: this run's own id/timing; target_ref stays from `old` (still
    # the same PR's base branch).
    merged.request_id   = new_partial.request_id
    merged.source_ref    = new_partial.source_ref
    merged.completed_at = new_partial.completed_at
    merged.duration_s   = (old.duration_s or 0.0) + (new_partial.duration_s or 0.0)
    merged.from_cache    = False

    # ── The 11 types governance/correlation.py::_collect reads — list attr +
    # any boolean signal gate_policy.py also checks on that same type. ──────
    for report_field, list_attr in _MERGE_LIST_ATTR.items():
        new_result = getattr(new_partial, report_field, None)
        if new_result is None:
            continue  # this agent didn't run (or found nothing) in the incremental slice
        old_result = getattr(merged, report_field, None)
        if old_result is None:
            setattr(merged, report_field, new_result)
            continue
        _merge_result(report_field, old_result, new_result,
                      list_attrs=(list_attr,), bool_or_attrs=_MERGE_BOOL_OR.get(report_field, ()))

    # ── dependency — gate_policy.py reads cve_hits/blast_radius_score
    # directly; correlation.py never touches this type at all. ─────────────
    if new_partial.dependency is not None:
        if merged.dependency is None:
            merged.dependency = new_partial.dependency
        else:
            d, nd = merged.dependency, new_partial.dependency
            d.cve_hits = [*(d.cve_hits or []), *(nd.cve_hits or [])]
            # Dedup by (package, cve_id) — unlike cve_hits, this feeds direct
            # severity/fix-version display, where a duplicate row looks broken.
            seen_cve = {(c.package, c.cve_id) for c in (d.cve_findings or [])}
            d.cve_findings = [*(d.cve_findings or []), *[
                c for c in (nd.cve_findings or []) if (c.package, c.cve_id) not in seen_cve
            ]]
            d.affected_services = list({*(d.affected_services or []), *(nd.affected_services or [])})
            d.dependency_nodes = [*(d.dependency_nodes or []), *(nd.dependency_nodes or [])]
            d.changed_packages = list({*(d.changed_packages or []), *(nd.changed_packages or [])})
            d.notes = [*(d.notes or []), *(nd.notes or [])]
            # Blast radius is a worst-case metric — never let merging LOWER it.
            d.blast_radius_score = max(d.blast_radius_score or 0, nd.blast_radius_score or 0)

    # ── license_compliance — has_copyleft/has_license_conflict gate BLOCK
    # rules; not read by correlation.py at all. ────────────────────────────
    if new_partial.license_compliance is not None:
        if merged.license_compliance is None:
            merged.license_compliance = new_partial.license_compliance
        else:
            _merge_result("license_compliance", merged.license_compliance, new_partial.license_compliance,
                          list_attrs=("findings",), bool_or_attrs=_MERGE_BOOL_OR["license_compliance"])

    # ── functional_validation — has_contradiction gates HOLD; not read by
    # correlation.py at all. ────────────────────────────────────────────────
    if new_partial.functional_validation is not None:
        if merged.functional_validation is None:
            merged.functional_validation = new_partial.functional_validation
        else:
            fv, nfv = merged.functional_validation, new_partial.functional_validation
            fv.requirements = [*(fv.requirements or []), *(nfv.requirements or [])]
            fv.impacts = [*(fv.impacts or []), *(nfv.impacts or [])]
            fv.docs_analysed = list({*(fv.docs_analysed or []), *(nfv.docs_analysed or [])})
            fv.has_contradiction = bool(fv.has_contradiction) or bool(nfv.has_contradiction)

    # ── test_coverage — coverage_delta gates BLOCK/HOLD on a % drop
    # threshold; more negative = worse, so the merged delta is the WORSE
    # (more negative) of the two, never an average or a silent overwrite. ──
    if new_partial.test_coverage is not None:
        if merged.test_coverage is None:
            merged.test_coverage = new_partial.test_coverage
        else:
            tc, ntc = merged.test_coverage, new_partial.test_coverage
            tc.coverage_delta = min(tc.coverage_delta or 0.0, ntc.coverage_delta or 0.0)
            tc.uncovered_paths = [*(tc.uncovered_paths or []), *(ntc.uncovered_paths or [])]
            tc.generated_stubs = [*(tc.generated_stubs or []), *(ntc.generated_stubs or [])]
            tc.method_coverage = [*(tc.method_coverage or []), *(ntc.method_coverage or [])]
            tc.hollow_tests = [*(tc.hollow_tests or []), *(ntc.hollow_tests or [])]

    # ── data_privacy — unencrypted_pii_count gates HOLD; pii_findings is
    # already merged above (via _MERGE_LIST_ATTR) but this scalar count
    # isn't derived from that list by gate_policy.py, so it needs its own
    # explicit sum. ─────────────────────────────────────────────────────────
    if new_partial.data_privacy is not None and merged.data_privacy is not None:
        merged.data_privacy.unencrypted_pii_count = (
            (merged.data_privacy.unencrypted_pii_count or 0) + (new_partial.data_privacy.unencrypted_pii_count or 0)
        )
        merged.data_privacy.logging_violations = [
            *(merged.data_privacy.logging_violations or []), *(new_partial.data_privacy.logging_violations or [])
        ]

    # Risk narrative reflects the LATEST push's own state ("this update"),
    # not an attempted merge of two risk assessments — the merged
    # top_issues/gate decision (recomputed by the caller from the fields
    # merged above) already reflects the full accumulated finding set
    # regardless of which risk narrative is attached. See
    # agents/risk_agent.py — its prompt uses request.total_churn/
    # len(request.hunks), which for an incremental request reflects only the
    # new commits, by design.
    if new_partial.risk is not None:
        merged.risk = new_partial.risk

    # Remediation: code_fixes/fix_suggestions are per-finding and merged like
    # the agent-result fields above, so old findings don't lose their
    # suggested fixes. The narrative fields (pr_walkthrough, executive_summary,
    # diagrams, validation_checklist, deployment_strategy) are kept from
    # new_partial as the current/latest state — a KNOWN IMPERFECTION: a truly
    # complete walkthrough would describe the full accumulated diff, not just
    # the latest incremental slice. Acceptable for v1; revisit if this proves
    # confusing in practice.
    if new_partial.remediation is not None:
        if merged.remediation is not None:
            merged.remediation.code_fixes = [*merged.remediation.code_fixes, *new_partial.remediation.code_fixes]
            merged.remediation.fix_suggestions = [*merged.remediation.fix_suggestions, *new_partial.remediation.fix_suggestions]
            merged.remediation.diagrams = new_partial.remediation.diagrams or merged.remediation.diagrams
            merged.remediation.validation_checklist = new_partial.remediation.validation_checklist
            merged.remediation.deployment_strategy = new_partial.remediation.deployment_strategy
            merged.remediation.executive_summary = new_partial.remediation.executive_summary
            merged.remediation.pr_walkthrough = new_partial.remediation.pr_walkthrough
        else:
            merged.remediation = new_partial.remediation

    return merged
