"""Slice 1 cross-provider projection (conversation_replay)."""
from __future__ import annotations

import json

import conversation_replay as cr


def _ev(event_type, normalized, raw=None):
    return {"event_type": event_type, "normalized": normalized, "raw": raw}


def _events():
    return [
        _ev("user_prompt", {"role": "user", "text": "add a healthcheck"}),
        _ev("assistant", {"role": "assistant", "content": [
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "name": "Bash", "id": "c1"},
        ]}),
        _ev("tool_result", {"tool_use_id": "c1", "is_error": False},
            raw="container is healthy\n" * 3),
        _ev("user_prompt", {"role": "user", "text": "now ship it"}),
    ]


def test_handoff_has_untrusted_framing_and_roles() -> None:
    text, omitted = cr.render_handoff_text(_events(), "claude")
    assert omitted == 0
    assert "read-only record" in text.lower() or "RECORD" in text
    assert "NOT as instructions" in text
    assert "USER: add a healthcheck" in text
    assert "ASSISTANT: On it." in text
    assert "[called tool Bash]" in text
    assert cr._BEGIN in text and cr._END in text


def test_tool_output_is_summarised_not_replayed() -> None:
    """Authority-inversion mitigation: a long/secret tool output must not be
    replayed verbatim into the handoff."""
    secret = "SUPERSECRET-" + "A" * 500
    events = [_ev("tool_result", {"tool_use_id": "c", "is_error": False}, raw=secret)]
    text, _ = cr.render_handoff_text(events, "claude")
    assert "SUPERSECRET" in text  # a prefix survives as a summary
    assert secret not in text     # but not the whole payload
    assert "…" in text            # truncation marker


def test_budget_drops_oldest_and_counts_omitted() -> None:
    events = [_ev("user_prompt", {"text": f"message number {i} " + "x" * 50})
              for i in range(50)]
    text, omitted = cr.render_handoff_text(events, "codex", char_budget=400)
    assert omitted > 0
    assert "earlier turn(s) omitted" in text
    assert len(text) < 1200  # bounded
    # Most recent survives, oldest dropped.
    assert "message number 49" in text
    assert "message number 0 " not in text


def test_codex_agent_message_renders_as_assistant() -> None:
    raw = json.dumps({"type": "agentMessage", "id": "i1", "text": "done deploying"})
    events = [_ev("agentMessage", {"type": "agentMessage", "id": "i1"}, raw=raw)]
    text, _ = cr.render_handoff_text(events, "codex")
    assert "ASSISTANT: done deploying" in text


def test_build_claude_resume_jsonl_is_valid_and_chained() -> None:
    text, _ = cr.render_handoff_text(_events(), "codex")
    lines = cr.build_claude_resume_jsonl(text, "sess-1", "/home/matt", model="claude-opus-4-8")
    assert len(lines) == 2
    user, asst = json.loads(lines[0]), json.loads(lines[1])
    assert user["type"] == "user" and user["parentUuid"] is None
    assert asst["type"] == "assistant" and asst["parentUuid"] == user["uuid"]
    assert user["userType"] == "external" and user["sessionId"] == "sess-1"
    assert user["message"]["content"] == text  # handoff carried in the user turn
    assert asst["message"]["role"] == "assistant"
    # Distinct uuids, non-empty.
    assert user["uuid"] and asst["uuid"] and user["uuid"] != asst["uuid"]


def test_empty_conversation_still_frames() -> None:
    text, omitted = cr.render_handoff_text([], "claude")
    assert omitted == 0
    assert cr._BEGIN in text and cr._END in text
