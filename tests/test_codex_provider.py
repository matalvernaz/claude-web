"""Codex provider: event translation, availability probe, session registry,
and the api_chat provider dispatch guards.

The live app-server protocol is exercised manually (it needs the codex
binary + OpenAI auth); these tests cover everything on our side of the
wire: notification→SSE translation, the JSON-RPC client's dispatch
plumbing, and the route-level validation that keeps the two providers'
sessions from cross-contaminating.
"""
import asyncio
import os
import shutil
from pathlib import Path

import pytest

import codex_provider


# ─── item_events translation ──────────────────────────────────────────────────

CAP = 800


def test_frontend_sends_permission_mode_for_codex():
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text()
    send_one = source.index("async function sendOne")
    start = source.index("const provider = currentProvider();", send_one)
    end = source.index("if (effortSelect", start)
    form_block = source[start:end]

    assert 'provider === "claude"' not in form_block
    assert "providerCapabilities(provider).permission_modes" in form_block
    assert 'fd.append("permission_mode", permModeSelect.value)' in form_block


def test_agent_message_completed_becomes_assistant_text():
    evs = codex_provider.item_events(
        {"type": "agentMessage", "id": "m1", "text": "hello"},
        completed=True, session_id="t1", preview_cap=CAP,
    )
    assert evs == [{
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
        "session_id": "t1",
    }]


def test_agent_message_started_emits_nothing():
    assert codex_provider.item_events(
        {"type": "agentMessage", "id": "m1", "text": ""},
        completed=False, session_id="t1", preview_cap=CAP,
    ) == []


def test_reasoning_completed_becomes_thinking_block():
    evs = codex_provider.item_events(
        {"type": "reasoning", "id": "r1", "text": "pondering"},
        completed=True, session_id="t1", preview_cap=CAP,
    )
    assert evs[0]["message"]["content"][0] == {
        "type": "thinking", "text": "pondering",
    }


def test_command_execution_lifecycle():
    started = codex_provider.item_events(
        {"type": "commandExecution", "id": "c1", "command": "ls -la",
         "cwd": "/tmp"},
        completed=False, session_id="t1", preview_cap=CAP,
    )
    blk = started[0]["message"]["content"][0]
    assert blk["type"] == "tool_use"
    assert blk["name"] == "Bash"
    assert blk["id"] == "c1"
    assert blk["input"]["command"] == "ls -la"

    done = codex_provider.item_events(
        {"type": "commandExecution", "id": "c1", "command": "ls -la",
         "aggregatedOutput": "file.txt", "exitCode": 0, "status": "completed"},
        completed=True, session_id="t1", preview_cap=CAP,
    )
    res = done[0]["message"]["content"][0]
    assert res["type"] == "tool_result"
    assert res["tool_use_id"] == "c1"
    assert res["is_error"] is False
    assert "file.txt" in res["content"]


def test_command_execution_failure_maps_exit_code():
    done = codex_provider.item_events(
        {"type": "commandExecution", "id": "c2", "command": "false",
         "aggregatedOutput": "", "exitCode": 1, "status": "failed"},
        completed=True, session_id="t1", preview_cap=CAP,
    )
    res = done[0]["message"]["content"][0]
    assert res["is_error"] is True
    assert "exit code 1" in res["content"]


def test_command_argv_list_joined():
    started = codex_provider.item_events(
        {"type": "commandExecution", "id": "c3", "command": ["git", "status"]},
        completed=False, session_id=None, preview_cap=CAP,
    )
    assert started[0]["message"]["content"][0]["input"]["command"] == "git status"


def test_file_change_uses_apply_patch_tool():
    item = {"type": "fileChange", "id": "f1",
            "changes": [{"path": "a.py", "kind": "update"},
                        {"path": "b.py", "kind": "add"}]}
    started = codex_provider.item_events(
        item, completed=False, session_id=None, preview_cap=CAP)
    blk = started[0]["message"]["content"][0]
    assert blk["name"] == "ApplyPatch"
    assert blk["input"]["files"] == ["a.py", "b.py"]
    done = codex_provider.item_events(
        dict(item, status="completed"), completed=True,
        session_id=None, preview_cap=CAP)
    assert done[0]["message"]["content"][0]["is_error"] is False


def test_patch_input_accepts_dict_changes():
    assert codex_provider.patch_input(
        {"changes": {"x.py": {"kind": "update"}}}
    ) == {"files": ["x.py"]}


