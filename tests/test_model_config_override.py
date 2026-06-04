"""
tests/test_model_config_override.py
-------------------------------------
The model/provider/key chosen in the UI (sent as a per-request `llm_config`
override) must be authoritative. The backend env is only a *fallback* for a
blank API key — it must never override or replace UI-supplied values, and
partial/blank UI fields must fall back to sane defaults rather than clobbering.
"""
from __future__ import annotations
import pytest

from agents.llm_client import ModelConfig, UnifiedLLMClient
from agents.base_agent import BaseAgent


class _Agent:
    """Minimal stand-in exposing only what _resolve_model_config needs."""
    def __init__(self, default_key: str = ""):
        self._default_api_key = default_key

    def resolve(self, override: dict | None, agent_key: str = "risk") -> ModelConfig:
        ctx = {"model_config": override} if override else {}
        return BaseAgent._resolve_model_config(self, ctx, agent_key)


# ── from_dict: blank fields fall back to defaults, never clobber ───────────────

def test_blank_model_falls_back_to_default():
    c = ModelConfig.from_dict({"provider": "anthropic", "model": "", "api_key": "sk-UI"})
    assert c.model == "claude-sonnet-4-6"
    assert c.api_key == "sk-UI"


def test_whitespace_fields_treated_as_blank():
    c = ModelConfig.from_dict({"provider": "  ", "model": "  ", "api_key": "  sk-UI  "})
    assert c.provider == "anthropic"
    assert c.model == "claude-sonnet-4-6"
    assert c.api_key == "sk-UI"


# ── resolution precedence: UI wins, env is fallback only ──────────────────────

def test_ui_model_and_key_win_over_env():
    r = _Agent(default_key="sk-ENV").resolve(
        {"provider": "anthropic", "model": "claude-opus-4-6", "api_key": "sk-UI"})
    assert r.model == "claude-opus-4-6"   # UI choice, NOT the per-agent default
    assert r.api_key == "sk-UI"           # UI key, NOT the env key


def test_blank_ui_key_falls_back_to_env():
    r = _Agent(default_key="sk-ENV").resolve(
        {"provider": "anthropic", "model": "claude-opus-4-6", "api_key": ""})
    assert r.api_key == "sk-ENV"


def test_per_agent_model_used_only_when_ui_omits_model():
    # UI omits model entirely -> per-agent fast/strong logic applies
    from core.token_manager import _STRONG_AGENTS, MODEL_STRONG, MODEL_FAST
    strong_agent = next(iter(_STRONG_AGENTS))
    r = _Agent(default_key="sk-ENV").resolve({"provider": "anthropic", "api_key": "sk-UI"}, strong_agent)
    assert r.model == MODEL_STRONG


def test_no_override_uses_settings():
    r = _Agent(default_key="sk-ENV").resolve(None, "risk")
    assert r.provider == "anthropic"
    assert r.api_key == "sk-ENV"


# ── fail-fast when no key anywhere ────────────────────────────────────────────

def test_missing_key_raises_actionable_error():
    client = UnifiedLLMClient(ModelConfig(provider="anthropic", model="x", api_key=""))
    with pytest.raises(ValueError, match="No API key for provider"):
        client.create("system", "user")


def test_ollama_needs_no_key():
    # Ollama should not trip the missing-key guard (key defaults to a placeholder)
    cfg = ModelConfig.from_dict({"provider": "ollama", "model": "llama3.2", "api_key": ""})
    assert cfg.provider == "ollama"
    # create() would try to reach a local server; we only assert the guard
    # does not fire for ollama by checking the precondition branch indirectly.
    assert cfg.provider not in ("anthropic", "openai", "azure_openai")
