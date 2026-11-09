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


def test_refused_resume_does_not_claim_a_conversation(client, monkeypatch):
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *args: True)
    session_id = "f52e2932-052b-4b73-a563-f8f7fbfe8344"
    project_key = app_module._sanitize_project_key(app_module.DEFAULT_CWD)
    app_module._claim_session_owner(session_id, "another-user", project_key)
    response = client.post("/api/chat", data={"message": "hello", "session_id": session_id})
    assert response.status_code == 403
    assert app_module._conversation_binding_by_native("claude", session_id) is None


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
    # A Claude binding is minted for the target provider, already active on
    # the forged id: the run claims that id before the CLI starts, so the init
    # hook never sees it as new and can't be the one to activate it.
    claude_row = db.execute(
        "SELECT status, native_session_id FROM conversation_binding "
        "WHERE conversation_id=? AND provider='claude'",
        (cid,)).fetchone()
    assert claude_row == ("active", new_sid)
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
    # The driver injects these as the new thread's history; the text above is
    # only its fallback.
    roles = [item["role"] for item in run.switch_handoff_items]
    assert roles == ["developer", "user", "assistant"]
    assert "deploy the thing" in run.switch_handoff_items[1]["content"][0]["text"]
    assert "deployed ok" in run.switch_handoff_items[2]["content"][0]["text"]
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


# ── Switching back: a chat switched into Claude can go to Codex again ─────────
# 2026-11-06: a chat went Codex→Claude, its Claude binding was never activated
# (the run claims the forged id up front, so the CLI's init repeated it and the
# hook saw nothing new), the next resume wrapped the session in a fresh
# legacy_partial record, and switching it back to Codex was refused with
# "conversation history is legacy_partial; can't carry it over".

def _cli_init(run, sid: str) -> None:
    run.emit({"type": "system", "subtype": "init", "session_id": sid})


def _switch_into_claude(tmp_path, codex_thread: str, run_id: str):
    """A Codex→Claude switch the way api_chat runs it: forge, claim the forged
    id for the run, then the CLI's init reports that same id."""
    pk = app_module._sanitize_project_key(tmp_path)
    cid, _ = _source_conversation("codex", codex_thread, pk)
    run = app_module.ActiveRun(run_id)
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, sid = app_module._wire_provider_switch(
        run, ("codex", codex_thread), "claude", tmp_path)
    assert kind == "claude"
    run.session_id = sid
    _cli_init(run, sid)
    return pk, cid, run, sid


def test_forged_claude_session_resolves_to_its_conversation(tmp_path) -> None:
    pk, cid, _run, sid = _switch_into_claude(tmp_path, "thread-rt-1", "run-rt-1")
    found = app_module._conversation_binding_by_native("claude", sid)
    assert found is not None and found[0] == cid
    # A later run resuming the chat (api_chat's lazy wrap) joins the same
    # conversation instead of starting a partial one.
    assert app_module._conv_ensure_for_native("claude", pk, sid, "sub-1")[0] == cid


def test_chat_switched_into_claude_switches_back_to_codex(tmp_path) -> None:
    pk, cid, _run, sid = _switch_into_claude(tmp_path, "thread-rt-2", "run-rt-2a")
    # Claude's own turn lands in the shared log.
    app_module._conv_append_event(
        cid, f"{sid}:a2", "claude", "assistant",
        {"role": "assistant",
         "content": [{"type": "text", "text": "claude finished the migration"}]},
        raw="{}")
    # Later, a fresh run picks the Claude chat back up, then the user switches.
    app_module._conv_ensure_for_native("claude", pk, sid, "sub-1")
    back = app_module.ActiveRun("run-rt-2b")
    back.owner_sub = "sub-1"
    back.provider = "codex"
    back.project_key = pk
    kind, _ = app_module._wire_provider_switch(back, ("claude", sid), "codex", tmp_path)
    assert kind == "codex"
    assert back.conversation_id == cid
    assert "deploy the thing" in back.switch_handoff
    assert "claude finished the migration" in back.switch_handoff