def test_todo_list_maps_to_todos_update():
    evs = codex_provider.item_events(
        {"type": "todoList", "id": "td1",
         "items": [{"text": "step one", "completed": True},
                   {"text": "step two", "completed": False}]},
        completed=True, session_id=None, preview_cap=CAP,
    )
    assert evs == [{"type": "todos_update", "todos": [
        {"content": "step one", "activeForm": "step one", "status": "completed"},
        {"content": "step two", "activeForm": "step two", "status": "pending"},
    ]}]


def test_error_item_and_user_message_echo():
    assert codex_provider.item_events(
        {"type": "error", "id": "e1", "message": "boom"},
        completed=True, session_id=None, preview_cap=CAP,
    ) == [{"type": "error", "message": "boom"}]
    assert codex_provider.item_events(
        {"type": "userMessage", "id": "u1", "content": []},
        completed=True, session_id=None, preview_cap=CAP,
    ) == []


def test_usage_tokens_flattens_total_bucket():
    assert codex_provider.usage_tokens({"total": {
        "inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 7,
    }}) == {
        "input_tokens": 100, "output_tokens": 7,
        "cache_read_input_tokens": 40, "cache_creation_input_tokens": None,
    }
    assert codex_provider.usage_tokens({})["input_tokens"] is None


def test_thread_transcript_roles():
    thread = {"turns": [{"items": [
        {"type": "userMessage", "id": "u1",
         "content": [{"type": "text", "text": "do the thing"}]},
        {"type": "commandExecution", "id": "c1", "command": "make",
         "aggregatedOutput": "ok", "exitCode": 0},
        {"type": "agentMessage", "id": "m1", "text": "done"},
    ]}]}
    msgs = codex_provider.thread_transcript(thread, CAP)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "tool_use", "tool_result", "assistant"]
    assert msgs[0]["text"] == "do the thing"
    assert msgs[1]["name"] == "Bash"
    assert msgs[-1]["text"] == "done"


# ─── availability probe ───────────────────────────────────────────────────────


def test_availability_no_binary(monkeypatch):
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: None)
    a = codex_provider.availability()
    assert a["available"] is False
    assert "not installed" in a["reason"]


def test_availability_binary_but_no_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/true")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = codex_provider.availability()
    assert a["available"] is False
    assert "not signed in" in a["reason"]


