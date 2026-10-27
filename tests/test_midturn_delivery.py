"""Explicit steering reaches the active Claude driver without interrupting it."""
import asyncio
import contextlib

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from starlette.datastructures import FormData
from starlette.requests import Request

import app as app_module


@pytest.fixture
async def driver(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *a, **k: True)
    state = {"queries": [], "messages": asyncio.Queue(), "ready": asyncio.Event()}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        state["run"] = run

        class Client:
            async def query(self, prompt, **kwargs):
                state["queries"].append(prompt)
                state["ready"].set()

            async def interrupt(self):
                pytest.fail("Steering must not interrupt the active task")

            async def receive_messages(self):
                yield SystemMessage(subtype="init", data={"session_id": "steering-session"})
                while True:
                    yield await state["messages"].get()

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    request = Request({"type": "http", "method": "POST", "path": "/api/chat",
                       "headers": [], "scheme": "http", "server": ("testserver", 80)})
    request._form = FormData()
    await app_module.api_chat(
        request=request, message="Original task", session_id="", project="", model="opus",
        effort="low", permission_mode="default", fork=False, personality_id=None,
        account_slot="", queue_id="", provider="claude", advisor="", panel_review="", user={"sub": "anonymous"},
    )
    await asyncio.wait_for(state["ready"].wait(), 3)
    try:
        yield state
    finally:
        state["run"].task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state["run"].task
        if app_module._STATE_DB is not None:
            app_module._STATE_DB.close()


async def wait_queries(state, count):
    async with asyncio.timeout(3):
        while len(state["queries"]) < count:
            await asyncio.sleep(0.01)


async def test_steering_bypasses_queued_input_and_keeps_the_active_turn(driver):
    run = driver["run"]
    assert not run.between_turns
    await app_module._inject_user_input(run, "Next task", [], 0, 0)
    await app_module._inject_user_input(run, "Correction now", [], 0, 0, steer=True)
    await wait_queries(driver, 2)
    assert driver["queries"][-1] == "Correction now"
    assert not run.between_turns
    assert run.user_input_queue.qsize() == 1
    driver["messages"].put_nowait(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="steering-session",
    ))
    await wait_queries(driver, 3)
    assert driver["queries"][-1] == "Next task"


async def test_steering_is_not_lost_when_a_provider_message_arrives_together(driver):
    run = driver["run"]
    driver["messages"].put_nowait(AssistantMessage(content=[TextBlock(text="Still working")], model="opus"))
    await app_module._inject_user_input(run, "Also check errors", [], 0, 0, steer=True)
    await wait_queries(driver, 2)
    assert driver["queries"][-1] == "Also check errors"


async def test_steering_counts_toward_the_backlog_cap_and_finish_reports_undelivered(monkeypatch):
    run = app_module.ActiveRun("steer-cap")
    for _ in range(app_module.MAX_USER_INPUT_QUEUE):
        run.steering_input_queue.put_nowait({"text": "pending steering"})
    assert await app_module._inject_user_input(run, "more", [], 0, 0, steer=True) == "queue_full"
    events = []
    monkeypatch.setattr(run, "emit", events.append)
    run.finish()
    assert run.steering_input_queue.empty()
    assert any(e.get("lost_input") for e in events)


async def test_codex_steering_uses_its_native_driver_queue():
    run = app_module.ActiveRun("codex-steer-queue")
    run.provider = "codex"
    await app_module._inject_user_input(run, "Correction", [], 0, 0, steer=True)
    assert run.steering_input_queue.empty()
    item = run.user_input_queue.get_nowait()
    assert item["text"] == "Correction"
    item["delivered"].set_result(None)
    await asyncio.sleep(0)
