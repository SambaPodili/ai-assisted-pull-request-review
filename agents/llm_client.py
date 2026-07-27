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
import threading
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


# ── Global LLM concurrency limiter ────────────────────────────────────────────
# Bounds how many LLM requests are in flight at once across the whole process.
# The pipeline fans out ~13 agents in parallel; a self-hosted/custom endpoint may
# not accept that many simultaneous connections. Sized once from
# LLM_MAX_CONCURRENCY (0 = unlimited). Holding the slot across retries also acts
# as back-pressure so a struggling endpoint isn't hammered.
_LLM_SEM: "threading.Semaphore | None" = None
_LLM_SEM_LOCK = threading.Lock()
_LLM_SEM_N = -1


class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _llm_gate():
    """Return a context manager that bounds concurrent LLM calls (or a no-op)."""
    global _LLM_SEM, _LLM_SEM_N
    try:
        from config.settings import get_settings
        n = int(getattr(get_settings(), "llm_max_concurrency", 8) or 0)
    except Exception:
        n = 8
    if n <= 0:
        return _NullCtx()
    if _LLM_SEM is None or _LLM_SEM_N != n:
        with _LLM_SEM_LOCK:
            if _LLM_SEM is None or _LLM_SEM_N != n:
                _LLM_SEM = threading.Semaphore(n)
                _LLM_SEM_N = n
    return _LLM_SEM


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
        wait=wait_exponential_jitter(initial=5, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


def _make_retry_conditional(max_attempts: int, max_wait: int, is_retryable, timeout_limit: int):
    """Like _make_retry, but timeout/connection errors get a SEPARATE (small)
    attempt limit. On a slow or overloaded self-hosted model a timeout won't
    recover by re-issuing the SAME heavy request — retrying it 5× just piles more
    load on the endpoint and makes every other agent time out too (a retry storm).
    Fail those fast (default 1 attempt) and let the pipeline move on."""
    if not _HAS_TENACITY:
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            yield

        class _NoopRetrying:
            def __iter__(self):
                yield _noop()

        return _NoopRetrying()

    from tenacity import Retrying
    from tenacity.stop import stop_base

    def _is_timeout(exc) -> bool:
        try:
            from openai import APITimeoutError, APIConnectionError
            return isinstance(exc, (APITimeoutError, APIConnectionError))
        except ImportError:
            return False

    class _CondStop(stop_base):
        def __call__(self, retry_state) -> bool:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            limit = timeout_limit if _is_timeout(exc) else max_attempts
            return retry_state.attempt_number >= max(1, limit)

    return Retrying(
        retry=retry_if_exception(is_retryable),
        wait=wait_exponential_jitter(initial=5, max=max_wait),
        stop=_CondStop(),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )


# ── Model config ──────────────────────────────────────────────────────────────

def _normalize_base_url(url: str) -> str:
    """Make a user-supplied endpoint URL usable by the HTTP client.

    The #1 custom-LLM misconfig is omitting the scheme — entering
    `my-gateway/v1` or `10.0.0.5:8000/v1` instead of `https://my-gateway/v1`.
    The OpenAI SDK then dies with `UnsupportedProtocol: Request URL is missing an
    'http://' or 'https://' protocol`. We add a sensible scheme (http for
    localhost / private hosts, https otherwise) and trim trailing slashes.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.startswith(("http://", "https://")):
        return u
    if u.startswith("//"):                       # protocol-relative
        u = u[2:]
    host = u.split("/")[0].split(":")[0].lower()
    local = (host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
             or host.endswith(".local")
             or host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                                 "172.19.", "172.2", "172.30.", "172.31.")))
    scheme = "http://" if local else "https://"
    fixed = scheme + u
    log.warning("[LLM] base_url '%s' had no scheme — using '%s'. "
                "Set LLM_BASE_URL / the UI Base URL with an explicit http(s):// to silence this.",
                url, fixed)
    return fixed


@dataclass
class ModelConfig:
    provider:    str = "anthropic"                  # see PROVIDERS above
    model:       str = "claude-sonnet-4-6"          # model name / deployment id
    api_key:     str = ""                           # API key (blank = use env)
    base_url:    str = ""                           # custom endpoint for Ollama / Azure / custom
    api_version: str = "2024-08-01-preview"         # Azure OpenAI API version

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        """Build from a UI-supplied override dict.

        Blank/whitespace-only fields are treated as 'not provided' and fall
        back to defaults — a half-filled override (e.g. provider chosen but
        model left empty) must never clobber a usable value with "" and break
        the downstream API call.
        """
        if not d:
            return cls()

        def _s(key: str, default: str) -> str:
            v = d.get(key)
            v = v.strip() if isinstance(v, str) else v
            return v if v else default

        return cls(
            provider=_s("provider",    "anthropic"),
            model=_s("model",          "claude-sonnet-4-6"),
            # api_key intentionally NOT defaulted — blank means "fall back to env"
            api_key=(d.get("api_key") or "").strip(),
            base_url=_normalize_base_url(d.get("base_url") or ""),
            api_version=_s("api_version", "2024-08-01-preview"),
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
            # custom / self-hosted (OpenAI-compatible). Prefer the dedicated
            # LLM_API_KEY (shared across models on the same endpoint), then OpenAI.
            key = getattr(cfg, "llm_api_key", "") or getattr(cfg, "openai_api_key", "") or cfg.anthropic_api_key
            model = getattr(cfg, "llm_model", "gpt-4o")
        return cls(
            provider=provider,
            model=model,
            api_key=key,
            base_url=_normalize_base_url(getattr(cfg, "llm_base_url", "")),
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

        # Fail fast with an actionable message when no credential is available
        # from EITHER the UI Model settings OR the backend env. Without this the
        # SDK raises an opaque auth error that the UI swallows into "simulation",
        # making it look like the app ignores the UI and demands an env var.
        if p in ("anthropic", "openai", "azure_openai") and not self._cfg.api_key:
            raise ValueError(
                f"No API key for provider '{p}'. Enter it in the UI under "
                f"Configure → AI Model, or set the corresponding environment "
                f"variable (e.g. ANTHROPIC_API_KEY / OPENAI_API_KEY)."
            )

        try:
            # Bound concurrent in-flight LLM requests (protects custom/local servers).
            with _llm_gate():
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
        from anthropic._exceptions import OverloadedError as _OverloadedError

        from config.settings import get_settings
        cfg = get_settings()

        def _is_retryable(exc: BaseException) -> bool:
            return isinstance(exc, (
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.InternalServerError,
                _OverloadedError,   # HTTP 529 — not re-exported at top level in SDK 0.100+
            ))

        if _HAS_TENACITY:
            from tenacity import Retrying, retry_if_exception, stop_after_attempt, before_sleep_log
            from tenacity import wait_exponential_jitter

            def _wait(retry_state):
                exc = retry_state.outcome.exception()
                if isinstance(exc, _OverloadedError):
                    # 529: short backoff — 2s/4s/8s capped at 10s.
                    # With stop_after_attempt(3) total exposure ≤ ~24s per agent.
                    return min(2.0 * (2 ** (retry_state.attempt_number - 1)), 10.0)
                # Other transient errors: standard exponential backoff
                return min(5.0 * (2 ** (retry_state.attempt_number - 1)), float(cfg.llm_retry_max_wait_s))

            def _stop(retry_state):
                exc = retry_state.outcome.exception()
                # Connection/timeout errors almost always mean a misconfigured or
                # unreachable endpoint (wrong base_url, no network egress) — retrying
                # 5× just burns the whole analysis budget. Fail fast (2 attempts).
                if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
                    limit = 2
                elif isinstance(exc, _OverloadedError):
                    limit = 3   # 529 gives up sooner to keep agents from blocking each other
                else:
                    limit = cfg.llm_retry_attempts
                return retry_state.attempt_number >= limit

            from tenacity.stop import stop_base
            class _ConditionalStop(stop_base):
                def __call__(self, retry_state) -> bool:
                    return _stop(retry_state)

            retrying = Retrying(
                retry=retry_if_exception(_is_retryable),
                wait=_wait,
                stop=_ConditionalStop(),
                before_sleep=before_sleep_log(log, logging.WARNING),
                reraise=True,
            )
        else:
            from contextlib import contextmanager

            @contextmanager
            def _noop_ctx():
                yield

            class _NoopRetrying:
                def __iter__(self):
                    yield _noop_ctx()

            retrying = _NoopRetrying()

        # Explicit per-call timeout + no SDK-internal retries: we handle retries
        # via tenacity above, and a stuck call must fail fast rather than hang for
        # the SDK default (which can be minutes) and blow the analysis timeout.
        req_timeout = float(getattr(cfg, "llm_request_timeout_s", 120) or 120)
        client = anthropic.Anthropic(
            api_key=self._cfg.api_key or None,
            timeout=req_timeout,
            max_retries=0,
        )
        resp = None
        for attempt in retrying:
            with attempt:
                resp = client.messages.create(
                    model=self._cfg.model,
                    max_tokens=max_tokens,
                    temperature=float(getattr(cfg, "llm_temperature", 0.0)),
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

        # Explicit per-call timeout + no SDK-internal retries (tenacity handles
        # retries) so a stuck/unreachable endpoint fails fast instead of hanging.
        from config.settings import get_settings
        req_timeout = float(getattr(get_settings(), "llm_request_timeout_s", 120) or 120)

        if p == "azure_openai":
            client = AzureOpenAI(
                api_key=self._cfg.api_key or None,
                azure_endpoint=self._cfg.base_url,
                api_version=self._cfg.api_version or "2024-08-01-preview",
                timeout=req_timeout,
                max_retries=0,
            )
        else:
            # For Ollama, custom org endpoints (Llama Maverick, etc.) — just set base_url
            base_url = self._cfg.base_url or None
            api_key  = self._cfg.api_key or "none"   # many servers accept any non-empty key

            if p == "ollama" and not base_url:
                base_url = "http://localhost:11434/v1"

            client = OpenAI(api_key=api_key, base_url=base_url, timeout=req_timeout, max_retries=0)

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
        temperature = float(getattr(cfg, "llm_temperature", 0.0))
        use_stream = bool(getattr(cfg, "llm_stream", True))

        def _one_call():
            """One completion → (text, reasoning, in_tokens, out_tokens).

            Streaming (default) makes the client-level timeout a PER-CHUNK read
            timeout: a slow model succeeds as long as it keeps emitting tokens,
            instead of the whole multi-minute generation having to fit inside a
            single read window (the cause of the APITimeoutError on heavy agents)."""
            if not use_stream:
                r = client.chat.completions.create(
                    model=self._cfg.model, max_tokens=max_tokens,
                    temperature=temperature, messages=messages,
                )
                u, m = r.usage, r.choices[0].message
                reasoning = (getattr(m, "reasoning_content", None)
                             or (getattr(m, "model_extra", None) or {}).get("reasoning_content") or "")
                return ((getattr(m, "content", None) or ""), reasoning,
                        (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0))

            kwargs = dict(model=self._cfg.model, max_tokens=max_tokens,
                          temperature=temperature, messages=messages, stream=True)
            try:
                stream = client.chat.completions.create(
                    **kwargs, stream_options={"include_usage": True})
            except TypeError:
                stream = client.chat.completions.create(**kwargs)   # very old SDK
            content, reasoning = [], []
            in_tok = out_tok = 0
            with stream as events:
                for ev in events:
                    u = getattr(ev, "usage", None)
                    if u:
                        in_tok  = getattr(u, "prompt_tokens", None) or in_tok
                        out_tok = getattr(u, "completion_tokens", None) or out_tok
                    for ch in (getattr(ev, "choices", None) or []):
                        d = getattr(ch, "delta", None)
                        if d is None:
                            continue
                        if getattr(d, "content", None):
                            content.append(d.content)
                        rc = (getattr(d, "reasoning_content", None)
                              or (getattr(d, "model_extra", None) or {}).get("reasoning_content"))
                        if rc:
                            reasoning.append(rc)
            text_out, reasoning_out = "".join(content), "".join(reasoning)
            # Many self-hosted servers (vLLM/SGLang/older builds) DON'T emit the
            # final usage chunk even with stream_options — so in_tok/out_tok stay 0
            # and token counts vanish from the UI/ELK. Estimate from the text when
            # the server didn't report usage, so accounting is never blank.
            if not in_tok:
                from core.token_manager import estimate_tokens
                in_tok = estimate_tokens(system) + estimate_tokens(user)
            if not out_tok:
                from core.token_manager import estimate_tokens
                out_tok = estimate_tokens(text_out) + estimate_tokens(reasoning_out)
            return text_out, reasoning_out, in_tok, out_tok

        text = reasoning = ""
        in_tokens = out_tokens = 0
        timeout_limit = int(getattr(cfg, "llm_timeout_retry_attempts", 1) or 1)
        try:
            for attempt in _make_retry_conditional(cfg.llm_retry_attempts, cfg.llm_retry_max_wait_s,
                                                   _is_retryable_openai, timeout_limit):
                with attempt:
                    text, reasoning, in_tokens, out_tokens = _one_call()
        except Exception as exc:
            # Surface the actual endpoint so connection failures are diagnosable
            # (wrong/unreachable base_url, proxy/TLS, scheme) instead of a bare
            # "Connection error.".
            shown = self._cfg.base_url or ("https://api.openai.com/v1" if p == "openai" else "(provider default)")
            if _is_retryable_openai(exc) and "Connection" in type(exc).__name__:
                raise ConnectionError(
                    f"Could not reach LLM endpoint '{shown}' (model '{self._cfg.model}'). "
                    f"Check the Base URL includes http(s):// and ends in /v1, the host is "
                    f"reachable from this machine, and any proxy/TLS is configured. ({exc})"
                ) from exc
            raise

        # Reasoning models (Qwen/QwQ/DeepSeek-R1 via vLLM/SGLang) split the output:
        # chain-of-thought goes to `reasoning_content`, the answer to `content`. If
        # the answer is EMPTY but reasoning is present, the model ran out of tokens
        # mid-think (raise LLM_MAX_OUTPUT_TOKENS) — OR some deployments put the whole
        # output (JSON included) in reasoning_content. Fall back to it so the JSON
        # can still be recovered, and log the situation so the cause is obvious.
        if not text.strip() and reasoning.strip():
            log.warning("[LLM] '%s' returned empty content but %d chars of reasoning_content "
                        "— using it as fallback. If answers are still empty, raise "
                        "LLM_MAX_OUTPUT_TOKENS (reasoning is consuming the output budget).",
                        self._cfg.model, len(reasoning))
            text = reasoning
        return LLMResponse(text=text, input_tokens=in_tokens, output_tokens=out_tokens)


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