def test_availability_via_env_key(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/true")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert codex_provider.availability()["available"] is True


def test_availability_via_auth_json(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/true")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "auth.json").write_text("{}")
    assert codex_provider.availability()["available"] is True


def test_personal_availability_does_not_inherit_service_api_key(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    assert codex_provider.availability(tmp_path)["available"] is True
    assert codex_provider.availability(
        tmp_path, allow_env_key=False,
    )["available"] is False


# ─── JSON-RPC dispatch plumbing ───────────────────────────────────────────────


async def test_dispatch_resolves_response_futures():
    server = codex_provider.CodexAppServer()
    fut = asyncio.get_running_loop().create_future()
    server._pending[7] = fut
    server._dispatch({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    assert (await fut) == {"ok": True}


async def test_dispatch_routes_error_responses():
    server = codex_provider.CodexAppServer()
    fut = asyncio.get_running_loop().create_future()
    server._pending[8] = fut
    server._dispatch({"jsonrpc": "2.0", "id": 8,
                      "error": {"code": -1, "message": "nope"}})
    with pytest.raises(codex_provider.CodexRPCError, match="nope"):
        await fut


async def test_dispatch_routes_thread_notifications():
    server = codex_provider.CodexAppServer()
    q = server.subscribe("t1")
    server._dispatch({"jsonrpc": "2.0", "method": "turn/started",
                      "params": {"threadId": "t1", "turn": {"id": "x"}}})
    got = q.get_nowait()
    assert got["method"] == "turn/started"
    # Unsubscribed thread → dropped, no error.
    server._dispatch({"jsonrpc": "2.0", "method": "turn/started",
                      "params": {"threadId": "t-unknown"}})
    server.unsubscribe("t1")
    assert "t1" not in server._thread_queues


def test_dispatch_records_device_login_completion():
    server = codex_provider.CodexAppServer()
    server._dispatch({
        "jsonrpc": "2.0",
        "method": "account/login/completed",
        "params": {"loginId": "login-1", "success": True},
    })
    assert server.login_result("login-1") == {
        "loginId": "login-1", "success": True,
    }


async def test_app_server_pool_is_keyed_and_rejects_setting_mismatch(
    monkeypatch, tmp_path,
):
    class _Proc:
        returncode = None

        def terminate(self):
            self.returncode = -15

    async def _fake_start(self):
        self.proc = _Proc()

    codex_provider.CodexAppServer.shutdown_all()
    monkeypatch.setattr(codex_provider.CodexAppServer, "_start", _fake_start)
    try:
        home_a = tmp_path / "account-a"
        first = await codex_provider.CodexAppServer.get(
            "account:a", home=home_a, isolated_auth=True,
        )
        again = await codex_provider.CodexAppServer.get(
            "account:a", home=home_a, isolated_auth=True,
        )
        second = await codex_provider.CodexAppServer.get(
            "account:b", home=tmp_path / "account-b", isolated_auth=True,
        )

        assert again is first
        assert second is not first
        with pytest.raises(codex_provider.CodexError, match="different settings"):
            await codex_provider.CodexAppServer.get(
                "account:a", home=tmp_path / "wrong-home", isolated_auth=True,
            )
    finally:
        codex_provider.CodexAppServer.shutdown_all()


async def test_isolated_server_forces_file_chatgpt_auth_and_strips_keys(
    monkeypatch, tmp_path,
):
    captured = {}
    stopped = asyncio.Event()

    class _Stream:
        async def readline(self):
            await stopped.wait()
            return b""

    class _Stdin:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

    class _Proc:
        def __init__(self):
            self.stdin = _Stdin()
            self.stdout = _Stream()
            self.stderr = _Stream()
            self.returncode = None
            self.pid = 1234

        def terminate(self):
            self.returncode = -15
            stopped.set()

    proc = _Proc()

    async def _fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return proc

    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/codex")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    monkeypatch.setenv("CODEX_API_KEY", "codex-shared")
    home = tmp_path / "slot-home"
    server = codex_provider.CodexAppServer(
        key="slot-test", home=home, isolated_auth=True,
    )

    async def _fake_request(method, params=None, timeout=None):
        assert method == "initialize"
        return {}

    server.request = _fake_request
    try:
        await server._start()
        assert captured["args"] == (
            "/bin/codex",
            "-c", 'cli_auth_credentials_store="file"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_provider="openai"',
            "app-server",
        )
        assert captured["env"]["CODEX_HOME"] == str(home)
        assert "OPENAI_API_KEY" not in captured["env"]
        assert "CODEX_API_KEY" not in captured["env"]
    finally:
        server.shutdown()
        await asyncio.gather(
            server._reader_task, server._stderr_task, return_exceptions=True,
        )


async def test_server_request_with_no_handler_declines():
    server = codex_provider.CodexAppServer()
    sent = []
    server._send = lambda obj: sent.append(obj)
    server._dispatch({"jsonrpc": "2.0", "id": 42,
                      "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": "t9", "command": "rm -rf /"}})
    await asyncio.sleep(0)  # let the answer task run
    assert sent == [{"jsonrpc": "2.0", "id": 42,
                     "result": {"decision": "decline"}}]


async def test_server_request_routes_to_thread_handler():
    server = codex_provider.CodexAppServer()
    sent = []
    server._send = lambda obj: sent.append(obj)
    seen = {}

    async def handler(method, params):
        seen["method"] = method
        seen["command"] = params.get("command")
        return {"decision": "accept"}

    server.set_request_handler("t1", handler)
    server._dispatch({"jsonrpc": "2.0", "id": 1,
                      "method": "item/commandExecution/requestApproval",
                      "params": {"threadId": "t1", "command": "ls"}})
    await asyncio.sleep(0)
    assert seen == {"method": "item/commandExecution/requestApproval",
                    "command": "ls"}
    assert sent[0]["result"] == {"decision": "accept"}


# ─── Session registry + route guards ──────────────────────────────────────────


def test_codex_session_registry_and_sidebar_merge(client):
    import app as app_module

    app_module._record_codex_session("t-abc", None, "", "Try codex", "gpt-x")
    row = app_module._codex_session_row("t-abc")
    assert row["title"] == "Try codex"
    assert row["model"] == "gpt-x"
    # Re-record with empty title/model — COALESCE keeps the originals.
    app_module._record_codex_session("t-abc", None, "", "", None)
    row = app_module._codex_session_row("t-abc")
    assert row["title"] == "Try codex"
    assert row["model"] == "gpt-x"

    r = client.get("/api/sessions")
    assert r.status_code == 200
    entries = {s["id"]: s for s in r.json()}
    assert entries["t-abc"]["provider"] == "codex"
    assert entries["t-abc"]["title"] == "Try codex"


def test_api_chat_rejects_unknown_provider(client):
    r = client.post("/api/chat", data={"message": "hi", "provider": "gemini"})
    assert r.status_code == 400


def test_api_chat_codex_unavailable_503(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": False, "reason": "codex CLI not installed"},
    )
    r = client.post("/api/chat", data={"message": "hi", "provider": "codex"})
    assert r.status_code == 503
    assert r.json()["error"] == "codex_not_configured"


def test_api_chat_codex_rejects_plan_and_fork(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": True, "reason": None},
    )
    for mode in ("plan", "dontAsk", "auto"):
        r = client.post("/api/chat", data={
            "message": "hi", "provider": "codex", "permission_mode": mode,
        })
        assert r.status_code == 400
        assert f"{mode} mode" in r.text
    r = client.post("/api/chat", data={
        "message": "hi", "provider": "codex", "fork": "true",
    })
    assert r.status_code == 400
    assert "fork" in r.text.lower()


def test_api_chat_provider_session_mismatch(client, monkeypatch):
    import app as app_module

    app_module._record_codex_session("t-mismatch", None, "", "x", None)
    # Claude explicitly requested for a codex-registered session id.
    r = client.post("/api/chat", data={
        "message": "hi", "provider": "claude", "session_id": "t-mismatch",
    })
    assert r.status_code == 400
    # Codex requested for a session id with no codex registry row.
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": True, "reason": None},
    )
    r = client.post("/api/chat", data={
        "message": "hi", "provider": "codex", "session_id": "0" * 32,
    })
    assert r.status_code == 400


def test_api_providers_payload(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": False, "reason": "codex CLI not installed"},
    )
    r = client.get("/api/providers")
    assert r.status_code == 200
    provs = {p["key"]: p for p in r.json()["providers"]}
    assert provs["claude"]["available"] is True
    assert provs["claude"]["models"]
    assert provs["claude"]["capabilities"]["plan_mode"] is True
    # Usage dialog is Claude-only; codex hides it (capability drives the UI).
    assert provs["claude"]["capabilities"]["usage"] is True
    assert provs["codex"]["available"] is False
    assert provs["codex"]["capabilities"]["plan_mode"] is False
    assert provs["codex"]["capabilities"]["usage"] is False
    assert provs["codex"]["capabilities"]["accounts"] is True
    assert provs["codex"]["accounts"]["shared_label"]
    # The frontend hides selector options not in this list.
    assert provs["codex"]["capabilities"]["permission_modes"] == [
        "default", "acceptEdits", "bypassPermissions",
    ]


async def test_codex_gate_decision_maps_to_wire_values(client, monkeypatch):
    """The codex approval gate offers allow-session (unlike Bash on the
    Claude path) and answers in codex's vocabulary."""
    import app as app_module

    run = app_module.ActiveRun("run-gate")
    captured = {}

    async def fake_await(run_, tool, tool_input, sig, allow_session_supported):
        captured["allow_session_supported"] = allow_session_supported
        captured["tool"] = tool
        return captured.pop("_inject")

    monkeypatch.setattr(app_module, "_await_permission_decision", fake_await)

    captured["_inject"] = "allow"
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "ls"}) == "accept"
    # Bash is coarse-signature on the Claude path, but codex still offers
    # session approval (codex owns the matching).
    assert captured["allow_session_supported"] is True

    captured["_inject"] = "allow_session"
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "ls"}) == "acceptForSession"

    captured["_inject"] = "deny"
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "ls"}) == "decline"

    # No-answer timeout (helper returns None) declines.
    captured["_inject"] = None
    assert await app_module._codex_gate_decision(
        run, "ApplyPatch", {"files": ["a.py"]}) == "decline"


