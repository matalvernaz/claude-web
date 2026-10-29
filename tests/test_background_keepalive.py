"""A quiet conversation stays open while its CLI has background work running,
and a panel answer reaches a turn that is still in progress."""
import asyncio
import contextlib

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage, TaskStartedMessage
from starlette.datastructures import FormData
from starlette.requests import Request

import app as app_module

IDLE_MS = 40


@contextlib.asynccontextmanager
async def conversation(monkeypatch, tmp_path, stale_tasks=None):
    """A Claude conversation driven by a fake CLI fed from ``state["messages"]``.

    ``stale_tasks`` is put on the run just as its CLI starts, the way a set
    left over from an earlier CLI would be.
    """
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "SESSION_IDLE_TIMEOUT_MS", IDLE_MS)
    state = {"queries": [], "messages": asyncio.Queue(), "ready": asyncio.Event()}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        state["run"] = run
        if stale_tasks is not None:
            run.background_tasks = set(stale_tasks)

        class Client:
            async def query(self, prompt, **kwargs):
                state["queries"].append(prompt)
                state["ready"].set()

            async def interrupt(self):
                pytest.fail("Nothing here should interrupt the turn")

            async def receive_messages(self):
                yield SystemMessage(subtype="init", data={"session_id": "keepalive-session"})
                while True:
                    yield await state["messages"].get()

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    request = Request({"type": "http", "method": "POST", "path": "/api/chat",
                       "headers": [], "scheme": "http", "server": ("testserver", 80)})
    request._form = FormData()
    await app_module.api_chat(
        request=request, message="Start the long job", session_id="", project="", model="opus",
        effort="low", permission_mode="default", fork=False, personality_id=None,
        account_slot="", queue_id="", provider="claude", advisor="", panel_review="",
        user={"sub": "anonymous"},
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


def tasks_changed(tasks):
    return SystemMessage(subtype="background_tasks_changed", data={"tasks": tasks})


def turn_ends():
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="keepalive-session",
    )


async def closes_within(run, seconds):
    try:
        await asyncio.wait_for(asyncio.shield(run.task), seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def test_a_quiet_conversation_stays_open_while_background_work_runs(monkeypatch, tmp_path):
    async with conversation(monkeypatch, tmp_path) as state:
        run = state["run"]
        state["messages"].put_nowait(tasks_changed([{"task_id": "b1", "task_type": "local_bash"}]))
        state["messages"].put_nowait(turn_ends())
        assert not await closes_within(run, IDLE_MS * 10 / 1000)
        assert run.background_tasks == {"b1"}
        state["messages"].put_nowait(tasks_changed([]))
        assert await closes_within(run, 2)


async def test_housekeeping_tasks_do_not_keep_it_open(monkeypatch, tmp_path):
    async with conversation(monkeypatch, tmp_path) as state:
        run = state["run"]
        state["messages"].put_nowait(tasks_changed(
            [{"task_id": "h1", "task_type": "local_bash", "ambient": True}]))
        state["messages"].put_nowait(turn_ends())
        assert await closes_within(run, 2)
        assert run.background_tasks == set()


async def test_a_long_enough_quiet_closes_it_anyway(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "BACKGROUND_KEEPALIVE_SECONDS", 0.3)
    async with conversation(monkeypatch, tmp_path) as state:
        run = state["run"]
        state["messages"].put_nowait(tasks_changed([{"task_id": "b1", "task_type": "local_bash"}]))
        state["messages"].put_nowait(turn_ends())
        assert not await closes_within(run, 0.15)
        assert await closes_within(run, 2)
        assert run.background_tasks == {"b1"}


async def test_task_started_alone_changes_nothing(monkeypatch, tmp_path):
    async with conversation(monkeypatch, tmp_path) as state:
        run = state["run"]
        state["messages"].put_nowait(TaskStartedMessage(
            subtype="task_started", data={}, task_id="b1", description="sleep",
            uuid="u1", session_id="keepalive-session", tool_use_id=None, task_type="local_bash",
        ))
        state["messages"].put_nowait(turn_ends())
        assert await closes_within(run, 2)


async def test_a_set_left_from_before_the_cli_started_is_dropped(monkeypatch, tmp_path):
    async with conversation(monkeypatch, tmp_path, stale_tasks={"old"}) as state:
        run = state["run"]
        state["messages"].put_nowait(turn_ends())
        assert await closes_within(run, 2)


def test_each_report_replaces_the_last():
    run = app_module.ActiveRun("replace")
    assert run.background_tasks == set()
    app_module._track_background_tasks(run, tasks_changed(
        [{"task_id": "a", "task_type": "local_bash"}, {"task_id": "b", "task_type": "local_agent"}]))
    assert run.background_tasks == {"a", "b"}
    app_module._track_background_tasks(run, tasks_changed([{"task_id": "b", "task_type": "local_agent"}]))
    assert run.background_tasks == {"b"}
    app_module._track_background_tasks(run, SystemMessage(subtype="background_tasks_changed", data={}))
    assert run.background_tasks == {"b"}
    app_module._track_background_tasks(run, SystemMessage(subtype="init", data={"tasks": []}))
    assert run.background_tasks == {"b"}
    app_module._track_background_tasks(run, tasks_changed([]))
    assert run.background_tasks == set()


async def test_a_panel_answer_reaches_a_turn_in_progress(monkeypatch, tmp_path):
    async with conversation(monkeypatch, tmp_path) as state:
        run = state["run"]
        async with asyncio.timeout(3):
            while run.session_id != "keepalive-session":
                await asyncio.sleep(0.01)
        assert not run.between_turns
        await app_module._panel_deliver(run.session_id, run, "The panel approves.")
        async with asyncio.timeout(3):
            while len(state["queries"]) < 2:
                await asyncio.sleep(0.01)
        assert state["queries"][-1] == "The panel approves."
        assert not run.between_turns
