"""
tests/test_fixes_and_consumers.py
-----------------------------------
Concrete fix-diff generation + downstream consumer-impact tracing.
Both are deterministic — no LLM.
"""
from __future__ import annotations

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk,
    AnalysisReport, RiskResult, RiskLevel, GateDecision,
    InterfaceResult, ContractBreak, ReferenceImpactResult, SymbolReference,
    SchemaChangeResult, SchemaChange,
)
from agents.fix_generator import generate_fixes
from governance.consumer_impact import trace_consumer_impacts


def _hunk(file_path, added_lines, start=10):
    body = [f"@@ -1,1 +{start},{len(added_lines)} @@"] + [f"+{l}" for l in added_lines]
    return DiffHunk(file_path=file_path, language="python", additions=len(added_lines),
                    deletions=0, content="\n".join(body))


def _req(*hunks) -> AnalysisRequest:
    return AnalysisRequest(request_id="t", change_type=ChangeType.PR, repo_url="r",
                           source_ref="a", target_ref="b", hunks=list(hunks))


# ── Fix generation ──────────────────────────────────────────────────────────

def test_hardcoded_secret_fix():
    fixes = generate_fixes(_req(_hunk("cfg.py", ['API_KEY = "sk-abc123def456"'])))
    f = next(x for x in fixes if x.category == "security" and "secret" in x.title.lower())
    assert 'os.environ["API_KEY"]' in f.after
    assert f.confidence == "high"
    assert f.diff.startswith("--- a/cfg.py")


def test_weak_hash_fix():
    fixes = generate_fixes(_req(_hunk("h.py", ["digest = hashlib.md5(data).hexdigest()"])))
    f = next(x for x in fixes if "hash" in x.title.lower())
    assert "hashlib.sha256(" in f.after


def test_bare_except_fix():
    fixes = generate_fixes(_req(_hunk("a.py", ["    except:"])))
    f = next(x for x in fixes if "except" in x.title.lower())
    assert "except Exception as exc:" in f.after


def test_verify_false_fix():
    fixes = generate_fixes(_req(_hunk("net.py", ["resp = requests.get(url, verify=False)"])))
    f = next(x for x in fixes if "tls" in x.title.lower() or "verif" in x.title.lower())
    assert "verify=True" in f.after


def test_clean_code_yields_no_fixes():
    fixes = generate_fixes(_req(_hunk("ok.py", ["x = compute_total(items)", "return x"])))
    assert fixes == []


# ── Consumer impact tracing ──────────────────────────────────────────────────

def _report_with(interface=None, schema=None, refs=None):
    return AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        risk=RiskResult(overall_risk=RiskLevel.HIGH, risk_score=70, gate_decision=GateDecision.HOLD),
        interface=interface, schema_change=schema, reference_impact=refs,
    )


def test_breaking_endpoint_maps_to_caller():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="/v1/refund", break_type="removed")])
    refs = ReferenceImpactResult(
        changed_symbols=["refund"],
        references=[SymbolReference(symbol="refund", file_path="clients/payment_client.py", line=88)],
        total_references=1, high_impact_files=["clients/payment_client.py"],
        intra_project_risk=RiskLevel.HIGH, search_backend="local_grep")
    impacts = trace_consumer_impacts(_report_with(interface=iface, refs=refs))
    assert any(i.file_path == "clients/payment_client.py" and i.line == 88
               and "404" in i.failure_mode for i in impacts)


def test_breaking_change_without_caller_still_recorded():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="/v1/legacy", break_type="removed")])
    impacts = trace_consumer_impacts(_report_with(interface=iface,
                                                  refs=ReferenceImpactResult(
                                                      changed_symbols=[], references=[], total_references=0,
                                                      high_impact_files=[], intra_project_risk=RiskLevel.LOW,
                                                      search_backend="none")))
    assert any(i.change == "/v1/legacy" for i in impacts)


def test_dropped_column_maps_to_caller():
    schema = SchemaChangeResult(changes=[
        SchemaChange(change_type="drop_column", table_name="users", column_name="email_verified",
                     severity=RiskLevel.HIGH, reversible=False, description="drop col",
                     file_path="migrations/003.sql")],
        has_destructive=True, has_irreversible=True)
    refs = ReferenceImpactResult(
        changed_symbols=["email_verified"],
        references=[SymbolReference(symbol="email_verified", file_path="services/auth.py", line=12)],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.HIGH, search_backend="local_grep")
    impacts = trace_consumer_impacts(_report_with(schema=schema, refs=refs))
    assert any(i.change_type == "drop_column" and i.file_path == "services/auth.py" for i in impacts)


def test_no_breaking_changes_no_impacts():
    assert trace_consumer_impacts(_report_with()) == []