async def test_codex_gate_decision_declines_while_interrupting(client):
    import app as app_module

    run = app_module.ActiveRun("run-int")
    run.interrupting = True
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "rm -rf /"}) == "decline"


def test_approval_policy_for_mode():
    assert (codex_provider.approval_policy_for_mode("default")
            == codex_provider.APPROVAL_POLICY)
    assert (codex_provider.approval_policy_for_mode("acceptEdits")
            == codex_provider.APPROVAL_POLICY)
    assert codex_provider.approval_policy_for_mode("bypassPermissions") == "never"


async def test_codex_gate_decision_mode_short_circuits(client, monkeypatch):
    """bypassPermissions accepts everything and acceptEdits accepts patches
    without emitting a permission card — mirrors the Claude SDK's mode
    short-circuits that run before can_use_tool."""
    import app as app_module

    async def no_card(*a, **k):
        raise AssertionError("permission card emitted despite mode short-circuit")

    monkeypatch.setattr(app_module, "_await_permission_decision", no_card)

    run = app_module.ActiveRun("run-bypass")
    run.permission_mode = "bypassPermissions"
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "make deploy"}) == "accept"
    assert await app_module._codex_gate_decision(
        run, "ApplyPatch", {"files": ["a.py"]}) == "accept"

    run = app_module.ActiveRun("run-edits")
    run.permission_mode = "acceptEdits"
    assert await app_module._codex_gate_decision(
        run, "ApplyPatch", {"files": ["a.py"]}) == "accept"


