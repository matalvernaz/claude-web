"""Shared-credential re-auth from /account.

/setup locks after first configuration (ENABLE_SETUP=auto), so the shared
slot needs its own recovery path when its token expires. These cover the
gate policy and the endpoint family mirroring the per-cred flows.
"""
import pytest

# test_csrf.py reloads the app in strict-CSRF mode and (alphabetically)
# runs before this file; a matching Origin passes in both modes. Same
# pattern as test_restart.py.
_ORIGIN = {"Origin": "http://testserver"}


class _FakeFlow:
    def __init__(self, status="awaiting_code", url="https://claude.ai/x"):
        self.status = status
        self.url = url

    def to_public(self):
        return {"status": self.status, "url": self.url}


@pytest.fixture
def app_module(client):
    import app as app_module
    return app_module


def test_gate_default_single_operator_allows(app_module):
    assert app_module._shared_reauth_allowed({"email": "anyone@example.com"}) is True


def test_gate_enable_setup_false_blocks(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ENABLE_SETUP", "false")
    assert app_module._shared_reauth_allowed({"email": "anyone@example.com"}) is False


def test_gate_admin_list_enforced(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {"matt@example.com"})
    assert app_module._shared_reauth_allowed({"email": "matt@example.com"}) is True
    assert app_module._shared_reauth_allowed({"email": "MATT@example.com"}) is True
    assert app_module._shared_reauth_allowed({"email": "other@example.com"}) is False


def test_gate_per_user_mode_requires_admin(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", set())
    assert app_module._shared_reauth_allowed({"email": "anyone@example.com"}) is False


def test_shared_status_shape(client):
    r = client.get("/api/account/shared/status")
    assert r.status_code == 200
    d = r.json()
    assert "credential" in d and "configured" in d["credential"]
    assert "flow" in d


def test_shared_oauth_start_and_code(client, app_module, monkeypatch):
    import setup_flow

    started = {}

    async def fake_start(variant, **kwargs):
        started["variant"] = variant
        return _FakeFlow("awaiting_code")

    async def fake_submit(code, **kwargs):
        started["code"] = code
        return _FakeFlow("done")

    monkeypatch.setattr(setup_flow, "start_oauth", fake_start)
    monkeypatch.setattr(setup_flow, "submit_code", fake_submit)

    r = client.post("/api/account/shared/oauth/start", json={"variant": "claudeai"}, headers=_ORIGIN)
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_code"
    assert started["variant"] == "claudeai"

    r = client.post("/api/account/shared/oauth/start", json={"variant": "bogus"}, headers=_ORIGIN)
    assert r.status_code == 400

    r = client.post("/api/account/shared/oauth/code", json={"code": "abc123"}, headers=_ORIGIN)
    assert r.status_code == 200
    assert r.json()["flow"]["status"] == "done"
    assert started["code"] == "abc123"

    r = client.post("/api/account/shared/oauth/code", json={"code": ""}, headers=_ORIGIN)
    assert r.status_code == 400


def test_shared_endpoints_403_when_locked(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ENABLE_SETUP", "false")
    assert client.post(
        "/api/account/shared/oauth/start", json={"variant": "claudeai"}, headers=_ORIGIN,
    ).status_code == 403
    assert client.post(
        "/api/account/shared/oauth/code", json={"code": "x"}, headers=_ORIGIN,
    ).status_code == 403
    assert client.post("/api/account/shared/oauth/cancel", headers=_ORIGIN).status_code == 403
    assert client.post(
        "/api/account/shared/apikey", json={"api_key": "sk-ant-x"}, headers=_ORIGIN,
    ).status_code == 403
    # Status stays readable — it mutates nothing.
    assert client.get("/api/account/shared/status").status_code == 200


def test_shared_apikey_save(client, monkeypatch):
    import setup_flow

    saved = {}

    def fake_save(key, home=None):
        saved["key"] = key

    monkeypatch.setattr(setup_flow, "save_api_key", fake_save)
    r = client.post("/api/account/shared/apikey", json={"api_key": "sk-ant-test"}, headers=_ORIGIN)
    assert r.status_code == 200
    assert saved["key"] == "sk-ant-test"
