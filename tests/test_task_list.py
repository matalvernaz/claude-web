"""The Tasks panel shows the chat's real task list.

claude-web used to guess the list from whichever task tool calls the current
run happened to see. A chat that had been idle for ten minutes is closed and
started again by the next message, and the new run began from nothing: a chat
with 21 tasks showed 9, a task in progress showed as "To do", and a task whose
only later change was its status showed with no name. A page that loaded while
a chat was running, or loaded a chat that was not running, showed no list at
all.

The messages and saved-conversation entries here are shaped like the ones the
CLI really sends (2.1.322): one tool result per message, each with the tool's
own result object beside the text the model was shown.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

import app as app_module

SESSION = "0f1e2d3c-task-list-chat"
# The session id a chat gets when the CLI starts it as a branch of SESSION.
BRANCH = "9a8b7c6d-branch-of-the-chat"
SECOND_BRANCH = "5e4f3a2b-branch-of-the-branch"
LIST_SETTING = "CLAUDE_CODE_TASK_LIST_ID"

FIRST = "Read the player code"
SECOND = "Fix the pause between items"
THIRD = "Open the pull request"


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


# ─── Messages as the CLI sends them ─────────────────────────────────────────

def _call(tool_name: str, call_id: str, tool_input: dict) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=call_id, name=tool_name, input=tool_input)],
        model="test",
    )


def _result(call_id: str, text: str, structured: dict | None = None, is_error: bool = False) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=call_id, content=text, is_error=is_error)],
        tool_use_result=structured,
    )


def _created(call_id: str, number: str, subject: str, description: str = "") -> UserMessage:
    return _result(
        call_id,
        f"Task #{number} created successfully: {subject}",
        {"task": {"id": number, "subject": subject, "description": description}},
    )


def _updated(call_id: str, number: str, fields: list[str], status_change: dict | None = None) -> UserMessage:
    structured = {"success": True, "taskId": number, "updatedFields": fields}
    if status_change is not None:
        structured["statusChange"] = status_change
    return _result(call_id, f"Updated task #{number} {', '.join(fields)}", structured)


def _not_found(call_id: str, number: str) -> UserMessage:
    return _result(
        call_id,
        "Task not found",
        {"success": False, "taskId": number, "updatedFields": [], "error": "Task not found"},
    )


def _feed(run, *messages) -> list[list[dict]]:
    """Run messages through the app's translator; return every list it sent."""
    lists = []
    for message in messages:
        for event in app_module._sdk_message_to_events(message, run=run):
            if event.get("type") == "todos_update":
                lists.append(event["todos"])
    return lists


def _three_tasks_first_started() -> list:
    """Three tasks made, the first one started: the usual opening of a list."""
    return [
        _call("TaskCreate", "tu_1", {"subject": FIRST, "description": "Trace the seek path.", "activeForm": "Reading the player"}),
        _created("tu_1", "1", FIRST, "Trace the seek path."),
        _call("TaskCreate", "tu_2", {"subject": SECOND, "description": "One rule for every door.", "activeForm": "Fixing the pause"}),
        _created("tu_2", "2", SECOND, "One rule for every door."),
        _call("TaskCreate", "tu_3", {"subject": THIRD, "description": "CI green first.", "activeForm": "Opening the PR"}),
        _created("tu_3", "3", THIRD, "CI green first."),
        _call("TaskUpdate", "tu_4", {"taskId": "1", "status": "in_progress"}),
        _updated("tu_4", "1", ["status"], {"from": "pending", "to": "in_progress"}),
    ]


def _names_and_statuses(todos: list[dict]) -> list[tuple[str, str]]:
    return [(todo["content"], todo["status"]) for todo in todos]


# ─── The saved conversation, as the CLI writes it ───────────────────────────

