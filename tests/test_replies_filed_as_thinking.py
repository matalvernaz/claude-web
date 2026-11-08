"""Messages the API files as thinking still reach the reader.

On Opus 5.5 and Fable 5.1, some of the messages Claude writes between tool calls come back from the API as a thinking block holding a shortened version of the text, instead of as a text block. The CLI asks for thinking with its content omitted, so real reasoning arrives empty and these are the only thinking blocks with words in them. claude-web hid every thinking block, so those messages never appeared, live or on reopen. Counted 2026-11-07 across this host's transcripts: 6,637 of 33,459 Opus 5.5 tool-call messages and 187 of 1,529 on Fable 5.1 since 2026-10-08, none on Opus 4.8 or Opus 5.

Messages are built through the SDK's own parser from the wire shape the CLI writes, so these tests follow the real path in. The text is a real one, from the 2026-11-07 chat where this was found.
"""
from __future__ import annotations

import json
import uuid

from claude_agent_sdk._internal.message_parser import parse_message

import app as app_module

# What the API sent back in place of a two-paragraph answer to "why does DeepSeek follow Local's format?".
FILED_AS_THINKING = (
    "DeepSeek runs through Claude Code like the Claude and Local options do, so it keeps tools, "
    "plan mode, rewind and MCP, minus the Anthropic account features."
)
LOCAL_REASONING = "The user wants a file written. I should call Write with the path first."


def _assistant(model: str, blocks: list[dict]) -> object:
    return parse_message({
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "id": "msg_1", "content": blocks},
        "parent_tool_use_id": None,
        "session_id": "s-1",
        "uuid": "u-1",
    })


def _thinking(text: str) -> dict:
    return {"type": "thinking", "thinking": text, "signature": "sig"}


def _content(events: list[dict]) -> list[dict]:
    (event,) = [e for e in events if e.get("type") == "assistant"]
    return event["message"]["content"]


# ── Live ──────────────────────────────────────────────────────────────────────

def test_a_message_filed_as_thinking_reaches_the_browser_as_text() -> None:
    events = app_module._sdk_message_to_events(
        _assistant("claude-opus-5-5", [_thinking(FILED_AS_THINKING)]))

    assert _content(events) == [{"type": "text", "text": FILED_AS_THINKING}]


def test_the_empty_thinking_block_before_it_stays_hidden() -> None:
    events = app_module._sdk_message_to_events(_assistant("claude-opus-5-5", [_thinking("")]))

    assert [b["type"] for b in _content(events)] == ["thinking"]


def test_a_local_models_reasoning_stays_hidden() -> None:
    # A Local (or any other routed) model's thinking is its raw reasoning, not a message.
    events = app_module._sdk_message_to_events(
        _assistant("qwen3.5:35b-32k", [_thinking(LOCAL_REASONING)]))

    assert _content(events) == [{"type": "thinking", "text": LOCAL_REASONING}]


def test_the_turn_notification_can_read_it_back() -> None:
    class Run:
        turn_last_text = ""
        turn_tool_count = 0
        provider = "claude"
        uuid = None

    run = Run()
    app_module._sdk_message_to_events(
        _assistant("claude-opus-5-5", [_thinking(FILED_AS_THINKING)]), run)

    assert run.turn_last_text == FILED_AS_THINKING


# ── Reopened chats ────────────────────────────────────────────────────────────
# Rows copied in shape from the 2026-11-07 transcript: the CLI writes one block per row, all sharing the API message id.

def _write_session(model: str, filed_text: str) -> str:
    message_id = "msg_011CgpbJkNRFd33nQRZemJj3"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "You haven't answered my question"}},
        {"type": "assistant", "message": {"role": "assistant", "model": model, "id": message_id,
                                          "content": [_thinking("")]}},
        {"type": "assistant", "message": {"role": "assistant", "model": model, "id": message_id,
                                          "content": [_thinking(filed_text)]}},
        {"type": "assistant", "message": {"role": "assistant", "model": model, "id": message_id,
                                          "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                       "input": {"command": "sed -n 1338,1352p app.py"}}]}},
    ]
    sid = str(uuid.uuid4())
    path = app_module._sessions_dir(app_module.PROJECTS[0]) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return sid


def test_a_reopened_chat_shows_the_message_where_it_was_said() -> None:
    sid = _write_session("claude-opus-5-5", FILED_AS_THINKING)

    msgs = app_module.session_transcript(sid)

    assert [m["role"] for m in msgs] == ["user", "assistant", "tool_use"]
    assert msgs[1]["text"] == FILED_AS_THINKING


def test_a_reopened_local_chat_keeps_its_reasoning_hidden() -> None:
    sid = _write_session("qwen3.5:35b-32k", LOCAL_REASONING)

    msgs = app_module.session_transcript(sid)

    assert [m["role"] for m in msgs] == ["user", "tool_use"]


def test_the_markdown_export_includes_it() -> None:
    sid = _write_session("claude-opus-5-5", FILED_AS_THINKING)

    assert FILED_AS_THINKING in app_module.session_to_markdown(sid)
