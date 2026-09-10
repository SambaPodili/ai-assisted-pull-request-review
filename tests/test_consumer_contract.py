"""
tests/test_consumer_contract.py
---------------------------------
Field-level consumer-contract compatibility (governance/consumer_contract.py)
— deterministic, no LLM. See that module's docstring for what this does
differently from governance/consumer_impact.py's symbol/endpoint matching.
"""
from __future__ import annotations

from core.models import (
    AnalysisRequest, ChangeType, DiffHunk,
    AnalysisReport, RiskResult, RiskLevel, GateDecision,
    InterfaceResult, ContractBreak, ReferenceImpactResult, SymbolReference,
)
from agents.interface_agent import _parse_openapi_diff
from governance.consumer_contract import check_consumer_contracts


def _report_with(interface=None, refs=None):
    return AnalysisReport(
        request_id="t", change_type=ChangeType.PR, repo_url="r", source_ref="a", target_ref="b",
        risk=RiskResult(overall_risk=RiskLevel.HIGH, risk_score=70, gate_decision=GateDecision.HOLD),
        interface=interface, reference_impact=refs,
    )


def test_removed_field_referenced_in_another_repo_is_flagged():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="openapi.yaml: field 'email'",
                      break_type="removed", field_name="email")])
    refs = ReferenceImpactResult(
        changed_symbols=["UserDTO"],
        references=[SymbolReference(
            symbol="UserDTO", file_path="clients/user_client.py", line=42,
            context="user.email if user.email else None", repo="org/downstream-service",
        )],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.HIGH,
        search_backend="local_grep",
    )
    impacts = check_consumer_contracts(_report_with(interface=iface, refs=refs))
    assert len(impacts) == 1
    assert impacts[0].file_path == "clients/user_client.py"
    assert impacts[0].repo == "org/downstream-service"
    assert impacts[0].change_type == "field_removed"
    assert "email" in impacts[0].change


def test_same_repo_reference_is_ignored():
    """A reference with no `repo` set is same-repo — already covered by
    whatever the diff's own agents flag directly; not this module's job."""
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="openapi.yaml: field 'email'",
                      break_type="removed", field_name="email")])
    refs = ReferenceImpactResult(
        changed_symbols=["UserDTO"],
        references=[SymbolReference(
            symbol="UserDTO", file_path="same_repo_file.py", line=5, context="self.email = None",
        )],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.LOW,
        search_backend="local_grep",
    )
    assert check_consumer_contracts(_report_with(interface=iface, refs=refs)) == []


def test_reference_not_mentioning_the_field_is_not_flagged():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="openapi.yaml: field 'email'",
                      break_type="removed", field_name="email")])
    refs = ReferenceImpactResult(
        changed_symbols=["UserDTO"],
        references=[SymbolReference(
            symbol="UserDTO", file_path="clients/user_client.py", line=10,
            context="user = UserDTO.from_json(payload)", repo="org/downstream-service",
        )],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.HIGH,
        search_backend="local_grep",
    )
    assert check_consumer_contracts(_report_with(interface=iface, refs=refs)) == []


def test_no_field_level_breaks_yields_no_impacts():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="/v1/refund", break_type="removed")])  # no field_name
    refs = ReferenceImpactResult(
        changed_symbols=["refund"],
        references=[SymbolReference(symbol="refund", file_path="c.py", line=1,
                                    context="refund()", repo="org/x")],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.LOW,
        search_backend="local_grep",
    )
    assert check_consumer_contracts(_report_with(interface=iface, refs=refs)) == []


def test_type_change_includes_old_type_in_change_description():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="openapi.yaml: type declaration",
                      break_type="type_change", field_name="amount", old_type="string")])
    refs = ReferenceImpactResult(
        changed_symbols=["PaymentDTO"],
        references=[SymbolReference(
            symbol="PaymentDTO", file_path="clients/pay.py", line=7,
            context="parseInt(payment.amount)", repo="org/downstream-service",
        )],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.HIGH,
        search_backend="local_grep",
    )
    impacts = check_consumer_contracts(_report_with(interface=iface, refs=refs))
    assert len(impacts) == 1
    assert impacts[0].change_type == "field_type_change"
    assert "string" in impacts[0].change


