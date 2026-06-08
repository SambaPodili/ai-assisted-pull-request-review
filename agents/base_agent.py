"""
agents/base_agent.py
--------------------
Abstract base class for all specialist agents.
Uses UnifiedLLMClient so agents work with Anthropic, OpenAI, Ollama, or Azure.

Model config resolution order (highest priority first):
  1. context["model_config"] dict — per-request override from the UI
  2. Environment variables / settings.py defaults
"""
from __future__ import annotations
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, TypeVar, Generic, Type

from pydantic import ValidationError

from core.token_manager import TokenBudgetManager, estimate_tokens
from core.models import AgentName, AnalysisRequest, AgentResultBase, DiffHunk
from agents.llm_client import UnifiedLLMClient, ModelConfig, make_llm_client
from governance.observability import agent_span

log = logging.getLogger(__name__)
T   = TypeVar("T", bound=AgentResultBase)


def format_hunks_for_prompt(
    hunks: list[DiffHunk],
    max_chars_per_hunk: int = 3000,
    max_total_chars:    int = 40_000,   # ~10K tokens — keeps prompts focused
    focus: str = "general",             # "security" | "interface" | "general"
) -> str:
    """
    Format diff hunks into a prompt block with per-file language headers.

    For large PRs (>15 files) ranks hunks by relevance before including them:
      - Security focus: auth/crypto/config paths ranked highest
      - Interface focus: openapi/proto/schema paths ranked highest
      - General: ranked by raw change volume (additions + deletions)
    Includes a summary line when files are omitted so the LLM knows the scope.
    """
    from ingestion.language_registry import lang_meta
    from ingestion.diff_parser import is_low_signal_path

    if not hunks:
        return "(no diff hunks)"

    # ── Input guardrails: drop binary/lockfile/generated/vendored noise ───────
    signal = [h for h in hunks if not is_low_signal_path(h.file_path)]
    skipped_noise = len(hunks) - len(signal)
    if signal:                       # never end up empty (a binary-only PR keeps its hunks)
        hunks = signal

    # ── Rank hunks for large PRs ──────────────────────────────────────────────
    def _hunk_score(h: DiffHunk) -> int:
        score = h.additions + h.deletions
        fp = h.file_path.lower()
        if focus == "security":
            if any(k in fp for k in ("auth", "crypt", "secret", "password", "token",
                                     "jwt", "oauth", "key", "cert", "ssl", "tls")):
                score += 500
            if any(k in fp for k in ("config", "setting", "env", "permission", "rbac")):
                score += 200
            # Deprioritise tests — they contain keywords but aren't the vuln
            if any(k in fp for k in ("test", "spec", "mock", "fixture")):
                score -= 300
        elif focus == "interface":
            if any(k in fp for k in ("openapi", "swagger", "proto", "schema",
                                     "api-spec", "asyncapi", "contract")):
                score += 500
        return score

    ranked = sorted(hunks, key=_hunk_score, reverse=True)
    total_files = len(ranked)

    # ── Select hunks that fit within max_total_chars ──────────────────────────
    selected:    list[DiffHunk] = []
    running_chars = 0
    for h in ranked:
        budget = min(max_chars_per_hunk, max_total_chars - running_chars)
        if budget <= 0:
            break
        selected.append(h)
        running_chars += min(len(h.content), budget)

    omitted = total_files - len(selected)

    # ── Format selected hunks ─────────────────────────────────────────────────
    parts: list[str] = []
    if omitted > 0:
        parts.append(
            f"[Large PR: showing {len(selected)} of {total_files} reviewable files, "
            f"ranked by {'security sensitivity' if focus=='security' else 'change volume'}. "
            f"{omitted} lower-priority files omitted.]"
        )
    if skipped_noise > 0:
        parts.append(
            f"[{skipped_noise} binary/lockfile/generated file(s) excluded as low-signal.]"
        )

    for h in selected:
        meta    = lang_meta(h.language)
        budget  = min(max_chars_per_hunk, max_total_chars - sum(
            min(len(x.content), max_chars_per_hunk) for x in selected[:selected.index(h)]
        ))
        content = h.content[:max(budget, 200)]
        if len(h.content) > len(content):
            content += f"\n... [truncated — {h.additions + h.deletions} lines total]"
        parts.append(
            f"### File: {h.file_path}  "
            f"[language: {meta.display}]  "
            f"(+{h.additions}/-{h.deletions})\n"
            + content
        )
    return "\n\n".join(parts)


