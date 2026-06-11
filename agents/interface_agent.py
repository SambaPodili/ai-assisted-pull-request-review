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
        # Data-model field additions + serialization-config edits are contract
        # changes even with no API spec file — the gap that left this agent silent
        # on POJO-only diffs and on JSON exclusion/annotation edits.
        additive = _detect_data_model_changes(request) + _detect_serialization_changes(request)

        has_iface_files = any(
            _is_openapi(h.file_path) or _is_proto(h.file_path) or _is_asyncapi(h.file_path)
            for h in request.hunks
        )

        if not has_iface_files:
            # No interface spec files — skip the LLM, but still report structural
            # breaks and additive data-model changes from the source diff.
            dur = round(time.monotonic() - t0, 3)
            self.report_static_progress(request, dur)
            return InterfaceResult(
                breaking_changes=structural_breaks,
                schema_diffs=[],
                affected_consumers=[],
                additive_changes=additive,
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
        if additive and not llm_result.additive_changes:
            llm_result.additive_changes = additive
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
            additive_changes=_detect_data_model_changes(request) + _detect_serialization_changes(request),
        )


# ── Data-model field-addition detector (additive contract changes) ────────────
# Adding a field to a serializable class (DTO/entity/POJO/record) is not breaking,
# but it IS contract-relevant: the field appears in JSON/API output, consumers and
# deserializers must tolerate it, and docs/backward-compat should be checked.
# Detected deterministically so the interface agent is never silent on POJO edits.

_DATA_FILE_HINT = re.compile(
    r'(?i)(model|dto|entity|domain|pojo|bean|record|schema|payload|'
    r'request|response|resource|vo|contract|wrapper|mapper|application|details)'
)
# Java/Kotlin/C#/Scala field: optional annotations/modifiers, Capitalized type, name, ; or =
_JAVA_FIELD = re.compile(
    r'^\s*(?:@\w+(?:\([^)]*\))?\s+)*'
    r'(?:public|private|protected|internal)?\s*'
    r'(?:static\s+|final\s+|val\s+|var\s+|transient\s+|volatile\s+|readonly\s+)*'
    r'([A-Z][A-Za-z0-9_]*(?:<[^>]+>)?(?:\[\])?)\s+'      # Type (Capitalized)
    r'([a-z_][A-Za-z0-9_]*)\s*[;=]'                       # fieldName then ; or =
)
# TypeScript/Kotlin style:  fieldName: Type
_TS_FIELD = re.compile(r'^\s*(?:readonly\s+)?([a-z_][A-Za-z0-9_]*)\s*[?!]?\s*:\s*[A-Za-z]')
_FIELD_SKIP = {"return", "if", "for", "while", "new", "import", "package", "throw",
               "else", "case", "switch", "assert", "public", "private", "catch"}
_DATA_LANGS = {"java", "kotlin", "csharp", "c#", "cs", "scala", "groovy", "typescript", "ts"}


def _detect_data_model_changes(request: AnalysisRequest, cap: int = 30) -> list[str]:
    """Return human-readable notes for fields added to data-model classes."""
    notes: list[str] = []
    seen: set[tuple] = set()
    for hunk in request.hunks:
        lang = (getattr(hunk, "language", "") or "").lower()
        path = hunk.file_path
        base = path.replace("\\", "/").split("/")[-1]
        data_ish = lang in _DATA_LANGS or bool(_DATA_FILE_HINT.search(base))
        if not data_ish:
            continue
        for raw in hunk.content.splitlines():
            if not raw.startswith("+") or raw.startswith("+++"):
                continue
            line = raw[1:]
            field = None
            m = _JAVA_FIELD.match(line)
            if m and m.group(1).split("<")[0] not in _FIELD_SKIP and m.group(2) not in _FIELD_SKIP:
                field = m.group(2)
            elif lang in ("typescript", "ts", "kotlin"):
                m2 = _TS_FIELD.match(line)
                if m2 and m2.group(1) not in _FIELD_SKIP:
                    field = m2.group(1)
            if not field:
                continue
            key = (base, field)
            if key in seen:
                continue
            seen.add(key)
            notes.append(
                f"Field '{field}' added to {base} — appears in serialized output; "
                f"verify consumer/deserializer backward-compatibility and update API docs."
            )
            if len(notes) >= cap:
                return notes
    return notes


