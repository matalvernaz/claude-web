"""A Claude chat is told the review panel exists, so it can ask for it.

The review tool is deferred, so its description stays unseen until the model
loads it with ToolSearch; with panel review off nothing else mentions it.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

import app as app_module
import panel


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
                yield SystemMessage(subtype="init", data={"session_id": "panel-prompt-session"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="panel-prompt-session",
                )

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *args, **kwargs: True)
    return captured


def _appended(client, spawned) -> str:
    response = client.post("/api/chat", data={"message": "hi", "provider": "claude", "model": "opus"})
    assert response.status_code == 200
    assert '"type":"error"' not in response.text.replace(" ", "")
    return spawned["options"].system_prompt.get("append") or ""


def test_a_claude_chat_is_told_about_the_panel(client, spawned, monkeypatch):
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", True)
    monkeypatch.setattr(app_module, "IS_WINDOWS", False)
    assert panel.SYSTEM_HINT in _appended(client, spawned)


@pytest.mark.parametrize("unavailable", ["no roundtable", "windows"])
def test_no_panel_no_mention(client, spawned, monkeypatch, unavailable):
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", unavailable != "no roundtable")
    monkeypatch.setattr(app_module, "IS_WINDOWS", unavailable == "windows")
    assert panel.SYSTEM_HINT not in _appended(client, spawned)
