"""SSE reconnect resume + stale-prompt suppression.

Covers the fix for WebFetch permission prompts re-looping on reconnect:
- subscribe(start_index=N) replays only events at idx >= N, so the frontend can
  resume from its high-watermark+1 instead of replaying the whole run from 0;
- the stream() guard drops a replayed prompt whose request id is no longer in
  PENDING (already answered), while still delivering one that is pending;
- the /api/chat/stream endpoint validates start_index.
"""
from __future__ import annotations

import asyncio

import app as app_module


def _drain(q) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def test_subscribe_replays_from_start_index() -> None:
    run = app_module.ActiveRun("resume-startidx")
    for i in range(5):
        run.emit({"type": "marker", "n": i})
    run.done = True  # so subscribe primes backlog + _done and doesn't tail live
    events = _drain(run.subscribe(start_index=2))
    idxs = [e["_idx"] for e in events if e.get("type") == "marker"]
    assert idxs == [2, 3, 4]
    assert events[-1].get("type") == "_done"


async def test_subscribe_from_zero_replays_everything() -> None:
    run = app_module.ActiveRun("resume-zero")
    for i in range(3):
        run.emit({"type": "marker", "n": i})
    run.done = True
    markers = [e["_idx"] for e in _drain(run.subscribe(start_index=0)) if e.get("type") == "marker"]
    assert markers == [0, 1, 2]


async def test_stream_drops_resolved_prompt_keeps_pending() -> None:
    """On replay, a permission_request whose id is gone from PENDING (already
    answered) is suppressed; one still in PENDING is delivered so it can be
    answered. Non-prompt events pass through untouched."""
    run = app_module.ActiveRun("resume-guard")
    run.emit({"type": "permission_request", "id": "req-stale", "tool": "WebFetch",
              "input": {"url": "https://stale.example/"}})
    run.emit({"type": "permission_request", "id": "req-live", "tool": "WebFetch",
              "input": {"url": "https://live.example/"}})
    run.emit({"type": "marker", "ok": True})
    run.done = True
    app_module.PENDING["req-live"] = {"future": None, "owner_sub": None, "run_id": run.run_id}
    try:
        resp = app_module._stream_run_response(run, start_index=0)
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c if isinstance(c, (bytes, bytearray)) else c.encode())
        text = b"".join(chunks).decode()
    finally:
        app_module.PENDING.pop("req-live", None)
    assert "req-stale" not in text  # suppressed: id not in PENDING
    assert "req-live" in text       # delivered: still pending
    assert "marker" in text         # non-prompt event untouched


def test_stream_endpoint_rejects_negative_start_index(client) -> None:
    assert client.get("/api/chat/stream/whatever?start_index=-1").status_code == 422


def test_stream_endpoint_unknown_run_is_404(client) -> None:
    """A valid start_index on a missing run still 404s — the new param doesn't
    short-circuit the normal not-found path."""
    assert client.get("/api/chat/stream/no-such-run?start_index=0").status_code == 404


async def test_subscribe_tail_replays_only_new_durable_events() -> None:
    """Tail-attach (start_index=_next_idx) replays nothing already on disk yet
    still delivers the in-flight turn's final whole-message event (idx >= tail).
    This is the guarantee the fresh-page restore attach relies on to avoid
    double-rendering the disk transcript while still showing the live turn."""
    run = app_module.ActiveRun("resume-tail")
    for i in range(4):
        run.emit({"type": "marker", "n": i})
    tail = run._next_idx
    run.emit({"type": "assistant_final", "n": 99})  # in-flight turn completes
    run.done = True
    events = _drain(run.subscribe(start_index=tail))
    kinds = [e.get("type") for e in events if e.get("type") != "_done"]
    assert kinds == ["assistant_final"]
    assert events[-1].get("type") == "_done"


async def test_stream_response_emits_head_event_first() -> None:
    """The /api/chat reuse path passes head_event so a tail-attached client
    (start_index past the original run_started) still learns the run_id."""
    run = app_module.ActiveRun("head-evt")
    for i in range(3):
        run.emit({"type": "marker", "n": i})
    tail = run._next_idx
    run.done = True
    head = {"type": "run_started", "run_id": run.run_id, "resumed": True}
    resp = app_module._stream_run_response(run, start_index=tail, head_event=head)
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c if isinstance(c, (bytes, bytearray)) else c.encode())
    text = b"".join(chunks).decode()
    first_frame = text.split("\n\n", 1)[0]
    assert "run_started" in first_frame
    assert run.run_id in first_frame
    # No marker replays at the tail; the head frame is purely the announce.
    assert "marker" not in first_frame
