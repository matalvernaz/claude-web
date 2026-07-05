"""Regression tests for the 2026-06 audit bugfixes.

Covers the backend-side fixes that are unit-testable without a live CLI
subprocess: zombie-run detection, the live-run purge guard, the session-search
newest-first cap, and the multi-user usage filter.
"""
from __future__ import annotations

import json
import time

import app as app_module


def test_existing_run_for_session_ignores_zombie() -> None:
    """A run registered but never given a driver task (api_chat raised between
    registration and create_task) must not be treated as live — otherwise it
    shadows the session forever and queued input is silently lost."""
    run = app_module.ActiveRun("zombie-run")
    run.session_id = "sess-zombie"
    run.task = None  # never spawned
    app_module.ACTIVE_RUNS["zombie-run"] = run
    app_module.ACTIVE_RUNS_BY_SESSION["sess-zombie"] = run
    try:
        assert app_module._existing_run_for_session("sess-zombie") is None
    finally:
        app_module.ACTIVE_RUNS.pop("zombie-run", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop("sess-zombie", None)


def test_gc_runs_evicts_zombie() -> None:
    run = app_module.ActiveRun("zombie-gc")
    run.session_id = "sess-zombie-gc"
    run.task = None
    run.created_at = time.time() - (app_module._ZOMBIE_RUN_GRACE_SECONDS + 5)
    app_module.ACTIVE_RUNS["zombie-gc"] = run
    app_module.ACTIVE_RUNS_BY_SESSION["sess-zombie-gc"] = run
    try:
        app_module._gc_runs()
        assert "zombie-gc" not in app_module.ACTIVE_RUNS
        assert "sess-zombie-gc" not in app_module.ACTIVE_RUNS_BY_SESSION
    finally:
        app_module.ACTIVE_RUNS.pop("zombie-gc", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop("sess-zombie-gc", None)


def test_purge_skips_live_run_events() -> None:
    """A run still live in memory must keep its persisted events even when its
    runs.last_activity has aged past the retention window."""
    db = app_module._state_db()
    rid = "purge-live-run"
    old = time.time() - app_module.PERSIST_RETENTION_SECONDS - 3600
    db.execute(
        "INSERT OR REPLACE INTO runs(run_id, owner_sub, session_id, project_key,"
        " created_at, finished_at, last_activity) VALUES(?,?,?,?,?,?,?)",
        (rid, None, "s", "p", old, None, old),
    )
    db.execute(
        "INSERT OR REPLACE INTO events(run_id, idx, payload) VALUES(?,?,?)",
        (rid, 0, json.dumps({"type": "run_started"})),
    )
    live = app_module.ActiveRun(rid)
    live.done = False
    app_module.ACTIVE_RUNS[rid] = live
    try:
        app_module._purge_old_persisted(time.time())
        rows = db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (rid,)).fetchone()
        assert rows[0] == 1, "live run's events were wrongly purged"
        # Now make it not-live: it should purge.
        app_module.ACTIVE_RUNS.pop(rid, None)
        app_module._purge_old_persisted(time.time())
        rows = db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (rid,)).fetchone()
        assert rows[0] == 0, "stale finished run's events should be purged"
    finally:
        app_module.ACTIVE_RUNS.pop(rid, None)
        db.execute("DELETE FROM events WHERE run_id=?", (rid,))
        db.execute("DELETE FROM runs WHERE run_id=?", (rid,))


def test_session_search_cap_keeps_newest(tmp_path) -> None:
    """The MAX_SEARCH_RESULTS cap must retain the most-recent matches, not the
    first ones the directory glob yielded."""
    cap = app_module.MAX_SEARCH_RESULTS
    candidates = []
    # Build cap+5 transcript files, each matching the query, with descending
    # mtimes so index 0 is newest. _scan_sessions_for_query expects newest-first.
    for i in range(cap + 5):
        p = tmp_path / f"sess{i:03d}.jsonl"
        p.write_text(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "find_me_marker here"},
        }) + "\n", encoding="utf-8")
        candidates.append((10_000 - i, p, "proj"))
    hits = app_module._scan_sessions_for_query("find_me_marker", candidates)
    assert len(hits) == cap
    returned_mtimes = {h["mtime"] for h in hits}
    # The newest `cap` mtimes are 10000..10000-(cap-1).
    expected = {10_000 - i for i in range(cap)}
    assert returned_mtimes == expected


