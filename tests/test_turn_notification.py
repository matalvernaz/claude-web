"""Long-turn Pushover notification: the per-turn digest and its formatting.

The notification body is meant to say what the turn actually did (the model's
final reply, the outcome, a light activity line, which chat), so these cover
the digest capture in _sdk_message_to_events, the pure (title, body) builder,
the markdown flattening a screen reader needs, and the fire/don't-fire gate.
"""
from __future__ import annotations

import time

import app as app_module


def _assistant(blocks):
    from claude_agent_sdk import AssistantMessage
    return AssistantMessage(content=blocks, model="test")


def _text(s):
    from claude_agent_sdk.types import TextBlock
    return TextBlock(text=s)


def _tool(name, inp=None):
    from claude_agent_sdk.types import ToolUseBlock
    return ToolUseBlock(id=f"tu_{name}", name=name, input=inp or {})


# ─── Digest capture in _sdk_message_to_events ──────────────────────────────

def test_digest_counts_work_tools_and_collects_files() -> None:
    run = app_module.ActiveRun("digest-1")
    app_module._sdk_message_to_events(
        _assistant([
            _text("Working on it."),
            _tool("Bash", {"command": "ls"}),
            _tool("Edit", {"file_path": "/home/matt/claude-web/app.py"}),
            _tool("Edit", {"file_path": "/home/matt/claude-web/auth.py"}),
            _text("Done. Fixed the loop."),
        ]),
        run=run,
    )
    assert run.turn_tool_count == 3            # Bash + two Edits
    assert run.turn_files == ["app.py", "auth.py"]
    # The last non-empty text block is the summary.
    assert run.turn_last_text == "Done. Fixed the loop."


def test_digest_excludes_bookkeeping_tools() -> None:
    run = app_module.ActiveRun("digest-2")
    app_module._sdk_message_to_events(
        _assistant([_tool("TodoWrite", {"todos": []}), _tool("TaskUpdate", {"taskId": "1"})]),
        run=run,
    )
    assert run.turn_tool_count == 0
    assert run.turn_files == []


def test_digest_dedupes_repeated_file_edits() -> None:
    run = app_module.ActiveRun("digest-3")
    app_module._sdk_message_to_events(
        _assistant([
            _tool("Edit", {"file_path": "/x/app.py"}),
            _tool("Edit", {"file_path": "/x/app.py"}),
        ]),
        run=run,
    )
    assert run.turn_files == ["app.py"]


# ─── Pure (title, body) formatter ──────────────────────────────────────────

def _run_with(**kw):
    run = app_module.ActiveRun("fmt")
    for k, v in kw.items():
        setattr(run, k, v)
    return run


def test_format_success_leads_with_summary() -> None:
    run = _run_with(
        turn_last_text="Fixed the sign-in loop. Tests pass and it's pushed.",
        turn_tool_count=12,
        turn_files=["app.py", "auth.py"],
        turn_result={"is_error": False, "subtype": "success"},
        checkpoints=[{"uuid": "u", "preview": "can you fix the sign-in loop"}],
    )
    title, body = app_module._format_turn_notification(run, 303.0)
    assert title == "Turn finished"
    assert body.startswith("Fixed the sign-in loop. Tests pass and it's pushed.")
    assert "5m03s in claude-web" in body          # project_key None → fallback
    assert "edited app.py, auth.py" in body
    assert "12 tool calls" in body
    assert "In reply to: can you fix the sign-in loop" in body


def test_format_failed_turn_titles_failure_and_uses_error_text() -> None:
    run = _run_with(
        turn_last_text="",
        turn_result={"is_error": True, "subtype": "error_during_execution",
                     "result": "You've hit your usage limit."},
    )
    title, body = app_module._format_turn_notification(run, 180.0)
    assert title == "Turn failed"
    assert "usage limit" in body


def test_format_interrupted_turn_from_subtype() -> None:
    run = _run_with(turn_result={"is_error": False, "subtype": "interrupted"})
    title, _ = app_module._format_turn_notification(run, 200.0)
    assert title == "Turn interrupted"


