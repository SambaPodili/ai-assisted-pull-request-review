"""
tests/test_qa_scenarios.py
--------------------------
Regression tests for the QA-scenario fallback classifier.

Driven by real user feedback: a plain CREATE TABLE / DDL file was being
classified as a SECURITY concern (purely because the word "sql" appeared) and
the agent then emitted nonsensical "test authentication flows -> 401 / injection
attack vectors" steps for a file that has no HTTP layer at all.

A DDL file must be a DATA concern only; genuine injection / auth / token code
must still be flagged SECURITY.
"""
from __future__ import annotations

import uuid

from core.models import AnalysisRequest, ChangeType, DiffHunk, QAScenarioType as T
from agents.qa_scenarios_agent import _categorise_hunk, _build_fallback_scenarios


# ── classifier ────────────────────────────────────────────────────────────────

def test_plain_ddl_is_data_not_security():
    ddl = "CREATE TABLE PDN_VRM_TBL ( id NUMBER PRIMARY KEY, name VARCHAR2(100) );"
    cats = _categorise_hunk("db/PDN_VRM_DDL_TBL_CREATION_008.sql", ddl)
    assert T.DATA in cats, "DDL should be a DATA / migration concern"
    assert T.SECURITY not in cats, "a plain DDL must NOT be flagged SECURITY"


def test_sql_injection_code_is_security():
    code = 'String q = "SELECT * FROM u WHERE n=" + name;  // sql injection risk'
    cats = _categorise_hunk("src/Dao.java", code)
    assert T.SECURITY in cats


def test_camelcase_security_identifiers_still_match():
    # word boundaries would miss these; substring matching must keep them
    assert T.SECURITY in _categorise_hunk("src/Login.java",
                                          "if(!user.hasPermission()) throw new AuthException();")
    assert T.SECURITY in _categorise_hunk("src/Token.java", "String t = getUserToken();")


# ── generated scenarios ─────────────────────────────────────────────────────────

def _ddl_request() -> AnalysisRequest:
    return AnalysisRequest(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://bitbucket.org/mybank/db-scripts",
        source_ref="feature/new-table",
        target_ref="main",
        hunks=[DiffHunk(
            file_path="db/PDN_VRM_DDL_TBL_CREATION_008.sql",
            language="sql",
            additions=12,
            deletions=0,
            content="CREATE TABLE PDN_VRM_TBL ( id NUMBER PRIMARY KEY, name VARCHAR2(100) );",
        )],
    )


def test_ddl_fallback_has_no_http_auth_steps():
    scenarios = _build_fallback_scenarios(_ddl_request())
    types = {s.type for s in scenarios}
    assert T.DATA in types, "DDL change must yield a data-integrity/migration scenario"
    assert T.SECURITY not in types, "DDL change must not yield a security scenario"

    # belt-and-braces: no scenario should reference HTTP-auth status codes
    joined = " ".join(
        " ".join(s.steps) + " " + s.expected_result for s in scenarios
    ).lower()
    assert "401" not in joined and "403" not in joined, \
        "DDL scenarios must not mention HTTP auth status codes"
