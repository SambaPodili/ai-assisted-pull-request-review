"""tests/test_production_hardening.py
Enterprise hardening: CORS credential safety, sanitized error envelope,
rate-limiter idle-bucket eviction.
"""
import time
from fastapi.testclient import TestClient

from config.settings import Settings
from api.app import create_app


def _client(**overrides):
    cfg = Settings(skip_auth=True, **overrides)
    return TestClient(create_app(cfg), raise_server_exceptions=False), create_app(cfg)


def test_cors_wildcard_disables_credentials():
    cfg = Settings(skip_auth=True, cors_origins=["*"])
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/live", headers={"Origin": "https://anything.example"})
    assert r.headers.get("access-control-allow-origin") == "*"
    # Wildcard + credentials is invalid — must NOT be advertised.
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}


def test_cors_explicit_origin_enables_credentials():
    cfg = Settings(skip_auth=True, cors_origins=["https://ui.example.com"])
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/live", headers={"Origin": "https://ui.example.com"})
    assert r.headers.get("access-control-allow-origin") == "https://ui.example.com"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_unhandled_exception_is_sanitized():
    cfg = Settings(skip_auth=True, cors_origins=["*"])
    app = create_app(cfg)

    @app.get("/_boom")
    def _boom():
        raise ValueError("secret internal detail that must not leak")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/_boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "Internal server error"
    assert "request_id" in body
    # The exception message must never reach the client.
    assert "secret internal detail" not in r.text


def test_rate_limiter_evicts_idle_buckets():
    import api.middleware.rate_limiter as rl
    rl._BUCKETS.clear()
    fresh = rl._get_bucket("active-key")
    idle = rl._get_bucket("idle-key")
    # Make 'idle' look untouched for longer than the TTL.
    idle.last_seen = time.monotonic() - (rl._IDLE_TTL_S + 60)
    fresh.last_seen = time.monotonic()
    # Force a sweep.
    rl._last_sweep = time.monotonic() - (rl._SWEEP_INTERVAL_S + 1)
    rl._maybe_sweep(time.monotonic())
    assert "idle-key" not in rl._BUCKETS
    assert "active-key" in rl._BUCKETS


def test_rate_limiter_blocks_over_limit():
    import api.middleware.rate_limiter as rl
    w = rl._SlidingWindow()
    assert all(w.allow(3) for _ in range(3))   # first 3 allowed
    assert w.allow(3) is False                  # 4th blocked
    assert w.retry_after() >= 1


def test_list_settings_accept_plain_and_csv_env(monkeypatch):
    """Regression: plain/CSV env values for list settings must not crash with
    JSONDecodeError (pydantic-settings would otherwise json.loads() them)."""
    from config.settings import Settings
    monkeypatch.setenv("CORS_ORIGINS", "*")
    monkeypatch.setenv("API_KEYS", "mykey")
    monkeypatch.setenv("COMPLIANCE_FRAMEWORKS", "MAS TRM, PCI-DSS")
    s = Settings()
    assert s.cors_origins == ["*"]
    assert s.api_keys == ["mykey"]
    assert s.compliance_frameworks == ["MAS TRM", "PCI-DSS"]


def test_list_settings_still_accept_json_env(monkeypatch):
    from config.settings import Settings
    monkeypatch.setenv("CORS_ORIGINS", '["https://a","https://b"]')
    monkeypatch.setenv("API_KEYS", '[{"key":"k","role":"admin"}]')
    s = Settings()
    assert s.cors_origins == ["https://a", "https://b"]
    assert s.api_keys == [{"key": "k", "role": "admin"}]
