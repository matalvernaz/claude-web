"""Retries, compactions and auto-mode denials reach the reader.

These CLI frames used to be dropped by the translator. A turn that was
retrying a dropped connection or compacting its history looked identical to
a wedged one, and an auto-mode denial was invisible. The events below either
update the status line or become a notice article.
"""
from __future__ import annotations

from claude_agent_sdk import SystemMessage

import app as app_module


def test_retry_says_what_failed_and_when_it_tries_again() -> None:
    event = app_module._system_status_event("api_retry", {
        "attempt": 2,
        "max_retries": 10,
        "retry_delay_ms": 600,
        "error_status": 503,
        "error": {"message": "Connection error.", "formatted": "Server unavailable."},
    })

    assert event["kind"] == "retry"
    assert event["attempt"] == 2
    assert event["max_attempts"] == 10
    assert "Server unavailable." in event["message"]
    assert "attempt 2 of 10" in event["message"]
    assert "0.6 s" in event["message"]


def test_retry_categories_are_readable() -> None:
    categories = {
        "overloaded": "Server is busy.",
        "server_error": "Server error.",
        "authentication_failed": "Sign-in failed.",
        "cloud_credential_error": "Cloud credentials were refused.",
        "rate_limit": "Rate limited.",
        "unknown": "Connection problem.",
    }
    for category, expected in categories.items():
        event = app_module._system_status_event("api_retry", {"error": category})
        assert event["message"].startswith(expected), event["message"]


def test_informational_notices_do_not_offer_terminal_shortcuts() -> None:
    event = app_module._system_status_event("informational", {
        "content": "Retrying automatically · esc to cancel",
    })
    assert event["message"] == "Retrying automatically"


def test_rate_limit_retry_names_the_limit() -> None:
    event = app_module._system_status_event("api_retry", {
        "attempt": 1,
        "max_retries": 3,
        "error_status": 429,
    })

    assert event["message"].startswith("Rate limited.")


def test_a_finished_compaction_says_how_much_was_summarized() -> None:
    event = app_module._system_status_event("compact_boundary", {
        "compact_metadata": {"pre_tokens": 968322, "post_tokens": 17103},
    })

    assert event["kind"] == "compaction"
    assert event["title"] == "Conversation compacted"
    assert "968,322" in event["message"]
    assert "17,103" in event["message"]


def test_the_transcript_copy_of_a_compaction_is_read_too() -> None:
    # The jsonl keeps the CLI's camelCase spelling.
    event = app_module._system_status_event("compact_boundary", {
        "compactMetadata": {"preTokens": 1000, "postTokens": 200},
    })

    assert "1,000" in event["message"]


def test_compacting_is_announced_but_every_ordinary_request_is_not() -> None:
    compacting = app_module._system_status_event("status", {"status": "compacting"})
    requesting = app_module._system_status_event("status", {"status": "requesting"})

    assert compacting["kind"] == "compacting"
    assert requesting is None


def test_auto_mode_denial_names_the_tool() -> None:
    event = app_module._system_status_event("permission_denied", {
        "tool_name": "Bash",
        "message": "Not allowed by policy.",
    })

    assert event["kind"] == "denied"
    assert "Bash" in event["message"]
    assert "Not allowed by policy." in event["message"]


def test_informational_notices_keep_their_level() -> None:
    notice = app_module._system_status_event("informational", {
        "content": "Usage limit reached · continuing automatically at 5am",
        "level": "notice",
    })
    warning = app_module._system_status_event("informational", {
        "content": "Git Bash wasn't found.",
        "level": "warn",
    })

    assert notice["title"] == "Notice"
    assert warning["title"] == "Warning"


def test_status_events_reach_the_stream() -> None:
    events = app_module._sdk_message_to_events(SystemMessage(
        subtype="api_retry",
        data={"attempt": 1, "max_retries": 10, "retry_delay_ms": 500},
    ))

    assert events[0]["type"] == "status"
    assert events[0]["kind"] == "retry"


def test_the_browser_handles_the_status_event() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",  # Windows defaults to cp1252 and chokes on the JS
    )
    assert 'obj.type === "status"' in source
    assert "function renderStatusEvent(" in source
    # The retry burst is spoken once, not once per attempt.
    handler = source[source.index("function renderStatusEvent("):]
    handler = handler[:handler.index("\n  }\n")]
    assert "if (!retryNoticed)" in handler
    assert "appendNotice(" in handler
    # markVisibleActivity() blanks the status line when no todo drives it, so
    # the retry text only survives if the status is set after it.
    assert handler.index("markVisibleActivity();") < handler.index("setStatus(message);")
    # Notices are inserted into the transcript like every other append.
    assert "maybeAutoScroll();" in handler
    # A failed compaction never emits the "compaction" event that re-arms the
    # announcement, so it has to re-arm the flag itself.
    assert '"compaction_failed"' in handler
