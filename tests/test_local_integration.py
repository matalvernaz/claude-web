"""Local sessions retain their provider, approvals, and model settings."""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3

import httpx
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage, SystemMessage

import app as app_module
import local_provider


# test_csrf.py reloads the app in strict-CSRF mode and (alphabetically) runs
# before this file; a matching Origin passes in both modes. Same pattern as
# test_restart.py.
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
def local_model(monkeypatch):
    monkeypatch.setattr(local_provider, "BASE_URL", "http://local.test:11434")
    monkeypatch.setattr(local_provider, "MODEL_NAMES", ("local-general", "local-coder"))

    async def check_model(name):
        return {
            "key": name, "model": name, "label": name, "context": 32768,
            "efforts": ["off", "on"], "default_effort": "on", "thinking": True,
        }

    monkeypatch.setattr(local_provider, "check_model", check_model)
    return check_model


def _unexpected(*args, **kwargs):
    raise AssertionError("local request reached cloud account handling")


@pytest.fixture
def sdk_capture(monkeypatch, local_model):
    captured = {}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        captured["options"] = options
        captured["run"] = run
        ready = asyncio.Event()

        class Client:
            async def query(self, prompt, **kwargs):
                captured["prompt"] = prompt
                captured["decisions"] = [
                    await options.can_use_tool("Write", {"file_path": "/project/result.txt", "content": "x"}, None),
                    await options.can_use_tool("Bash", {"command": "python -m pytest"}, None),
                ]
                ready.set()

            async def receive_messages(self):
                await ready.wait()
                yield SystemMessage(subtype="init", data={"session_id": options.resume or "local-test-session"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id=options.resume or "local-test-session",
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


def test_local_spawn_preserves_approval_gate_without_cloud_account(client, sdk_capture):
    response = client.post("/api/chat", data={
        "message": "Edit a file and run tests", "provider": "local", "effort": "off",
    })
    assert response.status_code == 200
    assert '"type":"error"' not in response.text.replace(" ", "")
    options = sdk_capture["options"]
    assert options.permission_mode == "default"
    assert options.model == "local-general"
    assert options.thinking == {"type": "disabled"}
    assert options.fallback_model is None
    assert options.env["ANTHROPIC_BASE_URL"] == "http://local.test:11434"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert sdk_capture["asked"] == ["Write", "Bash"]
    assert isinstance(sdk_capture["decisions"][0], PermissionResultAllow)
    assert isinstance(sdk_capture["decisions"][1], PermissionResultDeny)
    assert app_module._local_session_row("local-test-session") == {
        "model": "local-general", "effort": "off",
    }
    binding = app_module._conversation_binding_by_native("local", "local-test-session")
    assert binding is not None


def test_local_resume_infers_provider_and_model(client, sdk_capture):
    app_module._state_db().execute(
        "INSERT INTO local_session VALUES('existing-local', 'local-coder', 'off')")
    response = client.post("/api/chat", data={"message": "Continue", "session_id": "existing-local"})
    assert response.status_code == 200
    assert sdk_capture["options"].resume == "existing-local"
    assert sdk_capture["options"].model == "local-coder"
    assert sdk_capture["run"].provider == "local"
    assert sdk_capture["options"].thinking == {"type": "disabled"}


def test_local_page_does_not_require_claude_signin(client, monkeypatch, local_model):
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda: False)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_local_session_cannot_switch_to_cloud(client, local_model, provider):
    app_module._state_db().execute("INSERT INTO local_session VALUES('local-id','local-general','on')")
    response = client.post("/api/chat", data={
        "message": "Continue", "session_id": "local-id", "provider": provider,
    })
    assert response.status_code == 400


def test_local_cannot_resume_cloud_session(client, local_model):
    response = client.post("/api/chat", data={
        "message": "Continue", "session_id": "cloud-id", "provider": "local",
    })
    assert response.status_code == 400


def test_local_outage_never_falls_back(client, monkeypatch, local_model):
    async def offline(name):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(local_provider, "check_model", offline)
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)
    response = client.post("/api/chat", data={"message": "Hello", "provider": "local"})
    assert response.status_code == 503
    assert "offline" in response.text


