"""
governance/review_plan.py
--------------------------
Reviewer triage: turn the full multi-agent report into a PR-level "review plan"
so a reviewer instantly knows WHERE to spend attention instead of scrolling 22
agent tabs.

Every changed file is sorted into one of three buckets:

  • must_fix        — a CONFIRMED high/critical issue, a hardcoded secret, a
                      destructive schema change, or a breaking interface change.
                      The change should not merge until these are addressed.
  • needs_review    — a confirmed medium issue, a breaking-but-additive change,
                      or downstream/blast-radius impact. A human should look.
  • auto_approvable — no confirmed findings (only low-severity or low-confidence
                      noise). Safe to skim.

Plus: a read-first ordering (highest risk first) and a rough human-effort
estimate. Deterministic, zero tokens. Runs in orchestrator._finalize AFTER
correlation/evidence (so `unverified` flags are already set).
"""
from __future__ import annotations

import logging

from core.models import AnalysisReport, ReviewPlan, ReviewFile
from governance.correlation import _collect, _norm_path

log = logging.getLogger(__name__)

_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SEV_MIN  = {0: "low", 1: "medium", 2: "high", 3: "critical"}
_EFFORT   = {"critical": 15, "high": 10, "medium": 5, "low": 1}   # minutes per confirmed finding
_BUCKET_RANK = {"must_fix": 0, "needs_review": 1, "auto_approvable": 2}


def _worst(sevs: list[str]) -> str:
    return _SEV_MIN[max((_SEV_RANK.get(s, 0) for s in sevs), default=0)]


def _breaking_paths(report: AnalysisReport) -> set[str]:
    iface = getattr(report, "interface", None)
    out: set[str] = set()
    for b in (getattr(iface, "breaking_changes", None) or []):
        p = getattr(b, "path", "") or getattr(b, "endpoint", "")
        if p:
            out.add(_norm_path(p))
    return out


def _destructive_schema_paths(report: AnalysisReport) -> set[str]:
    sc = getattr(report, "schema_change", None)
    out: set[str] = set()
    for c in (getattr(sc, "changes", None) or []):
        ct = (getattr(c, "change_type", "") or "").lower()
        if ct in ("drop_table", "drop_column") or getattr(c, "reversible", True) is False:
            fp = getattr(c, "file_path", "")
            if fp:
                out.add(_norm_path(fp))
    return out


def _secret_paths(report: AnalysisReport) -> set[str]:
    out: set[str] = set()
    se = getattr(report, "secrets_entropy", None)
    for f in (getattr(se, "findings", None) or []):
        if getattr(f, "file_path", ""):
            out.add(_norm_path(f.file_path))
    sec = getattr(report, "security", None)
    for f in (getattr(sec, "findings", None) or []):
        # hardcoded-secret CWEs from the security agent
        if str(getattr(f, "cwe_id", "")) in {"CWE-798", "CWE-321", "CWE-522", "CWE-312"} \
           and not getattr(f, "unverified", False) and getattr(f, "file_path", ""):
            out.add(_norm_path(f.file_path))
    return out


