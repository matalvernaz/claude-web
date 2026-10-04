"""The CLI's model-swap notices reach the browser as a spoken notice.

When a model's safeguards refuse a message, the CLI retries on a fallback
model and, for the main thread, swaps the session onto it for good. The
official TUI shows a banner ("Fable 5.1's safeguards flagged this message ...
Switched to Opus 4.8."). claude-web used to drop every system message except
init, so the swap happened in silence and the header kept naming the model the
user picked. That happened four times between 2026-09-05 and 2026-09-30.

Messages are built through the SDK's own parser from the wire shape the CLI
writes to stdout, so these tests follow the real path in.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

import app as app_module

# The CLI's own text, as recorded in a real 2026-09-30 transcript.
_REFUSAL_TEXT = (
    "Fable 5.1's safeguards flagged this message. Our intentionally broad "
    "safeguards allow us to deliver more capabilities faster, but can sometimes "
    "flag legitimate coding and cybersecurity tasks. Switched to Opus 4.8. Send "
    "feedback with /feedback or learn more: "
    "https://support.claude.com/en/articles/15363606\n\nDetails: `[cyber]`"
)


def _system(subtype: str, **fields) -> object:
    data = {"type": "system", "subtype": subtype, "uuid": "u-1", "session_id": "s-1"}
    data.update(fields)
    return parse_message(data)


def _refusal_fallback(**overrides) -> object:
    fields = {
        "trigger": "refusal",
        "direction": "retry",
        "scope": "session",
        "original_model": "claude-fable-5-1",
        "fallback_model": "claude-opus-4-8",
        "request_id": "req_1",
        "api_refusal_category": "cyber",
        "api_refusal_explanation": None,
        "retracted_message_uuids": ["m-1"],
        "refused_user_message_uuid": "m-0",
        "content": _REFUSAL_TEXT,
    }
    fields.update(overrides)
    return _system("model_refusal_fallback", **fields)


def test_refusal_fallback_becomes_a_notice_with_the_cli_text() -> None:
    events = app_module._sdk_message_to_events(_refusal_fallback())

    assert len(events) == 1
    notice = events[0]
    assert notice["type"] == "model_notice"
    assert notice["subtype"] == "model_refusal_fallback"
    assert notice["message"] == _REFUSAL_TEXT
    assert notice["original_model"] == "claude-fable-5-1"
    assert notice["fallback_model"] == "claude-opus-4-8"
    assert notice["scope"] == "session"
    assert notice["title"]
    # The spoken form leads with what changed, in picker labels, short enough
    # to be heard before the announcer moves on.
    assert notice["summary"] == (
        "Fable 5.1's safeguards flagged a message. This chat is now on Opus 4.8."
    )


@pytest.mark.parametrize("subtype, fields", [
    ("model_refusal_no_fallback", {
        "original_model": "claude-fable-5-1", "request_id": None,
        "content": "Fable 5.1 declined to answer this message.",
    }),
    ("model_fallback", {
        "trigger": "overloaded", "original_model": "claude-opus-5-5",
        "fallback_model": "claude-opus-4-8",
        "content": "Opus 5.5 is overloaded. Switched to Opus 4.8 for this turn.",
    }),
    ("model_consent_fallback", {
        "choice": "cancelled", "original_model": "claude-fable-5-1",
        "fallback_model": "claude-opus-5-5", "persisted_as_default": False,
        "content": "Fable needs usage credits. Switched to Opus 5.5.",
    }),
])
def test_every_model_swap_subtype_is_surfaced(subtype: str, fields: dict) -> None:
    events = app_module._sdk_message_to_events(_system(subtype, **fields))

    assert [e["type"] for e in events] == ["model_notice"]
    assert events[0]["subtype"] == subtype
    assert events[0]["message"] == fields["content"]


def test_a_notice_without_text_still_names_both_models() -> None:
    # A future CLI could drop or rename the prose field; the swap itself must
    # still be said out loud.
    events = app_module._sdk_message_to_events(_refusal_fallback(content=None))

    assert len(events) == 1
    assert "Fable 5.1" in events[0]["message"]
    assert "Opus 4.8" in events[0]["message"]


def test_a_model_the_picker_does_not_list_is_named_by_its_id() -> None:
    events = app_module._sdk_message_to_events(
        _refusal_fallback(original_model="claude-future-9", content=None),
    )

    assert "claude-future-9" in events[0]["summary"]


def test_a_subagent_fallback_says_the_chat_model_is_unchanged() -> None:
    # scope "local": only a subagent / side question fell back, and the
    # session model did not move. The notice must not claim it did.
    events = app_module._sdk_message_to_events(
        _refusal_fallback(scope="local", content=None),
    )

    assert events[0]["scope"] == "local"
    assert "unchanged" in events[0]["message"]


def test_other_system_subtypes_stay_silent() -> None:
    # Housekeeping frames must not start appearing in the transcript.
    for subtype in ("per_turn_effort_changed", "status", "compact_boundary"):
        assert app_module._sdk_message_to_events(_system(subtype)) == []


def test_model_notice_is_rendered_and_spoken() -> None:
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",  # Windows defaults to cp1252 and chokes on the JS
    )
    start = source.index('obj.type === "model_notice"')
    block = source[start:source.index("} else if", start)]

    # The short summary is what gets spoken; the CLI's full text is the record.
    assert "appendNotice(obj.title" in block
    assert "obj.message" in block[:block.index("announce(")]
    assert "announce(obj.summary" in block

    helper = source[source.index("function appendNotice("):]
    helper = helper[:helper.index("\n  }\n")]
    assert 'className = "msg info"' in helper


# ── Reopened chats ────────────────────────────────────────────────────────────
# The live notice only reaches a browser that was watching. Reopening the chat
# later rebuilds it from the session jsonl, which the CLI writes the switch
# into as a system line (camelCase fields there, unlike the stdout copy).

def _write_session(lines: list[dict]) -> str:
    import json
    import uuid

    sid = str(uuid.uuid4())
    path = app_module._sessions_dir(app_module.PROJECTS[0]) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return sid


def _jsonl_refusal_fallback() -> dict:
    # Shape copied from a real 2026-09-30 transcript line.
    return {
        "type": "system", "subtype": "model_refusal_fallback",
        "content": _REFUSAL_TEXT, "level": "warning", "trigger": "refusal",
        "direction": "retry", "scope": "session",
        "originalModel": "claude-fable-5-1", "fallbackModel": "claude-opus-4-8",
        "apiRefusalCategory": "cyber", "isMeta": False, "uuid": "d0ff",
    }


def _jsonl_session() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": "Audit this binary"}},
        _jsonl_refusal_fallback(),
        {"type": "system", "subtype": "local_command", "content": "<command-name>/advisor</command-name>"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Here is the audit."},
        ]}},
    ]


def test_reopened_chat_keeps_the_model_notice_in_place() -> None:
    sid = _write_session(_jsonl_session())

    msgs = app_module.session_transcript(sid)

    assert [m["role"] for m in msgs] == ["user", "notice", "assistant"]
    notice = msgs[1]
    assert notice["title"] == "Model switched"
    assert notice["text"] == _REFUSAL_TEXT


def test_reopened_notice_without_text_still_names_both_models() -> None:
    line = _jsonl_refusal_fallback()
    line.pop("content")
    sid = _write_session([line])

    (notice,) = app_module.session_transcript(sid)

    assert "Fable 5.1" in notice["text"] and "Opus 4.8" in notice["text"]


def test_markdown_export_includes_the_model_notice() -> None:
    sid = _write_session(_jsonl_session())

    md = app_module.session_to_markdown(sid)

    assert "Model switched" in md
    assert "Switched to Opus 4.8" in md
    assert md.index("Audit this binary") < md.index("Model switched") < md.index("Here is the audit.")


def test_history_repaint_renders_notices() -> None:
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",
    )
    start = source.index('m.role === "notice"')
    block = source[start:source.index("} else if", start)]

    assert "appendNotice(" in block
