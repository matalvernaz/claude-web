"""A queued message must still drain when a ScheduleWakeup turn fires.

AGENTS.md carried a 2026-05-18 bug saying the fired wakeup's sentinel
``<<autonomous-loop-dynamic>>`` is dispatched into the next prompt slot
instead of the user's queued message, leaving a "1 QUEUED" chip that never
drains. The CLI now fires wakeups itself: the harness never sees a sentinel,
and the wakeup arrives as an ordinary turn on the same session (verified with
a raw stream-json probe: command_lifecycle, system/init, assistant, result).
These tests lock the property that matters either way: a message the user
queues around a wakeup still reaches the CLI.
"""
import asyncio
import contextlib

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)
from starlette.datastructures import FormData
from starlette.requests import Request

import app as app_module


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="wake-session",
    )


async def _wait_idle(run) -> None:
    async with asyncio.timeout(3):
        while not run.between_turns:
            await asyncio.sleep(0.01)


async def _wait_queries(state, count: int) -> None:
    async with asyncio.timeout(3):
        while len(state["queries"]) < count:
            await asyncio.sleep(0.01)


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
                pytest.fail("a wakeup must not interrupt anything")

            async def receive_messages(self):
                yield SystemMessage(
                    subtype="init", data={"session_id": "wake-session"}
                )
                while True:
                    yield await state["messages"].get()

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/chat",
         "headers": [], "scheme": "http", "server": ("testserver", 80)},
    )
    request._form = FormData()
    await app_module.api_chat(
        request=request, message="Original task", session_id="", project="",
        model="opus", effort="low", permission_mode="default", fork=False,
        personality_id=None, account_slot="", queue_id="", provider="claude",
        advisor="", panel_review="", user={"sub": "anonymous"},
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


async def test_a_queued_message_survives_a_wakeup_firing_at_the_same_moment(driver):
    """The user's message and the wakeup frames land in the same wait round."""
    run = driver["run"]
    driver["messages"].put_nowait(AssistantMessage(
        content=[TextBlock(text="Done.")], model="opus",
    ))
    driver["messages"].put_nowait(_result())
    await _wait_idle(run)

    inject = asyncio.create_task(
        app_module._inject_user_input(run, "do the next thing", [], 0, 0)
    )
    # What the CLI sends when a ScheduleWakeup fires: a fresh init for the
    # same session, then the turn itself.
    driver["messages"].put_nowait(SystemMessage(
        subtype="init", data={"session_id": "wake-session"},
    ))
    driver["messages"].put_nowait(AssistantMessage(
        content=[TextBlock(text="WAKE-ACK")], model="opus",
    ))
    driver["messages"].put_nowait(_result())

    await asyncio.wait_for(inject, 3)
    await _wait_queries(driver, 2)
    assert driver["queries"][-1] == "do the next thing"
    assert run.user_input_queue.empty()


async def test_a_message_queued_mid_wakeup_waits_for_the_wakeup_turn(driver):
    """Typing during the wakeup turn delivers after it, never instead of it."""
    run = driver["run"]
    driver["messages"].put_nowait(AssistantMessage(
        content=[TextBlock(text="Done.")], model="opus",
    ))
    driver["messages"].put_nowait(_result())
    await _wait_idle(run)

    # The wakeup turn opens and is mid-flight when the user types.
    driver["messages"].put_nowait(SystemMessage(
        subtype="init", data={"session_id": "wake-session"},
    ))
    driver["messages"].put_nowait(AssistantMessage(
        content=[TextBlock(text="WAKE-ACK")], model="opus",
    ))
    async with asyncio.timeout(3):
        while run.between_turns:
            await asyncio.sleep(0.01)

    inject = asyncio.create_task(
        app_module._inject_user_input(run, "do the next thing", [], 0, 0)
    )
    await asyncio.sleep(0.05)
    assert driver["queries"] == ["Original task"]

    driver["messages"].put_nowait(_result())
    await asyncio.wait_for(inject, 3)
    await _wait_queries(driver, 2)
    assert driver["queries"][-1] == "do the next thing"
    assert run.user_input_queue.empty()
