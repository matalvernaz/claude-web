"""DeepSeek chats run on Claude Code routed to DeepSeek, keep their provider, and never touch Anthropic accounts."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import httpx
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage, SystemMessage

import app as app_module
import deepseek_provider
from tests.test_deepseek_provider import MODELS_PAYLOAD


# test_csrf.py reloads the app in strict-CSRF mode; a matching Origin passes in both modes.
@pytest.fixture(autouse=True)
def _matching_origin(client):
    client.headers["Origin"] = "http://testserver"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    yield
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


@pytest.fixture
def deepseek(monkeypatch):
    """DeepSeek configured, with the live model list already fetched."""
    monkeypatch.setattr(deepseek_provider, "API_KEY", "sk-test")
    monkeypatch.setattr(deepseek_provider, "BASE_URL", "https://api.deepseek.com")
    entries = [
        deepseek_provider._model_entry(raw, is_default=index == 0)
        for index, raw in enumerate(MODELS_PAYLOAD["data"])
    ]
    monkeypatch.setattr(deepseek_provider, "_models_cache", {
        "origin": "https://api.deepseek.com", "key": "sk-test",
        "fetched_at": time.monotonic(), "models": entries,
    })
    return entries


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


def test_a_deepseek_chat_runs_claude_code_against_deepseek(client, sdk_capture, monkeypatch):
    # The host key sits in the service environment, as it does from .env.
    monkeypatch.setenv("CLAUDE_WEB_DEEPSEEK_API_KEY", "sk-host-secret")

    response = client.post("/api/chat", data={"message": "Edit a file and clean up", "provider": "deepseek"})

    assert response.status_code == 200
    assert '"type":"error"' not in response.text.replace(" ", "")
    options = sdk_capture["options"]
    assert options.model == "deepseek-flash"
    assert options.effort == "high"
    assert options.thinking == {"type": "adaptive"}
    assert options.fallback_model is None
    env = options.env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"
    # The model's own shell commands never see the host's copy of the key.
    assert env["CLAUDE_WEB_DEEPSEEK_API_KEY"] == ""
    # The routing overlay still pins the key over any settings file, but from a
    # private file: as JSON on the command line it showed in ps and /proc.
    assert "sk-test" not in options.settings
    assert sdk_capture["overlay"]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert sdk_capture["overlay"]["env"]["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert sdk_capture["overlay_mode"] == 0o600
    assert not Path(options.settings).exists(), "the overlay outlived its run"
    assert sdk_capture["asked"] == ["Write", "Bash"]
    assert isinstance(sdk_capture["decisions"][0], PermissionResultAllow)
    assert isinstance(sdk_capture["decisions"][1], PermissionResultDeny)
    assert app_module._routed_session_row("deepseek-session") == {
        "model": "deepseek-flash", "effort": "high", "provider": "deepseek",
    }
    assert app_module._conversation_binding_by_native("deepseek", "deepseek-session") is not None


def test_the_picked_model_and_effort_are_used(client, sdk_capture):
    response = client.post("/api/chat", data={
        "message": "Think hard", "provider": "deepseek", "model": "deepseek-v4-pro", "effort": "max",
    })

    assert response.status_code == 200
    options = sdk_capture["options"]
    assert options.model == "deepseek-v4-pro"
    assert options.effort == "max"
    assert "vision" not in options.env["ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES"]


def test_a_reopened_deepseek_chat_resumes_on_deepseek(client, sdk_capture):
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-existing', 'deepseek-v4-pro', 'low', 'deepseek')")

    response = client.post("/api/chat", data={"message": "Continue", "session_id": "ds-existing"})

    assert response.status_code == 200
    assert sdk_capture["run"].provider == "deepseek"
    assert sdk_capture["options"].resume == "ds-existing"
    assert sdk_capture["options"].model == "deepseek-v4-pro"
    assert sdk_capture["options"].effort == "low"


@pytest.mark.parametrize("provider", ["claude", "codex", "local"])
def test_a_deepseek_chat_cannot_switch_provider(client, deepseek, provider):
    app_module._state_db().execute(
        "INSERT INTO local_session(session_id, model, effort, provider) "
        "VALUES('ds-id', 'deepseek-flash', 'high', 'deepseek')")

    response = client.post("/api/chat", data={"message": "Continue", "session_id": "ds-id", "provider": provider})

    assert response.status_code == 400
    assert "deepseek provider changes require a new chat" in response.text


def test_deepseek_cannot_resume_someone_elses_transcript(client, deepseek):
    response = client.post("/api/chat", data={"message": "Continue", "session_id": "claude-id", "provider": "deepseek"})

    assert response.status_code == 400
    assert "does not belong to the deepseek provider" in response.text


def test_a_deepseek_outage_never_falls_back_to_claude(client, monkeypatch, deepseek):
    async def offline(name):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(deepseek_provider, "model_for_run", offline)
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)

    response = client.post("/api/chat", data={"message": "Hello", "provider": "deepseek"})

    assert response.status_code == 503
    assert "DeepSeek model unavailable" in response.text


def test_a_deepseek_result_is_never_billed_or_offered_failover(monkeypatch):
    run = app_module.ActiveRun("ds-result", account_slot="deepseek")
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


def test_the_provider_list_offers_deepseeks_live_models(client, deepseek):
    providers = {p["key"]: p for p in client.get("/api/providers").json()["providers"]}

    entry = providers["deepseek"]
    assert entry["available"] is True
    assert entry["label"] == "DeepSeek (China)"
    assert [m["key"] for m in entry["models"]] == ["deepseek-flash", "deepseek-v4-pro"]
    assert entry["capabilities"]["accounts"] is False
    assert entry["capabilities"]["usage"] is False
    assert entry["capabilities"]["advisor"] is False
    assert entry["capabilities"]["rewind"] is True


def test_without_a_key_deepseek_is_listed_as_unavailable_with_the_reason(client, monkeypatch):
    monkeypatch.setattr(deepseek_provider, "API_KEY", "")

    providers = {p["key"]: p for p in client.get("/api/providers").json()["providers"]}

    assert providers["deepseek"]["available"] is False
    assert "CLAUDE_WEB_DEEPSEEK_API_KEY" in providers["deepseek"]["reason"]


def test_the_page_opens_with_only_deepseek_set_up(client, monkeypatch, deepseek):
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda: False)

    assert client.get("/", follow_redirects=False).status_code == 200


def test_the_usage_dialog_gets_the_balance(client, monkeypatch, deepseek):
    async def balance():
        return {"sufficient": False, "balances": [
            {"currency": "USD", "total": "0.00", "granted": "0.00", "topped_up": "0.00"}]}

    monkeypatch.setattr(deepseek_provider, "balance", balance)

    assert client.get("/api/deepseek/usage").json() == {"available": True, "sufficient": False, "balances": [
        {"currency": "USD", "total": "0.00", "granted": "0.00", "topped_up": "0.00"}]}


def test_the_usage_dialog_says_why_when_the_balance_cant_be_read(client, monkeypatch):
    monkeypatch.setattr(deepseek_provider, "API_KEY", "")

    payload = client.get("/api/deepseek/usage").json()

    assert payload["available"] is False
    assert "CLAUDE_WEB_DEEPSEEK_API_KEY" in payload["reason"]


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


def test_overlays_left_by_a_restart_are_swept_at_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "ROUTED_SETTINGS_DIR", tmp_path / "routed-settings")
    left_over = app_module._write_routed_settings("cut-short-run", {"ANTHROPIC_AUTH_TOKEN": "sk-old"})

    app_module._clear_routed_settings()

    assert not left_over.exists()