async def test_codex_gate_accept_edits_still_gates_commands(client, monkeypatch):
    import app as app_module

    async def fake_await(run_, tool, tool_input, sig, allow_session_supported):
        return "deny"

    monkeypatch.setattr(app_module, "_await_permission_decision", fake_await)
    run = app_module.ActiveRun("run-edits-cmd")
    run.permission_mode = "acceptEdits"
    assert await app_module._codex_gate_decision(
        run, "Bash", {"command": "make deploy"}) == "decline"


def test_api_chat_permission_mode_codex(client):
    """Codex runs accept the supported subset (no client call to make) and
    reject Claude-only modes."""
    import types

    import app as app_module

    run = app_module.ActiveRun("run-pm")
    run.provider = "codex"
    run.session_id = "t-pm"
    run.client = object()
    run.task = types.SimpleNamespace(done=lambda: False)
    app_module.ACTIVE_RUNS_BY_SESSION["t-pm"] = run
    try:
        r = client.post("/api/chat/permission-mode", data={
            "session_id": "t-pm", "mode": "bypassPermissions",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True, "mode": "bypassPermissions"}
        assert run.permission_mode == "bypassPermissions"
        r = client.post("/api/chat/permission-mode", data={
            "session_id": "t-pm", "mode": "plan",
        })
        assert r.status_code == 400
        assert run.permission_mode == "bypassPermissions"
    finally:
        app_module.ACTIVE_RUNS_BY_SESSION.pop("t-pm", None)


def test_codex_result_event_shapes(client):
    import app as app_module

    run = app_module.ActiveRun("run-x")
    run.session_id = "t-x"
    run.codex_token_usage = {"total": {"inputTokens": 5, "outputTokens": 2}}
    ok = app_module._codex_result_event(run, interrupted=False, failed=False)
    assert ok["subtype"] == "success"
    assert ok["is_error"] is False
    assert ok["input_tokens"] == 5
    stopped = app_module._codex_result_event(run, interrupted=True, failed=False)
    assert stopped["subtype"] == "interrupted"
    assert stopped["is_error"] is False
    failed = app_module._codex_result_event(
        run, interrupted=False, failed=True, error_message="rate limited")
    assert failed["subtype"] == "error_during_execution"
    assert failed["is_error"] is True
    assert failed["errors"] == ["rate limited"]


def test_codex_session_delete_removes_registry_row(client, monkeypatch):
    import app as app_module

    app_module._record_codex_session("t-del", None, "", "bye", None)

    class _FakeServer:
        async def request(self, method, params=None, timeout=None):
            assert method == "thread/delete"
            return {}

    async def _fake_get(*args, **kwargs):
        return _FakeServer()

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    r = client.delete("/api/sessions/t-del")
    assert r.status_code == 200
    assert app_module._codex_session_row("t-del") is None


def test_codex_session_history_payload(client, monkeypatch):
    import app as app_module

    app_module._record_codex_session("t-hist", None, "", "history", None)
    opened_keys = []
    closed_keys = []

    class _FakeServer:
        async def request(self, method, params=None, timeout=None):
            assert method == "thread/read"
            assert params["threadId"] == "t-hist"
            return {"thread": {"turns": [{"items": [
                {"type": "userMessage", "id": "u1",
                 "content": [{"type": "text", "text": "hey codex"}]},
                {"type": "agentMessage", "id": "m1", "text": "hey matt"},
            ]}]}}

    async def _fake_get(key, **kwargs):
        opened_keys.append(key)
        return _FakeServer()

    async def _fake_close_key(key):
        closed_keys.append(key)

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    monkeypatch.setattr(
        codex_provider.CodexAppServer, "close_key",
        staticmethod(_fake_close_key),
    )
    r = client.get("/api/sessions/t-hist")
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "codex"
    assert data["messages"][0] == {"role": "user", "text": "hey codex"}
    assert data["messages"][1] == {"role": "assistant", "text": "hey matt"}
    assert len(opened_keys) == 1
    assert opened_keys[0].startswith("shared:read:")
    assert closed_keys == opened_keys


def test_codex_usage_endpoint_chatgpt_auth(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": True, "reason": None},
    )

    class _FakeServer:
        async def account_usage(self):
            return {
                "account": {"type": "chatgpt"}, "auth_mode": "chatgpt",
                "rate_limits": {"rateLimits": {
                    "primary": {"usedPercent": 12, "resetsAt": 1784550000,
                                "windowDurationMins": 300}}},
                "token_usage": {"summary": {"lifetimeTokens": 5000},
                                "dailyUsageBuckets": [{"startDate": "2026-07-20",
                                                       "tokens": 900}]},
                "unavailable_reason": None,
            }

    async def _fake_get(*args, **kwargs):
        return _FakeServer()

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    r = client.get("/api/codex/usage")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["auth_mode"] == "chatgpt"
    assert d["rate_limits"]["rateLimits"]["primary"]["usedPercent"] == 12
    assert d["token_usage"]["summary"]["lifetimeTokens"] == 5000


def test_codex_usage_endpoint_apikey_degrades(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": True, "reason": None},
    )

    class _FakeServer:
        async def account_usage(self):
            return {
                "account": {"type": "apiKey"}, "auth_mode": "apiKey",
                "rate_limits": None, "token_usage": None,
                "unavailable_reason": "chatgpt authentication required to read rate limits",
            }

    async def _fake_get(*args, **kwargs):
        return _FakeServer()

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    r = client.get("/api/codex/usage")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["auth_mode"] == "apiKey"
    assert d["rate_limits"] is None
    assert "chatgpt" in d["unavailable_reason"].lower()


def test_codex_usage_endpoint_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda *args, **kwargs: {"available": False, "reason": "codex CLI not installed"},
    )
    r = client.get("/api/codex/usage")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False
    assert "not installed" in d["reason"]