class BaseAgent(ABC, Generic[T]):

    agent_name:    AgentName
    output_model:  Type[T]
    system_prompt: str

    # Subclasses may override to allow more output tokens for verbose responses.
    # The effective cap is min(output_token_cap, remaining_budget).
    output_token_cap: int = 4000

    def __init__(self, api_key: str | None = None) -> None:
        self._default_api_key = api_key

    def run(self, request: AnalysisRequest, budget: TokenBudgetManager, context: dict[str, Any] | None = None) -> T:
        ctx       = context or {}
        agent_key = self.agent_name.value
        model_cfg = self._resolve_model_config(ctx, agent_key)
        client    = make_llm_client(model_cfg)

        from core.progress import get_progress_store
        progress = get_progress_store().get_or_create(request.request_id)
        progress.agent_started(agent_key)

        user_prompt = self.build_user_prompt(request, ctx)
        needed      = estimate_tokens(user_prompt) + 800

        if not budget.check_and_reserve(agent_key, needed):
            result = self.fallback_result(request)
            result.fallback_used = True
            result.duration_s    = 0.0
            progress.agent_done(agent_key, 0, 0.0, "", True)
            log.warning("[%s] %s: budget exhausted -> fallback", request.request_id, agent_key)
            return result

        t0 = time.monotonic()
        with agent_span(agent_key, request.request_id):
            try:
                result, tokens = self._call_llm(client, user_prompt, budget.get_remaining(agent_key))
                duration = round(time.monotonic() - t0, 2)
                budget.record_usage(agent_key, tokens, client.model_name)
                result.token_usage   = tokens
                result.model_used    = client.model_name
                result.fallback_used = False
                result.duration_s    = duration
                progress.agent_done(agent_key, tokens, duration, client.model_name, False)
                log.info("[%s] %-22s LLM   tokens=%-5d  time=%-6.2fs  model=%s",
                         request.request_id, agent_key, tokens, duration, client.model_name)
                return result
            except Exception as exc:
                duration = round(time.monotonic() - t0, 2)
                log.error("[%s] %s LLM error: %s", request.request_id, agent_key, exc, exc_info=True)
                result = self.fallback_result(request)
                result.fallback_used = True
                result.duration_s    = duration
                progress.agent_done(agent_key, 0, duration, "", True)
                log.warning("[%s] %-22s FALLBACK (static rules) — LLM call failed  time=%.2fs",
                            request.request_id, agent_key, duration)
                return result

    @abstractmethod
    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str: ...

    @abstractmethod
    def fallback_result(self, request: AnalysisRequest) -> T: ...

    def report_static_progress(self, request: AnalysisRequest, duration_s: float = 0.0,
                               tokens: int = 0) -> None:
        """
        Emit start+done progress for a run that did NOT go through the LLM path
        (e.g. budget too low, or a static-only branch was taken).

        Custom run() overrides that conditionally skip super().run() must call
        this in the skip branch so the agent still appears in the live progress
        panel and the Timings tab. Safe to call exactly once per run.
        """
        from core.progress import get_progress_store
        key = self.agent_name.value if not isinstance(self.agent_name, str) else self.agent_name
        progress = get_progress_store().get_or_create(request.request_id)
        progress.agent_started(key)
        progress.agent_done(key, tokens, round(duration_s, 2), "static", False)

    def _call_llm(self, client: UnifiedLLMClient, user_prompt: str, remaining: int) -> tuple[T, int]:
        # Leave at least 200 tokens for the input; cap output at output_token_cap.
        prompt_tokens = estimate_tokens(user_prompt)
        max_output    = min(self.output_token_cap, max(200, remaining - prompt_tokens))

        schema_hint = json.dumps(self.output_model.model_json_schema(), indent=2)
        full_user   = (
            f"{user_prompt}\n\n"
            f"Respond ONLY with a valid JSON object matching this schema "
            f"(no markdown fences, no preamble):\n{schema_hint}"
        )
        response = client.create(system=self.system_prompt, user=full_user, max_tokens=max_output)
        raw      = _strip_fences(response.text.strip())
        tokens   = response.total_tokens

        parsed = self._parse_json(raw)
        return parsed, tokens

    def _parse_json(self, raw: str) -> T:
        """
        Parse the LLM JSON response with three escalating recovery attempts:
          1. Direct parse — the happy path
          2. Regex extraction — strip preamble/suffix the LLM added anyway
          3. Truncation repair — close open strings/arrays/objects when the
             LLM hit its max_tokens cap mid-response
        """
        # Attempt 1: direct parse
        try:
            return self.output_model.model_validate_json(raw)
        except (ValidationError, ValueError):
            pass

        # Attempt 2: extract the first balanced {...} object (robust to preamble,
        # trailing prose, and code fences the model added anyway).
        candidate = _extract_balanced_json(raw) or _greedy_brace(raw)
        if candidate:
            try:
                return self.output_model.model_validate_json(candidate)
            except (ValidationError, ValueError):
                raw = candidate   # carry forward for repair attempt

        # Attempt 3: repair truncated JSON (LLM hit max_tokens mid-response)
        repaired = _repair_truncated_json(raw)
        if repaired != raw:
            log.warning("JSON from %s was truncated — repaired and retrying parse", self.agent_name)
            try:
                return self.output_model.model_validate_json(repaired)
            except (ValidationError, ValueError) as exc:
                log.warning("Repaired JSON still invalid for %s: %s", self.agent_name, exc)

        # All recovery failed — log WHAT came back so the cause is diagnosable
        # (truncated JSON vs. prose vs. an HTML proxy/error page vs. wrong model).
        diag = _diagnose_raw(raw)
        log.error(
            "[%s] Unparseable LLM response (%d chars) — %s\n  head: %s\n  tail: %s",
            self.agent_name, len(raw or ""), diag,
            (raw or "")[:300].replace("\n", "\\n"),
            (raw or "")[-200:].replace("\n", "\\n"),
        )
        raise ValueError(
            f"Could not parse {self.agent_name} response after all recovery attempts ({diag})"
        )

    def _resolve_model_config(self, context: dict[str, Any], agent_key: str) -> ModelConfig:
        """Resolve which provider/model/key this agent should use.

        Precedence, highest first:
          1. UI-supplied per-request override (context["model_config"])
          2. Backend settings / .env
        A blank api_key in the UI override means "use the configured env key"
        (env is only a *fallback*, never an override of a UI-supplied key).
        """
        override = context.get("model_config")
        if override:
            cfg = ModelConfig.from_dict(override)
            ui_named_model = bool((override.get("model") or "").strip())

            # The UI key is authoritative for every provider; only fall back to
            # the env key when the UI left it blank.
            if not cfg.api_key and self._default_api_key:
                cfg.api_key = self._default_api_key

            # Honour per-agent fast/strong selection ONLY when the UI did not
            # explicitly pick a model (otherwise the user's choice wins).
            if cfg.provider == "anthropic" and not ui_named_model:
                from core.token_manager import MODEL_FAST, MODEL_STRONG, _STRONG_AGENTS
                cfg.model = MODEL_STRONG if agent_key in _STRONG_AGENTS else MODEL_FAST
            return cfg

        cfg = ModelConfig.from_settings()
        if self._default_api_key and cfg.provider == "anthropic":
            cfg.api_key = self._default_api_key
        if cfg.provider == "anthropic":
            from core.token_manager import MODEL_FAST, MODEL_STRONG, _STRONG_AGENTS
            cfg.model = MODEL_STRONG if agent_key in _STRONG_AGENTS else MODEL_FAST
        return cfg


