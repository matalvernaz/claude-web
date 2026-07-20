"""Codex provider: event translation, availability probe, session registry,
and the api_chat provider dispatch guards.

The live app-server protocol is exercised manually (it needs the codex
binary + OpenAI auth); these tests cover everything on our side of the
wire: notification→SSE translation, the JSON-RPC client's dispatch
plumbing, and the route-level validation that keeps the two providers'
sessions from cross-contaminating.
"""
import asyncio

import pytest

import codex_provider


# ─── item_events translation ──────────────────────────────────────────────────

CAP = 800


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
        lambda: {"available": False, "reason": "codex CLI not installed"},
    )
    r = client.post("/api/chat", data={"message": "hi", "provider": "codex"})
    assert r.status_code == 503
    assert r.json()["error"] == "codex_not_configured"


def test_api_chat_codex_rejects_plan_and_fork(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda: {"available": True, "reason": None},
    )
    r = client.post("/api/chat", data={
        "message": "hi", "provider": "codex", "permission_mode": "plan",
    })
    assert r.status_code == 400
    assert "plan mode" in r.text
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
        lambda: {"available": True, "reason": None},
    )
    r = client.post("/api/chat", data={
        "message": "hi", "provider": "codex", "session_id": "0" * 32,
    })
    assert r.status_code == 400


def test_api_providers_payload(client, monkeypatch):
    monkeypatch.setattr(
        codex_provider, "availability",
        lambda: {"available": False, "reason": "codex CLI not installed"},
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

    async def _fake_get():
        return _FakeServer()

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    r = client.delete("/api/sessions/t-del")
    assert r.status_code == 200
    assert app_module._codex_session_row("t-del") is None


def test_codex_session_history_payload(client, monkeypatch):
    import app as app_module

    app_module._record_codex_session("t-hist", None, "", "history", None)

    class _FakeServer:
        async def request(self, method, params=None, timeout=None):
            assert method == "thread/read"
            assert params["threadId"] == "t-hist"
            return {"thread": {"turns": [{"items": [
                {"type": "userMessage", "id": "u1",
                 "content": [{"type": "text", "text": "hey codex"}]},
                {"type": "agentMessage", "id": "m1", "text": "hey matt"},
            ]}]}}

    async def _fake_get():
        return _FakeServer()

    monkeypatch.setattr(
        codex_provider.CodexAppServer, "get", staticmethod(_fake_get))
    r = client.get("/api/sessions/t-hist")
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "codex"
    assert data["messages"][0] == {"role": "user", "text": "hey codex"}
    assert data["messages"][1] == {"role": "assistant", "text": "hey matt"}