def _transcript_lines(messages: list, session_id: str = SESSION, clutter: bool = True) -> list[dict]:
    """Saved-conversation entries for these messages. With ``clutter``, also
    what a real file carries around them: a repeat of each call under another
    entry kind with no result, and a helper agent's own task call."""
    lines = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            blocks = [
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                for block in message.content
            ]
            lines.append({
                "type": "assistant", "isSidechain": False, "sessionId": session_id,
                "message": {"role": "assistant", "content": blocks},
            })
            if clutter:
                # The CLI also logs what it sent the API: the same call again,
                # and no result ever follows it.
                lines.append({
                    "type": "api-request-blob", "sessionId": session_id,
                    "message": {"role": "assistant", "content": blocks},
                })
        else:
            blocks = [
                {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
                for block in message.content
            ]
            lines.append({
                "type": "user", "isSidechain": False, "sessionId": session_id,
                "toolUseResult": message.tool_use_result,
                "message": {"role": "user", "content": blocks},
            })
    if not clutter:
        return lines
    lines.append({
        "type": "assistant", "isSidechain": True, "sessionId": session_id,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_helper", "name": "TaskCreate", "input": {"subject": "A helper's own task"}},
        ]},
    })
    lines.append({
        "type": "user", "isSidechain": True, "sessionId": session_id,
        "toolUseResult": {"task": {"id": "1", "subject": "A helper's own task"}},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_helper", "content": "Task #1 created successfully: A helper's own task"},
        ]},
    })
    return lines


def _save_conversation(messages: list, session_id: str = SESSION):
    folder = app_module._sessions_dir(app_module.DEFAULT_CWD)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    opening = {
        "type": "user", "isSidechain": False, "sessionId": session_id,
        "message": {"role": "user", "content": "Start on build A, and use TaskCreate to track it."},
    }
    with path.open("w", encoding="utf-8") as handle:
        for line in [opening, *_transcript_lines(messages, session_id)]:
            handle.write(json.dumps(line) + "\n")
    return path


@pytest.fixture
def saved_chat():
    path = _save_conversation(_three_tasks_first_started())
    yield path
    path.unlink(missing_ok=True)


# ─── A chat that is started again ───────────────────────────────────────────

@pytest.fixture
def cli(monkeypatch):
    """Stands in for the CLI. It records how each one was started, reports the
    session id it is given here, and plays the messages it is given here."""
    stand_in = SimpleNamespace(started=[], started_at=[], session=SESSION, messages=[])

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        stand_in.started.append(options)
        stand_in.started_at.append(time.time())
        session = stand_in.session
        messages = list(stand_in.messages)
        ready = asyncio.Event()

        class Client:
            async def query(self, prompt, **kwargs):
                ready.set()

            async def receive_messages(self):
                await ready.wait()
                yield SystemMessage(subtype="init", data={"session_id": session})
                for message in messages:
                    yield message
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id=session,
                )

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *args, **kwargs: True)
    return stand_in


def _finish_one_start_two() -> list:
    """Task 1 done and task 2 started, the way Claude usually says it: status
    only, no names."""
    return [
        _call("TaskUpdate", "tu_5", {"taskId": "1", "status": "completed"}),
        _updated("tu_5", "1", ["status"], {"from": "in_progress", "to": "completed"}),
        _call("TaskUpdate", "tu_6", {"taskId": "2", "status": "in_progress"}),
        _updated("tu_6", "2", ["status"], {"from": "pending", "to": "in_progress"}),
    ]


def _post(client, session_id: str, **extra):
    return client.post("/api/chat", data={
        "message": "carry on", "provider": "claude", "model": "opus",
        "session_id": session_id, **extra,
    })


def _lists_sent(response) -> list[list[dict]]:
    lists = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        event = json.loads(line[len("data:"):])
        if event.get("type") == "todos_update":
            lists.append(event["todos"])
    return lists


def test_a_chat_started_again_keeps_its_whole_list(client, cli, saved_chat):
    cli.messages = _finish_one_start_two()
    response = _post(client, SESSION)
    assert response.status_code == 200
    lists = _lists_sent(response)
    assert lists, "the restarted chat sent no task list at all"
    assert _names_and_statuses(lists[-1]) == [
        (FIRST, "completed"),
        (SECOND, "in_progress"),
        (THIRD, "pending"),
    ]
    # It is the same session, so the CLI finds its list without being told.
    started = cli.started[-1]
    assert started.resume == SESSION and not started.fork_session
    assert LIST_SETTING not in started.env


def test_a_page_that_loads_a_chat_which_is_not_running_gets_its_list(client, saved_chat):
    response = client.get(f"/api/sessions/{SESSION}")
    assert response.status_code == 200
    tasks = response.json().get("tasks")
    assert tasks is not None, "the page-load answer carries no task list"
    assert _names_and_statuses(tasks) == [
        (FIRST, "in_progress"),
        (SECOND, "pending"),
        (THIRD, "pending"),
    ]


class _RunningDriver:
    """Stands in for a run's driver task: present and not finished."""

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        """What being replaced does to a driver; a real one unwinds later."""


