"""CSRF middleware: safe methods pass, unsafe methods need Origin/Referer."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_get_does_not_require_origin(client: TestClient) -> None:
    """Safe methods are exempt — they don't change state."""
    r = client.get("/healthz")
    assert r.status_code == 200


def test_post_with_matching_origin_passes(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_WEB_CSRF_STRICT", "true")
    # Re-import to pick up the env. The middleware reads CSRF_STRICT at
    # module import time, so we need a fresh app in this strict-mode test.
    import importlib
    import app as app_module
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    # Origin matches the test client's base URL.
    r = c.post(
        "/api/permission/does-not-exist",
        data={"decision": "allow"},
        headers={"Origin": "http://testserver"},
    )
    # 404 because the request_id is fake — the middleware let it through.
    assert r.status_code == 404


def test_post_with_bad_origin_rejected(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_WEB_CSRF_STRICT", "true")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    r = c.post(
        "/api/permission/does-not-exist",
        data={"decision": "allow"},
        headers={"Origin": "https://evil.com"},
    )
    assert r.status_code == 403
    assert r.json().get("error") == "csrf"


def test_post_without_origin_or_referer_rejected_strict(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_WEB_CSRF_STRICT", "true")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    c = TestClient(app_module.app)
    # Force header drop — TestClient adds nothing if we don't ask.
    r = c.post(
        "/api/permission/does-not-exist",
        data={"decision": "allow"},
        headers={"Referer": ""},
    )
    assert r.status_code == 403
