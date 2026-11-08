"""Task lifecycle frames reach the browser.

The SDK parses task_started, task_progress and task_notification into
SystemMessage subclasses. The translator used to return early for every
SystemMessage, so all three produced no event: the task block, its Stop
button, the elapsed time and the completion cue never appeared, even though
the browser has rendered those events all along.
"""
from __future__ import annotations

from claude_agent_sdk import (
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
)

import app as app_module


def test_task_started_opens_a_task_block() -> None:
    events = app_module._sdk_message_to_events(TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id="task-1",
        description="Run the test suite",
        uuid="u1",
        session_id="s1",
        tool_use_id="toolu_1",
        task_type="local_bash",
    ))

    assert events == [{
        "type": "task_started",
        "task_id": "task-1",
        "description": "Run the test suite",
        "task_type": "local_bash",
        "tool_use_id": "toolu_1",
    }]


def test_task_progress_updates_the_block() -> None:
    events = app_module._sdk_message_to_events(TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id="task-1",
        description="Compiling",
        usage={},
        uuid="u2",
        session_id="s1",
        tool_use_id="toolu_1",
        last_tool_name="Bash",
    ))

    assert events == [{
        "type": "task_progress",
        "task_id": "task-1",
        "description": "Compiling",
        "last_tool_name": "Bash",
        "tool_use_id": "toolu_1",
    }]


def test_task_notification_closes_it() -> None:
    events = app_module._sdk_message_to_events(TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="task-1",
        status="completed",
        output_file="/tmp/task-1.out",
        summary="All tests passed",
        uuid="u3",
        session_id="s1",
        tool_use_id="toolu_1",
    ))

    assert events == [{
        "type": "task_notification",
        "task_id": "task-1",
        "status": "completed",
        "summary": "All tests passed",
        "output_file": "/tmp/task-1.out",
        "tool_use_id": "toolu_1",
    }]


def test_housekeeping_system_messages_stay_silent() -> None:
    # Moving the task branches up must not have opened the door to every
    # other SystemMessage.
    for subtype in ("stop_hook_summary", "per_turn_effort_changed"):
        assert app_module._sdk_message_to_events(
            SystemMessage(subtype=subtype, data={})
        ) == []
