"""Regression tests for the 2026-08 audit bugfixes.

One test per fixed defect, each asserting the behaviour that was wrong rather
than the shape of the fix. Grouped by the subsystem the bug lived in: child
process environment, SSE replay, canonical-log retention, instance-wide authz,
the Codex app-server pool, and roundtable grounding/verification.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

import app as app_module
import codex_provider
import roundtable.core as core

_IS_WINDOWS = os.name == "nt"


# ─── child process environment ───────────────────────────────────────────

def test_scrubbed_child_env_blanks_session_secret(monkeypatch) -> None:
    """SESSION_SECRET signs the auth cookie, so a child that can read it can
    mint a session for any user. The SDK merges options.env over the inherited
    environment and cannot remove a name, so it must be blanked."""
    monkeypatch.setenv("SESSION_SECRET", "cookie-signing-key")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "idp-secret")
    env = app_module._scrubbed_child_env({"CLAUDE_CONFIG_DIR": "/tmp/home"})
    assert env["SESSION_SECRET"] == ""
    assert env["OIDC_CLIENT_SECRET"] == ""
    # The caller's own entries survive untouched.
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/home"


def test_scrubbed_child_env_omits_absent_names(monkeypatch) -> None:
    """Only names actually set in the parent are included, so the overlay stays
    minimal instead of injecting a blank for everything on the list."""
    monkeypatch.delenv("CLAUDE_WEB_PUSHOVER_TOKEN", raising=False)
    assert "CLAUDE_WEB_PUSHOVER_TOKEN" not in app_module._scrubbed_child_env()


def test_scrubbed_child_env_caller_overrides_scrub(monkeypatch) -> None:
    """An explicit value from the caller wins over the blank."""
    monkeypatch.setenv("SESSION_SECRET", "x")
    env = app_module._scrubbed_child_env({"SESSION_SECRET": "deliberate"})
    assert env["SESSION_SECRET"] == "deliberate"


def test_codex_scrub_list_shared_with_app() -> None:
    """app.py hands its resolved list to codex_provider so the
    CLAUDE_WEB_CHILD_ENV_SCRUB knob covers both providers, not just Claude."""
    assert "SESSION_SECRET" in codex_provider.SCRUB_ENV_NAMES
    assert list(codex_provider.SCRUB_ENV_NAMES) == list(app_module.CHILD_ENV_SCRUB)


# ─── SSE replay / terminal delivery ──────────────────────────────────────

def test_force_terminal_evicts_to_make_room() -> None:
    """A full queue silently dropped the terminal marker, so the SSE consumer
    drained its backlog and then waited forever for events that never came."""
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    q.put_nowait({"type": "a"})
    q.put_nowait({"type": "b"})
    app_module._force_terminal(q, {"type": "_done"})
    drained = [q.get_nowait() for _ in range(q.qsize())]
    assert drained[-1] == {"type": "_done"}


def test_finish_delivers_done_to_a_full_subscriber() -> None:
    """emit_transient fills a slow subscriber's queue by design, so finish()
    must force the terminal rather than best-effort put_nowait it."""
    run = app_module.ActiveRun("run-full-queue")
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait({"type": "partial_text"})
    run.subscribers.add(q)
    run.finish()
    drained = [q.get_nowait() for _ in range(q.qsize())]
    assert {"type": "_done"} in drained


def test_subscribe_overflow_reports_resume_index() -> None:
    """Replay of a run longer than the queue cap always overflows. The marker
    has to name the first event that did not fit, or the client restarts from 0,
    overflows at the same point, and loops forever with a 0ms first retry."""
    run = app_module.ActiveRun("run-overflow")
    for i in range(app_module.MAX_SUBSCRIBER_QUEUE + 50):
        run.events.append({"type": "x", "_idx": i})
    run._next_idx = len(run.events)
    q = run.subscribe(start_index=0)
    drained = [q.get_nowait() for _ in range(q.qsize())]
    overflow = drained[-1]
    assert overflow["type"] == "_overflow"
    assert isinstance(overflow["next_index"], int)
    # Every event before the resume point was delivered: no hole at the head,
    # which is what evicting the oldest to fit the marker used to punch.
    delivered = [e["_idx"] for e in drained[:-1]]
    assert delivered == list(range(overflow["next_index"]))


# ─── canonical conversation log retention ────────────────────────────────

def _seed_conversation(conv_id: str, updated_at: float) -> None:
    db = app_module._state_db()
    db.execute(
        "INSERT OR REPLACE INTO conversation(conversation_id, owner_sub,"
        " project_key, title, last_seq, capture_state, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (conv_id, "sub-1", "proj", "t", 1, "live_complete", updated_at, updated_at),
    )
    db.execute(
        "INSERT OR REPLACE INTO conversation_event(conversation_id, seq,"
        " source_key, provider, event_type, visibility, normalized_json,"
        " created_at) VALUES(?,?,?,?,?,?,?,?)",
        (conv_id, 1, f"{conv_id}-k", "claude", "assistant", "transcript",
         "{}", updated_at),
    )


def _conv_rows(conv_id: str) -> int:
    return app_module._state_db().execute(
        "SELECT COUNT(*) FROM conversation_event WHERE conversation_id=?",
        (conv_id,),
    ).fetchone()[0]


def test_purge_old_conversations_drops_stale_and_keeps_recent() -> None:
    """conversation_event grew without bound: the retention sweep only ever
    covered `events` and `runs`, and this table is an order of magnitude
    larger."""
    now = time.time()
    _seed_conversation("conv-stale", now - app_module.CONVERSATION_RETENTION_SECONDS - 60)
    _seed_conversation("conv-fresh", now)
    app_module._purge_old_conversations(now)
    assert _conv_rows("conv-stale") == 0
    assert _conv_rows("conv-fresh") == 1


def test_purge_old_conversations_spares_a_live_run() -> None:
    """However old its last event, a conversation with a live run must survive
    or that run loses the history a provider switch would replay."""
    now = time.time()
    stale = now - app_module.CONVERSATION_RETENTION_SECONDS - 60
    _seed_conversation("conv-live", stale)
    run = app_module.ActiveRun("run-live-conv")
    run.conversation_id = "conv-live"
    app_module.ACTIVE_RUNS["run-live-conv"] = run
    try:
        app_module._purge_old_conversations(now)
        assert _conv_rows("conv-live") == 1
    finally:
        app_module.ACTIVE_RUNS.pop("run-live-conv", None)


# ─── instance-wide authorization ─────────────────────────────────────────

def test_global_config_admin_open_for_single_operator(monkeypatch) -> None:
    """No admin list and no per-user separation is the single-operator install;
    every signed-in user is the operator."""
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", False)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", set())
    app_module._require_global_config_admin({"email": "anyone@example.com"}, "restart")


def test_global_config_admin_closed_in_per_user_mode(monkeypatch) -> None:
    """The open default must not survive into multi-user: a restart SIGTERMs
    every other tenant's in-flight CLI, so an empty admin list locks it."""
    from fastapi import HTTPException

    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", set())
    with pytest.raises(HTTPException) as exc:
        app_module._require_global_config_admin({"email": "tenant@example.com"}, "restart")
    assert exc.value.status_code == 403


