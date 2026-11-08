"""DeepSeek is account-scoped like Claude and Codex: each user links their own API keys, chats run on Claude Code routed to DeepSeek with the picked account's key, and no key ever leaves the server."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage, SystemMessage

import app as app_module
import deepseek_provider
from tests.test_deepseek_provider import MODELS_PAYLOAD

USER = "anonymous"  # who the test client is signed in as (AUTH_MODE=none)
PERSONAL_KEY = "sk-personal-0001"
SECOND_KEY = "sk-second-0002"
SHARED_KEY = "sk-shared-host"


# test_csrf.py reloads the app in strict-CSRF mode; a matching Origin passes in both modes.
@pytest.fixture(autouse=True)
def _matching_origin(client):
    client.headers["Origin"] = "http://testserver"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "DEEPSEEK_KEYS_DIR", tmp_path / "deepseek-keys")
    monkeypatch.setattr(app_module, "ROUTED_SETTINGS_DIR", tmp_path / "routed-settings")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    yield
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


def _seed_models(key: str) -> None:
    entries = [
        deepseek_provider._model_entry(raw, is_default=index == 0)
        for index, raw in enumerate(MODELS_PAYLOAD["data"])
    ]
    deepseek_provider._models_cache[deepseek_provider.cache_key(key)] = {
        "fetched_at": time.monotonic(), "models": entries,
    }


@pytest.fixture
def deepseek(monkeypatch):
    """DeepSeek turned on, no shared key, every test key's model list already fetched, and key checks answered locally."""
    monkeypatch.setattr(deepseek_provider, "ENABLED", True)
    monkeypatch.setattr(deepseek_provider, "API_KEY", "")
    monkeypatch.setattr(deepseek_provider, "BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(deepseek_provider, "_models_cache", {})
    for key in (PERSONAL_KEY, SECOND_KEY, SHARED_KEY):
        _seed_models(key)
    checked = []

    async def validate_key(api_key):
        checked.append(api_key)
        if api_key.startswith("sk-bad"):
            raise ValueError(deepseek_provider.REFUSED_KEY)
        return {"sufficient": True, "balances": []}

    monkeypatch.setattr(deepseek_provider, "validate_key", validate_key)
    return checked


def _link(client, label="Personal", api_key=PERSONAL_KEY):
    return client.post("/api/account/deepseek/credentials", json={"label": label, "api_key": api_key})


def _unexpected(*args, **kwargs):
    raise AssertionError("a DeepSeek request reached Anthropic account handling")


@pytest.fixture
def sdk_capture(monkeypatch, deepseek):
    captured = {}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        captured["options"] = options
        captured["run"] = run
        # The overlay file as the CLI would find it at spawn.
        overlay = Path(options.settings)
        captured["overlay_mode"] = stat.S_IMODE(os.stat(overlay).st_mode)
        captured["overlay"] = json.loads(overlay.read_text())
        ready = asyncio.Event()

        class Client:
            async def query(self, prompt, **kwargs):
                captured["decisions"] = [
                    await options.can_use_tool("Write", {"file_path": "/project/result.txt", "content": "x"}, None),
                    await options.can_use_tool("Bash", {"command": "rm -rf build"}, None),
                ]
                ready.set()

            async def receive_messages(self):
                await ready.wait()
                yield SystemMessage(subtype="init", data={"session_id": options.resume or "deepseek-session"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                    num_turns=1, session_id=options.resume or "deepseek-session", total_cost_usd=0.08,
                )

        yield Client()

    async def decision(run, tool, *args, **kwargs):
        captured.setdefault("asked", []).append(tool)
        return "allow" if tool == "Write" else "deny"

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    monkeypatch.setattr(app_module, "_await_permission_decision", decision)
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)
    monkeypatch.setattr(app_module, "_select_account_slot", _unexpected)
    monkeypatch.setattr(app_module, "_log_usage", _unexpected)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda: False)
    monkeypatch.setattr(app_module, "FALLBACK_MODEL", "cloud-fallback")
    return captured


# ── Linking keys ──────────────────────────────────────────────────────────────