def _running_chat(session_id: str = SESSION):
    run = app_module.ActiveRun("run-" + session_id)
    run.session_id = session_id
    run.task = _RunningDriver()
    app_module.ACTIVE_RUNS[run.run_id] = run
    app_module.ACTIVE_RUNS_BY_SESSION[session_id] = run
    return run


def test_a_page_that_loads_while_the_chat_runs_gets_the_list_it_missed(client, saved_chat):
    run = _running_chat()
    # The run has moved on since the saved conversation was last written.
    for event in app_module._sdk_message_to_events(_call("TaskCreate", "tu_9", {"subject": "A newer task"}), run=run):
        run.emit(event)
    for event in app_module._sdk_message_to_events(_created("tu_9", "4", "A newer task"), run=run):
        run.emit(event)
    data = client.get(f"/api/sessions/{SESSION}").json()
    # The page attaches after every event already sent, so that list would
    # never reach it on its own.
    assert data["live_run"]["next_idx"] == run._next_idx
    assert [todo["content"] for todo in data.get("tasks") or []] == ["A newer task"]
    rejoin = client.get("/api/chat/active", params={"run_id": run.run_id}).json()
    assert [todo["content"] for todo in rejoin.get("tasks") or []] == ["A newer task"]


def test_a_running_codex_chat_hands_over_the_plan_it_last_sent():
    run = app_module.ActiveRun("codex-plan")
    assert run.todos_snapshot is None
    plan = app_module.codex_provider.plan_todos([{"step": "Read the code", "status": "inProgress"}])
    run.emit({"type": "todos_update", "todos": plan})
    assert run.todos_snapshot == plan


# ─── A chat that changes session id ─────────────────────────────────────────
#
# Claude Code names a chat's task list after its session id, and a branch
# (--fork-session) gets a new id and so a new, empty list. Checked against the
# real CLI (2.1.322) driven by a scripted model: a branch started with
# CLAUDE_CODE_TASK_LIST_ID naming the old list reads it, changes it and adds
# to it; started without, TaskList answers "No tasks found", and so does a
# later plain resume of the branch.

def _on_another_account(run) -> None:
    """Make a running chat one whose account is not the one the next message
    resolves to, which is what makes claude-web move the chat."""
    run.account_slot = "cred:999"


def test_a_chat_moved_to_another_account_stays_on_its_list(client, cli, saved_chat):
    """The move is the same conversation carrying on, but the CLI can only do
    it as a branch. Before this, Claude lost its whole list at every move."""
    old = _running_chat()
    _on_another_account(old)
    cli.session = BRANCH
    cli.messages = _finish_one_start_two()
    response = _post(client, SESSION)
    assert response.status_code == 200
    assert old.superseded_reason == "account_changed"
    started = cli.started[-1]
    assert started.resume == SESSION and started.fork_session is True
    assert started.env.get(LIST_SETTING) == SESSION
    assert _names_and_statuses(_lists_sent(response)[-1]) == [
        (FIRST, "completed"),
        (SECOND, "in_progress"),
        (THIRD, "pending"),
    ]
    # Written down, because every later start of the new id has to say it again.
    assert app_module._session_task_list_binding(BRANCH) == (SESSION, None)


def test_a_moved_chat_names_its_list_every_time_it_is_started(client, cli, saved_chat):
    old = _running_chat()
    _on_another_account(old)
    cli.session = BRANCH
    _post(client, SESSION)
    # The CLI's file for the branch starts with a copy of the old chat's.
    branch_file = _save_conversation(_three_tasks_first_started(), session_id=BRANCH)
    try:
        # Closed for being idle, then picked up again: a plain resume.
        cli.messages = _finish_one_start_two()
        response = _post(client, BRANCH)
        started = cli.started[-1]
        assert started.resume == BRANCH and not started.fork_session
        assert started.env.get(LIST_SETTING) == SESSION
        assert _names_and_statuses(_lists_sent(response)[-1]) == [
            (FIRST, "completed"),
            (SECOND, "in_progress"),
            (THIRD, "pending"),
        ]
        # Moved a second time, it is still on the list it began with.
        again = _running_chat(BRANCH)
        _on_another_account(again)
        cli.session = SECOND_BRANCH
        cli.messages = []
        _post(client, BRANCH)
        assert cli.started[-1].env.get(LIST_SETTING) == SESSION
        assert app_module._session_task_list_binding(SECOND_BRANCH) == (SESSION, None)
    finally:
        branch_file.unlink(missing_ok=True)


