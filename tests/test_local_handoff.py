"""A Local settings change must release its transcript before resuming it."""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from claude_agent_sdk import SystemMessage
from starlette.datastructures import FormData
from starlette.requests import Request

import app as app_module
import local_provider


SESSION_ID = "local-handoff-session"
USER = {"sub": "local-handoff-user"}


@pytest.fixture
async def delayed_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    monkeypatch.setattr(local_provider, "BASE_URL", "http://local.test:11434")
    monkeypatch.setattr(local_provider, "MODEL_NAMES", ("local-general", "local-coder"))

    async def check_model(name):
        return {
            "key": name, "model": name, "label": name, "context": 32768,
            "efforts": ["off", "on"], "default_effort": "on", "thinking": True,
        }

    monkeypatch.setattr(local_provider, "check_model", check_model)
    state = {
        "captures": [], "events": [], "ready": [asyncio.Event(), asyncio.Event()],
        "closing": asyncio.Event(), "release": asyncio.Event(),
    }

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        index = len(state["captures"])
        state["captures"].append((options, run))
        state["events"].append((index, "enter"))

        class Client:
            async def query(self, prompt, **kwargs):
                state["events"].append((index, "query"))
                state["ready"][index].set()

            async def receive_messages(self):
                yield SystemMessage(subtype="init", data={"session_id": options.resume})
                await asyncio.Event().wait()

        try:
            yield Client()
        finally:
            if index == 0:
                state["closing"].set()
                await state["release"].wait()
            state["events"].append((index, "exit"))

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    app_module._state_db().execute(
        "INSERT INTO local_session VALUES(?, 'local-general', 'on')", (SESSION_ID,),
    )
    try:
        yield state
    finally:
        state["release"].set()
        tasks = [run.task for run in app_module.ACTIVE_RUNS.values() if run.task]
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if app_module._STATE_DB is not None:
            app_module._STATE_DB.close()


def _request(path, **form):
    request = Request({
        "type": "http", "method": "POST", "path": path, "headers": [],
        "scheme": "http", "server": ("testserver", 80),
    })
    request._form = FormData(form)
    return request


async def _chat(**settings):
    return await app_module.api_chat(
        request=_request("/api/chat"), message="Continue", session_id=SESSION_ID,
        project="", model=settings.get("model", "local-general"),
        effort=settings.get("effort", "on"), permission_mode="default",
        fork=False, personality_id=None, account_slot="", queue_id="",
        provider="local", user=USER,
    )


async def _followup(run, **settings):
    return await app_module.api_chat_send(
        request=_request(f"/api/chat/send/{run.run_id}", **settings),
        run_id=run.run_id, message="Continue", provider="local", personality_id=None,
        account_slot="", queue_id="", user=USER,
    )


async def _start(state):
    response = await _chat()
    assert response.status_code == 200
    await asyncio.wait_for(state["ready"][0].wait(), timeout=2)
    return state["captures"][0][1]


async def _assert_replacement(state, old_run, request):
    state["release"].set()
    response = await asyncio.wait_for(request, timeout=2)
    assert response.status_code == 200
    await asyncio.wait_for(state["ready"][1].wait(), timeout=2)
    assert old_run.task.done()
    assert state["events"].index((0, "exit")) < state["events"].index((1, "enter"))
    assert all(
        options.resume == SESSION_ID and not options.fork_session
        for options, _run in state["captures"]
    )


async def test_runs_accept_cli_messages_larger_than_the_sdk_default(delayed_sdk):
    """One tool result carrying a screenshot is over the SDK's 1 MiB default
    per message ("JSON message exceeded maximum buffer size of 1048576
    bytes"), which killed the run; runs start with the larger limit."""
    await _start(delayed_sdk)
    options = delayed_sdk["captures"][0][0]
    assert options.max_buffer_size == app_module.SDK_MAX_BUFFER_BYTES
    assert options.max_buffer_size >= 64 << 20


def test_sdk_buffer_limit_setting(monkeypatch):
    monkeypatch.setenv("CLAUDE_WEB_SDK_MAX_BUFFER_MB", "8")
    assert app_module._sdk_max_buffer_bytes() == 8 << 20
    monkeypatch.setenv("CLAUDE_WEB_SDK_MAX_BUFFER_MB", "0.1")
    assert app_module._sdk_max_buffer_bytes() == 1 << 20, "never below the SDK default"
    monkeypatch.setenv("CLAUDE_WEB_SDK_MAX_BUFFER_MB", "lots")
    assert app_module._sdk_max_buffer_bytes() == 64 << 20
    monkeypatch.delenv("CLAUDE_WEB_SDK_MAX_BUFFER_MB")
    assert app_module._sdk_max_buffer_bytes() == 64 << 20


@pytest.mark.parametrize("settings", [{"effort": "off"}, {"model": "local-coder"}])
async def test_chat_waits_for_local_sdk_close_before_resuming(delayed_sdk, settings):
    state = delayed_sdk
    old_run = await _start(state)
    replacement = asyncio.create_task(_chat(**settings))
    try:
        await asyncio.wait_for(state["closing"].wait(), timeout=2)
        await asyncio.sleep(0)
        assert not replacement.done()
        assert len(state["captures"]) == 1
        assert not old_run.accepting_input
        await _assert_replacement(state, old_run, replacement)
    finally:
        state["release"].set()
        await asyncio.gather(replacement, return_exceptions=True)


@pytest.mark.parametrize("settings,error", [
    ({"effort": "off"}, "effort_changed"),
    ({"model": "local-coder"}, "model_changed"),
])
async def test_followup_and_concurrent_retry_share_local_teardown(delayed_sdk, settings, error):
    state = delayed_sdk
    old_run = await _start(state)
    followup = asyncio.create_task(_followup(old_run, **settings))
    replacement = None
    try:
        await asyncio.wait_for(state["closing"].wait(), timeout=2)
        assert not followup.done()
        rejected = await _followup(old_run, **settings)
        assert rejected.status_code == 409
        assert json.loads(rejected.body)["error"] == error

        replacement = asyncio.create_task(_chat(**settings))
        await asyncio.sleep(0)
        assert not replacement.done()
        assert old_run.task.cancelling() == 1
        assert len(state["captures"]) == 1

        await _assert_replacement(state, old_run, replacement)
        response = await asyncio.wait_for(followup, timeout=2)
        assert response.status_code == 409
        assert json.loads(response.body)["error"] == error
    finally:
        state["release"].set()
        await asyncio.gather(
            followup, *([replacement] if replacement else []), return_exceptions=True,
        )


@pytest.mark.parametrize("endpoint", ["chat", "followup"])
async def test_cancelled_settings_request_preserves_local_teardown(delayed_sdk, endpoint):
    state = delayed_sdk
    old_run = await _start(state)
    request = asyncio.create_task(
        _chat(effort="off") if endpoint == "chat" else _followup(old_run, effort="off"),
    )
    replacement = None
    try:
        await asyncio.wait_for(state["closing"].wait(), timeout=2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not old_run.task.done()
        assert old_run.task.cancelling() == 1
        assert app_module._existing_run_for_session(SESSION_ID) is old_run

        replacement = asyncio.create_task(_chat(effort="off"))
        await asyncio.sleep(0)
        assert not replacement.done()
        assert old_run.task.cancelling() == 1
        assert len(state["captures"]) == 1
        await _assert_replacement(state, old_run, replacement)
    finally:
        state["release"].set()
        await asyncio.gather(
            request, *([replacement] if replacement else []), return_exceptions=True,
        )