async def test_codex_account_usage_folds_rpc_errors():
    """account_usage reports the ChatGPT-auth rejection as a reason instead
    of raising, so an API-key login still returns the account-type line."""
    server = codex_provider.CodexAppServer()
    calls = {}

    async def fake_request(method, params=None, timeout=codex_provider.REQUEST_TIMEOUT_S):
        calls[method] = True
        if method == "account/read":
            return {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}
        raise codex_provider.CodexRPCError(
            method, {"code": -32600,
                     "message": "chatgpt authentication required to read rate limits"})

    server.request = fake_request
    out = await server.account_usage()
    assert out["auth_mode"] == "apiKey"
    assert out["rate_limits"] is None
    assert "chatgpt" in out["unavailable_reason"].lower()
    assert "account/usage/read" in calls  # still attempted


def test_codex_credential_home_shares_only_rollouts_and_configuration(
    client, monkeypatch, tmp_path,
):
    import app as app_module

    shared = tmp_path / "shared-codex"
    personal = tmp_path / "personal-codex"
    (shared / "sessions").mkdir(parents=True)
    (shared / "skills").mkdir()
    (shared / "config.toml").write_text('model = "gpt-test"\n')
    (shared / "state_5.sqlite").write_text("private index")
    (shared / "auth.json").write_text("shared credential")
    monkeypatch.setattr(app_module, "CODEX_PERSONAL_HOMES_DIR", personal)
    monkeypatch.setattr(codex_provider, "codex_home", lambda: shared)

    home = app_module._ensure_codex_credential_home("user-overlay", 41)

    assert os.path.samefile(home / "sessions", shared / "sessions")
    assert os.path.samefile(home / "skills", shared / "skills")
    assert os.path.samefile(home / "config.toml", shared / "config.toml")
    assert not (home / "state_5.sqlite").exists()
    assert not (home / "auth.json").exists()