def test_forged_claude_session_keeps_the_runs_picks(tmp_path) -> None:
    """The same skipped hook dropped the personality and panel picks for the
    forged session, so a later tab resolved the user defaults instead."""
    pk = app_module._sanitize_project_key(tmp_path)
    _source_conversation("codex", "thread-rt-3", pk)
    run = app_module.ActiveRun("run-rt-3", personality_id=4242)
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    run.panel_requested = True
    kind, sid = app_module._wire_provider_switch(
        run, ("codex", "thread-rt-3"), "claude", tmp_path)
    assert kind == "claude"
    run.session_id = sid
    _cli_init(run, sid)
    assert app_module._session_owner(sid) == "sub-1"
    assert app_module._session_personality_id(sid) == 4242
    assert app_module._panel_load(sid).enabled is True


# ── A Claude chat with a partial canonical log carries its own transcript ─────

@pytest.fixture
def project(tmp_path, monkeypatch):
    """tmp_path as a configured project, so its transcripts can be looked up."""
    pk = app_module._sanitize_project_key(tmp_path)
    monkeypatch.setitem(app_module.PROJECT_KEYS, pk, tmp_path)
    return tmp_path, pk


def _write_claude_transcript(tmp_path, sid: str, records: list[dict]) -> None:
    d = app_module._sessions_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


_PARTIAL_CHAT = [
    {"type": "user", "message": {"role": "user", "content": "rename the narrator voices"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Renamed all four."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git commit"}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "1 file changed"},
    ]}},
]


async def test_partial_claude_chat_switches_using_its_transcript(project) -> None:
    tmp_path, pk = project
    sid = "sess-partial-1"
    app_module._conv_ensure_for_native("claude", pk, sid, "sub-1", "T")  # legacy_partial
    _write_claude_transcript(tmp_path, sid, _PARTIAL_CHAT)
    run = app_module.ActiveRun("run-partial-1")
    run.owner_sub = "sub-1"
    run.provider = "codex"
    run.project_key = pk
    transcript = await app_module._switch_source_transcript(("claude", sid), "sub-1")
    kind, _ = app_module._wire_provider_switch(
        run, ("claude", sid), "codex", tmp_path, source_transcript=transcript)
    assert kind == "codex"
    handoff = run.switch_handoff
    assert "USER: rename the narrator voices" in handoff
    assert "ASSISTANT: Renamed all four." in handoff
    assert "[called tool Bash]" in handoff
    assert "1 file changed" in handoff


def _codex_turns(cid: str, thread: str, count: int, *, size: int = 400) -> None:
    for i in range(count):
        app_module._conv_append_event(
            cid, f"{thread}:a{i}", "codex", "agentMessage",
            {"type": "agentMessage", "id": f"{thread}-{i}"},
            raw=json.dumps({"type": "agentMessage",
                            "text": f"codex step {i}: " + "x" * size}))


async def test_split_chat_goes_to_codex_and_back_to_claude(project) -> None:
    """A chat already split into a partial record goes to Codex on its
    transcript, works there, and comes back. Its Codex side has no transcript
    here, but everything since the split was captured live, so once that part
    alone fills the handoff, the missing older turns would be cut anyway."""
    tmp_path, pk = project
    sid = "sess-split-1"
    cid, _ = app_module._conv_ensure_for_native("claude", pk, sid, "sub-1", "T")
    _write_claude_transcript(tmp_path, sid, _PARTIAL_CHAT)
    to_codex = app_module.ActiveRun("run-split-1a")
    to_codex.owner_sub = "sub-1"
    to_codex.provider = "codex"
    to_codex.project_key = pk
    transcript = await app_module._switch_source_transcript(("claude", sid), "sub-1")
    kind, _ = app_module._wire_provider_switch(
        to_codex, ("claude", sid), "codex", tmp_path, source_transcript=transcript)
    assert kind == "codex"
    app_module._conv_activate_binding(to_codex.binding_id, "thread-split-1")
    _codex_turns(cid, "thread-split-1", 40)
    back = app_module.ActiveRun("run-split-1b")
    back.owner_sub = "sub-1"
    back.provider = "claude"
    back.project_key = pk
    kind, new_sid = app_module._wire_provider_switch(
        back, ("codex", "thread-split-1"), "claude", tmp_path)
    assert kind == "claude"
    assert back.conversation_id == cid
    forged = (app_module._sessions_dir(tmp_path) / f"{new_sid}.jsonl").read_text()
    assert "codex step 39" in forged


