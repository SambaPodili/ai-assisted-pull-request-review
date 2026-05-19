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
from abc import ABC, abstractmethod
from typing import Any, TypeVar, Generic, Type

from pydantic import ValidationError

from core.token_manager import TokenBudgetManager, estimate_tokens
from core.models import AgentName, AnalysisRequest, AgentResultBase
from agents.llm_client import UnifiedLLMClient, ModelConfig, make_llm_client

log = logging.getLogger(__name__)
T   = TypeVar("T", bound=AgentResultBase)


class BaseAgent(ABC, Generic[T]):

    agent_name:    AgentName
    output_model:  Type[T]
    system_prompt: str

    def __init__(self, api_key: str | None = None) -> None:
        self._default_api_key = api_key

    def run(self, request: AnalysisRequest, budget: TokenBudgetManager, context: dict[str, Any] | None = None) -> T:
        ctx       = context or {}
        agent_key = self.agent_name.value
        model_cfg = self._resolve_model_config(ctx, agent_key)
        client    = make_llm_client(model_cfg)

        user_prompt = self.build_user_prompt(request, ctx)
        needed      = estimate_tokens(user_prompt) + 800

        if not budget.check_and_reserve(agent_key, needed):
            result = self.fallback_result(request)
            result.fallback_used = True
            log.warning("[%s] %s: budget exhausted -> fallback", request.request_id, agent_key)
            return result

        try:
            result, tokens = self._call_llm(client, user_prompt, budget.get_remaining(agent_key))
            budget.record_usage(agent_key, tokens, client.model_name)
            result.token_usage   = tokens
            result.model_used    = client.model_name
            result.fallback_used = False
            return result
        except Exception as exc:
            log.error("[%s] %s LLM error: %s", request.request_id, agent_key, exc, exc_info=True)
            result = self.fallback_result(request)
            result.fallback_used = True
            return result

    @abstractmethod
    def build_user_prompt(self, request: AnalysisRequest, context: dict[str, Any]) -> str: ...

    @abstractmethod
    def fallback_result(self, request: AnalysisRequest) -> T: ...

    def _call_llm(self, client: UnifiedLLMClient, user_prompt: str, remaining: int) -> tuple[T, int]:
        max_output  = min(1500, max(200, remaining - estimate_tokens(user_prompt)))
        schema_hint = json.dumps(self.output_model.model_json_schema(), indent=2)
        full_user   = (
            f"{user_prompt}\n\n"
            f"Respond ONLY with a valid JSON object matching this schema "
            f"(no markdown fences, no preamble):\n{schema_hint}"
        )
        response = client.create(system=self.system_prompt, user=full_user, max_tokens=max_output)
        raw      = _strip_fences(response.text.strip())
        tokens   = response.total_tokens
        try:
            parsed = self.output_model.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            log.warning("JSON parse error for %s: %s — trying partial parse", self.agent_name, exc)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                parsed = self.output_model.model_validate_json(m.group())
            else:
                raise
        return parsed, tokens

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


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    inner = lines[1:] if len(lines) > 1 else lines
    if inner and inner[-1].strip() == "```":
        inner = inner[:-1]
    return "\n".join(inner).strip()
