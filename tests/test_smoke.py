"""End-to-end smoke: app boots, /healthz returns 200, security headers set."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/healthz")
    assert "Content-Security-Policy" in r.headers
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_pages_ask_proxies_not_to_inject_scripts(client: TestClient) -> None:
    """Cloudflare adds its analytics beacon to any HTML page that doesn't say
    no-transform. The CSP blocks it, and on a network whose ad blocker answers
    the beacon's host with 0.0.0.0 the blocked script kept the iPhone
    home-screen app on a blank page."""
    for path in ("/setup", "/account", "/personalities"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, path
        cache_control = r.headers.get("Cache-Control", "")
        assert "no-transform" in cache_control, (path, cache_control)
        assert "no-store" in cache_control, (path, cache_control)
    assert "Cache-Control" not in client.get("/healthz").headers


def test_models_data_in_index_template(client: TestClient) -> None:
    """The models bootstrap should be a JSON-typed script tag, not an
    inline executable script — confirming the CSP-friendly fix."""
    r = client.get("/", follow_redirects=False)
    # AUTH_MODE=none so we hit /setup or / directly. Either way, the index
    # template is the one we care about. Skip if redirected (no setup yet).
    if r.status_code == 302:
        return
    assert r.status_code == 200
    body = r.text
    assert 'type="application/json"' in body
    assert "id=\"models-data\"" in body
    # The unsafe inline assignment should be gone.
    assert "window.CLAUDE_WEB_MODELS" not in body