def test_split_codex_chat_with_little_captured_still_refuses(project) -> None:
    """Too little captured to fill the handoff: the missing turns would have
    been in it, so the switch still refuses rather than carry a gap."""
    tmp_path, pk = project
    cid, _ = app_module._conv_ensure_for_native("codex", pk, "thread-split-2", "sub-1", "T")
    _codex_turns(cid, "thread-split-2", 3)
    run = app_module.ActiveRun("run-split-2")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("codex", "thread-split-2"), "claude", tmp_path)
    assert kind == "reject" and "legacy_partial" in reason


def test_partial_gate_uses_the_targets_budget(project) -> None:
    """The captured part has to overflow the budget of the handoff the target
    actually gets. 20,000 characters overflow Claude's 12,000 but not Codex's
    60,000, where the missing older turns would have been carried."""
    tmp_path, pk = project
    sid = "sess-split-budget"
    cid, _ = app_module._conv_ensure_for_native("claude", pk, sid, "sub-1", "T")
    for i in range(40):
        app_module._conv_append_event(
            cid, f"{sid}:a{i}", "claude", "assistant",
            {"role": "assistant",
             "content": [{"type": "text", "text": f"step {i}: " + "x" * 500}]},
            raw="{}")
    assert app_module._partial_log_events(cid, "legacy_partial", "claude", "claude")
    assert app_module._partial_log_events(cid, "legacy_partial", "claude", "codex") is None
    run = app_module.ActiveRun("run-split-budget")
    run.owner_sub = "sub-1"
    run.provider = "codex"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("claude", sid), "codex", tmp_path)
    assert kind == "reject" and "legacy_partial" in reason


def test_codex_chat_with_a_capture_failure_still_refuses(project) -> None:
    """'incomplete' means a write failed somewhere in the log, so a long
    captured part is no promise the recent turns are whole."""
    tmp_path, pk = project
    cid, bid = app_module._conv_create_with_binding("codex", pk, "sub-1", "T")
    app_module._conv_activate_binding(bid, "thread-holes")
    _codex_turns(cid, "thread-holes", 40)
    app_module._conv_mark_incomplete(cid, "test: a capture write failed")
    run = app_module.ActiveRun("run-holes")
    run.owner_sub = "sub-1"
    run.provider = "claude"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("codex", "thread-holes"), "claude", tmp_path)
    assert kind == "reject" and "incomplete" in reason


async def test_complete_log_is_carried_without_reading_the_transcript(project) -> None:
    """A complete canonical log is the record, even with a transcript on disk."""
    tmp_path, pk = project
    _source_conversation("claude", "sess-complete-1", pk)
    _write_claude_transcript(tmp_path, "sess-complete-1", _PARTIAL_CHAT)
    assert await app_module._switch_source_transcript(
        ("claude", "sess-complete-1"), "sub-1") is None


def test_partial_claude_chat_without_a_transcript_still_refuses(project) -> None:
    tmp_path, pk = project
    app_module._conv_ensure_for_native("claude", pk, "sess-gone", "sub-1", "T")
    run = app_module.ActiveRun("run-partial-gone")
    run.owner_sub = "sub-1"
    run.provider = "codex"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("claude", "sess-gone"), "codex", tmp_path)
    assert kind == "reject" and "legacy_partial" in reason
    assert run.conversation_id is None


async def test_partial_switch_never_reads_another_users_transcript(
    project, monkeypatch,
) -> None:
    """The fallback hands the target exactly what GET /api/sessions/{sid}
    would show this user: nothing, when the session is someone else's. (A
    non-owner's resume attempt records the lazy wrap under their own sub
    before its 403, so the conversation's owner alone doesn't settle it.)"""
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    tmp_path, pk = project
    sid = "sess-partial-other"
    app_module._claim_session_owner(sid, "sub-owner", pk)
    app_module._conv_ensure_for_native("claude", pk, sid, "sub-intruder", "T")
    _write_claude_transcript(tmp_path, sid, _PARTIAL_CHAT)
    assert await app_module._switch_source_transcript(
        ("claude", sid), "sub-intruder") is None
    run = app_module.ActiveRun("run-partial-other")
    run.owner_sub = "sub-intruder"
    run.provider = "codex"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("claude", sid), "codex", tmp_path,
        source_transcript=app_module.session_transcript(sid, pk))
    assert kind == "reject" and "not your conversation" in reason
    assert run.switch_handoff is None