# ── JSON extraction / diagnostics ─────────────────────────────────────────────

def _greedy_brace(raw: str) -> str:
    """First '{' to last '}' (fast path for prose-wrapped JSON)."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    return m.group() if m else ""


def _extract_balanced_json(raw: str) -> str:
    """Return the first COMPLETE top-level {...} object, respecting strings and
    escapes. More reliable than a greedy regex when the model appends trailing
    prose after valid JSON."""
    if not raw:
        return ""
    start = raw.find("{")
    if start == -1:
        return ""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:            esc = False
            elif c == "\\":    esc = True
            elif c == '"':     in_str = False
        else:
            if c == '"':       in_str = True
            elif c == "{":     depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
    return ""   # never closed → truncated; let the repair step handle it


def _diagnose_raw(raw: str) -> str:
    """Heuristic label for an unparseable response — points at the real cause."""
    s = (raw or "").lstrip().lower()
    if not s:
        return "empty response (model returned nothing — check the API key/endpoint)"
    if s.startswith("<!doctype") or s.startswith("<html") or "<body" in s[:200]:
        return "looks like an HTML page (corporate proxy / captive portal / blocked endpoint)"
    if s.startswith(("i ", "sure", "here", "the ", "as ", "based ")):
        return "model returned prose, not JSON (model not following the JSON instruction)"
    if "{" in s and s.count("{") > s.count("}"):
        return "JSON truncated mid-response (raise the agent's output budget/cap)"
    return "malformed JSON"


# ── JSON repair ───────────────────────────────────────────────────────────────

def _repair_truncated_json(raw: str) -> str:
    """
    Close any open strings, arrays, and objects left by a mid-stream truncation.

    Walk the JSON character by character tracking:
      - Whether we are inside a string literal
      - The nesting stack of '{' and '['
    Then append the necessary closing characters.

    A trailing comma before the repair suffix is also stripped so that
    ``{"a": [1, 2,`` becomes ``{"a": [1, 2]}`` rather than ``{"a": [1, 2,]}``.
    """
    in_string   = False
    escape_next = False
    stack: list[str] = []   # '{' or '['

    for ch in raw:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()

    suffix = ""
    if in_string:
        suffix += '"'          # close the dangling string

    # Drop a trailing comma that would produce invalid JSON after closing
    stripped = (raw + suffix).rstrip()
    if stripped.endswith(","):
        stripped = stripped[:-1]

    # Close containers in reverse order
    for opener in reversed(stack):
        stripped += "}" if opener == "{" else "]"

    return stripped


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:] if len(lines) > 1 else lines
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