def test_linking_a_key_checks_it_saves_it_privately_and_makes_it_the_default(client, deepseek):
    response = _link(client)

    assert response.status_code == 200
    payload = response.json()
    (cred,) = payload["credentials"]
    assert cred["label"] == "Personal" and cred["configured"] is True
    assert payload["active"] == f"cred:{cred['id']}"
    assert deepseek == [PERSONAL_KEY]
    # The key never comes back to the browser.
    assert PERSONAL_KEY not in response.text
    key_file = app_module._deepseek_key_path(USER, cred["id"])
    assert key_file.read_text() == PERSONAL_KEY
    if os.name == "posix":
        # Windows has no permission bits; the state folder sits in the user's own profile there.
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700


def test_a_refused_key_is_not_saved(client, deepseek):
    response = _link(client, api_key="sk-bad-key")

    assert response.status_code == 400
    assert "refused this API key" in response.text
    assert app_module._list_deepseek_credentials(USER) == []


def test_deepseek_offline_while_checking_a_key_says_try_again(client, deepseek, monkeypatch):
    async def offline(api_key):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(deepseek_provider, "validate_key", offline)

    response = _link(client)

    assert response.status_code == 502
    assert "Try again" in response.text
    assert app_module._list_deepseek_credentials(USER) == []


def test_keys_cannot_be_linked_where_deepseek_is_off(client, deepseek, monkeypatch):
    monkeypatch.setattr(deepseek_provider, "ENABLED", False)

    assert _link(client).status_code == 409


def test_a_key_can_be_replaced_and_an_account_renamed(client, deepseek):
    cred_id = _link(client).json()["credentials"][0]["id"]

    replaced = client.post(f"/api/account/deepseek/credentials/{cred_id}/apikey", json={"api_key": SECOND_KEY})
    renamed = client.patch(f"/api/account/deepseek/credentials/{cred_id}", json={"label": "Work"})

    assert replaced.status_code == 200 and SECOND_KEY not in replaced.text
    assert app_module._read_deepseek_key(USER, cred_id) == SECOND_KEY
    assert renamed.json()["credentials"][0]["label"] == "Work"


def test_removing_an_account_erases_its_key_and_its_default(client, deepseek):
    cred_id = _link(client).json()["credentials"][0]["id"]
    key_file = app_module._deepseek_key_path(USER, cred_id)

    response = client.delete(f"/api/account/deepseek/credentials/{cred_id}")

    assert response.status_code == 200
    assert response.json()["credentials"] == []
    assert not key_file.exists()
    assert app_module._deepseek_user_active_slot(USER) == "shared"


def test_an_account_in_use_by_a_running_chat_cannot_be_removed(client, deepseek):
    cred_id = _link(client).json()["credentials"][0]["id"]
    run = app_module.ActiveRun("ds-live", owner_sub=USER, account_slot=f"cred:{cred_id}")
    run.provider = "deepseek"
    app_module.ACTIVE_RUNS[run.run_id] = run

    response = client.delete(f"/api/account/deepseek/credentials/{cred_id}")

    assert response.status_code == 409
    assert app_module._deepseek_credential_is_configured(USER, cred_id)


def test_one_persons_keys_are_invisible_to_another(client, deepseek):
    cred_id = _link(client).json()["credentials"][0]["id"]
    other = {"sub": "someone-else"}

    resolved = app_module._resolve_deepseek_account_for_run(other, override_slot=f"cred:{cred_id}")

    assert resolved["api_key"] == ""
    assert app_module._deepseek_account_payload(other)["credentials"] == []
    assert app_module._read_deepseek_key("someone-else", cred_id) == ""


def test_the_default_account_can_be_switched(client, deepseek):
    first = _link(client, "Personal", PERSONAL_KEY).json()["credentials"][0]["id"]
    second = _link(client, "Work", SECOND_KEY).json()["credentials"][1]["id"]
    assert app_module._deepseek_user_active_slot(USER) == f"cred:{second}"

    response = client.post("/api/account/active", data={"active": f"cred:{first}", "provider": "deepseek"})

    assert response.status_code == 200
    assert response.json()["active"] == f"cred:{first}"
    # There's no shared key on this server to switch to.
    assert client.post("/api/account/active", data={"active": "shared", "provider": "deepseek"}).status_code == 400


# ── Chats ─────────────────────────────────────────────────────────────────────

