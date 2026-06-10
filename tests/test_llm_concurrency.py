"""tests/test_llm_concurrency.py
Global LLM concurrency limiter — protects custom/self-hosted endpoints from the
~13-way parallel agent fan-out that surfaces as "failed to connect".
"""
import threading, time
import config.settings as cs
import agents.llm_client as lc


def test_gate_caps_concurrency(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "3")
    cs.get_settings.cache_clear()
    peak = cur = 0
    lk = threading.Lock()

    def worker():
        nonlocal peak, cur
        with lc._llm_gate():
            with lk:
                cur += 1; peak = max(peak, cur)
            time.sleep(0.03)
            with lk:
                cur -= 1

    ts = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak <= 3


def test_gate_unlimited_when_zero(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "0")
    cs.get_settings.cache_clear()
    assert type(lc._llm_gate()).__name__ == "_NullCtx"


def test_gate_resizes_on_setting_change(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "5")
    cs.get_settings.cache_clear()
    lc._llm_gate(); assert lc._LLM_SEM_N == 5
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    cs.get_settings.cache_clear()
    lc._llm_gate(); assert lc._LLM_SEM_N == 2


def test_base_url_normalization():
    from agents.llm_client import _normalize_base_url, ModelConfig
    # Missing scheme → public host gets https, private/local gets http
    assert _normalize_base_url("my-gateway.corp/v1") == "https://my-gateway.corp/v1"
    assert _normalize_base_url("10.0.0.5:8000/v1")   == "http://10.0.0.5:8000/v1"
    assert _normalize_base_url("localhost:11434/v1") == "http://localhost:11434/v1"
    assert _normalize_base_url("192.168.1.9/v1")     == "http://192.168.1.9/v1"
    # Already-schemed and trailing slash handling
    assert _normalize_base_url("https://api.x.com/v1/") == "https://api.x.com/v1"
    assert _normalize_base_url("http://h/v1") == "http://h/v1"
    assert _normalize_base_url("") == ""
    # Applied when building config from a UI override (the failing case)
    mc = ModelConfig.from_dict({"provider": "custom", "model": "gpt-3.5-turbo", "base_url": "my-gateway.corp/v1"})
    assert mc.base_url == "https://my-gateway.corp/v1"
