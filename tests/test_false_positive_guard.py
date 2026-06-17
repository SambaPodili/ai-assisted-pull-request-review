"""
tests/test_false_positive_guard.py
-----------------------------------
Regression tests built from real screenshots the user flagged as false positives:

  1. PERFORMANCE: "Potential N+1 … call inside loop" emitted for a flat sequence
     of DAO calls (NO loop in the diff) → must be marked unverified.
  2. DATA PRIVACY: "Potential PII exposure through ERROR_MESSAGE column" on an
     operational error/status column (no real PII) → must be marked unverified.
  3. SCHEMA: PK/FK "may lock the table / prefer CONCURRENTLY" on a table CREATED
     in the same change set → severity lowered + on_new_table set.

A genuine N+1 (loop actually present) and a real PII field (email) must survive.
"""
from core.models import (
    AnalysisReport, ChangeType, RiskLevel,
    PerformanceImpactResult, PerformanceFinding,
    DataPrivacyResult, PIIFinding,
    SchemaChangeResult, SchemaChange,
    SecurityResult, SecurityFinding,
)
from governance.false_positive_guard import guard_false_positives


def _rep(**kw):
    base = dict(request_id="t", change_type=ChangeType.PR, repo_url="r",
                source_ref="a", target_ref="b")
    base.update(kw)
    return AnalysisReport(**base)


# real new-file lines: a flat sequence of calls, NO loop (screenshot #3)
_FLAT = [
    (591, "        // Update related party details with RIF number"),
    (592, "        relatedPartyDetailsDao.updateRelatedPartyWithRifNumber(relatedParty);"),
    (593, "        saveRpCreationDetails(requestBody.getApplicationId());"),
    (594, "        kycStatusDetailsDao.updateNotificationDetails("),
    (595, "            relatedParty.getRelatedPartyId(),"),
]
# same calls, but genuinely inside a loop
_LOOPED = [
    (100, "        for (RelatedParty relatedParty : relatedParties) {"),
    (101, "            relatedPartyDetailsDao.updateRelatedPartyWithRifNumber(relatedParty);"),
    (102, "        }"),
]


def test_n1_without_loop_is_flagged():
    rep = _rep(performance_impact=PerformanceImpactResult(findings=[PerformanceFinding(
        category="n_plus_1", severity=RiskLevel.MEDIUM,
        description="Potential N+1 query issue in KycStatusSaveServiceImpl due to "
                    "updateRelatedPartyWithRifNumber call inside loop",
        file_path="src/main/java/com/uob/cus/srm/service/impl/KycStatusSaveServiceImpl.java",
        line=594, suggestion="batch")]))
    src = {"src/main/java/com/uob/cus/srm/service/impl/kycstatussaveserviceimpl.java": _FLAT}
    n1, _, _, _ = guard_false_positives(rep, src)
    assert n1 == 1
    assert rep.performance_impact.findings[0].unverified is True


def test_security_on_import_line_is_flagged():
    # screenshot: CWE-20 "Improper Input Validation" pinned to an import statement.
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.MEDIUM, findings=[
        SecurityFinding(file_path="src/main/java/com/uob/cus/srm/api/VRMServicesApi.java",
                        line_range="38", severity=RiskLevel.MEDIUM, cwe_id="CWE-20",
                        description="`ManualRifUpdateRequest` Improper Input Validation",
                        remediation="x")]))
    src = {"src/main/java/com/uob/cus/srm/api/vrmservicesapi.java": [
        (37, "+import com.uob.cus.srm.model.manualrifdraft.ManualRifDraftSaveRequest;"),
        (38, "+import com.uob.cus.srm.model.manualrifdraft.ManualRifUpdateRequest;")]}
    _, _, _, imp = guard_false_positives(rep, src)
    assert imp == 1 and rep.security.findings[0].unverified is True


