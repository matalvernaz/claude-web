"""Chats get Claude Code's task-list tools whatever the model.

Claude Code only offers TaskCreate/TaskUpdate/... to Opus 4.7 and older
unless CLAUDE_CODE_ENABLE_TODO_TOOLS is set, so without it the task panel
never fills on any current model.
"""
from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

import app as app_module

FLAG = "CLAUDE_CODE_ENABLE_TODO_TOOLS"


# test_csrf.py reloads the app in strict-CSRF mode and (alphabetically) runs
# before this file; a matching Origin passes in both modes.
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
def spawned(monkeypatch):
    captured = {}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        captured["options"] = options
        ready = asyncio.Event()

        class Client:
            async def query(self, prompt, **kwargs):
                ready.set()

            async def receive_messages(self):
                await ready.wait()
                yield SystemMessage(subtype="init", data={"session_id": "task-list-session"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="task-list-session",
                )

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *args, **kwargs: True)
    return captured


def _cli_env(client, spawned) -> dict[str, str]:
    """The environment the CLI would start with, merged the way the SDK does."""
    response = client.post("/api/chat", data={"message": "hi", "provider": "claude", "model": "opus"})
    assert response.status_code == 200
    assert '"type":"error"' not in response.text.replace(" ", "")
    return {**os.environ, **spawned["options"].env}


def test_claude_chat_turns_the_task_list_tools_on(client, spawned, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    assert _cli_env(client, spawned).get(FLAG) == "1"


@pytest.mark.parametrize("value", ["0", "false", "1"])
def test_an_explicit_setting_is_left_alone(client, spawned, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    assert _cli_env(client, spawned)[FLAG] == value
