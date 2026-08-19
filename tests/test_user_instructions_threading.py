"""
tests/test_user_instructions_threading.py
--------------------------------------------
Proves the opt-in boundary for AnalysisRequest.user_instructions: it must
appear (wrapped by the untrusted-context delimiter) in the 5 agents that
deliberately consume it, and must NOT appear in agents that don't — even
though every agent's build_user_prompt receives the same request object.
"""
from __future__ import annotations

import uuid

from core.models import AnalysisRequest, ChangeType, DiffHunk
from agents.base_agent import format_user_priorities

MARKER = "USER-SUPPLIED REVIEW PRIORITIES"
PRIORITY_TEXT = "focus on security in the payment module"


def _req(**kw) -> AnalysisRequest:
    base = dict(
        request_id=str(uuid.uuid4()),
        change_type=ChangeType.PR,
        repo_url="https://github.com/bank/payments",
        source_ref="feature/x",
        target_ref="main",
        hunks=[DiffHunk(file_path="Foo.java", language="java", additions=3, deletions=1,
                         content="+// x\n-// y\n")],
        user_instructions=PRIORITY_TEXT,
    )
    base.update(kw)
    return AnalysisRequest(**base)


def test_format_user_priorities_wraps_and_delimits():
    out = format_user_priorities(PRIORITY_TEXT)
    assert MARKER in out
    assert PRIORITY_TEXT in out
    assert "END USER PRIORITIES" in out


def test_format_user_priorities_empty_text_is_empty():
    assert format_user_priorities("") == ""
    assert format_user_priorities("   ") == ""


# ── Opted-in agents ──────────────────────────────────────────────────────────

def test_code_analysis_includes_priorities():
    from agents.code_analysis_agent import CodeAnalysisAgent
    prompt = CodeAnalysisAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER in prompt and PRIORITY_TEXT in prompt


def test_security_includes_priorities():
    from agents.security_agent import SecurityReviewAgent
    prompt = SecurityReviewAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER in prompt and PRIORITY_TEXT in prompt


def test_qa_scenarios_includes_priorities():
    from agents.qa_scenarios_agent import QAScenariosAgent
    prompt = QAScenariosAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER in prompt and PRIORITY_TEXT in prompt


def test_risk_includes_priorities():
    from agents.risk_agent import RiskAssessmentAgent
    prompt = RiskAssessmentAgent(api_key=None).build_user_prompt(_req(), {"partial_report": {}})
    assert MARKER in prompt and PRIORITY_TEXT in prompt


def test_remediation_includes_priorities():
    from agents.remediation_agent import RemediationAgent
    prompt = RemediationAgent(api_key=None).build_user_prompt(_req(), {"full_report": {}})
    assert MARKER in prompt and PRIORITY_TEXT in prompt


# ── Deliberately untouched agents — opt-in boundary must hold ───────────────

def test_secrets_entropy_excludes_priorities():
    from agents.secrets_entropy_agent import SecretsEntropyAgent
    prompt = SecretsEntropyAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER not in prompt and PRIORITY_TEXT not in prompt


def test_ast_analysis_excludes_priorities():
    from agents.ast_analysis_agent import ASTAnalysisAgent
    prompt = ASTAnalysisAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER not in prompt and PRIORITY_TEXT not in prompt


def test_license_compliance_excludes_priorities():
    from agents.license_compliance_agent import LicenseComplianceAgent
    prompt = LicenseComplianceAgent(api_key=None).build_user_prompt(_req(), {})
    assert MARKER not in prompt and PRIORITY_TEXT not in prompt