def test_a_chat_runs_claude_code_against_deepseek_on_the_users_key(client, sdk_capture, monkeypatch):
    cred_id = _link(client).json()["credentials"][0]["id"]
    # A shared key in the service environment must not leak into the child.
    monkeypatch.setenv("CLAUDE_WEB_DEEPSEEK_API_KEY", "sk-host-secret")

    response = client.post("/api/chat", data={"message": "Edit a file and clean up", "provider": "deepseek"})

    assert response.status_code == 200
    assert '"type":"error"' not in response.text.replace(" ", "")
    options = sdk_capture["options"]
    # --model wins over ANTHROPIC_MODEL, so it carries the "[1m]" tag too.
    assert options.model == "deepseek-flash[1m]"
    assert options.effort == "high"
    assert options.thinking == {"type": "adaptive"}
    assert options.fallback_model is None
    env = options.env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == PERSONAL_KEY
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"
    assert env["CLAUDE_WEB_DEEPSEEK_API_KEY"] == ""
    # The routing overlay pins the key over any settings file, from a private
    # file: as JSON on the command line it showed in ps and /proc.
    assert PERSONAL_KEY not in options.settings
    assert sdk_capture["overlay"]["env"]["ANTHROPIC_AUTH_TOKEN"] == PERSONAL_KEY
    if os.name == "posix":
        assert sdk_capture["overlay_mode"] == 0o600
    assert not Path(options.settings).exists(), "the overlay outlived its run"
    assert sdk_capture["run"].account_slot == f"cred:{cred_id}"
    assert sdk_capture["asked"] == ["Write", "Bash"]
    assert isinstance(sdk_capture["decisions"][0], PermissionResultAllow)
    assert isinstance(sdk_capture["decisions"][1], PermissionResultDeny)
    assert app_module._routed_session_row("deepseek-session") == {
        "model": "deepseek-flash", "effort": "high", "provider": "deepseek",
    }
    assert app_module._session_account_slot("deepseek-session") == f"cred:{cred_id}"
    assert app_module._conversation_binding_by_native("deepseek", "deepseek-session") is not None


def test_the_account_picked_in_the_header_is_the_one_used(client, sdk_capture):
    _link(client, "Personal", PERSONAL_KEY)
    work = _link(client, "Work", SECOND_KEY).json()["credentials"][1]["id"]
    first = app_module._list_deepseek_credentials(USER)[0]["id"]

    response = client.post("/api/chat", data={
        "message": "Hi", "provider": "deepseek", "account_slot": f"cred:{first}",
        "model": "deepseek-v4-pro", "effort": "max",
    })

    assert response.status_code == 200
    assert sdk_capture["options"].env["ANTHROPIC_AUTH_TOKEN"] == PERSONAL_KEY
    assert sdk_capture["options"].model == "deepseek-v4-pro[1m]"
    assert sdk_capture["options"].effort == "max"
    assert sdk_capture["run"].account_slot != f"cred:{work}"


def test_without_a_key_of_their_own_a_user_runs_on_the_shared_key(client, sdk_capture, monkeypatch):
    monkeypatch.setattr(deepseek_provider, "API_KEY", SHARED_KEY)

    response = client.post("/api/chat", data={"message": "Hi", "provider": "deepseek"})

    assert response.status_code == 200
    assert sdk_capture["options"].env["ANTHROPIC_AUTH_TOKEN"] == SHARED_KEY
    assert sdk_capture["run"].account_slot == "shared"


def test_no_key_anywhere_points_at_the_accounts_page(client, deepseek, monkeypatch):
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)

    response = client.post("/api/chat", data={"message": "Hi", "provider": "deepseek"})

    assert response.status_code == 503
    assert response.json() == {"error": "deepseek_not_configured", "detail": deepseek_provider.NO_KEY}


def test_a_reopened_deepseek_chat_resumes_on_its_own_account(client, sdk_capture):
    _link(client, "Personal", PERSONAL_KEY)
    work = _link(client, "Work", SECOND_KEY).json()["credentials"][1]["id"]
    first = app_module._list_deepseek_credentials(USER)[0]["id"]
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-existing', 'deepseek-v4-pro', 'low', 'deepseek')")
    app_module._bind_session_account("ds-existing", USER, f"cred:{first}")

    response = client.post("/api/chat", data={"message": "Continue", "session_id": "ds-existing"})

    assert response.status_code == 200
    assert sdk_capture["run"].provider == "deepseek"
    assert sdk_capture["options"].resume == "ds-existing"
    assert sdk_capture["options"].model == "deepseek-v4-pro[1m]"
    assert sdk_capture["options"].effort == "low"
    # The session's account wins over the newer default.
    assert app_module._deepseek_user_active_slot(USER) == f"cred:{work}"
    assert sdk_capture["options"].env["ANTHROPIC_AUTH_TOKEN"] == PERSONAL_KEY


