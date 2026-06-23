"""
tests/test_usage_telemetry.py
-----------------------------
GenAI usage telemetry → ELK. Pins the agreed field mapping (user_id = repo slug,
app_code = last 3 chars of the project key, domain = logged user's), the 5-doc
success lifecycle, and that emission is a safe no-op when disabled.
"""
from types import SimpleNamespace as NS

from governance import usage_telemetry as ut


def _cfg(**kw):
    base = dict(
        elk_usage_enabled=True,
        elk_usage_url="https://example/genai_usage/_doc/",
        elk_tool_id="G040", elk_tool_name="Code Analysis and Review",
        elk_tool_version="1.0.0", elk_app_code_default="CLR",
        elk_integration_id="ownpccoelkint", elk_environment="SIT",
        elk_default_domain="", elk_auth_header="ApiKey LW123", elk_verify_ssl=True, elk_timeout_s=5.0,
        elk_accept="application/vnd.elasticsearch+json; compatible-with=8",
        elk_content_type="application/vnd.elasticsearch+json; compatible-with=8",
    )
    base.update(kw)
    return NS(**base)


# ── field mapping ─────────────────────────────────────────────────────────────

def test_slug_and_app_code_from_bitbucket_server():
    # PROJECT KEY / repo  →  user_id = repo, app_code = last 3 of project key
    proj, repo = ut.slug_parts("SOMECLR/uncs16")
    assert proj == "SOMECLR" and repo == "uncs16"
    assert ut._app_code(proj, "XXX") == "CLR"


def test_slug_from_full_https_url():
    proj, repo = ut.slug_parts("https://bitbucket.company.com/scm/SOMECLR/uncs16.git")
    assert proj == "SOMECLR" and repo == "uncs16"


def test_user_id_is_actual_user_not_repo():
    # When a real user is supplied it must win over the repo slug; repo stays in metadata.
    d = ut.started_doc("task-1", "SOMECLR/uncs16", "uobgroup.com", files_changed=2,
                       user_id="unc12345", cfg=_cfg())
    assert d["user_id"] == "unc12345"
    assert d["metadata"]["repo_slug"] == "uncs16"


def test_user_id_falls_back_to_repo_when_no_user():
    # No user known → fall back to repo slug so the field is never empty.
    d = ut.started_doc("task-1", "SOMECLR/uncs16", "uobgroup.com", cfg=_cfg())
    assert d["user_id"] == "uncs16"


def test_doc_shape_matches_portal_schema():
    d = ut.started_doc("task-1", "SOMECLR/uncs16", "uobgroup.com", files_changed=4, cfg=_cfg())
    # exact field set the portal expects
    assert set(d) == {"id", "user_id", "action", "task_id", "description", "timestamp",
                      "metadata", "tool_version", "tool_id", "tool_name", "app_code", "domain",
                      "integration_id", "environment"}
    assert d["user_id"] == "uncs16"          # = repo slug
    assert d["app_code"] == "CLR"            # = last 3 of project key
    assert d["domain"] == "uobgroup.com"
    assert d["integration_id"] == "ownpccoelkint" and d["environment"] == "SIT"
    assert d["tool_id"] == "G040" and d["app_code"]
    assert d["metadata"]["repo_slug"] == "uncs16" and d["metadata"]["files_changed"] == 4


# ── lifecycle ─────────────────────────────────────────────────────────────────

def _report():
    return NS(request_id="task-9", repo_url="SOMECLR/uncs16", risk_score=42,
              gate_decision=NS(value="HOLD"),
              security=NS(findings=[NS(severity="critical"), NS(severity="high"), NS(severity="low")]),
              top_issues=[1, 2, 3])


def test_success_lifecycle_is_two_docs():
    cfg = _cfg()
    started = ut.started_doc("task-9", "SOMECLR/uncs16", "uobgroup.com", 4, cfg=cfg)
    completion = ut.completion_docs(_report(), "uobgroup.com", result_length=9700, duration_s=12.3, cfg=cfg)
    # A run emits exactly 2 docs: started + a single consolidated success doc.
    assert len(completion) == 1
    actions = [started["action"]] + [d["action"] for d in completion]
    assert actions == ["code_analysis_started", "code_analysis_success"]
    md = completion[0]["metadata"]
    # the one success doc carries analysis + security + gate + report metadata
    assert md["result_length"] == 9700 and md["duration_s"] == 12.3
    assert md["gate"] == "HOLD" and md["security_findings"] == 3
    assert md["critical"] == 1 and md["high"] == 1


def test_failure_path_is_two_docs():
    cfg = _cfg()
    fail = ut.failure_doc("task-9", "SOMECLR/uncs16", "uobgroup.com", "boom", cfg=cfg)
    assert fail["action"] == "code_analysis_failure"
    assert fail["metadata"]["error"] == "boom"


# ── emission safety ───────────────────────────────────────────────────────────

def test_emit_noop_when_disabled():
    assert ut.emit([ut.failure_doc("t", "P/r", "d", "x")], cfg=_cfg(elk_usage_enabled=False)) == 0


def test_emit_posts_each_doc(monkeypatch):
    posted = []
    class _Resp:
        status_code = 201
        text = ""
    def _fake_post(url, json=None, headers=None, timeout=None, verify=None):
        posted.append((url, json, headers))
        return _Resp()
    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    docs = ut.completion_docs(_report(), "uobgroup.com", 100, 1.0, cfg=_cfg())
    sent = ut.emit(docs, cfg=_cfg())
    assert sent == 1 and len(posted) == 1
    assert all(p[0] == "https://example/genai_usage/_doc/" for p in posted)
    # the configured content-negotiation + auth headers are sent on every doc
    h = posted[0][2]
    assert h["Accept"] == "application/vnd.elasticsearch+json; compatible-with=8"
    assert h["Content-Type"] == "application/vnd.elasticsearch+json; compatible-with=8"
    assert h["Authorization"] == "ApiKey LW123"
