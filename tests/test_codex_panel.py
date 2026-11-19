"""Codex's real IPC/MCP adapters plus driver lifecycle and review gates."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse

import app as app_module
import codex_panel
import codex_provider
import panel
from tests.test_panel_app import APPROVE_ALL, FakeCore, _finish_round, _git
from tests.test_panel_app import chat as chat, repo as repo

pytestmark = pytest.mark.skipif(os.name == "nt", reason="panel is unavailable on Windows")


async def hook(bridge, run, repo, tool, command, event="PreToolUse", tool_id="call-1"):
    return await asyncio.to_thread(codex_panel.request, bridge.path, {
        "kind": "hook", "input": {
            "session_id": run.session_id, "hook_event_name": event,
            "tool_name": tool, "tool_use_id": tool_id, "cwd": str(repo),
            "tool_input": {"command": command},
        },
    }, 3)


async def test_codex_hooks_hold_edits_and_exact_commits_even_in_bypass_mode(chat, repo):
    run, _, _ = chat
    run.provider = "codex"
    run.permission_mode = "bypassPermissions"
    bridge = codex_panel.PanelBridge(lambda payload: app_module._codex_panel_dispatch(run, payload))
    await bridge.start()
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-a = 1\n+a = 2\n*** End Patch"
    try:
        denied = await hook(bridge, run, repo, "apply_patch", patch)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        state = app_module._panel_load(run.session_id)
        state.plan_ok = True
        app_module._panel_save(run.session_id, run.owner_sub, state)
        assert await hook(bridge, run, repo, "apply_patch", patch) == {}
        (repo / "a.py").write_text("a = 2\n")
        denied = await hook(bridge, run, repo, "Bash", "git commit -am change")
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        state.approvals[str(repo)] = panel.working_state(str(repo))
        app_module._panel_save(run.session_id, run.owner_sub, state)
        assert await hook(bridge, run, repo, "Bash", "git commit -am change") == {}
        (repo / "a.py").write_text("a = 3\n")
        denied = await hook(bridge, run, repo, "Bash", "git commit -am change", tool_id="later")
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        (repo / "a.py").write_text("a = 2\n")
        _git(repo, "commit", "-qam", "change")
        assert await hook(bridge, run, repo, "Bash", "git commit -am change", "PostToolUse") == {}
        assert not app_module._panel_load(run.session_id).plan_ok
    finally:
        await bridge.close()
    assert not Path(bridge.path).exists()


async def test_codex_home_exemption_does_not_change_claude_gates(chat, repo, monkeypatch):
    run, _, _ = chat
    monkeypatch.setenv("CODEX_HOME", str(repo))
    result = await app_module._panel_edit_gate(run, {
        "cwd": str(repo), "tool_input": {"file_path": str(repo / "a.py")},
    })
    if run.provider == "claude":
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert result == {}


@pytest.mark.parametrize("failure", ["steer", "start", "exit", "initial", "startup"])
@pytest.mark.parametrize("with_user_message", [False, True])
async def test_rejected_codex_panel_answers_are_parked_instead_of_lost(client, monkeypatch, failure, with_user_message):
    suffix = failure + str(with_user_message)
    run = app_module.ActiveRun("panel-delivery-" + suffix, owner_sub="anonymous")
    run.provider = "codex"
    if failure == "startup" or with_user_message:
        run.session_id = "panel-delivery-thread-" + suffix
        monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {run.session_id: run})
    answer = panel.MESSAGE_MARKER + " A review answer."
    text = panel.user_prefix() + "My follow-up" if with_user_message else answer
    if with_user_message and failure in ("initial", "startup"):
        state = panel.PanelState(pending_message=answer)
        app_module._panel_save(run.session_id, run.owner_sub, state)

    class Server:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.started = 0

        async def request(self, method, params=None, timeout=None):
            if failure == "startup" and method == "thread/resume":
                raise RuntimeError("Resume failed before any turn")
            if method in ("thread/start", "thread/resume"):
                return {"thread": {"id": "panel-delivery-thread-" + suffix}}
            if method == "turn/start":
                self.started += 1
                if failure == "initial":
                    raise RuntimeError("Initial turn rejected")
                if self.started == 1:
                    if with_user_message:
                        app_module._panel_save(run.session_id, run.owner_sub, panel.PanelState(pending_message=answer))
                        await app_module._inject_user_input(run, text, [], 1, 0, display_text="My follow-up")
                    else:
                        await app_module._panel_deliver(run.session_id, run, text)
                    if failure in ("start", "exit"):
                        self.queue.put_nowait({"method": "turn/completed", "params": {"turn": {"id": "first"}}}
                                              if failure == "start" else {"method": codex_provider.SERVER_EXITED_METHOD})
                    return {"turn": {"id": "first"}}
                self.queue.put_nowait({"method": codex_provider.SERVER_EXITED_METHOD})
                raise RuntimeError("Turn start rejected")
            if method == "turn/steer":
                self.queue.put_nowait({"method": codex_provider.SERVER_EXITED_METHOD})
                raise RuntimeError("The turn already ended")
            return {}

        def subscribe(self, tid):
            return self.queue

        def set_request_handler(self, *args):
            pass

        def unsubscribe(self, *args):
            pass

    server = Server()

    async def server_for_account(*args, **kwargs):
        return server

    async def close_key(key):
        pass

    monkeypatch.setattr(app_module, "_panel_available", lambda: False)
    monkeypatch.setattr(app_module, "_codex_server_for_account", server_for_account)
    monkeypatch.setattr(codex_provider.CodexAppServer, "close_key", close_key)
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    await asyncio.wait_for(app_module._codex_driver(
        run, initial_text=text if failure in ("initial", "startup") else "Task", initial_images=[],
        resume_thread_id=run.session_id if failure == "startup" or with_user_message else None,
        model_id="test", effort="", cwd=app_module.DEFAULT_CWD, personality_append="",
        first_prompt_title="Task", account={"server_key": "test"},
    ), 3)
    await asyncio.sleep(0)
    assert app_module._panel_load(run.session_id).pending_message == answer
    lost = [e for e in run.events if e.get("lost_input")]
    assert len(lost) == int(with_user_message and failure not in ("initial", "startup"))
    if lost:
        assert lost[0]["lost_input"] == "My follow-up"
    assert sum(e["type"] == "panel_parked" for e in run.events) == int(not with_user_message)
    assert app_module._panel_take_pending(run.session_id, run.owner_sub) == answer + panel.PARKED_SEPARATOR
    assert app_module._panel_take_pending(run.session_id, run.owner_sub) == ""


async def test_a_consumed_panel_answer_is_not_parked_again_by_its_acknowledgement(client):
    run = app_module.ActiveRun("panel-consumed", owner_sub="anonymous")
    run.provider, run.session_id = "codex", "panel-consumed-thread"
    text = panel.MESSAGE_MARKER + " Answer"
    app_module._emit_lost_input(run, text, "turn ended")
    assert app_module._panel_take_pending(run.session_id, run.owner_sub).startswith(text)
    acknowledgement = asyncio.get_running_loop().create_future()
    acknowledgement.set_exception(app_module._DeliveryAlreadyReported("already parked"))
    await app_module._await_delivery_quietly(run, text, acknowledgement)
    assert app_module._panel_take_pending(run.session_id, run.owner_sub) == ""


@pytest.mark.parametrize("endpoint", ["chat", "send"])
async def test_full_input_queue_preserves_the_parked_answer_for_one_retry(tmp_path, monkeypatch, endpoint):
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setitem(app_module.RESTART_STATE, "pending", False)
    run = app_module.ActiveRun("panel-api-" + endpoint, owner_sub="anonymous")
    run.provider, run.session_id, run.model = "codex", "panel-api-thread-" + endpoint, "test"
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {run.run_id: run})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {run.session_id: run})
    monkeypatch.setattr(app_module, "_existing_run_for_session", lambda sid: run)
    monkeypatch.setattr(app_module, "_panel_available", lambda: True)
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *a, **kw: {"id": None, "append": ""})
    monkeypatch.setattr(app_module, "_resolve_codex_account_for_run", lambda *a, **kw: {"slot": "shared"})
    monkeypatch.setattr(app_module, "_stream_run_response", lambda *a, **kw: JSONResponse({"ok": True}))
    monkeypatch.setattr(codex_provider, "availability", lambda *a, **kw: {"available": True})

    class Server:
        async def models(self):
            return []

    async def server_for_account(*args, **kwargs):
        return Server()

    monkeypatch.setattr(app_module, "_codex_server_for_account", server_for_account)
    app_module._record_codex_session(run.session_id, run.owner_sub, "", "Title", "test")
    state = app_module._panel_set_enabled(run.session_id, run.owner_sub, True)
    answer = panel.MESSAGE_MARKER + " Saved verdict"
    state.pending_message = answer
    app_module._panel_save(run.session_id, run.owner_sub, state)
    for _ in range(app_module.MAX_USER_INPUT_QUEUE):
        run.user_input_queue.put_nowait({"text": "other queued message"})

    async def send():
        request = Request({"type": "http", "method": "POST", "path": "/api/chat",
                           "headers": [], "scheme": "http", "server": ("testserver", 80)})
        request._form = FormData()
        user = {"sub": "anonymous"}
        if endpoint == "send":
            return await app_module.api_chat_send(
                request, run.run_id, message="Follow-up", provider="codex", personality_id=None,
                account_slot="", steer=False, queue_id="", user=user,
            )
        return await app_module.api_chat(
            request, message="Follow-up", session_id=run.session_id, project="", model="test",
            advisor="", effort="", permission_mode="default", fork=False, personality_id=None,
            account_slot="", queue_id="", provider="codex", panel_review="", user=user,
        )

    try:
        response = await send()
        assert response.status_code == 429
        assert app_module._panel_load(run.session_id).pending_message == answer
        while not run.user_input_queue.empty():
            run.user_input_queue.get_nowait()
        response = await send()
        assert response.status_code in (200, 202)
        queued = run.user_input_queue.get_nowait()
        assert answer not in queued["text"]
        assert queued["text"].endswith("Follow-up")
        # Accepting a queue entry does not mean Codex has received it yet.
        assert app_module._panel_load(run.session_id).pending_message == answer
        queued["delivered"].set_result(True)
        await asyncio.sleep(0)
    finally:
        if app_module._STATE_DB is not None:
            app_module._STATE_DB.close()


@pytest.mark.parametrize("boundary", ["start", "steer"])
@pytest.mark.parametrize("recall", [False, True])
async def test_saved_verdict_is_consumed_only_by_an_accepted_codex_input(client, monkeypatch, boundary, recall):
    run = app_module.ActiveRun(f"outbox-{boundary}-{recall}", owner_sub="anonymous")
    run.provider = "codex"
    answer = panel.MESSAGE_MARKER + " Saved verdict"
    payloads = []

    class Server:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def request(self, method, params=None, timeout=None):
            if method == "thread/start":
                return {"thread": {"id": run.run_id + "-thread"}}
            if method in ("turn/start", "turn/steer"):
                payloads.append(params["input"][0]["text"])
                if len(payloads) == 1:
                    app_module._panel_save(run.session_id, run.owner_sub, panel.PanelState(pending_message=answer))
                    if recall:
                        await app_module._inject_user_input(run, "Withdrawn follow-up", [], 0, 0, queue_id="old")
                        run.canceled_input_ids.add("old")
                    await app_module._inject_user_input(run, "Replacement", [], 0, 0, queue_id="new")
                    if boundary == "start":
                        self.queue.put_nowait({"method": "turn/completed", "params": {"turn": {"id": "first"}}})
                else:
                    self.queue.put_nowait({"method": codex_provider.SERVER_EXITED_METHOD})
                return {"turn": {"id": "first" if len(payloads) == 1 else "second"}}
            return {}

        def subscribe(self, tid):
            return self.queue

        def set_request_handler(self, *args):
            pass

        def unsubscribe(self, *args):
            pass

    server = Server()

    async def server_for_account(*args, **kwargs):
        return server

    async def close_key(key):
        pass

    monkeypatch.setattr(app_module, "_panel_available", lambda: False)
    monkeypatch.setattr(app_module, "_codex_server_for_account", server_for_account)
    monkeypatch.setattr(codex_provider.CodexAppServer, "close_key", close_key)
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    await asyncio.wait_for(app_module._codex_driver(
        run, initial_text="Original task", initial_images=[], resume_thread_id=None,
        model_id="test", effort="", cwd=app_module.DEFAULT_CWD, personality_append="",
        first_prompt_title="Task", account={"server_key": "test"},
    ), 3)
    await asyncio.sleep(0)
    assert payloads == ["Original task", answer + panel.PARKED_SEPARATOR + "Replacement"]
    assert app_module._panel_load(run.session_id).pending_message is None
    assert not any(e.get("lost_input") for e in run.events)


async def test_mcp_review_uses_the_shared_engine_and_delivers_the_answer(chat, repo, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    run, events, delivered = chat
    run.provider = "codex"
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))
    bridge = codex_panel.PanelBridge(lambda payload: app_module._codex_panel_dispatch(run, payload))
    await bridge.start()
    try:
        async with stdio_client(StdioServerParameters(
            command=sys.executable, args=bridge.command("mcp")[1:],
        )) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["review"]
                result = await session.call_tool("review", {
                    "stage": "plan", "repo": str(repo), "message": "Change a to 2.",
                })
                assert not result.isError
                assert result.content[0].text.startswith("Sent to the panel")
                assert "End your turn" in result.content[0].text
                await _finish_round(run)
                assert app_module._panel_load(run.session_id).plan_ok
                assert delivered[-1].startswith(panel.MESSAGE_MARKER)
                assert any(e["type"] == "panel_reply" for e in events)
                invalid = await session.call_tool("review", {
                    "stage": "bad", "repo": str(repo), "message": "Invalid stage",
                })
                assert invalid.isError
    finally:
        await bridge.close()


async def test_bridge_close_cancels_waiting_requests():
    waiting = asyncio.Event()

    async def dispatch(payload):
        waiting.set()
        await asyncio.Event().wait()

    bridge = codex_panel.PanelBridge(dispatch)
    await bridge.start()
    reader, writer = await asyncio.open_unix_connection(bridge.path)
    writer.write(b'{"kind":"review"}\n')
    await writer.drain()
    await waiting.wait()
    await bridge.close()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize("resume,fork", [(False, False), (True, False), (True, True)])
async def test_codex_driver_installs_panel_on_start_resume_and_fork(client, monkeypatch, resume, fork):
    run = app_module.ActiveRun("panel-driver", owner_sub="anonymous")
    run.provider = "codex"
    run.panel_requested = True
    calls = []
    bridge_calls = []

    class Bridge:
        def __init__(self, dispatch):
            self.dispatch = dispatch

        async def start(self):
            bridge_calls.append("start")

        async def configuration(self, server, cwd):
            return {"panel_config": True}

        async def verify_hooks(self, server, tid):
            bridge_calls.append("verified")

        async def close(self):
            bridge_calls.append("closed")

    class Server:
        async def request(self, method, params=None, timeout=None):
            calls.append((method, params))
            if method.startswith("thread/") and method != "thread/unsubscribe":
                return {"thread": {"id": "panel-driver-thread"}}
            return {"turn": {"id": "turn-1"}}

        def subscribe(self, tid):
            queue = asyncio.Queue()
            queue.put_nowait({"method": codex_provider.SERVER_EXITED_METHOD})
            return queue

        def set_request_handler(self, *args):
            pass

        def unsubscribe(self, *args):
            pass

    async def server_for_account(*args, **kwargs):
        return Server()

    async def close_key(key):
        pass

    monkeypatch.setattr(app_module, "_panel_available", lambda: True)
    monkeypatch.setattr(codex_panel, "PanelBridge", Bridge)
    monkeypatch.setattr(app_module, "_codex_server_for_account", server_for_account)
    monkeypatch.setattr(codex_provider.CodexAppServer, "close_key", close_key)
    await app_module._codex_driver(
        run, initial_text="Task", initial_images=[], resume_thread_id="old" if resume else None,
        model_id="test", effort="", cwd=app_module.DEFAULT_CWD, personality_append="My persona",
        first_prompt_title="Task", account={"server_key": "test"}, fork=fork,
    )
    method, params = calls[0]
    assert method == ("thread/fork" if fork else "thread/resume" if resume else "thread/start")
    assert params["config"] == {"panel_config": True}
    assert params["developerInstructions"].startswith("My persona\n\n")
    assert panel.TOOL_NAME in params["developerInstructions"]
    assert "ToolSearch" not in params["developerInstructions"]
    assert bridge_calls == ["start", "verified", "closed"]
    assert app_module._panel_load(run.session_id).enabled


def test_codex_panel_toggle_reopen_and_prefix(client, monkeypatch):
    monkeypatch.setattr(app_module, "_panel_available", lambda: True)
    sid = "codex-panel-toggle"
    app_module._record_codex_session(sid, "anonymous", "", "Title", None)
    response = client.post("/api/chat/panel", data={"session_id": sid, "enabled": "1"})
    assert response.status_code == 200 and response.json()["panel"]["enabled"]
    run = app_module.ActiveRun("toggle", owner_sub="anonymous")
    run.provider, run.session_id = "codex", sid
    prefix = app_module._panel_prefix(run, "Requirements")
    assert prefix.startswith(panel.USER_PREFIX_MARKER) and "ToolSearch" not in prefix
    assert run.panel_notes == ["Requirements"]
    assert client.post("/api/chat/panel", data={"session_id": sid, "enabled": ""}).status_code == 200
    assert app_module._panel_prefix(run, "Next") == ""


@pytest.mark.skipif(os.getenv("CLAUDE_WEB_TEST_CODEX_CLI") != "1",
                    reason="set CLAUDE_WEB_TEST_CODEX_CLI=1 for the installed CLI handshake")
@pytest.mark.parametrize("trust_existing", [False, True])
async def test_installed_codex_accepts_and_trusts_only_the_panel_hooks(tmp_path, trust_existing):
    (tmp_path / "config.toml").write_text('''[[hooks.PreToolUse]]
matcher = "^Bash$"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "true"
''')
    server = codex_provider.CodexAppServer(key="panel-contract", home=tmp_path)
    bridge = codex_panel.PanelBridge(lambda payload: asyncio.sleep(0, result={}))
    await bridge.start()
    try:
        await server._start()
        if trust_existing:
            listed = await server.request("hooks/list", {"cwds": [str(tmp_path)]})
            old = listed["data"][0]["hooks"][0]
            with (tmp_path / "config.toml").open("a") as config_file:
                config_file.write(f'\n[hooks.state.{json.dumps(old["key"])}]\ntrusted_hash = {json.dumps(old["currentHash"])}\n')
        config = await bridge.configuration(server, tmp_path)
        response = await server.request("thread/start", {
            "cwd": str(tmp_path), "ephemeral": True, "config": config,
        })
        await bridge.verify_hooks(server, response["thread"]["id"])
        listed = await server.request("hooks/list", {"threadId": response["thread"]["id"]})
        inherited = [h for row in listed["data"] for h in row["hooks"] if h.get("command") == "true"]
        assert len(inherited) == 1, inherited
        assert all(h["trustStatus"] == ("trusted" if trust_existing else "untrusted") for h in inherited)
        assert config["mcp_servers.panel"]["args"] == bridge.command("mcp")[1:]
    finally:
        server.shutdown()
        if server.proc:
            await server.proc.wait()
        await bridge.close()


@pytest.mark.skipif(os.getenv("CLAUDE_WEB_TEST_CODEX_CLI") != "1",
                    reason="set CLAUDE_WEB_TEST_CODEX_CLI=1 for the installed CLI tool contract")
async def test_installed_codex_enforces_hooks_and_calls_review_without_a_cloud_model(chat, repo, tmp_path, monkeypatch):
    """Script the real CLI's model responses through loopback, with no billing."""
    run, _, delivered = chat
    run.provider = "codex"
    monkeypatch.setenv("CODEX_PANEL_TEST_KEY", "local-contract-only")
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))
    (repo / "a.py").write_text("a = 2\n")
    before = panel.head_commit(str(repo))
    requests = []
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            number = len(requests)
            if number == 1:
                item = {"type": "function_call", "id": "fc1", "call_id": "exec1", "name": "exec_command",
                        "arguments": json.dumps({"cmd": "git commit -am unreviewed", "workdir": str(repo)})}
            elif number == 2:
                item = {"type": "custom_tool_call", "id": "fc2", "call_id": "patch1", "name": "apply_patch",
                        "input": "*** Begin Patch\n*** Update File: " + str(repo / "a.py") +
                                 "\n@@\n-a = 2\n+a = 3\n*** End Patch"}
            elif number == 3:
                item = {"type": "function_call", "id": "fc3", "call_id": "review1", "namespace": "mcp__panel", "name": "review",
                        "arguments": json.dumps({"stage": "plan", "repo": str(repo), "message": "Change a to 3."})}
            else:
                item = {"type": "message", "id": "msg1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Contract complete", "annotations": []}]}
            response = {"id": f"resp{number}", "object": "response", "status": "completed", "output": [item]}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()

    async def dispatch(payload):
        observed.append(payload)
        return await app_module._codex_panel_dispatch(run, payload)

    bridge = codex_panel.PanelBridge(dispatch)
    await bridge.start()
    server = codex_provider.CodexAppServer(key="panel-wire-contract", home=tmp_path / "codex")
    try:
        await server._start()
        config = await bridge.configuration(server, repo)
        config.update({
            "model_provider": "test", "features.code_mode": False, "features.apply_patch_freeform": True,
            "model_providers.test": {"name": "Contract", "base_url": f"http://127.0.0.1:{http.server_port}",
                                     "wire_api": "responses", "env_key": "CODEX_PANEL_TEST_KEY"},
        })
        response = await server.request("thread/start", {
            "cwd": str(repo), "ephemeral": True, "config": config, "model": "gpt-6.2-sol",
            "approvalPolicy": "never", "sandbox": "danger-full-access",
        })
        sid = response["thread"]["id"]
        app_module._panel_set_enabled(sid, run.owner_sub, True)
        run.emit({"type": "system", "subtype": "init", "session_id": sid})
        queue = server.subscribe(sid)
        await bridge.verify_hooks(server, sid)
        await server.request("turn/start", {"threadId": sid, "input": [{"type": "text", "text": "Run the contract"}]})
        while True:
            event = await asyncio.wait_for(queue.get(), 30)
            if event["method"] == "turn/completed":
                assert not event["params"]["turn"].get("error")
                break
        await _finish_round(run)
        assert len(requests) == 4
        assert panel.head_commit(str(repo)) == before
        assert (repo / "a.py").read_text() == "a = 2\n"
        assert {p["input"]["tool_name"] for p in observed if p["kind"] == "hook"} >= {"Bash", "apply_patch"}
        assert any(p["kind"] == "review" for p in observed)
        assert delivered[-1].startswith(panel.MESSAGE_MARKER)
    finally:
        server.shutdown()
        if server.proc:
            await server.proc.wait()
        await bridge.close()
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)
        app_module.ACTIVE_RUNS_BY_SESSION.pop(run.session_id, None)


def test_mcp_error_results_and_review_ack_render_as_text():
    item = {"type": "mcpToolCall", "id": "review", "server": "panel", "tool": "review",
            "status": "completed", "result": {"content": [{"type": "text", "text": "Sent to panel"}],
                                               "isError": True}}
    messages = codex_provider.thread_transcript({"turns": [{"items": [item]}]}, 1000)
    assert messages[0]["name"] == panel.TOOL_NAME
    assert messages[1]["text"] == "Sent to panel"
    assert messages[1]["is_error"]