def test_reopening_a_deepseek_chat_shows_the_account_it_is_bound_to(
    client, deepseek, monkeypatch, tmp_path,
):
    """The picker's value is sent with the next message as an explicit pick,
    so a reopened chat that shows the user's newer default would move the
    chat (and its billing) onto that account without the user choosing to."""
    _link(client, "Personal", PERSONAL_KEY)
    work = _link(client, "Work", SECOND_KEY).json()["credentials"][1]["id"]
    first = app_module._list_deepseek_credentials(USER)[0]["id"]
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-reopen', 'deepseek-v4-pro', 'low', 'deepseek')")
    app_module._bind_session_account("ds-reopen", USER, f"cred:{first}")
    path = tmp_path / "project" / "ds-reopen.jsonl"
    path.parent.mkdir()
    path.write_text("")
    monkeypatch.setattr(app_module, "_find_session_path", lambda *a: path)
    monkeypatch.setattr(app_module, "session_transcript", lambda *a: [])

    response = client.get("/api/sessions/ds-reopen")

    assert response.status_code == 200
    assert response.json()["account_slot"] == f"cred:{first}"
    assert app_module._deepseek_user_active_slot(USER) == f"cred:{work}"


def test_switching_accounts_between_turns_respawns_on_the_new_key(client, monkeypatch, deepseek):
    _link(client, "Personal", PERSONAL_KEY)
    work = _link(client, "Work", SECOND_KEY).json()["credentials"][1]["id"]
    first = app_module._list_deepseek_credentials(USER)[0]["id"]
    run = app_module.ActiveRun("ds-run", owner_sub=USER, account_slot=f"cred:{first}")
    run.provider, run.model, run.effort = "deepseek", "deepseek-flash", "high"
    app_module.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *a, **k: {"id": None})
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)

    response = client.post("/api/chat/send/ds-run", data={"message": "Go on", "account_slot": f"cred:{work}"})

    assert response.status_code == 409
    assert response.json()["error"] == "account_changed"


@pytest.mark.parametrize("provider", ["claude", "codex", "local"])
def test_a_deepseek_chat_cannot_switch_provider(client, deepseek, provider):
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-id', 'deepseek-flash', 'high', 'deepseek')")

    response = client.post("/api/chat", data={"message": "Continue", "session_id": "ds-id", "provider": provider})

    assert response.status_code == 400
    assert "deepseek provider changes require a new chat" in response.text


def test_deepseek_cannot_resume_someone_elses_transcript(client, deepseek):
    _link(client)

    response = client.post("/api/chat", data={"message": "Continue", "session_id": "claude-id", "provider": "deepseek"})

    assert response.status_code == 400
    assert "does not belong to the deepseek provider" in response.text


def test_a_deepseek_outage_never_falls_back_to_claude(client, monkeypatch, deepseek):
    _link(client)

    async def offline(name, api_key=None):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(deepseek_provider, "model_for_run", offline)
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)

    response = client.post("/api/chat", data={"message": "Hello", "provider": "deepseek"})

    assert response.status_code == 503
    assert "DeepSeek model unavailable" in response.text


def test_a_deepseek_result_is_never_billed_or_offered_failover(monkeypatch):
    run = app_module.ActiveRun("ds-result", account_slot="cred:1")
    run.provider, run.model = "deepseek", "deepseek-flash"
    monkeypatch.setattr(app_module, "_log_usage", _unexpected)
    monkeypatch.setattr(app_module, "_failover_offer", _unexpected)
    monkeypatch.setattr(app_module, "_note_model_denial", _unexpected)

    events = app_module._sdk_message_to_events(ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="ds-id",
        result="There is an issue with the selected model", total_cost_usd=0.08,
    ), run)

    # The CLI's figure is a Claude-priced guess, never a DeepSeek bill.
    assert events[0]["total_cost_usd"] == 0
    assert events[0]["cost_is_billed"] is False
    assert events[-1]["type"] == "error"