def test_finish_drains_deferred_user_item() -> None:
    """A message held in the one-slot deferred field surfaces a lost_input on
    run finish, just like a queued one — and it carries the queue_id so the
    client can clear the stuck '(sending…)' chip."""
    run = app_module.ActiveRun("finish-deferred")
    run._deferred_user_item = {
        "text": "held message", "delivered": None, "queue_id": "q-held-1",
    }
    run.finish()
    assert run._deferred_user_item is None
    lost = [
        e for e in run.events
        if e.get("type") == "error" and "held message" in (e.get("lost_input") or "")
    ]
    assert lost and lost[0].get("queue_id") == "q-held-1"


def test_pending_prompts_for_run_filters_to_unresolved() -> None:
    """A fresh-page attach must be handed the prompt events still awaiting a
    decision (in PENDING), and only those — resolved or unrelated events are
    excluded so the client re-renders exactly the open cards (H5)."""
    run = app_module.ActiveRun("pending-prompts")
    run.emit({"type": "permission_request", "id": "p-open", "tool": "WebFetch"})
    run.emit({"type": "permission_request", "id": "p-closed", "tool": "Bash"})
    run.emit({"type": "assistant", "id": "not-a-prompt"})
    app_module.PENDING["p-open"] = {"future": None, "run_id": run.run_id}
    app_module.PENDING["other-run"] = {"future": None, "run_id": "someone-else"}
    try:
        out = app_module._pending_prompts_for_run(run)
        ids = [e.get("id") for e in out]
        assert ids == ["p-open"]  # only the one still in PENDING for this run
    finally:
        app_module.PENDING.pop("p-open", None)
        app_module.PENDING.pop("other-run", None)


def test_next_backup_path_never_overwrites(tmp_path) -> None:
    """Numbered backups: a second apply on the same file must not clobber the
    first backup (which holds the true original)."""
    import app as app_module
    f = tmp_path / "foo.py"
    f.write_text("orig", encoding="utf-8")
    b1 = app_module._next_backup_path(f)
    assert b1.name == "foo.py.rt-orig"
    b1.write_bytes(b"first-backup")
    b2 = app_module._next_backup_path(f)
    assert b2.name == "foo.py.rt-orig.2"
    assert b2 != b1
    b2.write_bytes(b"second-backup")
    b3 = app_module._next_backup_path(f)
    assert b3.name == "foo.py.rt-orig.3"


def test_roundtable_usage_totals(tmp_path, monkeypatch) -> None:
    """roundtable_usage aggregates persisted per-turn rows by participant."""
    import importlib
    import roundtable.core as core
    # Point the core DB at a temp file and reset the cached connection.
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "rt.db")
    monkeypatch.setattr(core, "_db", None)
    tid = 4242
    fake = type("R", (), {})()
    fake.usage = {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 5}
    fake.finish_reason = "stop"
    core._log_usage("gemini", fake, thread_id=tid)
    fake2 = type("R", (), {})()
    fake2.usage = {"input_tokens": 50, "output_tokens": 10, "cached_tokens": 0}
    fake2.finish_reason = "stop"
    core._log_usage("gemini", fake2, thread_id=tid)
    fake3 = type("R", (), {})()
    fake3.usage = {"input_tokens": 200, "output_tokens": 40, "cached_tokens": 0}
    fake3.finish_reason = "stop"
    core._log_usage("openai", fake3, thread_id=tid)
    usage = core.roundtable_usage(tid)
    assert usage["total_input_tokens"] == 350
    assert usage["total_output_tokens"] == 70
    gem = next(p for p in usage["by_participant"] if p["participant"] == "gemini")
    assert gem["turns"] == 2 and gem["input_tokens"] == 150


