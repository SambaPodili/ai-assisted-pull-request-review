"""
tests/test_review_plan.py
--------------------------
Reviewer triage: every changed file lands in must_fix / needs_review /
auto_approvable, with read-first ordering and an effort estimate.
"""
from core.models import (
    AnalysisReport, ChangeType, RiskLevel,
    SecurityResult, SecurityFinding,
    CodeAnalysisResult, CodeFinding,
    SchemaChangeResult, SchemaChange,
)
from governance.review_plan import build_review_plan


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


def _sec(file, sev, desc="issue", cwe="CWE-89", unv=False):
    return SecurityFinding(file_path=file, line_range="10", severity=sev, cwe_id=cwe,
                           owasp_cat="A03", description=desc, remediation="fix", unverified=unv)


def test_confirmed_high_is_must_fix_clean_file_is_auto():
    rep = _rep(
        security=SecurityResult(findings=[_sec("src/Dao.java", RiskLevel.HIGH, "SQL injection")],
                                overall_severity=RiskLevel.HIGH),
    )
    plan = build_review_plan(rep, {"src/Dao.java", "src/Clean.java", "README.md"})
    paths = {f.file_path: f.bucket for f in plan.must_fix + plan.needs_review + plan.auto_approvable}
    assert paths["src/Dao.java"] == "must_fix"
    assert paths["src/Clean.java"] == "auto_approvable"
    assert paths["README.md"] == "auto_approvable"
    assert len(plan.must_fix) == 1 and len(plan.auto_approvable) == 2
    # read-first leads with the risky file, skips the clean ones
    assert plan.read_first == ["src/Dao.java"]
    assert plan.effort_minutes > 0
    assert "1 must-fix" in plan.headline


def test_confirmed_medium_is_needs_review():
    rep = _rep(code_analysis=CodeAnalysisResult(summary="x", change_type="modify", findings=[
        CodeFinding(file_path="src/Svc.java", line_range="20", severity=RiskLevel.MEDIUM,
                    category="logic", description="dead code", suggestion="")]))
    plan = build_review_plan(rep, {"src/Svc.java"})
    assert len(plan.needs_review) == 1
    assert plan.needs_review[0].bucket == "needs_review"


def test_unverified_finding_does_not_escalate_file():
    # an unverified (hallucinated/speculative) high finding must NOT make a file must-fix
    rep = _rep(security=SecurityResult(
        findings=[_sec("src/Ghost.java", RiskLevel.HIGH, "maybe", unv=True)],
        overall_severity=RiskLevel.HIGH))
    plan = build_review_plan(rep, {"src/Ghost.java"})
    assert len(plan.must_fix) == 0
    assert plan.auto_approvable[0].file_path == "src/Ghost.java"


def test_hardcoded_secret_forces_must_fix():
    rep = _rep(security=SecurityResult(
        findings=[_sec("config/app.py", RiskLevel.HIGH, "hardcoded password", cwe="CWE-798")],
        overall_severity=RiskLevel.HIGH))
    plan = build_review_plan(rep, {"config/app.py"})
    assert plan.must_fix[0].file_path == "config/app.py"
    assert any("secret" in r.lower() for r in plan.must_fix[0].reasons)


def test_destructive_schema_forces_must_fix():
    rep = _rep(schema_change=SchemaChangeResult(changes=[
        SchemaChange(file_path="db/m.sql", change_type="drop_table", table_name="users",
                     severity=RiskLevel.CRITICAL, reversible=False, description="drop")]))
    plan = build_review_plan(rep, {"db/m.sql"})
    assert plan.must_fix and plan.must_fix[0].file_path == "db/m.sql"
    assert any("schema" in r.lower() for r in plan.must_fix[0].reasons)


def test_all_clean_reads_as_quick_skim():
    plan = build_review_plan(_rep(), {"a.py", "b.py"})
    assert len(plan.auto_approvable) == 2 and not plan.must_fix and not plan.needs_review
    assert plan.effort_minutes == 0
    assert "auto-approvable" in plan.headline


def test_empty_changed_files():
    plan = build_review_plan(_rep(), set())
    assert plan.headline and not plan.must_fix