def test_split_chat_wrapped_by_someone_else_stays_the_owners(
    project, monkeypatch,
) -> None:
    """The captured part of a split chat is the owner's own turns, even when
    someone else's refused resume made the wrap and so owns the record."""
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    tmp_path, pk = project
    sid = "sess-wrapped-by-other"
    app_module._claim_session_owner(sid, "sub-owner", pk)
    cid, _ = app_module._conv_ensure_for_native("claude", pk, sid, "sub-intruder", "T")
    for i in range(40):
        app_module._conv_append_event(
            cid, f"{sid}:a{i}", "claude", "assistant",
            {"role": "assistant",
             "content": [{"type": "text", "text": f"owner step {i}: " + "x" * 400}]},
            raw="{}")
    run = app_module.ActiveRun("run-wrapped-other")
    run.owner_sub = "sub-intruder"
    run.provider = "codex"
    run.project_key = pk
    kind, reason = app_module._wire_provider_switch(
        run, ("claude", sid), "codex", tmp_path)
    assert kind == "reject" and "not your conversation" in reason
    assert run.switch_handoff is None


def test_refused_switch_leaves_the_source_run_alone(client, monkeypatch) -> None:
    """A refusal is decided before the source chat's run is stopped. Before,
    the barrier stopped it first, and the refusal came after."""
    monkeypatch.setattr(app_module, "PROVIDER_SWITCH_ENABLED", True)
    monkeypatch.setattr(
        app_module.codex_provider, "availability",
        lambda *args, **kwargs: {"available": True, "reason": None})

    class _Server:
        async def models(self):
            return []

    async def _server_for_account(account, server_key=None):
        return _Server()

    stopped: list[str] = []

    async def _fake_supersede(run, reason):
        stopped.append(run.run_id)

    monkeypatch.setattr(app_module, "_codex_server_for_account", _server_for_account)
    monkeypatch.setattr(app_module, "_supersede_run_for_switch", _fake_supersede)
    # The source chat lives in another project, so the switch is refused.
    _source_conversation("claude", "sess-refused", "-another-project")
    source = app_module.ActiveRun("run-refused-src", owner_sub="anonymous")
    source.provider = "claude"
    source.session_id = "sess-refused"
    source.task = _LiveTask()
    source.between_turns = True  # idle: the barrier would stop it, not refuse
    app_module.ACTIVE_RUNS[source.run_id] = source
    app_module.ACTIVE_RUNS_BY_SESSION["sess-refused"] = source
    try:
        r = client.post("/api/chat", data={
            "message": "carry on", "provider": "codex", "session_id": "sess-refused",
        })
        assert r.status_code == 409
        assert "different project" in r.text
        assert stopped == []
    finally:
        app_module.ACTIVE_RUNS.pop(source.run_id, None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop("sess-refused", None)


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


@pytest.mark.parametrize(("model", "reasoning"), [
    # The CLI asks Claude for thinking with its content omitted, so real
    # reasoning arrives as an empty, signed block. A Claude thinking block
    # with words in it is a message to the reader instead (see
    # test_replies_filed_as_thinking.py).
    ("claude-opus-4-8", ""),
    # A Local model's thinking block holds its full reasoning.
    ("qwen3.5:35b-32k", "hidden reasoning"),
])
def test_converter_assistant_excludes_thinking_from_replay(model: str, reasoning: str) -> None:
    run, cid = _run_with_conv("run-asst")
    msg = app_module.AssistantMessage(
        content=[
            app_module.ThinkingBlock(thinking=reasoning, signature="s"),
            app_module.TextBlock(text="hello"),
            app_module.ToolUseBlock(id="call-1", name="Bash", input={"command": "ls"}),
        ],
        model=model, uuid="a-1", session_id="sess-1",
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