# ── Provider list, Usage dialog and Accounts page ─────────────────────────────

def test_the_provider_list_offers_deepseek_with_the_users_accounts(client, deepseek):
    _link(client)

    providers = {p["key"]: p for p in client.get("/api/providers").json()["providers"]}

    entry = providers["deepseek"]
    assert entry["available"] is True
    assert entry["label"] == "DeepSeek"
    assert [m["key"] for m in entry["models"]] == ["deepseek-flash", "deepseek-v4-pro"]
    assert entry["capabilities"]["accounts"] is True
    assert entry["capabilities"]["failover"] is False
    assert entry["capabilities"]["usage"] is False
    assert entry["capabilities"]["advisor"] is False
    assert [c["label"] for c in entry["accounts"]["credentials"]] == ["Personal"]
    assert PERSONAL_KEY not in json.dumps(entry)


def test_a_user_without_a_key_sees_how_to_get_one(client, deepseek):
    providers = {p["key"]: p for p in client.get("/api/providers").json()["providers"]}

    assert providers["deepseek"]["available"] is False
    assert providers["deepseek"]["reason"] == deepseek_provider.NO_KEY


def test_where_deepseek_is_off_it_offers_nothing(client, deepseek, monkeypatch):
    monkeypatch.setattr(deepseek_provider, "ENABLED", False)

    providers = {p["key"]: p for p in client.get("/api/providers").json()["providers"]}

    assert providers["deepseek"]["available"] is False
    assert providers["deepseek"]["accounts"] == {}
    assert "CLAUDE_WEB_DEEPSEEK_ENABLED" in providers["deepseek"]["reason"]


def test_the_usage_dialog_reads_the_balance_of_the_account_in_use(client, monkeypatch, deepseek):
    first = _link(client, "Personal", PERSONAL_KEY).json()["credentials"][0]["id"]
    _link(client, "Work", SECOND_KEY)
    asked = []

    async def balance(api_key):
        asked.append(api_key)
        return {"sufficient": True, "balances": [
            {"currency": "USD", "total": "4.20", "granted": "0.00", "topped_up": "4.20"}]}

    monkeypatch.setattr(deepseek_provider, "balance", balance)

    payload = client.get(f"/api/deepseek/usage?account_slot=cred:{first}").json()

    assert asked == [PERSONAL_KEY]
    assert payload["available"] is True
    assert payload["account_label"] == "Personal"
    assert payload["balances"][0]["total"] == "4.20"
    assert PERSONAL_KEY not in json.dumps(payload)


def test_the_usage_dialog_says_why_without_a_key(client, deepseek):
    payload = client.get("/api/deepseek/usage").json()

    assert payload["available"] is False
    assert payload["reason"] == deepseek_provider.NO_KEY


def test_the_accounts_page_has_a_deepseek_section_only_where_it_is_on(client, deepseek, monkeypatch):
    _link(client)

    page = client.get("/account").text
    assert 'id="deepseek-creds-heading"' in page
    assert "Personal" in page and PERSONAL_KEY not in page

    monkeypatch.setattr(deepseek_provider, "ENABLED", False)
    assert 'id="deepseek-creds-heading"' not in client.get("/account").text


def test_the_page_opens_with_only_deepseek_set_up(client, monkeypatch, deepseek):
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda: False)

    assert client.get("/", follow_redirects=False).status_code == 200


# ── Storage ───────────────────────────────────────────────────────────────────

def test_overlays_left_by_a_restart_are_swept_at_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "ROUTED_SETTINGS_DIR", tmp_path / "routed-settings")
    left_over = app_module._write_routed_settings("cut-short-run", {"ANTHROPIC_AUTH_TOKEN": "sk-old"})

    app_module._clear_routed_settings()

    assert not left_over.exists()