def test_security_on_real_code_line_survives():
    # same CWE family but anchored to an actual sink line → must NOT be flagged.
    rep = _rep(security=SecurityResult(overall_severity=RiskLevel.HIGH, findings=[
        SecurityFinding(file_path="src/Dao.java", line_range="50", severity=RiskLevel.HIGH,
                        cwe_id="CWE-89", description="`buildQuery(recordId)` SQL injection",
                        remediation="x")]))
    src = {"src/dao.java": [(50, '+    String sql = "SELECT * FROM t WHERE id=" + recordId;')]}
    _, _, _, imp = guard_false_positives(rep, src)
    assert imp == 0 and rep.security.findings[0].unverified is False


def test_real_n1_with_loop_survives():
    rep = _rep(performance_impact=PerformanceImpactResult(findings=[PerformanceFinding(
        category="n_plus_1", severity=RiskLevel.HIGH,
        description="N+1 query: DB call inside loop",
        file_path="src/Dao.java", line=101, suggestion="batch")]))
    src = {"src/dao.java": _LOOPED}
    n1, _, _, _ = guard_false_positives(rep, src)
    assert n1 == 0
    assert rep.performance_impact.findings[0].unverified is False


def test_pii_on_operational_column_is_flagged():
    rep = _rep(data_privacy=DataPrivacyResult(pii_findings=[PIIFinding(
        pii_type="error_message", risk_level=RiskLevel.MEDIUM,
        description="Potential PII exposure through ERROR_MESSAGE column in tvrm_batch_ewf_reports table",
        file_path="db/pdn_vrm_ddl.sql", line=9, context="`ERROR_CODE` VARCHAR(5) DEFAULT NULL,")]))
    _, pii, _, _ = guard_false_positives(rep, {})
    assert pii == 1
    assert rep.data_privacy.pii_findings[0].unverified is True


def test_real_pii_field_survives():
    rep = _rep(data_privacy=DataPrivacyResult(pii_findings=[PIIFinding(
        pii_type="email", risk_level=RiskLevel.MEDIUM,
        description="PII field 'email' introduced without apparent encryption",
        file_path="src/User.java", line=12, context="private String email;")]))
    _, pii, _, _ = guard_false_positives(rep, {})
    assert pii == 0
    assert rep.data_privacy.pii_findings[0].unverified is False


def test_index_on_new_table_is_downgraded():
    rep = _rep(schema_change=SchemaChangeResult(changes=[
        SchemaChange(file_path="db/x.sql", change_type="add_table",
                     table_name="tvrm_batch_ewf_reports", severity=RiskLevel.MEDIUM,
                     description="New table created"),
        SchemaChange(file_path="db/x.sql", change_type="add_index",
                     table_name="tvrm_batch_ewf_reports", column_name="ID",
                     severity=RiskLevel.MEDIUM, description="Primary key added on column ID"),
        SchemaChange(file_path="db/x.sql", change_type="add_index",
                     table_name="tvrm_batch_ewf_reports", column_name="APPLICATION_ID",
                     severity=RiskLevel.MEDIUM, description="Foreign key added on column APPLICATION_ID"),
    ]))
    _, _, schema, _ = guard_false_positives(rep, {})
    assert schema == 2
    idx = [c for c in rep.schema_change.changes if c.change_type == "add_index"]
    assert all(c.on_new_table and c.severity == RiskLevel.LOW for c in idx)
    # the add_table itself is untouched
    tbl = next(c for c in rep.schema_change.changes if c.change_type == "add_table")
    assert tbl.on_new_table is False


def test_index_on_existing_table_not_downgraded():
    rep = _rep(schema_change=SchemaChangeResult(changes=[
        SchemaChange(file_path="db/y.sql", change_type="add_index",
                     table_name="existing_large_table", column_name="customer_id",
                     severity=RiskLevel.MEDIUM, description="New index"),
    ]))
    _, _, schema, _ = guard_false_positives(rep, {})
    assert schema == 0
    assert rep.schema_change.changes[0].severity == RiskLevel.MEDIUM
