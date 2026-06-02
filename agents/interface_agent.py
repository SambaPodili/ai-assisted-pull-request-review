"""
agents/interface_agent.py
--------------------------
Phase 2 agent: detects contract-breaking changes in REST/gRPC/AsyncAPI/MQ interfaces.

Two passes:
  1. Structural scan  — parses OpenAPI/proto/AsyncAPI diffs deterministically (zero tokens)
  2. LLM pass        — Sonnet for semantic/ambiguous changes (runs only if interface files changed)

Fallback: structural scan only.
"""
from __future__ import annotations
import json
import re
from typing import Any

from core.models import (
    AgentName, AnalysisRequest,
    InterfaceResult, ContractBreak, RiskLevel,
)
from core.token_manager import trim_diff_for_budget
from agents.base_agent import BaseAgent


class InterfaceAnalysisAgent(BaseAgent[InterfaceResult]):

    agent_name   = AgentName.INTERFACE
    output_model = InterfaceResult

    system_prompt = (
        "You are an API governance expert for enterprise banking systems.\n"
        "Analyse the interface file diffs for contract-breaking changes:\n"
        "  1. REST/OpenAPI: removed paths, removed/renamed fields, new required fields, type changes\n"
        "  2. gRPC/Proto: field number changes, type changes, removed fields\n"
        "  3. AsyncAPI/Kafka/MQ: topic renames, schema field removals, partition key changes\n"
        "  4. For each break: interface_type, path, break_type, consumers (downstream services), severity\n\n"
        "break_type values: removed | renamed | type_change | required_added | schema_incompatible\n"
        "Output ONLY compact JSON. No prose."
    )

    # ── Structural detection (always runs, zero tokens) ───────────────────────

    def detect_structural_breaks(self, request: AnalysisRequest) -> list[ContractBreak]:
        """Deterministic parsing of interface diffs — no LLM required."""
        breaks: list[ContractBreak] = []
        for hunk in request.hunks:
            path = hunk.file_path
            if _is_openapi(path):
                breaks.extend(_parse_openapi_diff(hunk.content, path))
            elif _is_proto(path):
                breaks.extend(_parse_proto_diff(hunk.content, path))
            elif _is_asyncapi(path):
                breaks.extend(_parse_asyncapi_diff(hunk.content, path))
            elif _is_shared_lib(path):
                breaks.extend(_parse_shared_lib_break(hunk.content, path))
        return breaks

    # ── Override run to apply both passes ────────────────────────────────────

    def run(self, request: AnalysisRequest, budget, context: dict | None = None) -> InterfaceResult:
        import time
        t0 = time.monotonic()
        ctx = context or {}
        structural_breaks = self.detect_structural_breaks(request)

        has_iface_files = any(
            _is_openapi(h.file_path) or _is_proto(h.file_path) or _is_asyncapi(h.file_path)
            for h in request.hunks
        )

        if not has_iface_files:
            # No interface files changed — skip LLM entirely
            dur = round(time.monotonic() - t0, 3)
            self.report_static_progress(request, dur)
            return InterfaceResult(
                breaking_changes=structural_breaks,
                schema_diffs=[],
                affected_consumers=[],
                duration_s=dur,
            )

        # LLM pass for semantic analysis
        llm_result = super().run(request, budget, ctx)

        # Merge: structural findings take precedence; deduplicate by path+break_type
        seen   = {(b.path, b.break_type) for b in structural_breaks}
        merged = structural_breaks + [
            b for b in llm_result.breaking_changes
            if (b.path, b.break_type) not in seen
        ]
        llm_result.breaking_changes = merged
        return llm_result

    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str:
        iface_hunks = [
            h for h in request.hunks
            if _is_openapi(h.file_path) or _is_proto(h.file_path) or _is_asyncapi(h.file_path)
        ] or request.hunks   # fall back to all if no specific interface files

        from agents.base_agent import format_hunks_for_prompt
        trimmed  = format_hunks_for_prompt(iface_hunks, max_chars_per_hunk=3000, focus="interface")
        consumers = context.get("known_consumers", {})

        return (
            f"Repository: {request.repo_url}\n"
            f"Known consumers: {json.dumps(consumers)}\n\n"
            f"Interface diffs:\n{trimmed}"
        )


    def fallback_result(self, request: AnalysisRequest) -> InterfaceResult:
        breaks = self.detect_structural_breaks(request)
        return InterfaceResult(
            breaking_changes=breaks,
            schema_diffs=[
                f"Static scan of {h.file_path}"
                for h in request.hunks if _is_openapi(h.file_path)
            ],
            affected_consumers=[],
        )