def test_gemini_streaming_emits_and_accumulates(monkeypatch) -> None:
    """on_delta fires per chunk and the ProviderResult carries the full text."""
    import roundtable.core as core

    class _Chunk:
        def __init__(self, text):
            self.text = text
            self.usage_metadata = None

    class _Models:
        def generate_content_stream(self, **kw):
            for t in ["Hel", "lo ", "world"]:
                yield _Chunk(t)

    class _FakeGemini:
        models = _Models()

    monkeypatch.setattr(core, "_gemini", _FakeGemini())
    got = []
    result = core._call_gemini(
        "gemini-pro-latest", "sys", "transcript", "",
        on_delta=lambda t: got.append(t),
    )
    assert got == ["Hel", "lo ", "world"]
    assert result.text == "Hello world"


def test_gemini_non_streaming_unchanged(monkeypatch) -> None:
    """on_delta=None keeps the single-shot generate_content path."""
    import roundtable.core as core

    class _Resp:
        text = "one shot"
        usage_metadata = None

    class _Models:
        def generate_content(self, **kw):
            return _Resp()

    class _FakeGemini:
        models = _Models()

    monkeypatch.setattr(core, "_gemini", _FakeGemini())
    result = core._call_gemini("gemini-pro-latest", "sys", "t", "")
    assert result.text == "one shot"


# ─── Fresh-page sidebar restore of an in-progress session ────────────────────
# A live run is observable to _existing_run_for_session only with a non-done
# task; in a unit test a stand-in object whose .done() is False satisfies the
# liveness check without a running event loop.
import types  # noqa: E402


def _live_task():
    return types.SimpleNamespace(done=lambda: False)