def test_an_operators_own_list_setting_is_left_alone(client, cli, saved_chat, monkeypatch):
    """Someone who names one list for the whole service gets it: ours is laid
    over the inherited environment, so it has to be left out, not overwritten."""
    monkeypatch.setenv(LIST_SETTING, "house-list")
    old = _running_chat()
    _on_another_account(old)
    cli.session = BRANCH
    _post(client, SESSION)
    assert cli.started[-1].fork_session is True
    assert LIST_SETTING not in cli.started[-1].env


def test_a_branch_the_user_asked_for_starts_on_its_own_empty_list(client, cli, saved_chat):
    """/fork: both chats live on, so the branch keeps the empty list the CLI
    gives it, and the page stops showing the old chat's."""
    before = time.time()
    cli.session = BRANCH
    response = _post(client, SESSION, fork="true")
    assert response.status_code == 200
    started = cli.started[-1]
    assert started.resume == SESSION and started.fork_session is True
    assert LIST_SETTING not in started.env
    assert _lists_sent(response) == [[]]
    list_id, history_from = app_module._session_task_list_binding(BRANCH)
    assert list_id == BRANCH
    assert history_from is not None and history_from >= before
    # Its own history starts once the CLI is up and has copied the old chat,
    # which may have still been writing when the branch was asked for.
    assert history_from >= cli.started_at[-1]


def _written(lines: list[dict], seconds_from_now: float) -> list[dict]:
    """The same entries with the time the CLI wrote them."""
    moment = datetime.fromtimestamp(time.time() + seconds_from_now, tz=timezone.utc)
    stamp = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return [dict(line, timestamp=stamp) for line in lines]


