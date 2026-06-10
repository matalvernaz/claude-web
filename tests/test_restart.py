"""Drain-restart machinery: busy detection, gating, request/cancel."""

# test_csrf re-imports app with CSRF strict baked in, and module caching can
# leak that into later test files. A matching Origin passes in both modes.
_ORIGIN = {"Origin": "http://testserver"}


class _FakeTask:
    """Stands in for run.task; _busy_runs only consults .done()."""

    def done(self) -> bool:
        return False


def _make_run(app_module, run_id: str, *, between_turns: bool, with_task: bool = True):
    run = app_module.ActiveRun(run_id)
    run.between_turns = between_turns
    if with_task:
        run.task = _FakeTask()
    return run


def test_busy_runs_classification(client):
    import app as app_module

    app_module.ACTIVE_RUNS.clear()
    try:
        app_module.ACTIVE_RUNS["mid-turn"] = _make_run(
            app_module, "mid-turn", between_turns=False)
        app_module.ACTIVE_RUNS["idle"] = _make_run(
            app_module, "idle", between_turns=True)
        app_module.ACTIVE_RUNS["orphan"] = _make_run(
            app_module, "orphan", between_turns=False, with_task=False)

        busy = app_module._busy_runs()
        assert busy == ["mid-turn"]

        # Queued input on an idle run makes it busy: the queue would start
        # a new turn the moment the driver wakes.
        app_module.ACTIVE_RUNS["idle"].user_input_queue.put_nowait({"text": "hi"})
        assert sorted(app_module._busy_runs()) == ["idle", "mid-turn"]
    finally:
        app_module.ACTIVE_RUNS.clear()


def test_assistant_streams_count_as_busy(client):
    import app as app_module

    app_module.ACTIVE_RUNS.clear()
    app_module.ASSISTANT_STREAMS.clear()
    try:
        live = app_module.AssistantStream("s1", "tester")
        finished = app_module.AssistantStream("s2", "tester")
        finished.done = True
        app_module.ASSISTANT_STREAMS.update({"s1": live, "s2": finished})
        assert app_module._busy_runs() == ["roundtable-assistant×1"]
    finally:
        app_module.ASSISTANT_STREAMS.clear()


def test_chat_refused_while_draining(client):
    import app as app_module

    app_module.request_restart("test")
    try:
        r = client.post("/api/chat", data={"message": "hello"}, headers=_ORIGIN)
        assert r.status_code == 503
        assert r.json()["error"] == "restart_pending"

        r = client.post("/api/chat/send/some-run", data={"message": "hello"}, headers=_ORIGIN)
        assert r.status_code == 503
        assert r.json()["error"] == "restart_pending"
    finally:
        app_module.cancel_restart()


def test_request_and_cancel_roundtrip(client):
    import app as app_module

    assert app_module.RESTART_STATE["pending"] is False
    out = app_module.request_restart("test")
    assert out["status"] == "draining"
    assert app_module.RESTART_STATE["pending"] is True
    # Idempotent: a second request doesn't reset requested_at.
    first_at = app_module.RESTART_STATE["requested_at"]
    app_module.request_restart("test-again")
    assert app_module.RESTART_STATE["requested_at"] == first_at

    assert app_module.cancel_restart()["status"] == "cancelled"
    assert app_module.RESTART_STATE["pending"] is False
    assert app_module.cancel_restart()["status"] == "idle"


def test_admin_restart_endpoint(client):
    import app as app_module

    try:
        r = client.post("/api/admin/restart", headers=_ORIGIN)
        assert r.status_code == 200
        assert r.json()["status"] == "draining"
        assert app_module.RESTART_STATE["pending"] is True

        r = client.delete("/api/admin/restart", headers=_ORIGIN)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        assert app_module.RESTART_STATE["pending"] is False
    finally:
        app_module.cancel_restart()