@pytest.mark.parametrize("field,value,error", [
    ("model", "local-coder", "model_changed"),
    ("effort", "off", "effort_changed"),
])
def test_followup_restarts_for_changed_local_settings(client, monkeypatch, local_model, field, value, error):
    run = app_module.ActiveRun("local-run", account_slot="local")
    run.provider, run.model, run.effort = "local", "local-general", "on"
    app_module.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *a, **k: {"id": None})
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)
    async def supersede(run, reason):
        run.accepting_input = False
    monkeypatch.setattr(app_module, "_supersede_run_for_switch", supersede)
    response = client.post("/api/chat/send/local-run", data={"message": "Continue", field: value})
    assert response.status_code == 409
    assert response.json()["error"] == error
    assert not run.accepting_input


def test_unchanged_local_followup_skips_cloud_failover(client, monkeypatch, local_model):
    run = app_module.ActiveRun("local-run", account_slot="local")
    run.provider, run.model, run.effort = "local", "local-general", "on"
    app_module.ACTIVE_RUNS[run.run_id] = run
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *a, **k: {"id": None})
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)
    monkeypatch.setattr(app_module, "_follow_up_handoff_reason", _unexpected)
    async def inject(*args, **kwargs):
        return None
    monkeypatch.setattr(app_module, "_inject_user_input", inject)
    response = client.post("/api/chat/send/local-run", data={"message": "Continue"})
    assert response.status_code == 202


def test_local_errors_do_not_offer_cloud_failover(monkeypatch):
    run = app_module.ActiveRun("local-result", account_slot="local")
    run.provider, run.model = "local", "local-general"
    monkeypatch.setattr(app_module, "_log_usage", _unexpected)
    monkeypatch.setattr(app_module, "_failover_offer", _unexpected)
    monkeypatch.setattr(app_module, "_note_model_denial", _unexpected)
    events = app_module._sdk_message_to_events(ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="local-id",
        result="There is an issue with the selected model", total_cost_usd=2.5,
    ), run)
    assert events[0]["total_cost_usd"] == 0
    assert events[0]["cost_is_billed"] is False
    assert events[-1]["type"] == "error"


def test_local_session_reopens_without_cloud_credentials(client, monkeypatch, tmp_path):
    app_module._state_db().execute("INSERT INTO local_session VALUES('local-id','local-general','off')")
    path = tmp_path / "project" / "local-id.jsonl"
    path.parent.mkdir()
    path.write_text("")
    monkeypatch.setattr(app_module, "_find_session_path", lambda *a: path)
    monkeypatch.setattr(app_module, "session_transcript", lambda *a: [])
    monkeypatch.setattr(app_module, "_resolve_account_for_run", _unexpected)
    response = client.get("/api/sessions/local-id")
    assert response.status_code == 200
    assert response.json()["provider"] == "local"
    assert response.json()["model"] == "local-general"
    assert response.json()["effort"] == "off"


def test_local_schema_migration_preserves_bindings(monkeypatch, tmp_path):
    fresh = app_module._state_db()
    schema = fresh.execute("SELECT sql FROM sqlite_master WHERE name='conversation_binding'").fetchone()[0]
    old_path = tmp_path / "old.db"
    with sqlite3.connect(old_path) as old:
        old.execute(schema.replace(",'local'", ""))
        old.execute("INSERT INTO conversation_binding(binding_id,conversation_id,provider,project_key,created_at,updated_at) VALUES('b','c','claude','p',1,1)")
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", old_path)
    migrated = app_module._state_db()
    assert migrated.execute("SELECT provider FROM conversation_binding WHERE binding_id='b'").fetchone() == ("claude",)
    migrated.execute("INSERT INTO conversation_binding(binding_id,conversation_id,provider,project_key,created_at,updated_at) VALUES('l','c','local','p',1,1)")
    assert {r[1] for r in migrated.execute("PRAGMA index_list(conversation_binding)")} >= {"idx_binding_native", "idx_binding_live"}
    fresh.close()