def _write_lines(path, lines: list[dict]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_a_branchs_page_shows_its_own_tasks_not_the_history_it_copied(client):
    """A branch's file starts with the old chat's conversation, original times
    and all. None of it is about the branch's list."""
    app_module._bind_session_task_list(BRANCH, None, BRANCH, time.time())
    folder = app_module._sessions_dir(app_module.DEFAULT_CWD)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{BRANCH}.jsonl"
    copied = _written(_transcript_lines(_three_tasks_first_started(), session_id=BRANCH), -3600)
    try:
        _write_lines(path, copied)
        assert client.get(f"/api/sessions/{BRANCH}").json()["tasks"] == []
        own = _written(_transcript_lines([
            _call("TaskCreate", "tu_b1", {"subject": "The branch's first task"}),
            _created("tu_b1", "1", "The branch's first task"),
        ], session_id=BRANCH, clutter=False), 5)
        _write_lines(path, copied + own)
        tasks = client.get(f"/api/sessions/{BRANCH}").json()["tasks"]
        assert [task["content"] for task in tasks] == ["The branch's first task"]
    finally:
        path.unlink(missing_ok=True)


def test_for_a_branch_an_entry_with_no_readable_time_does_not_count(saved_chat):
    # The saved chat's entries here carry no times at all.
    assert len(app_module.task_list.tasks_from_transcript(saved_chat).todos) == 3
    assert app_module.task_list.tasks_from_transcript(saved_chat, history_from=0.0).todos == []


def test_deleting_a_chat_forgets_which_list_it_was_on(client):
    path = _save_conversation(_three_tasks_first_started(), session_id=BRANCH)
    try:
        app_module._bind_session_task_list(BRANCH, None, SESSION, None)
        assert app_module._session_task_list_binding(BRANCH) == (SESSION, None)
        assert client.delete(f"/api/sessions/{BRANCH}").status_code == 200
        assert app_module._session_task_list_binding(BRANCH) == (None, None)
    finally:
        path.unlink(missing_ok=True)


def test_a_list_id_that_is_not_shaped_like_one_is_never_handed_to_a_cli():
    app_module._bind_session_task_list(BRANCH, None, "../../somewhere else", None)
    assert app_module._session_task_list_binding(BRANCH) == (None, None)
    # Nor one that got into the table some other way.
    app_module._state_db().execute(
        "INSERT INTO session_task_list(session_id, list_id, updated_at) VALUES(?, ?, ?)",
        (BRANCH, "bad id; rm -rf", time.time()),
    )
    assert app_module._session_task_list_binding(BRANCH) == (None, None)


def test_account_homes_share_one_tasks_folder():
    """The CLI makes tasks/ the first time a chat uses a list. Made under one
    account's home, it would be private to it, and a chat moved to another
    account would be told to stay on a list that account cannot see."""
    shared = app_module.CLAUDE_HOME / "tasks"
    # Start from a home where no chat has used a task list yet. Any account
    # home an earlier test set up will have made the folder, empty.
    if shared.is_dir() and not any(shared.iterdir()):
        shared.rmdir()
    assert not shared.exists()
    sub = "task-list-owner"
    cred = app_module._create_credential(sub, "task-list-home")
    try:
        home = app_module._ensure_credential_home(sub, cred["id"])
        assert shared.is_dir()
        assert (home / "tasks").resolve() == shared.resolve()
    finally:
        app_module._state_db().execute(
            "DELETE FROM user_credential WHERE user_sub = ? AND id = ?", (sub, cred["id"]),
        )


# ─── What the list carries ──────────────────────────────────────────────────

def test_each_task_carries_its_number_and_description():
    run = app_module.ActiveRun("numbers")
    lists = _feed(run, *_three_tasks_first_started())
    assert lists[-1][0] == {
        "id": "1",
        "content": FIRST,
        "activeForm": "Reading the player",
        "status": "in_progress",
        "description": "Trace the seek path.",
    }
    assert [todo["id"] for todo in lists[-1]] == ["1", "2", "3"]


def test_nothing_is_sent_until_the_cli_answers():
    run = app_module.ActiveRun("waits")
    assert _feed(run, _call("TaskCreate", "tu_1", {"subject": FIRST})) == []
    assert len(_feed(run, _created("tu_1", "1", FIRST))) == 1
    # Same for a change: the call alone says nothing about whether it took.
    assert _feed(run, _call("TaskUpdate", "tu_2", {"taskId": "1", "status": "in_progress"})) == []
    lists = _feed(run, _updated("tu_2", "1", ["status"], {"from": "pending", "to": "in_progress"}))
    assert _names_and_statuses(lists[-1]) == [(FIRST, "in_progress")]


def test_the_saved_name_wins_over_the_one_asked_for():
    """The CLI repairs malformed calls, and a hook can rewrite them."""
    run = app_module.ActiveRun("saved-name")
    lists = _feed(
        run,
        _call("TaskCreate", "tu_1", {"subject": "as asked", "description": "as asked"}),
        _created("tu_1", "1", "as saved", "saved description"),
    )
    assert lists[-1][0]["content"] == "as saved"
    assert lists[-1][0]["description"] == "saved description"


# ─── Changes the CLI turned down ────────────────────────────────────────────

def test_a_refused_update_changes_nothing():
    """A hook can block a task from being marked done; the CLI answers
    success false with the hook's message."""
    run = app_module.ActiveRun("refused")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_5", {"taskId": "1", "status": "completed"}),
        _result("tu_5", "Tests are still failing.", {
            "success": False, "taskId": "1", "updatedFields": [], "error": "Tests are still failing.",
        }),
    )
    assert lists == []
    assert _names_and_statuses(run.task_ledger.as_todos())[0] == (FIRST, "in_progress")


def test_a_task_the_cli_cannot_find_leaves_the_list():
    run = app_module.ActiveRun("not-found")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_5", {"taskId": "2", "status": "completed"}),
        _not_found("tu_5", "2"),
    )
    assert _names_and_statuses(lists[-1]) == [(FIRST, "in_progress"), (THIRD, "pending")]


def test_a_task_nobody_knew_about_is_not_invented_by_a_failed_update():
    run = app_module.ActiveRun("unknown-not-found")
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_1", {"taskId": "42", "status": "completed"}),
        _not_found("tu_1", "42"),
    )
    assert lists == []
    assert run.task_ledger.as_todos() == []


def test_a_failed_tool_call_adds_nothing():
    run = app_module.ActiveRun("create-error")
    lists = _feed(
        run,
        _call("TaskCreate", "tu_1", {"subject": "broken"}),
        _result("tu_1", "permission denied", is_error=True),
    )
    assert lists == []
    assert run.task_ledger.as_todos() == []
    assert run.task_ledger.pending_calls == {}
    # An error is an error whatever else the result holds.
    lists = _feed(
        run,
        _call("TaskCreate", "tu_2", {"subject": "broken"}),
        _result(
            "tu_2", "Task #1 created successfully: broken",
            {"task": {"id": "1", "subject": "broken"}}, is_error=True,
        ),
    )
    assert lists == []
    assert run.task_ledger.as_todos() == []