@pytest.mark.parametrize("old_providers", ["'claude','codex'", "'claude','codex','local'"])
def test_an_older_database_admits_deepseek_and_keeps_its_rows(monkeypatch, tmp_path, old_providers):
    fresh = app_module._state_db()
    binding_schema = fresh.execute(
        "SELECT sql FROM sqlite_master WHERE name='conversation_binding'").fetchone()[0]
    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as old:
        old.execute(binding_schema.replace("'claude','codex','local','deepseek'", old_providers))
        old.execute("INSERT INTO conversation_binding(binding_id,conversation_id,provider,project_key,"
                    "created_at,updated_at) VALUES('b','c','claude','p',1,1)")
        # local_session as it was before DeepSeek: no provider column.
        old.execute("CREATE TABLE local_session (session_id TEXT PRIMARY KEY, model TEXT NOT NULL, "
                    "effort TEXT NOT NULL DEFAULT '')")
        old.execute("INSERT INTO local_session VALUES('old-local', 'qwen3:30b', 'on')")
    fresh.close()
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", old_path)

    migrated = app_module._state_db()

    assert migrated.execute("SELECT provider FROM conversation_binding WHERE binding_id='b'").fetchone() == ("claude",)
    migrated.execute("INSERT INTO conversation_binding(binding_id,conversation_id,provider,project_key,"
                     "created_at,updated_at) VALUES('d','c','deepseek','p',1,1)")
    assert {r[1] for r in migrated.execute("PRAGMA index_list(conversation_binding)")} >= {
        "idx_binding_native", "idx_binding_live"}
    assert app_module._routed_session_row("old-local") == {
        "model": "qwen3:30b", "effort": "on", "provider": "local"}


# ── Replacing a key under a running chat ──────────────────────────────────────

def _live_deepseek_run(cred_id, key, session_id="ds-live-session"):
    run = app_module.ActiveRun("ds-live-run", owner_sub=USER, account_slot=f"cred:{cred_id}")
    run.provider, run.model, run.effort = "deepseek", "deepseek-flash", "high"
    run.session_id = session_id
    run.key_fingerprint = app_module._key_fingerprint(key)
    # A live run has a driver task still going; without one it counts as dead.
    run.task = SimpleNamespace(done=lambda: False)
    app_module.ACTIVE_RUNS[run.run_id] = run
    app_module.ACTIVE_RUNS_BY_SESSION[session_id] = run
    return run


def test_a_replaced_key_bounces_the_next_follow_up_for_a_respawn(client, monkeypatch, deepseek):
    cred_id = _link(client).json()["credentials"][0]["id"]
    run = _live_deepseek_run(cred_id, PERSONAL_KEY)
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *a, **k: {"id": None})
    superseded = []

    async def supersede(run, reason):
        superseded.append(reason)
        run.accepting_input = False

    monkeypatch.setattr(app_module, "_supersede_run_for_switch", supersede)
    client.post(f"/api/account/deepseek/credentials/{cred_id}/apikey", json={"api_key": SECOND_KEY})

    response = client.post("/api/chat/send/ds-live-run", data={"message": "Carry on"})

    # The CLI still holds the old key, so the browser re-sends through /api/chat.
    assert response.status_code == 409
    assert response.json()["error"] == "account_changed"
    assert superseded == ["account_changed"]
    assert not run.accepting_input


def test_the_respawn_resumes_the_same_chat_on_the_new_key(client, sdk_capture, monkeypatch):
    cred_id = _link(client).json()["credentials"][0]["id"]
    old_run = _live_deepseek_run(cred_id, PERSONAL_KEY)
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-live-session', 'deepseek-flash', 'high', 'deepseek')")
    superseded = []

    async def supersede(run, reason):
        superseded.append((run.run_id, reason))
        run.accepting_input = False
        run.done = True

    monkeypatch.setattr(app_module, "_supersede_run_for_switch", supersede)
    client.post(f"/api/account/deepseek/credentials/{cred_id}/apikey", json={"api_key": SECOND_KEY})

    response = client.post("/api/chat", data={"message": "Carry on", "session_id": "ds-live-session"})

    assert response.status_code == 200
    assert superseded == [(old_run.run_id, "local_settings_changed")]
    assert sdk_capture["options"].resume == "ds-live-session"
    assert sdk_capture["options"].env["ANTHROPIC_AUTH_TOKEN"] == SECOND_KEY
    assert sdk_capture["run"].key_fingerprint == app_module._key_fingerprint(SECOND_KEY)
    # Same chat, not a fork: the key changed, not the account.
    assert sdk_capture["options"].fork_session is False
