"""_apply_turn_state: the driver's between_turns transitions per SDK message.

Regression coverage for the 2026-07-10 interrupt wedge: after
``client.interrupt()``, the CLI emits the interrupted turn's ResultMessage and
then echoes the marker ("[Request interrupted by user]") as a bare
UserMessage. Treating that echo as a turn start flipped ``between_turns`` back
to False with no turn running; the driver only polls user_input_queue between
turns, so every queued message sat undelivered until the mid-turn silence cap
tore the run down 30 minutes later.
"""
from __future__ import annotations

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)

import app as app_module


INTERRUPT_MARKER = "[Request interrupted by user]"


@pytest.fixture()
def run(monkeypatch):
    """A bare ActiveRun with the long-turn push notifier stubbed out."""
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    return app_module.ActiveRun("turn-state-test")


def _result(subtype: str = "success", is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
    )


def _marker_echo() -> UserMessage:
    return UserMessage(content=[TextBlock(text=INTERRUPT_MARKER)], uuid="u-marker")


def _translate(msg, run) -> list[dict]:
    """Real translator output, so the tests lock the no-events invariant the
    interrupt-echo gate depends on."""
    return app_module._sdk_message_to_events(msg, run)


def test_result_message_returns_to_idle(run) -> None:
    run.between_turns = False
    run.partial_text_buf = "leftover"
    app_module._apply_turn_state(run, _result(), [])
    assert run.between_turns is True
    assert run.partial_text_buf == ""


def test_interrupt_echo_keeps_run_idle(run) -> None:
    """The marker echo after an interrupted result must not re-enter mid-turn."""
    run.between_turns = False
    app_module._apply_turn_state(run, _result("error_during_execution", True), [])
    assert run.between_turns is True
    # The driver arms this when it rewrites the interrupted ResultMessage.
    run.expect_interrupt_echo = True

    echo = _marker_echo()
    evts = _translate(echo, run)
    assert evts == []  # bare text UserMessage produces no events
    app_module._apply_turn_state(run, echo, evts)

    assert run.between_turns is True
    assert run.expect_interrupt_echo is False


def test_interrupt_echo_still_records_checkpoint(run) -> None:
    run.between_turns = True
    run.expect_interrupt_echo = True
    echo = _marker_echo()
    app_module._apply_turn_state(run, echo, _translate(echo, run))
    assert run.checkpoints and run.checkpoints[-1]["uuid"] == "u-marker"


def test_bare_user_message_without_flag_starts_turn(run) -> None:
    """A ScheduleWakeup firing inside the CLI opens a turn with a bare user
    echo; without the armed flag it must still count as turn activity."""
    run.between_turns = True
    msg = UserMessage(content=[TextBlock(text="wakeup prompt")], uuid="u-wake")
    app_module._apply_turn_state(run, msg, _translate(msg, run))
    assert run.between_turns is False


def test_flag_consumed_once(run) -> None:
    run.between_turns = True
    run.expect_interrupt_echo = True
    echo = _marker_echo()
    app_module._apply_turn_state(run, echo, _translate(echo, run))
    assert run.between_turns is True

    followup = UserMessage(content=[TextBlock(text="next turn")], uuid="u-next")
    app_module._apply_turn_state(run, followup, _translate(followup, run))
    assert run.between_turns is False


def test_stale_flag_ignored_mid_turn(run) -> None:
    """The gate requires between_turns: a bare user message arriving mid-turn
    with a stale flag is normal turn activity, and the flag is dropped."""
    run.between_turns = False
    run.expect_interrupt_echo = True
    msg = UserMessage(content=[TextBlock(text="mid-turn echo")], uuid="u-mid")
    app_module._apply_turn_state(run, msg, _translate(msg, run))
    assert run.between_turns is False
    assert run.expect_interrupt_echo is False


def test_tool_result_user_message_is_turn_activity(run) -> None:
    run.between_turns = False
    msg = UserMessage(
        content=[ToolResultBlock(tool_use_id="t1", content="ok")],
        uuid="u-tool",
    )
    evts = _translate(msg, run)
    assert evts  # tool results do produce events
    app_module._apply_turn_state(run, msg, evts)
    assert run.between_turns is False
    # Tool-result echoes are not rewind anchors.
    assert not run.checkpoints


def test_assistant_message_starts_turn_and_stamps_clock(run) -> None:
    run.between_turns = True
    before = run.turn_started_at
    msg = AssistantMessage(content=[TextBlock(text="hi")], model="m")
    app_module._apply_turn_state(run, msg, [{"type": "assistant"}])
    assert run.between_turns is False
    assert run.turn_started_at >= before
