"""
tests/test_report_purge.py
----------------------------
Admin report-purge endpoint: dry-run preview, repo-substring + age filters,
and the safety guard against matching everything.
"""
from __future__ import annotations
import os
import pytest
from fastapi.testclient import TestClient

from core.models import AnalysisReport, ChangeType, RiskResult, RiskLevel, GateDecision


@pytest.fixture
def client():
    os.environ["SKIP_AUTH"] = "true"
    from api.app import create_app
    app = create_app()
    c = TestClient(app)
    # Seed: 2 test/demo repos + 1 "real" repo
    from api.app import get_report_store
    st = get_report_store()
    for i, repo in enumerate(["https://github.com/test/demo",
                              "https://github.com/test/demo",
                              "https://github.com/acme/payments"]):
        rep = AnalysisReport(request_id=f"purge-{i}", change_type=ChangeType.PR,
                             repo_url=repo, source_ref=f"f{i}", target_ref="main",
                             risk=RiskResult(overall_risk=RiskLevel.LOW, risk_score=10,
                                             gate_decision=GateDecision.APPROVE))
        rep.freeze_gate()
        st.save(rep)
    return c


def test_dry_run_previews_without_deleting(client):
    r = client.post("/admin/reports/purge", json={"repo_contains": "test/demo", "dry_run": True})
    assert r.status_code == 200
    d = r.json()
    assert d["dry_run"] is True
    assert d["would_delete"] >= 2
    # nothing actually deleted — the real repo report still resolves
    from api.app import get_report_store
    assert get_report_store().get("purge-2") is not None


def test_purge_deletes_matching(client):
    r = client.post("/admin/reports/purge", json={"repo_contains": "test/demo", "dry_run": False})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 2
    from api.app import get_report_store
    st = get_report_store()
    assert st.get("purge-0") is None       # demo report gone
    assert st.get("purge-2") is not None    # real report kept


def test_refuses_to_match_everything(client):
    r = client.post("/admin/reports/purge", json={"dry_run": True})  # no filters
    assert r.status_code == 400
