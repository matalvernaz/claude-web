"""Slice 0 of mid-chat provider switching: the canonical conversation layer.

Unit-tests the shadow-write helpers against the temp state.db the conftest
bootstraps. No live CLI/app-server — these exercise the sqlite layer only:
atomic seq allocation, source-key dedup, untruncated raw capture + byte cap,
idempotent native-session wrapping, and provisional→active binding activation.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app as app_module


def _seqs_for(conv_id: str) -> list[int]:
    return [
        r[0]
        for r in app_module._state_db().execute(
            "SELECT seq FROM conversation_event WHERE conversation_id=? ORDER BY seq",
            (conv_id,),
        )
    ]


def test_create_binding_and_seq_monotonic() -> None:
    cid, bid = app_module._conv_create_with_binding("claude", "-proj", "sub-1", "t")
    assert cid and bid
    s1 = app_module._conv_append_event(cid, "k1", "claude", "user_prompt", {"text": "a"})
    s2 = app_module._conv_append_event(cid, "k2", "claude", "assistant", {"text": "b"})
    s3 = app_module._conv_append_event(cid, "k3", "claude", "result", {"ok": True})
    assert [s1, s2, s3] == [1, 2, 3]
    assert _seqs_for(cid) == [1, 2, 3]
    last = app_module._state_db().execute(
        "SELECT last_seq FROM conversation WHERE conversation_id=?", (cid,)
    ).fetchone()[0]
    assert last == 3


def test_source_key_dedup_allocates_no_second_seq() -> None:
    cid, _ = app_module._conv_create_with_binding("codex", "-proj", "sub-1")
    first = app_module._conv_append_event(cid, "dup", "codex", "tool_result", {"n": 1})
    again = app_module._conv_append_event(cid, "dup", "codex", "tool_result", {"n": 2})
    assert first == again == 1
    assert _seqs_for(cid) == [1]  # no second row


def test_untruncated_raw_beats_live_800_cap() -> None:
    """A 2,000-char tool result must land in the canonical store in full — the
    whole point of capturing at ingress rather than at the 800-char emit path."""
    cid, _ = app_module._conv_create_with_binding("claude", "-proj", "sub-1")
    big = "X" * 2000
    seq = app_module._conv_append_event(
        cid, "tr", "claude", "tool_result", {"tool_use_id": "t"}, raw=big
    )
    row = app_module._state_db().execute(
        "SELECT raw_json, original_bytes, stored_bytes, truncated, payload_sha256 "
        "FROM conversation_event WHERE conversation_id=? AND seq=?",
        (cid, seq),
    ).fetchone()
    raw_json, original_bytes, stored_bytes, truncated, sha = row
    assert original_bytes == 2000
    assert len(raw_json) > 800  # decisively past the live cap
    assert raw_json == big
    assert truncated == 0
    assert len(sha) == 64


def test_raw_payload_cap_truncates_and_records_original() -> None:
    cid, _ = app_module._conv_create_with_binding("claude", "-proj", "sub-1")
    huge = "Y" * (app_module.CANONICAL_PAYLOAD_CAP + 5000)
    seq = app_module._conv_append_event(
        cid, "huge", "claude", "tool_result", {}, raw=huge
    )
    row = app_module._state_db().execute(
        "SELECT original_bytes, stored_bytes, truncated FROM conversation_event "
        "WHERE conversation_id=? AND seq=?",
        (cid, seq),
    ).fetchone()
    original_bytes, stored_bytes, truncated = row
    assert original_bytes == app_module.CANONICAL_PAYLOAD_CAP + 5000
    assert stored_bytes <= app_module.CANONICAL_PAYLOAD_CAP
    assert truncated == 1


def test_ensure_for_native_is_idempotent() -> None:
    a = app_module._conv_ensure_for_native("claude", "-proj", "native-xyz", "sub-1", "T")
    b = app_module._conv_ensure_for_native("claude", "-proj", "native-xyz", "sub-1", "T")
    assert a is not None and a == b
    n = app_module._state_db().execute(
        "SELECT COUNT(*) FROM conversation_binding WHERE native_session_id=?",
        ("native-xyz",),
    ).fetchone()[0]
    assert n == 1
    # Wrapped historical sessions are legacy_partial (can't reconstruct fidelity).
    state = app_module._state_db().execute(
        "SELECT capture_state FROM conversation WHERE conversation_id=?", (a[0],)
    ).fetchone()[0]
    assert state == "legacy_partial"


def test_activate_binding_sets_native_and_status() -> None:
    cid, bid = app_module._conv_create_with_binding("codex", "-proj", "sub-1")
    app_module._conv_activate_binding(bid, "thread-abc", provider_version="0.144.6")
    row = app_module._state_db().execute(
        "SELECT native_session_id, status, provider_version FROM "
        "conversation_binding WHERE binding_id=?",
        (bid,),
    ).fetchone()
    assert row == ("thread-abc", "active", "0.144.6")
    # Now discoverable by native id, resolving to the same conversation.
    found = app_module._binding_ids_for_native("codex", "-proj", "thread-abc")
    assert found == (bid, cid)


def test_seq_shared_across_runs_in_one_conversation() -> None:
    """Two runs (a switch mints a new run/binding) writing into one conversation
    share the strictly-increasing seq space."""
    cid, _ = app_module._conv_create_with_binding("claude", "-proj", "sub-1")
    s1 = app_module._conv_append_event(cid, "r1e1", "claude", "user_prompt", {}, run_id="run-A")
    s2 = app_module._conv_append_event(cid, "r2e1", "codex", "user_prompt", {}, run_id="run-B")
    assert [s1, s2] == [1, 2]


def test_append_to_missing_conversation_is_noop() -> None:
    assert app_module._conv_append_event("", "k", "claude", "x", {}) is None


def test_load_conversation_events_ordered_and_filtered() -> None:
    cid, _ = app_module._conv_create_with_binding("claude", "-proj", "sub-1")
    app_module._conv_append_event(cid, "e1", "claude", "user_prompt", {"text": "a"}, raw="a")
    app_module._conv_append_event(
        cid, "e2", "claude", "assistant",
        {"content": [{"type": "text", "text": "b"}]}, raw='{"content":[]}')
    app_module._conv_append_event(
        cid, "e3", "claude", "internal_marker", {"x": 1}, visibility="control")
    evs = app_module._load_conversation_events(cid)
    assert [e["event_type"] for e in evs] == ["user_prompt", "assistant"]  # control dropped
    assert [e["seq"] for e in evs] == [1, 2]
    assert evs[0]["normalized"]["text"] == "a"
    assert evs[0]["raw"] == "a"
    assert len(app_module._load_conversation_events(cid, transcript_only=False)) == 3


# ── Cross-provider switch mechanics (_wire_provider_switch) ───────────────────

def _source_conversation(provider: str, native_id: str, project_key: str = "-sw"):
    # A live-captured source: create (live_complete) then activate the binding
    # with a native id, so _wire_provider_switch's capture_state gate passes.
    cid, bid = app_module._conv_create_with_binding(provider, project_key, "sub-1", "T")
    app_module._conv_activate_binding(bid, native_id)
    app_module._conv_append_event(
        cid, f"{native_id}:u1", provider, "user_prompt",
        {"role": "user", "text": "deploy the thing"}, raw="deploy the thing",
        binding_id=bid)
    if provider == "codex":
        app_module._conv_append_event(
            cid, f"{native_id}:a1", provider, "agentMessage",
            {"type": "agentMessage", "id": "i1"},
            raw=json.dumps({"type": "agentMessage", "text": "deployed ok"}),
            binding_id=bid)
    else:
        app_module._conv_append_event(
            cid, f"{native_id}:a1", provider, "assistant",
            {"role": "assistant", "content": [{"type": "text", "text": "deployed ok"}]},
            raw='{"content":[]}', binding_id=bid)
    return cid, bid


def test_switch_codex_to_claude_forges_resume_session(tmp_path) -> None:
    pk = app_module._sanitize_project_key(tmp_path)
    cid, codex_bid = _source_conversation("codex", "thread-src-1", pk)
    run = app_module.ActiveRun("run-sw-1")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, new_sid = app_module._wire_provider_switch(run, ("codex", "thread-src-1"), "claude", tmp_path)
    assert kind == "claude" and new_sid
    assert run.conversation_id == cid  # joined the SAME conversation
    # Forged transcript exists and carries the prior turns in the user line.
    path = app_module._sessions_dir(tmp_path) / f"{new_sid}.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    content = json.loads(lines[0])["message"]["content"]
    assert "PRIOR CONVERSATION" in content and "deploy the thing" in content
    db = app_module._state_db()
    # A fresh provisional Claude binding is minted for the target provider…
    claude_status = db.execute(
        "SELECT status FROM conversation_binding WHERE conversation_id=? AND provider='claude'",
        (cid,)).fetchone()[0]
    assert claude_status == "provisional"
    # …while the source Codex binding stays active (its thread still exists and
    # is resumable). supersede only retires a PRIOR binding of the TARGET
    # provider, so the partial index still permits one active binding per
    # provider — no double-active within a provider.
    codex_status = db.execute(
        "SELECT status FROM conversation_binding WHERE binding_id=?", (codex_bid,)).fetchone()[0]
    assert codex_status == "active"


def test_switch_claude_to_codex_stashes_handoff(tmp_path) -> None:
    pk = app_module._sanitize_project_key(tmp_path)
    cid, _ = _source_conversation("claude", "sess-src-2", pk)
    run = app_module.ActiveRun("run-sw-2")
    run.owner_sub = "sub-1"
    run.provider = "codex"
    run.project_key = pk
    kind, val = app_module._wire_provider_switch(run, ("claude", "sess-src-2"), "codex", tmp_path)
    assert kind == "codex" and val is None  # codex resumes via injected message
    assert run.conversation_id == cid
    assert run.switch_handoff and "deploy the thing" in run.switch_handoff
    codex_status = app_module._state_db().execute(
        "SELECT status FROM conversation_binding WHERE conversation_id=? AND provider='codex'",
        (cid,)).fetchone()[0]
    assert codex_status == "provisional"


def test_switch_unresolved_source_rejects(tmp_path) -> None:
    run = app_module.ActiveRun("run-sw-3")
    run.provider = "claude"
    run.project_key = "-x"
    kind, reason = app_module._wire_provider_switch(run, ("codex", "nope-not-real"), "claude", tmp_path)
    assert kind == "reject"
    assert run.conversation_id is None


def test_switch_rejects_legacy_partial_source(tmp_path) -> None:
    """Fail closed: a wrapped (legacy_partial) conversation can't be carried
    over — we'd announce history we can't actually back."""
    pk = app_module._sanitize_project_key(tmp_path)
    app_module._conv_ensure_for_native("codex", pk, "thread-legacy", "sub-1", "T")
    run = app_module.ActiveRun("run-sw-lp")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(run, ("codex", "thread-legacy"), "claude", tmp_path)
    assert kind == "reject" and "legacy_partial" in reason
    assert run.conversation_id is None


def test_switch_rejects_project_mismatch(tmp_path) -> None:
    """Never seed a target in a different workspace than the source."""
    _source_conversation("codex", "thread-projA", "-projA")
    run = app_module.ActiveRun("run-sw-pm")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = "-projB"
    kind, reason = app_module._wire_provider_switch(run, ("codex", "thread-projA"), "claude", tmp_path)
    assert kind == "reject" and "different project" in reason


def test_switch_claude_target_injects_when_forge_unsupported(tmp_path) -> None:
    """When forged-resume isn't verified on the CLI version, a Claude-target
    switch falls back to injecting the handoff (no forged file, no context loss)."""
    pk = app_module._sanitize_project_key(tmp_path)
    cid, _ = _source_conversation("codex", "thread-fb", pk)
    run = app_module.ActiveRun("run-sw-fb")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, val = app_module._wire_provider_switch(
        run, ("codex", "thread-fb"), "claude", tmp_path, claude_forge_ok=False)
    assert kind == "claude_text" and val is None
    assert run.conversation_id == cid
    assert run.switch_handoff and "deploy the thing" in run.switch_handoff


def test_forge_capable_probes_once_then_caches(monkeypatch) -> None:
    calls = {"n": 0}

    def _fake_probe() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(app_module, "_run_forge_probe", _fake_probe)
    key = f"{app_module._claude_cli_version()}:{app_module.CLAUDE_FORGE_ADAPTER_VERSION}"
    app_module._FORGE_PROBE_CACHE.pop(key, None)
    r1 = asyncio.run(app_module._claude_forge_capable())
    r2 = asyncio.run(app_module._claude_forge_capable())
    assert r1 is True and r2 is True
    assert calls["n"] == 1  # probed once, then served from cache


# ── Source-run barrier (_stop_source_run_for_switch, blocker #1) ──────────────

class _LiveTask:
    def done(self) -> bool:
        return False


def test_stop_source_run_rejects_when_busy() -> None:
    """A switch must be refused while the source run is mid-turn — otherwise
    two providers would drive one workspace."""
    run = app_module.ActiveRun("src-busy")
    run.owner_sub = "sub-1"
    run.session_id = "native-busy"
    run.conversation_id = "conv-busy"
    run.between_turns = False  # a turn is in flight
    run.task = _LiveTask()
    app_module.ACTIVE_RUNS["src-busy"] = run
    app_module.ACTIVE_RUNS_BY_SESSION["native-busy"] = run
    try:
        with pytest.raises(app_module.HTTPException) as ei:
            asyncio.run(app_module._stop_source_run_for_switch(
                "conv-busy", "native-busy", {"sub": "sub-1"}))
        assert ei.value.status_code == 409
    finally:
        app_module.ACTIVE_RUNS.pop("src-busy", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop("native-busy", None)


def test_stop_source_run_noop_without_live_run() -> None:
    """Reopening an old conversation (no live run) and switching is fine."""
    asyncio.run(app_module._stop_source_run_for_switch("no-conv", "no-such-native", {"sub": "x"}))


# ── Live capture through the real _sdk_message_to_events converter ────────────

def _run_with_conv(run_id: str, provider: str = "claude"):
    cid, bid = app_module._conv_create_with_binding(provider, "-proj", "sub-1")
    run = app_module.ActiveRun(run_id)
    run.provider = provider
    run.conversation_id = cid
    run.binding_id = bid
    return run, cid


def _row(cid: str, source_key: str):
    return app_module._state_db().execute(
        "SELECT event_type, normalized_json, raw_json, original_bytes, truncated "
        "FROM conversation_event WHERE conversation_id=? AND source_key=?",
        (cid, source_key),
    ).fetchone()


def test_converter_captures_untruncated_tool_result() -> None:
    """The decisive Slice 0 guarantee: a 2,000-char tool result driven through
    the REAL converter lands in the canonical log in full — past the 800-char
    live UI slice that runs in the same function."""
    run, cid = _run_with_conv("run-tr")
    big = "Z" * 2000
    msg = app_module.UserMessage(
        content=[app_module.ToolResultBlock(tool_use_id="tu-1", content=big, is_error=False)],
        uuid="u-1",
    )
    app_module._sdk_message_to_events(msg, run)
    row = _row(cid, "claude:tool_result:tu-1")
    assert row is not None
    event_type, _norm, raw_json, original_bytes, truncated = row
    assert event_type == "tool_result"
    assert original_bytes == 2000
    assert len(raw_json) == 2000 and raw_json == big  # untruncated
    assert truncated == 0


def test_converter_assistant_excludes_thinking_from_replay() -> None:
    run, cid = _run_with_conv("run-asst")
    msg = app_module.AssistantMessage(
        content=[
            app_module.ThinkingBlock(thinking="hidden reasoning", signature="s"),
            app_module.TextBlock(text="hello"),
            app_module.ToolUseBlock(id="call-1", name="Bash", input={"command": "ls"}),
        ],
        model="claude-opus-4-8", uuid="a-1", session_id="sess-1",
    )
    app_module._sdk_message_to_events(msg, run)
    row = _row(cid, "claude:assistant:a-1")
    assert row is not None
    normalized = json.loads(row[1])
    types = [b["type"] for b in normalized["content"]]
    assert "thinking" not in types  # hidden reasoning kept out of replayable content
    assert "text" in types and "tool_use" in types
    raw_types = [b["type"] for b in json.loads(row[2])["content"]]
    assert "thinking" in raw_types  # but preserved verbatim in raw


def test_converter_tool_result_dedups_on_reecho() -> None:
    """A resume re-echoes prior tool results; keyed on tool_use_id they must not
    double-write."""
    run, cid = _run_with_conv("run-dup")
    msg = app_module.UserMessage(
        content=[app_module.ToolResultBlock(tool_use_id="tu-dup", content="x", is_error=False)],
        uuid="u-2",
    )
    app_module._sdk_message_to_events(msg, run)
    app_module._sdk_message_to_events(msg, run)
    n = app_module._state_db().execute(
        "SELECT COUNT(*) FROM conversation_event WHERE conversation_id=? AND source_key=?",
        (cid, "claude:tool_result:tu-dup"),
    ).fetchone()[0]
    assert n == 1


def test_converter_no_capture_without_conversation() -> None:
    """A run with no conversation linkage (legacy/unwrapped) must not raise and
    must write nothing."""
    run = app_module.ActiveRun("run-nolink")
    run.provider = "claude"
    before = app_module._state_db().execute(
        "SELECT COUNT(*) FROM conversation_event"
    ).fetchone()[0]
    msg = app_module.UserMessage(
        content=[app_module.ToolResultBlock(tool_use_id="tu-x", content="y", is_error=False)],
        uuid="u-3",
    )
    app_module._sdk_message_to_events(msg, run)
    after = app_module._state_db().execute(
        "SELECT COUNT(*) FROM conversation_event"
    ).fetchone()[0]
    assert before == after
