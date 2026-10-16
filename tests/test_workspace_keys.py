"""API keys that span workspaces.

Anthropic's identity-linked keys (personal and service account keys) can be
made for one workspace or for every workspace their owner can use. The second
kind has to name a workspace on every request, in the anthropic-workspace-id
header, or the API answers 400 before doing anything. These cover finding that
workspace when the key is saved, keeping it next to the key, and handing it to
the CLI of every Claude run on that credential.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import httpx
import pytest

import app as app_module
import setup_flow

ANON = "anonymous"
_KEY = "sk-ant-usr-" + "b" * 90
_WS = "wrkspc_01Ck8RGN69c9zaJUuE14PSNa"
_IS_WINDOWS = os.name == "nt"

# What the API said on 2026-10-16 to a personal key made for every workspace.
_NOT_SCOPED = (400, {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "This API key is not scoped to a workspace, so this request must include "
            "the anthropic-workspace-id header with the ID of the workspace to use. "
            "Add the header, or use an API key that is scoped to a workspace."
        ),
    },
})
# The same refusal as the authentication docs word it.
_NOT_SCOPED_DOCS = (400, {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "anthropic-workspace-id is required when authenticating with an "
            "identity-linked API key; send the id of the workspace this request acts in."
        ),
    },
})
_MODELS = (200, {"data": [{"type": "model", "id": "claude-haiku-5-5"}], "has_more": True})


def _ws(ws_id: str, name: str, archived_at=None) -> dict:
    return {"id": ws_id, "type": "workspace", "name": name, "archived_at": archived_at}


def _page(*rows: dict, has_more: bool = False):
    return (200, {"data": list(rows), "has_more": has_more})


class _FakeAnthropic:
    """Answers the three requests detect_workspace makes, and records them."""

    def __init__(self, models=_MODELS, workspaces=None, confirm=_MODELS):
        self.models = models
        self.workspaces = workspaces
        self.confirm = confirm
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/models"):
            spec = self.confirm if setup_flow.WORKSPACE_HEADER in request.headers else self.models
        elif request.url.path.endswith("/v1/organizations/workspaces"):
            spec = self.workspaces
        else:
            spec = (404, {"type": "error", "error": {"type": "not_found_error", "message": "?"}})
        if isinstance(spec, Exception):
            raise spec
        status, body = spec
        return httpx.Response(status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@pytest.fixture
def check_on(monkeypatch):
    # conftest switches the check off so no test reaches the network by accident.
    monkeypatch.setenv("CLAUDE_WEB_KEY_WORKSPACE_CHECK", "true")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


async def _detect(fake: _FakeAnthropic, key: str = _KEY):
    return await setup_flow.detect_workspace(key, transport=fake.transport())


# ─── detection ──────────────────────────────────────────────────────────────


async def test_a_key_made_for_one_workspace_needs_nothing(check_on) -> None:
    fake = _FakeAnthropic()
    assert await _detect(fake) is None
    assert len(fake.requests) == 1
    assert fake.requests[0].headers["x-api-key"] == _KEY
    assert setup_flow.WORKSPACE_HEADER not in fake.requests[0].headers


@pytest.mark.parametrize("refusal", [_NOT_SCOPED, _NOT_SCOPED_DOCS])
async def test_a_key_for_every_workspace_gets_the_only_one(check_on, refusal) -> None:
    fake = _FakeAnthropic(models=refusal, workspaces=_page(_ws(_WS, "Default")))
    assert await _detect(fake) == setup_flow.Workspace(_WS, "Default")
    models, listing, confirm = fake.requests
    assert listing.url.params["include_default"] == "true"
    assert setup_flow.WORKSPACE_HEADER not in listing.headers
    assert confirm.headers[setup_flow.WORKSPACE_HEADER] == _WS


async def test_archived_workspaces_are_not_candidates(check_on) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page(
        _ws("wrkspc_01Old", "Old", archived_at="2026-01-01T00:00:00Z"), _ws(_WS, "Default"),
    ))
    assert await _detect(fake) == setup_flow.Workspace(_WS, "Default")


async def test_several_workspaces_are_refused_not_guessed(check_on) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page(
        _ws(_WS, "Default"), _ws("wrkspc_01Second", "Research"),
    ))
    with pytest.raises(ValueError) as e:
        await _detect(fake)
    assert "Default" in str(e.value) and "Research" in str(e.value)
    assert "wasn't saved" in str(e.value)
    assert len(fake.requests) == 2


async def test_an_incomplete_list_is_never_guessed_from(check_on) -> None:
    # One active workspace on this page proves nothing when more pages follow.
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page(_ws(_WS, "Default"), has_more=True))
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)
    assert len(fake.requests) == 2


@pytest.mark.parametrize("body", [
    {"data": [_ws(_WS, "Default")]},
    {"data": [_ws(_WS, "Default")], "has_more": None},
    {"data": [_ws(_WS, "Default")], "has_more": "false"},
], ids=["missing", "null", "string"])
async def test_a_list_that_doesnt_say_it_is_complete_is_never_guessed_from(check_on, body) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=(200, body))
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)
    assert len(fake.requests) == 2  # never got as far as the confirm call


async def test_no_usable_workspace_is_refused(check_on) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page())
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)


async def test_a_workspace_id_of_the_wrong_shape_is_refused(check_on) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page(_ws("wrkspc_x\nX-Injected: 1", "Default")))
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)
    assert len(fake.requests) == 2


async def test_a_workspace_the_key_cannot_use_is_refused(check_on) -> None:
    fake = _FakeAnthropic(
        models=_NOT_SCOPED, workspaces=_page(_ws(_WS, "Default")),
        confirm=(404, {"type": "error", "error": {"type": "not_found_error",
                                                  "message": f"Workspace `{_WS}` not found."}}),
    )
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)


async def test_a_refused_workspace_lookup_is_refused(check_on) -> None:
    fake = _FakeAnthropic(models=_NOT_SCOPED, workspaces=(403, {
        "type": "error", "error": {"type": "permission_error", "message": "no"},
    }))
    with pytest.raises(ValueError, match="wasn't saved"):
        await _detect(fake)


@pytest.mark.parametrize("fake", [
    _FakeAnthropic(models=httpx.ConnectError("down")),
    _FakeAnthropic(models=httpx.ReadTimeout("slow")),
    _FakeAnthropic(models=(503, {"type": "error", "error": {"type": "api_error", "message": "x"}})),
    _FakeAnthropic(models=(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}})),
    _FakeAnthropic(models=_NOT_SCOPED, workspaces=httpx.ConnectError("down")),
    _FakeAnthropic(models=_NOT_SCOPED, workspaces=(500, {"type": "error", "error": {"message": "x"}})),
    _FakeAnthropic(models=_NOT_SCOPED, workspaces=_page(_ws(_WS, "Default")),
                   confirm=(529, {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})),
], ids=["connect", "timeout", "503", "429", "list-connect", "list-500", "confirm-529"])
async def test_trouble_reaching_anthropic_refuses_rather_than_guessing(check_on, fake) -> None:
    with pytest.raises(ValueError) as e:
        await _detect(fake)
    assert "reach Anthropic" in str(e.value) and "wasn't saved" in str(e.value)


@pytest.mark.parametrize("answer", [
    (401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}),
    (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "something else"}}),
    (404, {"type": "error", "error": {"type": "not_found_error", "message": "no such route"}}),
    (400, "not json at all"),
])
async def test_other_answers_leave_the_key_as_it_was(check_on, answer) -> None:
    # Saving never validated keys; only the workspace refusal changes anything.
    fake = _FakeAnthropic(models=answer)
    assert await _detect(fake) is None
    assert len(fake.requests) == 1


async def test_a_malformed_key_never_reaches_the_network(check_on) -> None:
    fake = _FakeAnthropic()
    with pytest.raises(ValueError, match="sk-ant"):
        await _detect(fake, key="export ANTHROPIC_API_KEY=sk-ant-abc")
    assert fake.requests == []


async def test_the_check_goes_where_the_cli_goes(check_on, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/anthropic/")
    fake = _FakeAnthropic()
    await _detect(fake)
    assert str(fake.requests[0].url).startswith("https://proxy.example/anthropic/v1/models")


async def test_the_check_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_WEB_KEY_WORKSPACE_CHECK", "false")
    fake = _FakeAnthropic(models=_NOT_SCOPED)
    assert await _detect(fake) is None
    assert fake.requests == []


# ─── the sidecar ────────────────────────────────────────────────────────────


def test_saving_with_a_workspace_keeps_it_private_beside_the_key(tmp_path) -> None:
    home = tmp_path / "slot"
    setup_flow.save_api_key(_KEY, home, workspace_id=_WS)
    path = setup_flow.workspace_id_path(home)
    assert path.read_text(encoding="utf-8") == _WS
    assert setup_flow.read_workspace_id(home) == _WS
    if not _IS_WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_new_key_drops_the_last_keys_workspace(tmp_path) -> None:
    home = tmp_path / "slot"
    setup_flow.save_api_key(_KEY, home, workspace_id=_WS)
    setup_flow.save_api_key("sk-ant-api03-" + "c" * 90, home)
    assert not setup_flow.workspace_id_path(home).exists()


def test_a_bad_workspace_id_saves_nothing(tmp_path) -> None:
    home = tmp_path / "slot"
    with pytest.raises(ValueError):
        setup_flow.save_api_key(_KEY, home, workspace_id="wrkspc_x\nX-Injected: 1")
    assert not setup_flow.api_key_path(home).exists()
    assert not setup_flow.workspace_id_path(home).exists()


def test_the_shared_slot_never_takes_a_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup_flow, "CLAUDE_HOME", tmp_path / "claude")
    monkeypatch.setattr(setup_flow, "API_KEY_FILE", tmp_path / "state" / "anthropic_api_key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        setup_flow.save_api_key(_KEY, workspace_id=_WS)
    assert not setup_flow.api_key_path(tmp_path / "claude").exists()
    assert not (tmp_path / "state" / "anthropic_api_key").exists()
    assert "ANTHROPIC_API_KEY" not in os.environ


@pytest.mark.parametrize("contents", [
    b"", b"garbage", b"wrkspc_", b"wrkspc_" + b"a" * 65, b"wrkspc_ok\nX-Injected: 1",
    b"wrkspc_ok: x", b"\xff\xfe\xfd",
])
def test_only_a_workspace_id_is_ever_read_back(tmp_path, contents) -> None:
    home = tmp_path / "slot"
    home.mkdir()
    setup_flow.workspace_id_path(home).write_bytes(contents)
    assert setup_flow.read_workspace_id(home) == ""


def test_reading_tolerates_a_trailing_newline_and_an_unreadable_file(tmp_path) -> None:
    home = tmp_path / "slot"
    home.mkdir()
    setup_flow.workspace_id_path(home).write_text(_WS + "\n", encoding="utf-8")
    assert setup_flow.read_workspace_id(home) == _WS
    setup_flow.workspace_id_path(home).unlink()
    setup_flow.workspace_id_path(home).mkdir()  # reading a directory is an OSError
    assert setup_flow.read_workspace_id(home) == ""
    assert setup_flow.read_workspace_id(tmp_path / "missing") == ""


async def test_signing_out_forgets_the_workspace(tmp_path, monkeypatch) -> None:
    class _Done:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return _Done()

    monkeypatch.setattr(setup_flow.asyncio, "create_subprocess_exec", fake_exec)
    home = tmp_path / "slot"
    setup_flow.save_api_key(_KEY, home, workspace_id=_WS)
    await setup_flow.sign_out(home)
    assert not setup_flow.api_key_path(home).exists()
    assert not setup_flow.workspace_id_path(home).exists()


async def test_signing_the_shared_slot_out_tolerates_and_clears_a_sidecar(tmp_path, monkeypatch) -> None:
    class _Done:
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*args, **kwargs):
        return _Done()

    monkeypatch.setattr(setup_flow.asyncio, "create_subprocess_exec", fake_exec)
    shared = tmp_path / "claude"
    shared.mkdir()
    monkeypatch.setattr(setup_flow, "CLAUDE_HOME", shared)
    monkeypatch.setattr(setup_flow, "API_KEY_FILE", tmp_path / "state" / "anthropic_api_key")
    await setup_flow.sign_out()  # nothing there: must not raise
    setup_flow.workspace_id_path().write_text(_WS, encoding="utf-8")
    await setup_flow.sign_out()
    assert not (shared / ".anthropic_workspace_id").exists()


# ─── what the CLI is given ──────────────────────────────────────────────────


def _home(tmp_path: Path, *, key: str = "", token: str = "", workspace: str = "") -> Path:
    home = tmp_path / "slot"
    home.mkdir(parents=True)
    if key:
        (home / ".anthropic_api_key").write_text(key, encoding="utf-8")
    if token:
        (home / ".claude_oauth_token").write_text(token, encoding="utf-8")
    if workspace:
        (home / ".anthropic_workspace_id").write_text(workspace, encoding="utf-8")
    return home


@pytest.fixture
def no_inherited_headers(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)


def test_the_cli_names_the_keys_workspace(tmp_path, no_inherited_headers) -> None:
    env = app_module._credential_cli_env(_home(tmp_path, key=_KEY, workspace=_WS))
    assert env["ANTHROPIC_API_KEY"] == _KEY
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"anthropic-workspace-id: {_WS}"


def test_a_slot_without_a_workspace_gets_exactly_what_it_got_before(tmp_path, no_inherited_headers) -> None:
    env = app_module._credential_cli_env(_home(tmp_path, key=_KEY))
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env


def test_a_signed_in_slot_ignores_a_leftover_workspace(tmp_path, no_inherited_headers) -> None:
    env = app_module._credential_cli_env(
        _home(tmp_path, token="sk-ant-oat01-" + "t" * 60, workspace=_WS),
    )
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env


def test_a_bad_sidecar_adds_no_header(tmp_path, no_inherited_headers) -> None:
    env = app_module._credential_cli_env(
        _home(tmp_path, key=_KEY, workspace="wrkspc_x\nX-Injected: 1"),
    )
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env


def test_the_operators_own_headers_survive_and_their_workspace_does_not(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "X-Proxy-Auth: abc\r\nAnthropic-Workspace-Id: wrkspc_01Other\n\nX-Trace: 1",
    )
    env = app_module._credential_cli_env(_home(tmp_path, key=_KEY, workspace=_WS))
    assert env["ANTHROPIC_CUSTOM_HEADERS"].split("\n") == [
        "X-Proxy-Auth: abc", "X-Trace: 1", f"anthropic-workspace-id: {_WS}",
    ]


def test_an_inherited_workspace_never_reaches_another_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "anthropic-workspace-id: wrkspc_01Other")
    # Blank is how the SDK says "unset"; the CLI then sends no custom headers.
    assert app_module._credential_cli_env(_home(tmp_path, key=_KEY))["ANTHROPIC_CUSTOM_HEADERS"] == ""
    signed_in = app_module._credential_cli_env(_home(tmp_path / "b", token="sk-ant-oat01-" + "t" * 60))
    assert signed_in["ANTHROPIC_CUSTOM_HEADERS"] == ""


def test_the_header_survives_the_child_env_scrub(tmp_path, no_inherited_headers) -> None:
    env = app_module._scrubbed_child_env(
        app_module._credential_cli_env(_home(tmp_path, key=_KEY, workspace=_WS)),
    )
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"anthropic-workspace-id: {_WS}"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    shared = tmp_path / "claude"
    shared.mkdir()
    monkeypatch.setattr(app_module, "CLAUDE_HOME", shared)
    monkeypatch.setattr(app_module, "PERSONAL_HOMES_DIR", tmp_path / "homes")
    return shared


@pytest.fixture
def api(client, monkeypatch, tmp_path, homes):
    client.headers["Origin"] = "http://testserver"
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    yield client
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


def test_a_run_on_the_slot_carries_the_header(api, no_inherited_headers) -> None:
    cred = app_module._create_credential(ANON, "Work API")
    home = app_module._ensure_credential_home(ANON, cred["id"])
    setup_flow.save_api_key(_KEY, home, workspace_id=_WS)
    account = app_module._resolve_account_for_run({"sub": ANON}, override_slot=f"cred:{cred['id']}")
    assert account["slot"] == f"cred:{cred['id']}"
    assert account["env"]["ANTHROPIC_CUSTOM_HEADERS"] == f"anthropic-workspace-id: {_WS}"


# ─── the endpoints ──────────────────────────────────────────────────────────


def _fake_detect(monkeypatch, outcome):
    calls = []

    async def detect(key, *, transport=None):
        calls.append(key)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(setup_flow, "detect_workspace", detect)
    return calls


def test_saving_a_key_for_every_workspace_pins_the_workspace(api, monkeypatch) -> None:
    calls = _fake_detect(monkeypatch, setup_flow.Workspace(_WS, "Default"))
    cred = app_module._create_credential(ANON, "Work API")
    r = api.post(f"/api/account/credentials/{cred['id']}/apikey", json={"api_key": _KEY})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert r.json()["workspace"] == {"id": _WS, "name": "Default"}
    assert calls == [_KEY]
    home = app_module._credential_home_path(ANON, cred["id"])
    assert setup_flow.read_workspace_id(home) == _WS


def test_a_scoped_key_saves_as_before(api, monkeypatch) -> None:
    _fake_detect(monkeypatch, None)
    cred = app_module._create_credential(ANON, "Work API")
    r = api.post(f"/api/account/credentials/{cred['id']}/apikey", json={"api_key": _KEY})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"] is None
    home = app_module._credential_home_path(ANON, cred["id"])
    assert setup_flow.api_key_path(home).read_text(encoding="utf-8") == _KEY
    assert not setup_flow.workspace_id_path(home).exists()


def test_re_saving_a_scoped_key_drops_the_old_workspace(api, monkeypatch, no_inherited_headers) -> None:
    cred = app_module._create_credential(ANON, "Work API")
    url = f"/api/account/credentials/{cred['id']}/apikey"
    home = app_module._credential_home_path(ANON, cred["id"])
    _fake_detect(monkeypatch, setup_flow.Workspace(_WS, "Default"))
    assert api.post(url, json={"api_key": _KEY}).status_code == 200
    assert setup_flow.read_workspace_id(home) == _WS
    scoped = "sk-ant-api03-" + "c" * 90
    _fake_detect(monkeypatch, None)
    r = api.post(url, json={"api_key": scoped})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"] is None
    assert setup_flow.api_key_path(home).read_text(encoding="utf-8") == scoped
    assert not setup_flow.workspace_id_path(home).exists()
    assert "ANTHROPIC_CUSTOM_HEADERS" not in app_module._credential_cli_env(home)


def test_a_refused_key_leaves_the_slots_key_and_workspace_alone(api, monkeypatch) -> None:
    cred = app_module._create_credential(ANON, "Work API")
    url = f"/api/account/credentials/{cred['id']}/apikey"
    home = app_module._credential_home_path(ANON, cred["id"])
    _fake_detect(monkeypatch, setup_flow.Workspace(_WS, "Default"))
    assert api.post(url, json={"api_key": _KEY}).status_code == 200
    _fake_detect(monkeypatch, ValueError("Couldn't reach Anthropic. It wasn't saved."))
    assert api.post(url, json={"api_key": "sk-ant-api03-" + "d" * 90}).status_code == 400
    assert setup_flow.api_key_path(home).read_text(encoding="utf-8") == _KEY
    assert setup_flow.read_workspace_id(home) == _WS


def test_a_refused_key_is_not_saved(api, monkeypatch) -> None:
    _fake_detect(monkeypatch, ValueError("This key works in several workspaces. It wasn't saved."))
    cred = app_module._create_credential(ANON, "Work API")
    r = api.post(f"/api/account/credentials/{cred['id']}/apikey", json={"api_key": _KEY})
    assert r.status_code == 400
    assert "wasn't saved" in r.json()["detail"]
    home = app_module._credential_home_path(ANON, cred["id"])
    assert not setup_flow.api_key_path(home).exists()


@pytest.mark.parametrize("path", ["/api/account/shared/apikey", "/api/setup/apikey"])
def test_the_shared_slot_refuses_a_key_that_needs_a_workspace(api, monkeypatch, path) -> None:
    _fake_detect(monkeypatch, setup_flow.Workspace(_WS, "Default"))
    saved = []
    monkeypatch.setattr(setup_flow, "save_api_key", lambda *a, **k: saved.append((a, k)))
    r = api.post(path, json={"api_key": _KEY})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "workspace" in detail and "wasn't saved" in detail
    assert saved == []


@pytest.mark.parametrize("path", ["/api/account/shared/apikey", "/api/setup/apikey"])
def test_the_shared_slot_checks_before_it_saves(api, monkeypatch, path) -> None:
    order = []

    async def detect(key, *, transport=None):
        order.append("detect")
        return None

    monkeypatch.setattr(setup_flow, "detect_workspace", detect)
    monkeypatch.setattr(setup_flow, "save_api_key", lambda *a, **k: order.append("save"))
    assert api.post(path, json={"api_key": _KEY}).status_code == 200
    assert order == ["detect", "save"]
