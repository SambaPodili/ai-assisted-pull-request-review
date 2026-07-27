"""
tests/test_llm_streaming.py
---------------------------
OpenAI-compatible streaming: with llm_stream=True the client-level timeout is a
PER-CHUNK read timeout, so a slow on-prem model that keeps emitting tokens no
longer trips APITimeoutError on the heavy agents. These tests pin the streaming
accumulation, usage extraction, reasoning fallback, and the stream_options
compatibility retry — all with a fake OpenAI client (no network).
"""
from __future__ import annotations

import types

import openai
import pytest

import config.settings as settings_mod
from agents.llm_client import ModelConfig, UnifiedLLMClient


class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning
        self.model_extra = {}


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Usage:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Event:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _Stream:
    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)


class _Completions:
    def __init__(self, events, reject_stream_options=False, nonstream_msg=None):
        self._events = events
        self._reject = reject_stream_options
        self._nonstream_msg = nonstream_msg
        self.calls = 0
        self.saw_stream_options = None

    def create(self, **kw):
        self.calls += 1
        self.saw_stream_options = "stream_options" in kw
        if self._reject and "stream_options" in kw:
            raise TypeError("unexpected keyword argument 'stream_options'")
        if kw.get("stream"):
            return _Stream(self._events)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=self._nonstream_msg)],
            usage=_Usage(120, 40))


def _client(monkeypatch, completions, stream=True):
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: fake)
    cfg = settings_mod.get_settings()
    monkeypatch.setattr(cfg, "llm_stream", stream, raising=False)
    return UnifiedLLMClient(ModelConfig(provider="custom", model="qwen",
                                        base_url="http://x/v1", api_key="k"))


def test_streaming_accumulates_content_and_usage(monkeypatch):
    events = [
        _Event([_Choice(_Delta(reasoning="thinking..."))]),
        _Event([_Choice(_Delta(content='{"ok":'))]),
        _Event([_Choice(_Delta(content='true}'))]),
        _Event(usage=_Usage(120, 40)),
    ]
    comp = _Completions(events)
    r = _client(monkeypatch, comp)._call_openai_compat("sys", "user", 4000)
    assert r.text == '{"ok":true}'
    assert r.input_tokens == 120 and r.output_tokens == 40
    assert comp.saw_stream_options is True


def test_streaming_empty_content_falls_back_to_reasoning(monkeypatch):
    # Whole answer emitted in reasoning_content (some vLLM/SGLang deployments)
    events = [_Event([_Choice(_Delta(reasoning='{"answer":1}'))]), _Event(usage=_Usage(50, 10))]
    r = _client(monkeypatch, _Completions(events))._call_openai_compat("sys", "u", 4000)
    assert r.text == '{"answer":1}'


def test_stream_options_unsupported_retries_without_it(monkeypatch):
    events = [_Event([_Choice(_Delta(content="hi"))])]
    comp = _Completions(events, reject_stream_options=True)
    r = _client(monkeypatch, comp)._call_openai_compat("sys", "u", 4000)
    assert r.text == "hi" and comp.calls == 2   # 1st with options (TypeError) → 2nd without


def test_non_streaming_still_works(monkeypatch):
    msg = types.SimpleNamespace(content='{"ok":true}', reasoning_content="", model_extra={})
    comp = _Completions([], nonstream_msg=msg)
    r = _client(monkeypatch, comp, stream=False)._call_openai_compat("sys", "u", 4000)
    assert r.text == '{"ok":true}' and r.input_tokens == 120 and r.output_tokens == 40
    assert comp.saw_stream_options is False


def test_timeout_fails_fast_but_500_still_retries(monkeypatch):
    """On the OpenAI-compat path, timeouts must NOT retry 5× (that hammers an
    overloaded self-hosted endpoint). Other transient errors still retry fully."""
    import types
    from openai import APITimeoutError, InternalServerError

    class _FailComp:
        def __init__(self, exc):
            self.exc = exc
            self.calls = 0

        def create(self, **kw):
            self.calls += 1
            if self.exc is APITimeoutError:
                raise APITimeoutError(request=types.SimpleNamespace())
            raise InternalServerError(
                "500", response=types.SimpleNamespace(status_code=500, request=None, headers={}), body=None)

    def _run(exc):
        comp = _FailComp(exc)
        cli = _client(monkeypatch, comp)
        cfg = settings_mod.get_settings()
        monkeypatch.setattr(cfg, "llm_retry_attempts", 5, raising=False)
        monkeypatch.setattr(cfg, "llm_timeout_retry_attempts", 1, raising=False)
        monkeypatch.setattr(cfg, "llm_retry_max_wait_s", 1, raising=False)
        try:
            cli._call_openai_compat("s", "u", 100)
        except Exception:
            pass
        return comp.calls

    assert _run(APITimeoutError) == 1     # fail fast — no retry storm
    assert _run(InternalServerError) == 5  # genuine transient → full retries
