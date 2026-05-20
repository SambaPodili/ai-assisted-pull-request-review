"""
agents/llm_client.py
---------------------
Unified LLM client that normalises Anthropic, OpenAI-compatible APIs,
Ollama (local), and Azure OpenAI into a single interface.

All agents call client.create(system, user, max_tokens) and get back
an LLMResponse regardless of which provider is configured.

Supported providers:
  • anthropic    — Claude models (Sonnet, Haiku, Opus)
  • openai       — GPT-4o, GPT-4-turbo, GPT-3.5-turbo, etc.
  • azure_openai — Azure-hosted OpenAI models (custom endpoint)
  • ollama       — Any model running locally via Ollama (OpenAI-compatible API)
  • custom       — Any OpenAI-compatible endpoint (LM Studio, vLLM, etc.)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

try:
    from tenacity import (
        retry, stop_after_attempt, wait_exponential_jitter,
        retry_if_exception, before_sleep_log, RetryError,
    )
    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False

log = logging.getLogger(__name__)


def _make_retry(max_attempts: int, max_wait: int, is_retryable):
    """Return a tenacity Retrying context or a no-op fallback."""
    if not _HAS_TENACITY:
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield

        return _noop()

    from tenacity import Retrying
    return Retrying(
        retry=retry_if_exception(is_retryable),
        wait=wait_exponential_jitter(initial=2, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


# ── Model config ──────────────────────────────────────────────────────────────

@dataclass
class ModelConfig:
    provider:    str = "anthropic"                  # see PROVIDERS above
    model:       str = "claude-sonnet-4-6"          # model name / deployment id
    api_key:     str = ""                           # API key (blank = use env)
    base_url:    str = ""                           # custom endpoint for Ollama / Azure / custom
    api_version: str = "2024-08-01-preview"         # Azure OpenAI API version

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        if not d:
            return cls()
        return cls(
            provider=d.get("provider",    "anthropic"),
            model=d.get("model",          "claude-sonnet-4-6"),
            api_key=d.get("api_key",      ""),
            base_url=d.get("base_url",    ""),
            api_version=d.get("api_version", "2024-08-01-preview"),
        )

    @classmethod
    def from_settings(cls) -> "ModelConfig":
        from config.settings import get_settings
        cfg = get_settings()
        provider = getattr(cfg, "llm_provider", "anthropic")
        # Pick the right key depending on provider
        if provider == "anthropic":
            key = cfg.anthropic_api_key
            model = getattr(cfg, "llm_model", "claude-sonnet-4-6")
        elif provider == "openai":
            key = getattr(cfg, "openai_api_key", "")
            model = getattr(cfg, "llm_model", "gpt-4o")
        elif provider == "ollama":
            key = "ollama"  # Ollama doesn't need a real key
            model = getattr(cfg, "llm_model", "llama3.2")
        else:
            key = getattr(cfg, "openai_api_key", "") or cfg.anthropic_api_key
            model = getattr(cfg, "llm_model", "gpt-4o")
        return cls(
            provider=provider,
            model=model,
            api_key=key,
            base_url=getattr(cfg, "llm_base_url", ""),
            api_version=getattr(cfg, "llm_api_version", "2024-08-01-preview"),
        )


# ── Response ──────────────────────────────────────────────────────────────────

class LLMResponse:
    def __init__(self, text: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.text          = text
        self.input_tokens  = input_tokens
        self.output_tokens = output_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ── Unified client ────────────────────────────────────────────────────────────

class UnifiedLLMClient:
    """
    Unified interface that dispatches to the right SDK based on provider.

    Usage:
        client = UnifiedLLMClient(ModelConfig(provider="openai", model="gpt-4o", api_key="sk-..."))
        response = client.create(system="You are...", user="Analyse this...", max_tokens=1000)
        print(response.text, response.total_tokens)
    """

    def __init__(self, config: ModelConfig) -> None:
        self._cfg = config

    @property
    def model_name(self) -> str:
        return f"{self._cfg.provider}/{self._cfg.model}"

    def create(self, system: str, user: str, max_tokens: int = 1000) -> LLMResponse:
        """Dispatch to the right provider and return a normalised LLMResponse."""
        p = self._cfg.provider
        try:
            if p == "anthropic":
                return self._call_anthropic(system, user, max_tokens)
            elif p in ("openai", "azure_openai", "ollama", "custom"):
                return self._call_openai_compat(system, user, max_tokens)
            else:
                raise ValueError(f"Unsupported provider: {p}")
        except Exception as exc:
            log.error("[LLM] %s call failed: %s", self.model_name, exc, exc_info=True)
            raise

    # ── Anthropic ─────────────────────────────────────────────────────────────

    def _call_anthropic(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        import anthropic

        def _is_retryable(exc: BaseException) -> bool:
            return isinstance(exc, (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.InternalServerError,
            ))

        from config.settings import get_settings
        cfg = get_settings()

        client = anthropic.Anthropic(api_key=self._cfg.api_key or None)
        resp = None
        for attempt in _make_retry(cfg.llm_retry_attempts, cfg.llm_retry_max_wait_s, _is_retryable):
            with attempt:
                resp = client.messages.create(
                    model=self._cfg.model,
                    max_tokens=max_tokens,
                    system=[{
                        "type":          "text",
                        "text":          system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user}],
                )
        return LLMResponse(
            text=resp.content[0].text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    # ── OpenAI-compatible (OpenAI, Azure, Ollama, custom/org Llama) ──────────────

    def _call_openai_compat(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        """
        Handles all OpenAI-compatible endpoints:
          • openai       — api.openai.com
          • azure_openai — Azure-hosted OpenAI (uses AzureOpenAI client)
          • ollama       — localhost:11434/v1
          • custom       — any endpoint (org Llama, Maverick, vLLM, Groq, etc.)

        Industry-standard pattern:
          headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
          payload = {"model": model_name, "messages": [...]}

        This is what every OpenAI-compatible server (including org-deployed
        Llama/Maverick models) expects.
        """
        try:
            from openai import OpenAI, AzureOpenAI
        except ImportError:
            raise ImportError("pip install openai  — required for OpenAI/Azure/Ollama/custom providers")

        p = self._cfg.provider

        if p == "azure_openai":
            client = AzureOpenAI(
                api_key=self._cfg.api_key or None,
                azure_endpoint=self._cfg.base_url,
                api_version=self._cfg.api_version or "2024-08-01-preview",
            )
        else:
            # For Ollama, custom org endpoints (Llama Maverick, etc.) — just set base_url
            base_url = self._cfg.base_url or None
            api_key  = self._cfg.api_key or "none"   # many servers accept any non-empty key

            if p == "ollama" and not base_url:
                base_url = "http://localhost:11434/v1"

            client = OpenAI(api_key=api_key, base_url=base_url)

        # Standard OpenAI messages format — works with ALL compatible endpoints
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]

        def _is_retryable_openai(exc: BaseException) -> bool:
            try:
                from openai import RateLimitError, APIConnectionError, APITimeoutError, InternalServerError
                return isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError))
            except ImportError:
                return False

        from config.settings import get_settings
        cfg = get_settings()

        resp = None
        for attempt in _make_retry(cfg.llm_retry_attempts, cfg.llm_retry_max_wait_s, _is_retryable_openai):
            with attempt:
                resp = client.chat.completions.create(
                    model=self._cfg.model,
                    max_tokens=max_tokens,
                    messages=messages,
                )

        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens     if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )


# ── Factory ───────────────────────────────────────────────────────────────────

def make_llm_client(
    config: ModelConfig | dict | None = None,
    provider: str = "",
    model: str = "",
    api_key: str = "",
    base_url: str = "",
) -> UnifiedLLMClient:
    """
    Convenience factory. Accepts a ModelConfig, a dict, or keyword args.

    Examples:
        make_llm_client()                                         # reads from settings
        make_llm_client({"provider":"openai","model":"gpt-4o"})   # from dict
        make_llm_client(provider="ollama", model="llama3.2")      # kwargs
    """
    if isinstance(config, dict):
        config = ModelConfig.from_dict(config)
    elif config is None:
        if provider:
            config = ModelConfig(
                provider=provider,
                model=model or _default_model(provider),
                api_key=api_key,
                base_url=base_url,
            )
        else:
            config = ModelConfig.from_settings()
    return UnifiedLLMClient(config)


def _default_model(provider: str) -> str:
    return {
        "anthropic":    "claude-sonnet-4-6",
        "openai":       "gpt-4o",
        "azure_openai": "gpt-4o",
        "ollama":       "llama3.2",
        "custom":       "gpt-4o",
    }.get(provider, "gpt-4o")


# ── Provider catalogue (used by UI) ───────────────────────────────────────────

PROVIDERS = {
    "anthropic": {
        "label":     "Anthropic Claude",
        "models":    ["claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-opus-4-6"],
        "needs_key": True,
        "needs_url": False,
        "hint":      "Best accuracy for code analysis. Get key at console.anthropic.com",
    },
    "openai": {
        "label":     "OpenAI",
        "models":    ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "needs_key": True,
        "needs_url": False,
        "hint":      "Strong alternative. Get key at platform.openai.com",
    },
    "azure_openai": {
        "label":     "Azure OpenAI",
        "models":    ["gpt-4o", "gpt-4-turbo", "gpt-35-turbo"],
        "needs_key": True,
        "needs_url": True,   # Azure endpoint URL
        "hint":      "Your Azure-hosted OpenAI deployment. Enter the Azure resource endpoint URL.",
    },
    "ollama": {
        "label":     "Ollama (Local Llama)",
        "models":    ["llama3.2", "llama3.1", "codellama", "mistral", "qwen2.5-coder", "deepseek-coder", "phi3", "gemma2"],
        "needs_key": False,
        "needs_url": True,
        "hint":      "Fully local. Run: ollama serve && ollama pull llama3.2",
    },
    "custom": {
        "label":     "Custom / Org endpoint",
        "models":    [],     # user specifies model name
        "needs_key": True,
        "needs_url": True,
        "hint":      (
            "Any OpenAI-compatible API — including org-deployed Llama/Maverick, "
            "vLLM, LM Studio, Groq, Together AI, Fireworks. "
            "Set base_url to your endpoint and the model name to your deployment "
            "(e.g. 'llama-maverick' or 'gpt-4o'). "
            "Auth uses: Authorization: Bearer {api_key}. "
            "Payload: {\"model\": model_name, \"messages\": [{\"role\": \"user\", \"content\": prompt}]}"
        ),
    },
}