def _make_session_file(sid: str) -> "object":
    """Create a minimal session jsonl under the default project so
    /api/sessions/{sid} resolves a path instead of 404ing. Returns the path."""
    sessions_dir = (
        app_module.CLAUDE_HOME / "projects"
        / app_module._sanitize_project_key(app_module.DEFAULT_CWD)
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{sid}.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    return path


def test_api_session_includes_live_run(client) -> None:
    """A fresh page must be able to discover the session's live run so it can
    attach instead of mis-routing later sends."""
    sid = "11111111-1111-1111-1111-111111111111"
    path = _make_session_file(sid)
    run = app_module.ActiveRun("live-run-1")
    run.session_id = sid
    run.done = False
    run.between_turns = False
    run.task = _live_task()
    for i in range(3):
        run.emit({"type": "marker", "n": i})
    app_module.ACTIVE_RUNS["live-run-1"] = run
    app_module.ACTIVE_RUNS_BY_SESSION[sid] = run
    try:
        data = client.get(f"/api/sessions/{sid}").json()
        assert data["live_run"] is not None
        assert data["live_run"]["run_id"] == "live-run-1"
        assert data["live_run"]["active"] is True
        assert data["live_run"]["between_turns"] is False
        assert data["live_run"]["next_idx"] == run._next_idx
    finally:
        app_module.ACTIVE_RUNS.pop("live-run-1", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop(sid, None)
        path.unlink(missing_ok=True)


def test_api_session_live_run_null_for_zombie(client) -> None:
    """A driver-less run must not be surfaced — the client would attach to a
    run that can never produce events."""
    sid = "22222222-2222-2222-2222-222222222222"
    path = _make_session_file(sid)
    run = app_module.ActiveRun("zombie-live")
    run.session_id = sid
    run.task = None
    app_module.ACTIVE_RUNS["zombie-live"] = run
    app_module.ACTIVE_RUNS_BY_SESSION[sid] = run
    try:
        assert client.get(f"/api/sessions/{sid}").json()["live_run"] is None
    finally:
        app_module.ACTIVE_RUNS.pop("zombie-live", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop(sid, None)
        path.unlink(missing_ok=True)


def test_api_session_live_run_owner_gated(client) -> None:
    """run_id must never leak across users — owner-gated like /api/chat/active.
    The AUTH_MODE=none test user has sub None, so a run owned by someone else
    is hidden."""
    sid = "33333333-3333-3333-3333-333333333333"
    path = _make_session_file(sid)
    run = app_module.ActiveRun("other-owner")
    run.session_id = sid
    run.done = False
    run.task = _live_task()
    run.owner_sub = "someone-else"
    app_module.ACTIVE_RUNS["other-owner"] = run
    app_module.ACTIVE_RUNS_BY_SESSION[sid] = run
    try:
        assert client.get(f"/api/sessions/{sid}").json()["live_run"] is None
    finally:
        app_module.ACTIVE_RUNS.pop("other-owner", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop(sid, None)
        path.unlink(missing_ok=True)


def test_existing_run_recovered_when_index_lost() -> None:
    """If ACTIVE_RUNS_BY_SESSION lost the mapping (e.g. an SDK session_id
    re-index popped the key) but a live run still owns the session jsonl,
    _existing_run_for_session recovers it by scanning ACTIVE_RUNS and self-heals
    the index — so a second CLI is never spawned on one transcript."""
    run = app_module.ActiveRun("recover-run")
    run.session_id = "sess-recover"
    run.done = False
    run.task = _live_task()
    app_module.ACTIVE_RUNS["recover-run"] = run  # NOT registered by session
    try:
        assert app_module._existing_run_for_session("sess-recover") is run
        assert app_module.ACTIVE_RUNS_BY_SESSION.get("sess-recover") is run
    finally:
        app_module.ACTIVE_RUNS.pop("recover-run", None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop("sess-recover", None)


def test_existing_run_scan_skips_finished_and_zombie() -> None:
    """The recovery scan must not resurrect a done or driver-less run — those
    fall through to a fresh resume-from-disk spawn."""
    done = app_module.ActiveRun("scan-done")
    done.session_id = "sess-scan"
    done.done = True
    done.task = types.SimpleNamespace(done=lambda: True)
    zombie = app_module.ActiveRun("scan-zombie")
    zombie.session_id = "sess-scan"
    zombie.task = None
    app_module.ACTIVE_RUNS["scan-done"] = done
    app_module.ACTIVE_RUNS["scan-zombie"] = zombie
    try:
        assert app_module._existing_run_for_session("sess-scan") is None
    finally:
        app_module.ACTIVE_RUNS.pop("scan-done", None)
        app_module.ACTIVE_RUNS.pop("scan-zombie", None)


async def test_inject_user_input_rejects_when_queue_full() -> None:
    """Server-side backlog is bounded — a direct API caller can't grow
    user_input_queue (and its per-item delivery task + Future) without limit."""
    run = app_module.ActiveRun("cap-run")
    run.accepting_input = True
    run.done = False
    for _ in range(app_module.MAX_USER_INPUT_QUEUE):
        run.user_input_queue.put_nowait({"text": "x"})
    ok = await app_module._inject_user_input(
        run, "one too many", [], image_count=0, file_count=0)
    assert ok is False


def test_persist_event_keeps_unserializable_as_placeholder() -> None:
    """A non-serializable event must not be dropped — that would leave a hole in
    the persisted idx sequence. It's stored as a typed placeholder instead."""
    db = app_module._state_db()
    rid = "persist-placeholder"
    try:
        app_module._persist_event(rid, 7, {"type": "weird", "_idx": 7, "obj": object()})
        row = db.execute(
            "SELECT payload FROM events WHERE run_id=? AND idx=7", (rid,)
        ).fetchone()
        assert row is not None, "event was dropped, leaving an idx gap"
        assert json.loads(row[0])["type"] == "_unpersisted"
    finally:
        db.execute("DELETE FROM events WHERE run_id=?", (rid,))


def test_restore_handles_idx_gap_without_collision() -> None:
    """A gap in the persisted idx sequence must not make a restart-synth event
    collide with (and INSERT OR REPLACE overwrite) a real restored event."""
    db = app_module._state_db()
    rid = "restore-gap"
    old = time.time() - 100
    try:
        db.execute(
            "INSERT OR REPLACE INTO runs(run_id, owner_sub, session_id,"
            " project_key, created_at, finished_at, last_activity)"
            " VALUES(?,?,?,?,?,?,?)",
            (rid, None, None, "p", old, None, old),  # finished_at NULL = killed mid-turn
        )
        for i in (0, 1, 3):  # idx 2 missing — a gap
            db.execute(
                "INSERT OR REPLACE INTO events(run_id, idx, payload) VALUES(?,?,?)",
                (rid, i, json.dumps({"type": "marker", "_idx": i})),
            )
        app_module._restore_persisted_runs()
        run = app_module.ACTIVE_RUNS.get(rid)
        assert run is not None
        idxs = [e["_idx"] for e in run.events]
        assert len(idxs) == len(set(idxs)), f"colliding idxs after restore: {idxs}"
        assert run._next_idx > max(idxs)
        assert idxs.count(3) == 1  # the restart synth landed past the gap, not on idx 3
    finally:
        app_module.ACTIVE_RUNS.pop(rid, None)
        db.execute("DELETE FROM events WHERE run_id=?", (rid,))
        db.execute("DELETE FROM runs WHERE run_id=?", (rid,))


def test_setup_gate_honors_admin_emails_without_per_user(monkeypatch) -> None:
    """Shared-slot setup must be admin-gated whenever ADMIN_EMAILS is set, even
    outside PER_USER_SESSIONS — otherwise any signed-in user could rotate the
    shared credential. Empty ADMIN_EMAILS keeps the single-operator default."""
    import pytest
    monkeypatch.setattr(app_module, "ENABLE_SETUP", "true")  # skip the auto-lock gate
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", False)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {"admin@x"})
    with pytest.raises(app_module.HTTPException) as ei:
        app_module._require_setup_access({"email": "user@x", "sub": "u"})
    assert ei.value.status_code == 403
    app_module._require_setup_access({"email": "admin@x", "sub": "a"})  # admin: no raise
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", set())
    app_module._require_setup_access({"email": "anyone@x", "sub": "b"})  # single-operator: no raise


def test_usage_history_payload_groups_by_day(tmp_path, monkeypatch) -> None:
    """Per-day aggregation for /api/usage/history: cost counts only api_key
    (billed) rows; oauth turns count toward turns/tokens but not cost (F3)."""
    log = tmp_path / "usage.jsonl"
    now = int(time.time())
    day = 86400
    rows = [
        {"ts": now, "input_tokens": 10, "output_tokens": 5,
         "total_cost_usd": 0.02, "credential_mode": "api_key"},
        {"ts": now, "input_tokens": 3, "output_tokens": 1,
         "total_cost_usd": 0.99, "credential_mode": "oauth"},  # unbilled
        {"ts": now - day, "input_tokens": 7, "output_tokens": 2,
         "total_cost_usd": 0.05, "credential_mode": "api_key"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(app_module, "USAGE_LOG", log)
    out = app_module._usage_history_payload(None, days=7)
    assert len(out["days"]) == 2  # two distinct days
    today = out["days"][-1]  # sorted ascending, so newest last
    assert today["turns"] == 2 and today["billed_turns"] == 1
    assert today["input_tokens"] == 13  # both rows count toward tokens
    assert abs(today["cost_usd"] - 0.02) < 1e-9  # only the api_key row's cost
    assert out["totals"]["turns"] == 3
    assert out["totals"]["has_billed_usage"] is True
    assert abs(out["totals"]["cost_usd"] - 0.07) < 1e-9