# Serialization config: edits to field exclusion/inclusion maps or @Json* annotations
# change what appears in JSON output — a contract concern (e.g. adding a field to an
# OBJECT_FIELD_EXCLUSIONS map hides it from consumers that may rely on it).
_SERIAL_TOKEN = re.compile(
    r'(?i)(exclusion|excluded|fieldfilter|field_filter|hidden_?fields?|'
    r'ignoredfields?|@jsonignore|@jsonproperty|@jsoninclude|@jsonignoreproperties)'
)
_QUOTED = re.compile(r'["\']([A-Za-z_]\w+)["\']')


def _detect_serialization_changes(request: AnalysisRequest, cap: int = 20) -> list[str]:
    """Notes for edits to serialization config (exclusion maps, @Json* annotations)."""
    notes: list[str] = []
    seen: set[tuple] = set()
    for hunk in request.hunks:
        base = hunk.file_path.replace("\\", "/").split("/")[-1]
        for raw in hunk.content.splitlines():
            if not raw.startswith("+") or raw.startswith("+++"):
                continue
            line = raw[1:]
            if not _SERIAL_TOKEN.search(line):
                continue
            low = line.lower()
            fields = _QUOTED.findall(line)
            excluding = any(t in low for t in ("exclu", "ignore", "hidden", "filter"))
            for f in fields:
                key = (base, f)
                if key in seen:
                    continue
                seen.add(key)
                if excluding:
                    notes.append(
                        f"{base}: '{f}' added to a serialization exclusion list — it will be "
                        f"hidden from JSON output; confirm no consumer depends on it (potential "
                        f"contract change) and that this is intended (e.g. masking sensitive data)."
                    )
                else:
                    notes.append(
                        f"{base}: serialization mapping changed for '{f}' — verify the JSON "
                        f"contract and downstream consumers."
                    )
                if len(notes) >= cap:
                    return notes
            if not fields and ("@jsonignore" in low or "@jsonignoreproperties" in low):
                key = (base, "@JsonIgnore")
                if key not in seen:
                    seen.add(key)
                    notes.append(
                        f"{base}: a field/class is now @JsonIgnore — it will no longer appear in "
                        f"serialized output; consumers relying on it will stop receiving it."
                    )
    return notes


# OpenAPI structural keywords — spec scaffolding, not contract fields.
_OPENAPI_STRUCTURAL = {
    "get", "post", "put", "delete", "patch", "head", "options", "trace",
    "summary", "description", "operationid", "parameters", "responses",
    "requestbody", "tags", "deprecated", "security", "servers", "content",
    "schema", "name", "in", "required",
}


# ── Structural parsers ────────────────────────────────────────────────────────

def _parse_openapi_diff(diff: str, file_path: str) -> list[ContractBreak]:
    breaks: list[ContractBreak] = []
    in_removed_path = False
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            in_removed_path = False
            continue
        content = line[1:].strip()

        # Removed API path
        if re.match(r"/([\w{}/.-]+):", content):
            in_removed_path = True
            breaks.append(ContractBreak(
                interface_type="REST",
                path=f"{file_path}: {content.rstrip(':')}",
                break_type="removed",
                severity=RiskLevel.CRITICAL,
            ))
        # Removed field from schema object (indented property key)
        elif re.match(r"[a-zA-Z_][a-zA-Z0-9_]*:", content) and not content.startswith("type:"):
            key = content.split(":")[0].lower()
            # Children of an already-reported removed path, and OpenAPI structural
            # keywords (verbs / metadata), are not contract fields — reporting them
            # individually is pure noise on top of the path-removal finding.
            if in_removed_path or key in _OPENAPI_STRUCTURAL:
                continue
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