# ── interface_agent's OpenAPI parser actually populates field_name/old_type ──

def test_openapi_parser_populates_field_name_on_removed_field():
    diff = "\n".join([
        "--- a/openapi.yaml",
        "+++ b/openapi.yaml",
        "@@ -10,3 +10,1 @@",
        " properties:",
        "-  email:",
        "-    type: string",
    ])
    breaks = _parse_openapi_diff(diff, "openapi.yaml")
    removed = [b for b in breaks if b.break_type == "removed"]
    assert any(b.field_name == "email" for b in removed)


def test_openapi_parser_populates_old_type_and_field_on_type_change():
    diff = "\n".join([
        "--- a/openapi.yaml",
        "+++ b/openapi.yaml",
        "@@ -10,2 +10,2 @@",
        "   amount:",
        "-    type: string",
        "+    type: integer",
    ])
    breaks = _parse_openapi_diff(diff, "openapi.yaml")
    type_changes = [b for b in breaks if b.break_type == "type_change"]
    assert len(type_changes) == 1
    assert type_changes[0].field_name == "amount"
    assert type_changes[0].old_type == "string"


def test_openapi_parser_populates_new_type_from_the_paired_added_line():
    """Regression test: `new_type` was declared on the model and mentioned in
    consumer_contract.py's docstring but never actually populated — every
    type_change break silently had new_type=None. A unified diff shows a
    one-line replacement as the removed line immediately followed by its
    added replacement; that adjacency is what lets this be recovered."""
    diff = "\n".join([
        "--- a/openapi.yaml",
        "+++ b/openapi.yaml",
        "@@ -10,2 +10,2 @@",
        "   amount:",
        "-    type: string",
        "+    type: integer",
    ])
    breaks = _parse_openapi_diff(diff, "openapi.yaml")
    type_changes = [b for b in breaks if b.break_type == "type_change"]
    assert len(type_changes) == 1
    assert type_changes[0].old_type == "string"
    assert type_changes[0].new_type == "integer"


def test_consumer_contract_description_shows_old_and_new_type():
    iface = InterfaceResult(breaking_changes=[
        ContractBreak(interface_type="REST", path="openapi.yaml: type declaration",
                      break_type="type_change", field_name="amount", old_type="string", new_type="integer")])
    refs = ReferenceImpactResult(
        changed_symbols=["PaymentDTO"],
        references=[SymbolReference(
            symbol="PaymentDTO", file_path="clients/pay.py", line=7,
            context="parseInt(payment.amount)", repo="org/downstream-service",
        )],
        total_references=1, high_impact_files=[], intra_project_risk=RiskLevel.HIGH,
        search_backend="local_grep",
    )
    impacts = check_consumer_contracts(_report_with(interface=iface, refs=refs))
    assert len(impacts) == 1
    assert "string" in impacts[0].change and "integer" in impacts[0].change


def test_full_field_deletion_is_not_also_reported_as_a_type_change():
    """Regression test: a field removed entirely (its key line AND its
    nested `type:` line both removed) used to also emit a spurious
    'type_change' break for the same field — misleading a reviewer into
    thinking the field still exists with a new type, when it was actually
    deleted. Must be exactly one 'removed' break, no 'type_change'."""
    diff = "\n".join([
        "--- a/openapi.yaml",
        "+++ b/openapi.yaml",
        "@@ -10,2 +10,0 @@",
        " properties:",
        "-  email:",
        "-    type: string",
    ])
    breaks = _parse_openapi_diff(diff, "openapi.yaml")
    assert len(breaks) == 1
    assert breaks[0].break_type == "removed"
    assert breaks[0].field_name == "email"
