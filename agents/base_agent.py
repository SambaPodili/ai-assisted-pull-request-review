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
from core.models import AgentName, AnalysisRequest, AgentResultBase
from agents.llm_client import UnifiedLLMClient, ModelConfig, make_llm_client
from governance.observability import agent_span

log = logging.getLogger(__name__)
T   = TypeVar("T", bound=AgentResultBase)


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

        # Attempt 2: extract the outermost {...} block (handles preamble/suffix)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            candidate = m.group()
            try:
                return self.output_model.model_validate_json(candidate)
            except (ValidationError, ValueError):
                raw = candidate   # carry forward for repair attempt

        # Attempt 3: repair truncated JSON (LLM hit max_tokens mid-string)
        repaired = _repair_truncated_json(raw)
        if repaired != raw:
            log.warning(
                "JSON from %s was truncated — repaired and retrying parse",
                self.agent_name,
            )
            try:
                return self.output_model.model_validate_json(repaired)
            except (ValidationError, ValueError) as exc:
                log.warning("Repaired JSON still invalid for %s: %s", self.agent_name, exc)

        raise ValueError(f"Could not parse {self.agent_name} response after all recovery attempts")

    def _resolve_model_config(self, context: dict[str, Any], agent_key: str) -> ModelConfig:
        override = context.get("model_config")
        if override:
            cfg = ModelConfig.from_dict(override)
            if not cfg.api_key and cfg.provider == "anthropic" and self._default_api_key:
                cfg.api_key = self._default_api_key
            return cfg

        cfg = ModelConfig.from_settings()
        if self._default_api_key and cfg.provider == "anthropic":
            cfg.api_key = self._default_api_key
        if cfg.provider == "anthropic":
            from core.token_manager import MODEL_FAST, MODEL_STRONG, _STRONG_AGENTS
            cfg.model = MODEL_STRONG if agent_key in _STRONG_AGENTS else MODEL_FAST
        return cfg


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