def test_format_single_tool_is_singular() -> None:
    run = _run_with(turn_tool_count=1, turn_result={})
    _, body = app_module._format_turn_notification(run, 130.0)
    assert "1 tool call" in body and "tool calls" not in body


def test_format_caps_file_list() -> None:
    run = _run_with(
        turn_files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"],
        turn_result={},
    )
    _, body = app_module._format_turn_notification(run, 130.0)
    assert "a.py, b.py, c.py, d.py +2 more" in body


# ─── Markdown flattening for a screen reader ───────────────────────────────

def test_clean_notify_strips_bold_and_backticks_keeps_identifiers() -> None:
    out = app_module._clean_notify_text(
        "Fixed **the bug** in `file_path` handling. See PR #14."
    )
    # Bold/backtick marks gone; underscores and '#' preserved.
    assert out == "Fixed the bug in file_path handling. See PR #14."


def test_clean_notify_drops_code_fences_and_collapses_newlines() -> None:
    out = app_module._clean_notify_text(
        "Here is the result:\n\n```\nrm -rf /\n```\n\nAll good now."
    )
    assert "rm -rf" not in out
    assert out == "Here is the result: All good now."


def test_truncate_notify_cuts_on_word_boundary() -> None:
    out = app_module._truncate_notify("one two three four five", 12)
    assert out.endswith("...")
    assert out == "one two..." or out == "one two three..."[: len(out)]
    assert len(out) <= 15


def test_truncate_notify_leaves_short_text() -> None:
    assert app_module._truncate_notify("short", 100) == "short"


# ─── Project label matches the header picker ───────────────────────────────

def test_project_label_known_key_is_basename(monkeypatch) -> None:
    from pathlib import Path
    monkeypatch.setitem(app_module.PROJECT_KEYS, "-home-matt", Path("/home/matt"))
    assert app_module._project_label("-home-matt") == "matt"


def test_project_label_unknown_falls_back_to_key() -> None:
    assert app_module._project_label("-some-unknown-key") == "-some-unknown-key"


def test_project_label_none_is_claude_web() -> None:
    assert app_module._project_label(None) == "claude-web"


# ─── Fire / don't-fire gate ────────────────────────────────────────────────

def _arm(monkeypatch):
    """Config so the gate would fire, and capture create_task instead of a loop."""
    created = []
    monkeypatch.setattr(app_module, "PUSHOVER_TOKEN", "tok")
    monkeypatch.setattr(app_module, "PUSHOVER_USER", "usr")
    monkeypatch.setattr(app_module, "NOTIFY_MIN_SECONDS", 120.0)

    def _capture(coro):
        created.append(coro)
        coro.close()       # we only assert it was scheduled; don't run the thread
        return None

    monkeypatch.setattr(app_module.asyncio, "create_task", _capture)
    return created


def test_notify_fires_for_long_turn(monkeypatch) -> None:
    created = _arm(monkeypatch)
    run = app_module.ActiveRun("fire-1")
    run.turn_started_at = time.monotonic() - 200
    run.turn_last_text = "Did the thing."
    run.turn_result = {"is_error": False}
    app_module._notify_turn_complete(run)
    assert len(created) == 1


def test_notify_skips_short_turn(monkeypatch) -> None:
    """A turn that only just started (elapsed ~0) must not notify. This is
    also the shape of the old Codex bug: turn_started_at reset to 'now' right
    before notifying made every long Codex turn look instant and go silent."""
    created = _arm(monkeypatch)
    run = app_module.ActiveRun("fire-2")
    run.turn_started_at = time.monotonic()      # ~0s elapsed
    app_module._notify_turn_complete(run)
    assert created == []


def test_notify_skips_without_credentials(monkeypatch) -> None:
    created = _arm(monkeypatch)
    monkeypatch.setattr(app_module, "PUSHOVER_TOKEN", "")
    run = app_module.ActiveRun("fire-3")
    run.turn_started_at = time.monotonic() - 200
    app_module._notify_turn_complete(run)
    assert created == []
