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
    run finish, just like a queued one."""
    run = app_module.ActiveRun("finish-deferred")
    run._deferred_user_item = {"text": "held message", "delivered": None}
    run.finish()
    assert run._deferred_user_item is None
    assert any(
        e.get("type") == "error" and "held message" in (e.get("lost_input") or "")
        for e in run.events
    )


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