def test_global_config_admin_allows_listed_admin(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PER_USER_SESSIONS", True)
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {"boss@example.com"})
    app_module._require_global_config_admin({"email": "BOSS@example.com"}, "restart")


# ─── Codex app-server pool + protocol ────────────────────────────────────

def test_codex_input_items_carries_images(tmp_path, monkeypatch) -> None:
    """turn/steer built a text-only input array while the UI still echoed the
    attachment count, so mid-turn images vanished silently."""
    monkeypatch.setattr(app_module, "UPLOADS_ROOT", tmp_path)
    blocks = [{
        "source": {
            "type": "base64", "media_type": "image/png",
            # 1x1 transparent PNG.
            "data": (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP"
                "4z8AAAAMBAQDuG6xzAAAAAElFTkSuQmCC"
            ),
        },
    }]
    items = app_module._codex_input_items("hello", blocks, "run-abc")
    assert items[0] == {"type": "text", "text": "hello"}
    assert items[1]["type"] == "localImage"
    # Under uploads/<run_id>/, which _purge_old_uploads already reaps, rather
    # than the shared temp dir where they accumulated forever.
    written = tmp_path / "run-abc"
    assert written.is_dir()
    assert next(written.iterdir()).is_file()


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX file modes don't apply on NTFS")
def test_codex_image_written_mode_600(tmp_path, monkeypatch) -> None:
    """A multi-user host must not be able to read whatever the operator pasted
    into a chat; the old path wrote world-readable files into the shared temp
    dir."""
    monkeypatch.setattr(app_module, "UPLOADS_ROOT", tmp_path)
    blk = [{"source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
    paths = app_module._codex_image_paths(blk, "run-mode")
    assert paths
    assert oct(Path(paths[0]).stat().st_mode & 0o777) == "0o600"


def test_codex_image_paths_no_index_collision(tmp_path, monkeypatch) -> None:
    """A second turn's image 0 used to overwrite the first turn's."""
    monkeypatch.setattr(app_module, "UPLOADS_ROOT", tmp_path)
    blk = [{"source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
    first = app_module._codex_image_paths(blk, "run-same")
    second = app_module._codex_image_paths(blk, "run-same")
    assert first and second and first != second


def test_key_lock_survives_teardown_during_spawn() -> None:
    """close_key used to pop the per-key lock while get() still held it, so the
    next caller built a second Lock, both spawned, and one process ended up
    referenced by nothing."""
    async def scenario() -> None:
        key = "test-key-lock"
        codex_provider.CodexAppServer._instances.pop(key, None)
        codex_provider.CodexAppServer._instance_locks.pop(key, None)
        order: list[str] = []

        async def holder() -> None:
            async with codex_provider.CodexAppServer._key_lock(key):
                order.append("holder-in")
                await asyncio.sleep(0.05)
                order.append("holder-out")

        async def closer() -> None:
            await asyncio.sleep(0.01)
            await codex_provider.CodexAppServer.close_key(key)
            order.append("closer-done")

        await asyncio.gather(holder(), closer())
        # The closer waited for the holder instead of racing past it.
        assert order == ["holder-in", "holder-out", "closer-done"]
        # Refcount hit zero, so the entry was cleaned up: a uuid-unique reader
        # key must not leave a Lock behind forever.
        assert key not in codex_provider.CodexAppServer._instance_locks
        assert key not in codex_provider.CodexAppServer._lock_users

    asyncio.run(scenario())


def test_read_loop_terminates_a_child_it_abandons() -> None:
    """_on_exit dropped the pool entry without terminating, so a reader that
    died with the child still alive orphaned a process that close_key and
    shutdown_all could no longer see."""
    class _FakeProc:
        returncode = None  # still running

        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    inst = codex_provider.CodexAppServer(key="test-on-exit")
    inst.proc = _FakeProc()
    inst._on_exit()
    assert inst.proc.terminated


def test_shutdown_does_not_warn_terminate_twice() -> None:
    """A deliberate shutdown must not be mistaken for an abandoned reader."""
    class _FakeProc:
        returncode = None

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1

    inst = codex_provider.CodexAppServer(key="test-shutdown")
    inst.proc = _FakeProc()
    inst.shutdown()
    inst._on_exit()  # what the cancelled reader task's finally does
    assert inst.proc.terminate_calls == 1


# ─── roundtable grounding + verification ─────────────────────────────────

def test_deny_policy_grants_no_tools(tmp_path) -> None:
    """deny was the WEAKEST setting: the allowed_tools clamp only fired for
    readonly, so a caller-supplied callback left it uncapped, which the SDK
    path reads as the full claude_code preset (Bash, Edit, Write)."""
    tid = core.roundtable_create("deny-policy", participants=["gemini-pro"])["thread_id"]
    core.roundtable_bind_repo(tid, str(tmp_path), permission_policy="deny")
    explicit = core.ToolUseContext(
        permission_callback=lambda *a, **k: "allow",
        working_directory=None,
        allowed_tools=None,
    )
    assert core._effective_tool_context(tid, explicit) is None


def test_readonly_policy_still_clamps(tmp_path) -> None:
    tid = core.roundtable_create("readonly-policy", participants=["gemini-pro"])["thread_id"]
    core.roundtable_bind_repo(tid, str(tmp_path), permission_policy="readonly")
    ctx = core._effective_tool_context(tid)
    assert ctx is not None
    assert set(ctx.allowed_tools) == set(core._READONLY_TOOLS)


def test_compact_keep_last_over_length_compacts_nothing(tmp_path) -> None:
    """keep_last above the message count made len-keep_last negative, which
    Python reads as "all but the last N" — so asking to keep everything
    compacted the HEAD of the thread, irreversibly."""
    tid = core.roundtable_create("compact-guard")["thread_id"]
    for i in range(4):
        core.roundtable_post(tid, f"message {i}")
    # Pin the summariser to a provider the suite always has. The default is
    # claude-opus, and roundtable_compact resolves the participant before it
    # reaches the guard under test, so on a runner with no ANTHROPIC_API_KEY and
    # no claude binary the resolve raised first and the test failed for an
    # unrelated reason. conftest sets a fake GEMINI_API_KEY, and the ValueError
    # below fires before any provider call, so this stays hermetic.
    with pytest.raises(ValueError, match="not worth a summariser turn"):
        core.roundtable_compact(tid, keep_last=10, summarizer="gemini-flash")


def test_read_line_window_reaches_past_the_read_cap(tmp_path) -> None:
    """Verification sliced a 64 KiB-capped read, so any finding cited past the
    cap got an EMPTY excerpt — and a verifier told to judge only from the code
    shown ruled real defects refuted."""
    target = tmp_path / "big.py"
    filler = "x = 1  # padding line\n"
    lines_needed = (core._TOOL_READ_MAX_BYTES // len(filler)) + 200
    body = [filler] * lines_needed
    needle_line = lines_needed  # last line, far past the byte cap
    body[needle_line - 1] = "NEEDLE = 'find me'\n"
    target.write_text("".join(body), encoding="utf-8")

    tools = core._RepoTools(tmp_path, lambda *a, **k: "allow", "tester")
    out = tools.execute("Read", {"path": "big.py", "line": needle_line, "context": 3})
    assert "NEEDLE" in out
    assert f"{needle_line}: " in out
    # The plain capped read cannot see it, which is the bug being fixed.
    assert "NEEDLE" not in tools.execute("Read", {"path": "big.py"})


def test_read_line_window_flags_a_citation_past_end(tmp_path) -> None:
    """An out-of-range citation must say so, not return an empty string that
    reads to the verifier as "no such code"."""
    target = tmp_path / "small.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    tools = core._RepoTools(tmp_path, lambda *a, **k: "allow", "tester")
    out = tools.execute("Read", {"path": "small.py", "line": 900, "context": 5})
    assert out.endswith("does not exist]")


def test_read_line_window_keeps_the_jail(tmp_path) -> None:
    """The window read is a new entry point, so it needs the same jail."""
    (tmp_path / "inside.py").write_text("ok\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    tools = core._RepoTools(tmp_path, lambda *a, **k: "allow", "tester")
    out = tools.execute(
        "Read", {"path": "../outside-secret.txt", "line": 1, "context": 2},
    )
    assert out.startswith("[no such file")


def test_gemini_tool_config_set_when_web_search_and_tools(monkeypatch) -> None:
    """Gemini rejects a built-in tool alongside function declarations unless
    include_server_side_tool_invocations is set — the reported 400."""
    genai_types = pytest.importorskip("google.genai.types")
    captured: dict = {}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            captured["config"] = config
            raise RuntimeError("stop after capture")

    class _FakeClient:
        models = _FakeModels()

    monkeypatch.setattr(core, "_gemini", _FakeClient())
    with pytest.raises(RuntimeError, match="stop after capture"):
        core._call_gemini_with_tools(
            model="gemini-pro-latest",
            system_prompt="sys",
            transcript="msg",
            instruction="",
            effort=None,
            web_search=True,
            tools=core._RepoTools(Path("."), lambda *a, **k: "deny", "t"),
        )
    cfg = captured["config"]
    assert isinstance(cfg["tool_config"], genai_types.ToolConfig)
    assert cfg["tool_config"].include_server_side_tool_invocations is True


def test_synthesis_prompt_names_failed_panellists() -> None:
    """A synthesizer that doesn't know most of the panel died reads the
    survivors as full agreement."""
    out = core.roundtable_coding_synthesis_prompt(
        "review", ["Gemini Pro"], "Claude Fable", False, None,
        {"GPT-5": "ProviderWallTimeout"},
    )
    assert "GPT-5" in out
    assert "ProviderWallTimeout" in out