def test_codex_device_login_api_marks_account_active(client, monkeypatch):
    import app as app_module

    created = client.post(
        "/api/account/codex/credentials", json={"label": "Device Plan Test"},
    )
    assert created.status_code == 200
    cred_id = created.json()["id"]
    login_id = "device-login-test"

    class _FakeServer:
        async def request(self, method, params=None, timeout=None):
            if method == "account/login/start":
                assert params == {"type": "chatgptDeviceCode"}
                return {
                    "type": "chatgptDeviceCode",
                    "loginId": login_id,
                    "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": "ABCD-EFGH",
                }
            if method == "account/read":
                return {
                    "requiresOpenaiAuth": True,
                    "account": {
                        "type": "chatgpt",
                        "email": "plan@example.test",
                        "planType": "plus",
                    },
                }
            raise AssertionError(method)

        def login_result(self, candidate):
            assert candidate == login_id
            return {"loginId": login_id, "success": True}

    server = _FakeServer()

    async def _fake_server_for_account(account):
        assert account["isolated_auth"] is True
        return server

    monkeypatch.setattr(codex_provider, "codex_binary", lambda: "/bin/true")
    monkeypatch.setattr(
        app_module, "_codex_server_for_account", _fake_server_for_account,
    )
    try:
        started = client.post(
            f"/api/account/codex/credentials/{cred_id}/login/start",
        )
        assert started.status_code == 200
        assert started.json() == {
            "login_id": login_id,
            "verification_url": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-EFGH",
        }
        home = app_module._codex_credential_home_path("anonymous", cred_id)
        (home / "auth.json").write_text("{}")
        status = client.get(
            f"/api/account/codex/credentials/{cred_id}/status",
            params={"login_id": login_id},
        )
        assert status.status_code == 200
        assert status.json()["credential"]["configured"] is True
        assert status.json()["flow"]["status"] == "done"
        assert app_module._codex_user_active_slot("anonymous") == f"cred:{cred_id}"
    finally:
        app_module._set_codex_user_active("anonymous", "shared")
        app_module._delete_codex_credential_row("anonymous", cred_id)
        shutil.rmtree(
            app_module._codex_credential_home_path("anonymous", cred_id),
            ignore_errors=True,
        )


def test_codex_credential_routes_hide_other_users_rows(client):
    import app as app_module

    cred = app_module._create_codex_credential(
        "different-oidc-sub", "Hidden OpenAI Account",
    )
    try:
        response = client.get(
            f"/api/account/codex/credentials/{cred['id']}/status",
        )
        assert response.status_code == 404
    finally:
        app_module._delete_codex_credential_row(
            "different-oidc-sub", cred["id"],
        )


def test_account_page_renders_openai_device_code_controls(client):
    response = client.get("/account")
    assert response.status_code == 200
    assert "OpenAI accounts" in response.text
    assert 'id="codex-login-start"' in response.text
    assert 'src="/static/codex-account.js' in response.text