# ── Structural parsers ────────────────────────────────────────────────────────

def _parse_openapi_diff(diff: str, file_path: str) -> list[ContractBreak]:
    breaks: list[ContractBreak] = []
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        content = line[1:].strip()

        # Removed API path
        if re.match(r"/([\w{}/.-]+):", content):
            breaks.append(ContractBreak(
                interface_type="REST",
                path=f"{file_path}: {content.rstrip(':')}",
                break_type="removed",
                severity=RiskLevel.CRITICAL,
            ))
        # Removed field from schema object (indented property key)
        elif re.match(r"[a-zA-Z_][a-zA-Z0-9_]*:", content) and not content.startswith("type:"):
            breaks.append(ContractBreak(
                interface_type="REST",
                path=f"{file_path}: field '{content.split(':')[0]}'",
                break_type="removed",
                severity=RiskLevel.HIGH,
            ))
        # Type change
        elif content.startswith("type:"):
            breaks.append(ContractBreak(
                interface_type="REST",
                path=f"{file_path}: type declaration",
                break_type="type_change",
                severity=RiskLevel.HIGH,
            ))
    return breaks


def _parse_proto_diff(diff: str, file_path: str) -> list[ContractBreak]:
    breaks: list[ContractBreak] = []
    field_pattern = re.compile(
        r"^\s*(optional|required|repeated)?\s*(\w+)\s+(\w+)\s*=\s*(\d+)"
    )
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        m = field_pattern.match(line[1:])
        if m:
            field_name   = m.group(3)
            field_number = m.group(4)
            breaks.append(ContractBreak(
                interface_type="gRPC",
                path=f"{file_path}: field '{field_name}' (number {field_number})",
                break_type="removed",
                severity=RiskLevel.CRITICAL,
                consumers=[],
            ))
    return breaks


def _parse_asyncapi_diff(diff: str, file_path: str) -> list[ContractBreak]:
    breaks: list[ContractBreak] = []
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        content = line[1:].strip()
        # Removed channel / topic
        if re.match(r"[a-zA-Z0-9._/-]+:", content):
            breaks.append(ContractBreak(
                interface_type="AsyncAPI",
                path=f"{file_path}: {content.rstrip(':')}",
                break_type="removed",
                severity=RiskLevel.HIGH,
            ))
    return breaks


# ── File type detectors ───────────────────────────────────────────────────────

_SHARED_LIB_RE = re.compile(
    r'(?:^|/)(?:shared|common|core|lib|libs|util|utils|base|sdk|'
    r'platform|framework|contract|domain|kernel|foundation)(?:/|$)',
    re.I,
)

def _is_shared_lib(path: str) -> bool:
    return bool(_SHARED_LIB_RE.search(path)) and not _is_openapi(path) and not _is_proto(path)


def _parse_shared_lib_break(diff: str, file_path: str) -> list[ContractBreak]:
    """Flag removed public symbols in shared/common modules as potential cross-project breaks."""
    breaks: list[ContractBreak] = []
    # Look for removed function/class/method signatures in shared code
    sig_pattern = re.compile(
        r'^-(.*(?:def|func|function|class|interface|struct|public|export)\s+\w+)',
        re.I,
    )
    for line in diff.splitlines():
        if line.startswith("---"):
            continue
        m = sig_pattern.match(line)
        if m:
            symbol = m.group(1).strip()[:80]
            breaks.append(ContractBreak(
                interface_type="SharedLib",
                path=f"{file_path}: {symbol}",
                break_type="removed",
                consumers=[],
                severity=RiskLevel.HIGH,
            ))
            if len(breaks) >= 5:   # cap noise
                break
    return breaks


def _is_openapi(path: str) -> bool:
    return any(s in path.lower() for s in ("openapi", "swagger", "api-spec", "api_spec", "oas"))


def _is_proto(path: str) -> bool:
    return path.endswith(".proto")


def _is_asyncapi(path: str) -> bool:
    return any(s in path.lower() for s in ("asyncapi", "kafka", "event-schema", "mq-schema", "amqp"))