def test_a_deleted_task_leaves_the_list():
    run = app_module.ActiveRun("deleted")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_5", {"taskId": "2", "status": "deleted"}),
        _updated("tu_5", "2", ["deleted"], {"from": "pending", "to": "deleted"}),
    )
    assert [todo["id"] for todo in lists[-1]] == ["1", "3"]


# ─── When the CLI's own list changes under us ───────────────────────────────

def test_a_low_number_means_the_cli_started_a_fresh_list():
    """Numbers only go up within a list. A chat that was branched off another
    before lists were carried across shows the old chat's tasks in its saved
    conversation, while Claude had an empty list and made its tasks afresh
    from 1."""
    run = app_module.ActiveRun("fresh-list")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(
        run,
        _call("TaskCreate", "tu_5", {"subject": "A new first task"}),
        _created("tu_5", "1", "A new first task"),
        _call("TaskCreate", "tu_6", {"subject": "A new second task"}),
        _created("tu_6", "2", "A new second task"),
    )
    assert _names_and_statuses(lists[-1]) == [
        ("A new first task", "pending"),
        ("A new second task", "pending"),
    ]


def test_a_fresh_list_is_noticed_even_when_its_first_number_was_free():
    """Seen in a real chat: task 1 had already gone as "not found", so the new
    task 1 did not collide with anything, but tasks 2 and 3 were ghosts."""
    run = app_module.ActiveRun("fresh-list-gap")
    _feed(run, *_three_tasks_first_started())
    _feed(run, _call("TaskUpdate", "tu_5", {"taskId": "1", "status": "completed"}), _not_found("tu_5", "1"))
    lists = _feed(
        run,
        _call("TaskCreate", "tu_6", {"subject": "A new first task"}),
        _created("tu_6", "1", "A new first task"),
        _call("TaskCreate", "tu_7", {"subject": "A new second task"}),
        _created("tu_7", "2", "A new second task"),
    )
    assert [todo["content"] for todo in lists[-1]] == ["A new first task", "A new second task"]


def test_a_listing_replaces_what_we_thought_the_list_was():
    run = app_module.ActiveRun("listing")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(
        run,
        _call("TaskList", "tu_5", {}),
        _result(
            "tu_5",
            f"#2 [completed] {SECOND}\n#7 [pending] Made by someone else",
            {"tasks": [
                {"id": "2", "subject": SECOND, "status": "completed", "blockedBy": []},
                {"id": "7", "subject": "Made by someone else", "status": "pending", "blockedBy": []},
            ]},
        ),
    )
    assert lists, "the listing changed nothing"
    assert _names_and_statuses(lists[-1]) == [(SECOND, "completed"), ("Made by someone else", "pending")]
    # What the listing does not carry is kept from before.
    assert lists[-1][0]["description"] == "One rule for every door."


def test_an_empty_listing_empties_the_list():
    run = app_module.ActiveRun("empty-listing")
    _feed(run, *_three_tasks_first_started())
    lists = _feed(run, _call("TaskList", "tu_5", {}), _result("tu_5", "No tasks found", {"tasks": []}))
    assert lists == [[]]


def test_fetching_one_task_fills_in_what_was_missing():
    run = app_module.ActiveRun("fetched")
    # A task this run never saw being made: its name is not known yet.
    _feed(
        run,
        _call("TaskUpdate", "tu_1", {"taskId": "7", "status": "in_progress"}),
        _updated("tu_1", "7", ["status"], {"from": "pending", "to": "in_progress"}),
    )
    assert run.task_ledger.as_todos()[0]["content"] == ""
    lists = _feed(
        run,
        _call("TaskGet", "tu_2", {"taskId": "7"}),
        _result("tu_2", "Task #7: Found it\nStatus: in_progress\nDescription: The details", {
            "task": {"id": "7", "subject": "Found it", "description": "The details",
                     "status": "in_progress", "blocks": [], "blockedBy": []},
        }),
    )
    assert lists[-1][0]["content"] == "Found it"
    assert lists[-1][0]["description"] == "The details"