def test_codex_account_change_supersedes_run_without_changing_session(
    client, monkeypatch,
):
    import app as app_module

    cred = app_module._create_codex_credential(
        "anonymous", "Same Chat Switch Test",
    )
    cred_id = cred["id"]
    home = app_module._ensure_codex_credential_home("anonymous", cred_id)
    (home / "auth.json").write_text("{}")
    run = app_module.ActiveRun(
        "run-codex-account-switch",
        owner_sub="anonymous",
        account_slot="shared",
    )
    run.provider = "codex"
    run.session_id = "thread-codex-account-switch"
    run.personality_id = app_module._resolve_personality_for_run(
        {"sub": "anonymous"}, session_id=run.session_id,
    )["id"]
    app_module.ACTIVE_RUNS[run.run_id] = run
    app_module.ACTIVE_RUNS_BY_SESSION[run.session_id] = run
    seen = {}

    async def _fake_supersede(candidate, reason):
        seen["run"] = candidate
        seen["reason"] = reason

    monkeypatch.setattr(
        app_module, "_supersede_run_for_switch", _fake_supersede,
    )
    try:
        response = client.post(
            f"/api/chat/send/{run.run_id}",
            data={
                "message": "continue here",
                "account_slot": f"cred:{cred_id}",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"] == "account_changed"
        assert seen == {"run": run, "reason": "account_changed"}
        assert run.session_id == "thread-codex-account-switch"
    finally:
        app_module.ACTIVE_RUNS.pop(run.run_id, None)
        app_module.ACTIVE_RUNS_BY_SESSION.pop(run.session_id, None)
        app_module._delete_codex_credential_row("anonymous", cred_id)
        shutil.rmtree(home, ignore_errors=True)


async def test_codex_account_handoff_waits_for_interrupted_turn_to_finish(client):
    import app as app_module

    events = []
    run = app_module.ActiveRun(
        "run-handoff-barrier", owner_sub="anonymous", account_slot="shared",
    )
    run.provider = "codex"
    run.between_turns = False
    run.codex_turn_done.clear()

    class _Client:
        async def interrupt(self):
            events.append("interrupt")
            assert run.accepting_input is False
            loop = asyncio.get_running_loop()
            loop.call_soon(events.append, "turn_completed")
            loop.call_soon(run.codex_turn_done.set)

    async def _driver():
        try:
            await asyncio.Future()
        finally:
            events.append("driver_closed")

    run.client = _Client()
    run.task = asyncio.create_task(_driver())
    await asyncio.sleep(0)
    await app_module._supersede_run_for_switch(run, "account_changed")

    assert events == ["interrupt", "turn_completed", "driver_closed"]
    assert run.superseded_reason == "account_changed"
    assert run.task.cancelled()


async def test_codex_driver_resumes_same_thread_in_fresh_run_server(
    client, monkeypatch,
):
    import app as app_module

    class _FakeServer:
        def __init__(self):
            self.calls = []
            self.queue = asyncio.Queue()

        async def request(self, method, params=None, timeout=None):
            self.calls.append((method, params))
            if method == "thread/resume":
                return {"thread": {"id": "thread-handoff", "model": "gpt-test"}}
            if method == "turn/start":
                self.queue.put_nowait({
                    "method": codex_provider.SERVER_EXITED_METHOD,
                    "params": {},
                })
                return {}
            if method == "thread/unsubscribe":
                return {"status": "unsubscribed"}
            raise AssertionError(method)

        def subscribe(self, thread_id):
            assert thread_id == "thread-handoff"
            return self.queue

        def set_request_handler(self, thread_id, handler):
            self.handler = handler

        def unsubscribe(self, thread_id):
            self.unsubscribed = thread_id

    server = _FakeServer()

    seen_server_keys = []
    closed_server_keys = []

    async def _fake_server_for_account(account, *, server_key=None):
        seen_server_keys.append(server_key)
        return server

    async def _fake_close_key(server_key):
        closed_server_keys.append(server_key)

    monkeypatch.setattr(
        app_module, "_codex_server_for_account", _fake_server_for_account,
    )
    monkeypatch.setattr(
        codex_provider.CodexAppServer, "close_key", _fake_close_key,
    )
    run = app_module.ActiveRun(
        "run-thread-handoff", owner_sub="anonymous", account_slot="cred:999",
    )
    run.provider = "codex"
    run.project_key = "project-test"
    await app_module._codex_driver(
        run,
        initial_text="continue",
        initial_images=[],
        resume_thread_id="thread-handoff",
        model_id="gpt-test",
        effort="",
        cwd=app_module.DEFAULT_CWD,
        personality_append="",
        first_prompt_title="continue",
        account={"server_key": "test"},
    )

    assert run.session_id == "thread-handoff"
    assert server.calls[0] == (
        "thread/resume",
        {
            "cwd": str(app_module.DEFAULT_CWD),
            "approvalPolicy": codex_provider.APPROVAL_POLICY,
            "sandbox": codex_provider.SANDBOX_MODE,
            "model": "gpt-test",
            "threadId": "thread-handoff",
        },
    )
    assert server.calls[-1] == (
        "thread/unsubscribe", {"threadId": "thread-handoff"},
    )
    assert server.unsubscribed == "thread-handoff"
    assert seen_server_keys == ["test:run:run-thread-handoff"]
    assert closed_server_keys == ["test:run:run-thread-handoff"]
    app_module._state_db().execute(
        "DELETE FROM codex_session WHERE thread_id = ?", ("thread-handoff",),
    )