def build_review_plan(report: AnalysisReport, changed_files: set[str] | None) -> ReviewPlan:
    """Triage every changed file into must_fix / needs_review / auto_approvable."""
    files = sorted(changed_files or set())
    if not files:
        return ReviewPlan(headline="No changed files to review.", summary="")

    # Normalised findings from every agent (same source as Top Issues).
    rows = _collect(report)
    by_file: dict[str, list[dict]] = {}
    for r in rows:
        by_file.setdefault(r["file"], []).append(r)

    breaking   = _breaking_paths(report)
    destructive = _destructive_schema_paths(report)
    secrets    = _secret_paths(report)

    reviewed: list[ReviewFile] = []
    total_effort = 0

    for fp in files:
        nf = _norm_path(fp)
        frows = by_file.get(nf, [])
        # match by basename too (agents sometimes cite a short path)
        if not frows:
            base = nf.rsplit("/", 1)[-1]
            frows = [r for r in rows if r["file"].rsplit("/", 1)[-1] == base] if base else []

        confirmed = [r for r in frows if not r.get("unverified") and not r.get("speculative")]
        conf_sevs = [r["severity"] for r in confirmed]
        reasons: list[str] = []
        bucket = "auto_approvable"

        # ── escalations independent of finding severity ──
        if nf in secrets:
            bucket = "must_fix"; reasons.append("Hardcoded secret detected")
        if nf in destructive:
            bucket = "must_fix"; reasons.append("Destructive / irreversible schema change")
        if nf in breaking:
            if bucket != "must_fix":
                bucket = "needs_review"
            reasons.append("Breaking interface change")

        # ── severity of confirmed findings ──
        worst = _worst(conf_sevs) if conf_sevs else "low"
        if conf_sevs:
            if _SEV_RANK[worst] >= _SEV_RANK["high"]:
                bucket = "must_fix"
            elif _SEV_RANK[worst] == _SEV_RANK["medium"] and bucket == "auto_approvable":
                bucket = "needs_review"
            # a short, deduped reason from the worst confirmed finding(s)
            top = sorted(confirmed, key=lambda r: -_SEV_RANK.get(r["severity"], 0))[0]
            cat = (top.get("category") or top.get("agent") or "issue").strip()
            reasons.append(f"Confirmed {top['severity'].upper()}: {cat}")

        # effort: weight confirmed findings by severity + a base per non-trivial file
        eff = sum(_EFFORT.get(s, 1) for s in conf_sevs)
        if bucket == "must_fix":
            eff += 5
        elif bucket == "needs_review":
            eff += 2
        total_effort += eff

        # dedupe reasons, keep order
        seen: set[str] = set()
        reasons = [r for r in reasons if not (r in seen or seen.add(r))]

        reviewed.append(ReviewFile(
            file_path=fp,
            bucket=bucket,
            top_severity=worst,
            finding_count=len(frows),
            confirmed_count=len(confirmed),
            reasons=reasons[:4],
        ))

    must_fix     = [f for f in reviewed if f.bucket == "must_fix"]
    needs_review = [f for f in reviewed if f.bucket == "needs_review"]
    auto_ok      = [f for f in reviewed if f.bucket == "auto_approvable"]

    def _order_key(f: ReviewFile):
        return (_BUCKET_RANK[f.bucket], -_SEV_RANK.get(f.top_severity, 0), -f.confirmed_count)

    ordered = sorted(reviewed, key=_order_key)
    # read-first = everything that isn't auto-approvable, highest risk first
    read_first = [f.file_path for f in ordered if f.bucket != "auto_approvable"]

    headline = (
        f"🚨 {len(must_fix)} must-fix · "
        f"⚠ {len(needs_review)} need a human · "
        f"✅ {len(auto_ok)} auto-approvable · "
        f"~{max(total_effort, 1) if (must_fix or needs_review) else 0} min review"
    )
    summary = (
        f"{len(reviewed)} changed file(s). "
        + (f"Start with: {', '.join(read_first[:3])}." if read_first
           else "No confirmed issues — a quick skim should suffice.")
    )

    plan = ReviewPlan(
        must_fix=sorted(must_fix, key=_order_key),
        needs_review=sorted(needs_review, key=_order_key),
        auto_approvable=auto_ok,
        read_first=read_first,
        effort_minutes=max(total_effort, 1) if (must_fix or needs_review) else 0,
        headline=headline,
        summary=summary,
    )
    log.info("[%s] Review plan: %s", report.request_id, headline)
    return plan


def review_plan_markdown(report: AnalysisReport) -> str:
    """Compact Markdown triage block for the PR comment. '' when no plan."""
    rp = getattr(report, "review_plan", None)
    if not rp or not (rp.must_fix or rp.needs_review or rp.auto_approvable):
        return ""
    lines = ["### 🧭 Review plan", "", rp.headline, ""]
    if rp.must_fix:
        lines.append("**🚨 Must fix**")
        for f in rp.must_fix[:8]:
            why = f" — {'; '.join(f.reasons)}" if f.reasons else ""
            lines.append(f"- `{f.file_path}`{why}")
        lines.append("")
    if rp.needs_review:
        lines.append("**⚠️ Needs a human**")
        for f in rp.needs_review[:8]:
            why = f" — {'; '.join(f.reasons)}" if f.reasons else ""
            lines.append(f"- `{f.file_path}`{why}")
        lines.append("")
    if rp.auto_approvable:
        lines.append(f"**✅ Auto-approvable:** {len(rp.auto_approvable)} file(s) with no confirmed issues.")
        lines.append("")
    return "\n".join(lines)