def test_tasks_stay_in_number_order():
    run = app_module.ActiveRun("order")
    messages = []
    for call_id, number, subject in [("tu_a", "9", "nine"), ("tu_b", "10", "ten"), ("tu_c", "11", "eleven")]:
        messages.append(_call("TaskCreate", call_id, {"subject": subject}))
        messages.append(_created(call_id, number, subject))
    lists = _feed(run, *messages)
    assert [todo["content"] for todo in lists[-1]] == ["nine", "ten", "eleven"]
    # Touching an early task does not move it.
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_d", {"taskId": "9", "subject": "nine, renamed"}),
        _updated("tu_d", "9", ["subject"]),
    )
    assert [todo["content"] for todo in lists[-1]] == ["nine, renamed", "ten", "eleven"]


def test_a_message_that_arrives_twice_counts_once():
    """A chat that is started again can repeat messages it already sent."""
    run = app_module.ActiveRun("twice")
    messages = _three_tasks_first_started()
    _feed(run, *messages)
    assert _feed(run, *messages) == []
    assert len(run.task_ledger.as_todos()) == 3


# ─── A result that arrives as text only ─────────────────────────────────────

def test_the_wording_alone_is_enough_when_no_result_object_comes():
    run = app_module.ActiveRun("text-only")
    lists = _feed(
        run,
        _call("TaskCreate", "tu_1", {"subject": FIRST, "description": "From the call."}),
        _result("tu_1", f"Task #1 created successfully: {FIRST}"),
        _call("TaskUpdate", "tu_2", {"taskId": "1", "status": "in_progress"}),
        _result("tu_2", "Updated task #1 status"),
    )
    assert lists[-1] == [{
        "id": "1", "content": FIRST, "activeForm": "", "status": "in_progress", "description": "From the call.",
    }]
    # And a refusal is still a refusal.
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_3", {"taskId": "1", "status": "completed"}),
        _result("tu_3", "Tests are still failing."),
    )
    assert lists == []
    lists = _feed(
        run,
        _call("TaskUpdate", "tu_4", {"taskId": "1", "status": "completed"}),
        _result("tu_4", "Task not found"),
    )
    assert lists[-1] == []


# ─── Reading a saved conversation ───────────────────────────────────────────

def test_a_saved_conversation_gives_back_its_list(saved_chat):
    found = app_module.task_list.tasks_from_transcript(saved_chat)
    # The repeated calls and the helper agent's own task are not counted.
    assert _names_and_statuses(found.todos) == [
        (FIRST, "in_progress"),
        (SECOND, "pending"),
        (THIRD, "pending"),
    ]
    assert found.ledger.pending_calls == {}


def test_only_claudes_own_turns_in_the_file_count(tmp_path):
    """The file holds other kinds of entry that carry messages too (what was
    sent to the API, for one). Real ones only repeat calls already counted;
    this one carries a call of its own to pin the rule down."""
    lines = _transcript_lines(_three_tasks_first_started())
    lines.append({
        "type": "api-request-blob",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_blob", "name": "TaskCreate", "input": {"subject": "Not a turn"}},
        ]},
    })
    lines.append({
        "type": "api-request-blob",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_blob", "content": "Task #4 created successfully: Not a turn"},
        ]},
    })
    path = tmp_path / "other-kinds.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    found = app_module.task_list.tasks_from_transcript(path)
    assert [todo["content"] for todo in found.todos] == [FIRST, SECOND, THIRD]


def test_a_missing_saved_conversation_gives_an_empty_list(tmp_path):
    found = app_module.task_list.tasks_from_transcript(tmp_path / "never-written.jsonl")
    assert found.todos == []
    assert app_module.task_list.tasks_from_transcript(None).todos == []


def test_a_chat_that_last_used_the_whole_list_tool_shows_that_list(tmp_path):
    whole_list = [{"content": "Old-style item", "activeForm": "Doing it", "status": "in_progress"}]
    path = tmp_path / "todo-write.jsonl"
    lines = _transcript_lines(_three_tasks_first_started())
    lines.append({
        "type": "assistant", "isSidechain": False,
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_todo", "name": "TodoWrite", "input": {"todos": whole_list}},
        ]},
    })
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert app_module.task_list.tasks_from_transcript(path).todos == whole_list
    # A later task tool result puts the task list back in front.
    lines.extend(_transcript_lines([
        _call("TaskUpdate", "tu_late", {"taskId": "2", "status": "in_progress"}),
        _updated("tu_late", "2", ["status"], {"from": "pending", "to": "in_progress"}),
    ], clutter=False))
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    assert [todo["id"] for todo in app_module.task_list.tasks_from_transcript(path).todos] == ["1", "2", "3"]
