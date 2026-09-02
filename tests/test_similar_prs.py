"""
tests/test_similar_prs.py
-------------------------
Office-deployment regression: "Similar past PRs" showed several entries all at
an identical 50%, because the fingerprint collapsed to (finding-file overlap +
a flat 0.2 same-repo bonus) and near-identical past runs weren't deduped.

Now it fingerprints on the real changed-file set with build/config/test noise
stripped, and collapses identical (repo, branch, file-set) history.
"""
from datetime import datetime, timedelta

import pytest

from core.models import AnalysisReport, ChangeType
import api.routes.insights as insights


def _report(rid, files, repo="https://git/acme/svc", branch="feature/x", ago_h=1):
    r = AnalysisReport(
        request_id=rid, change_type=ChangeType.PR, repo_url=repo,
        source_ref=branch, target_ref="master",
    )
    r.files_changed_list = files
    r.completed_at = datetime.utcnow() - timedelta(hours=ago_h)
    return r


@pytest.fixture
def patched_store(monkeypatch):
    reports: dict[str, AnalysisReport] = {}

    class _Store:
        def get(self, rid):
            return reports.get(rid)

    monkeypatch.setattr(insights, "_get_store", lambda: _Store())
    monkeypatch.setattr(insights, "_load_reports", lambda limit=500: list(reports.values()))
    return reports


def test_shared_pom_only_is_not_similar(patched_store):
    patched_store["target"] = _report("target", ["cswservice-core/pom.xml", "cswservice-id/pom.xml"])
    patched_store["other"] = _report("other", ["pom.xml", "cswservice-my/pom.xml"], branch="feature/y")

    out = insights.similar_prs("target")
    assert out["similar"] == []


def test_shared_real_source_is_similar(patched_store):
    patched_store["target"] = _report("target", ["src/main/java/com/x/FeeService.java", "pom.xml"])
    patched_store["other"] = _report(
        "other", ["src/main/java/com/x/FeeService.java", "src/main/java/com/x/Other.java"],
        branch="feature/y",
    )

    out = insights.similar_prs("target")
    assert len(out["similar"]) == 1
    assert out["similar"][0]["similarity"] >= 0.3


def test_identical_history_is_deduped(patched_store):
    patched_store["target"] = _report("target", ["src/main/java/com/x/FeeService.java"])
    for i in range(4):
        patched_store[f"run{i}"] = _report(
            f"run{i}", ["src/main/java/com/x/FeeService.java"], branch="feature/dup", ago_h=i + 1,
        )

    out = insights.similar_prs("target")
    assert len(out["similar"]) == 1  # 4 identical past runs collapse to one
