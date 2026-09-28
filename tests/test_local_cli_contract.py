"""Opt-in checks of the installed Claude CLI's Local wire contract.

Run the fast cases after a CLI/SDK upgrade:
    CLAUDE_WEB_TEST_LOCAL_CLI=1 .venv/bin/pytest -q tests/test_local_cli_contract.py -k 'not silent'

The separate silent-response check takes at least seven minutes:
    CLAUDE_WEB_TEST_LOCAL_CLI=1 CLAUDE_WEB_TEST_LOCAL_SILENT_SECONDS=420 .venv/bin/pytest -q tests/test_local_cli_contract.py -k silent

Requests go to loopback capture servers, never Ollama or a cloud model. The
servers deliberately return HTTP 400 after capturing the request. Configuration,
project files, and CLI sessions live under pytest's temporary directory.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage

import app as app_module
import local_provider


pytestmark = pytest.mark.skipif(
    os.getenv("CLAUDE_WEB_TEST_LOCAL_CLI") != "1",
    reason="set CLAUDE_WEB_TEST_LOCAL_CLI=1 to exercise the installed Claude CLI",
)
CAPTURE_STOP = "intentional local CLI contract capture stop"
MIN_SILENT_SECONDS = 420
CLI_STARTUP_ALLOWANCE_SECONDS = 90


@pytest.fixture
def capture_servers():
    servers = []

    def start(delay=0):
        requests = []
        stopping = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                requests.append({
                    "path": self.path, "body": payload,
                    "authorization": self.headers.get("Authorization"),
                    "api_key": self.headers.get("X-Api-Key"),
                })
                if stopping.wait(delay):
                    return
                body = json.dumps({"type": "error", "error": {
                    "type": "invalid_request_error", "message": CAPTURE_STOP,
                }}).encode()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        servers.append((server, worker, stopping))
        return f"http://127.0.0.1:{server.server_port}", requests

    yield start
    for server, worker, stopping in servers:
        stopping.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.fixture
def assembled_options(client, monkeypatch, tmp_path):
    assert shutil.which("claude"), "opt-in CLI contract tests require claude on PATH"
    project = tmp_path / "project"
    config = tmp_path / "config"
    project.mkdir()
    config.mkdir()
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "_resolve_project", lambda key: project)
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    monkeypatch.setattr(app_module, "_in_process_mcp_servers_for_run", lambda **kwargs: {})
    monkeypatch.setattr(app_module, "_resolve_skills_for_run", lambda: [])
    monkeypatch.setattr(app_module, "_resolve_personality_for_run", lambda *args, **kwargs: {
        "id": None, "append": "",
    })
    monkeypatch.setenv("CLAUDECODE", "")
    account_for_run = app_module._local_account_for_run

    def isolated_account(user, model):
        account = account_for_run(user, model)
        account["env"]["CLAUDE_CONFIG_DIR"] = str(config)
        return account

    monkeypatch.setattr(app_module, "_local_account_for_run", isolated_account)
    captured = {}

    @contextlib.asynccontextmanager
    async def capture_options(options, run, stderr):
        captured["options"] = options

        class Client:
            async def query(self, prompt, **kwargs):
                pass

            async def receive_messages(self):
                yield SystemMessage(subtype="init", data={"session_id": "contract-capture"})
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1,
                    is_error=False, num_turns=1, session_id="contract-capture",
                )

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", capture_options)

    def build(origin, conflict_origin, source, model, family, effort):
        settings = {
            "env": {
                "ANTHROPIC_BASE_URL": conflict_origin,
                "ANTHROPIC_AUTH_TOKEN": "conflicting-settings-token",
                "ANTHROPIC_MODEL": "conflicting-model",
                "CLAUDE_CODE_EXTRA_BODY": json.dumps({
                    "thinking": {"type": "enabled", "budget_tokens": 8192},
                    "output_config": {"effort": "high"},
                }),
                "API_FORCE_IDLE_TIMEOUT": "true",
                "API_TIMEOUT_MS": "1000",
            },
        }
        settings_path = {
            "user": config / "settings.json",
            "project": project / ".claude" / "settings.json",
            "local": project / ".claude" / "settings.local.json",
        }[source]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings))
        monkeypatch.setattr(local_provider, "BASE_URL", origin)
        monkeypatch.setattr(local_provider, "MODEL_NAMES", (model,))
        # Block incidental CLI network requests through a loopback proxy too.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, conflict_origin)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

        async def check_model(name):
            return local_provider._model_metadata(name, {
                "capabilities": ["tools", "thinking"], "details": {"family": family},
            }, "0.33.3")

        monkeypatch.setattr(local_provider, "check_model", check_model)
        response = client.post("/api/chat", headers={"Origin": "http://testserver"}, data={
            "message": "Say OK.", "provider": "local", "model": model, "effort": effort,
        })
        assert response.status_code == 200, response.text
        assert "options" in captured, response.text
        return captured["options"]

    yield build
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


async def _query_installed_cli(options, timeout):
    async with asyncio.timeout(timeout):
        async with ClaudeSDKClient(options=options) as client:
            await client.query("Say OK.")
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    return message
    pytest.fail("installed CLI returned no ResultMessage")


@pytest.mark.parametrize("source", ["user", "project", "local"])
@pytest.mark.parametrize("model,family,effort,thinking", [
    ("qwen3:30b", "qwen3moe", "off", {"type": "disabled"}),
    ("qwen3:30b", "qwen3moe", "on", {"type": "enabled", "budget_tokens": 1024}),
    ("gpt-oss:20b", "gptoss", "low", {"type": "adaptive"}),
    ("gpt-oss:20b", "gptoss", "medium", {"type": "adaptive"}),
    ("gpt-oss:20b", "gptoss", "high", {"type": "adaptive"}),
])
def test_installed_cli_preserves_local_routing_and_thinking(
    assembled_options, capture_servers, source, model, family, effort, thinking,
):
    origin, requests = capture_servers()
    conflict_origin, conflicts = capture_servers()
    options = assembled_options(origin, conflict_origin, source, model, family, effort)
    result = asyncio.run(_query_installed_cli(options, CLI_STARTUP_ALLOWANCE_SECONDS))
    assert result.is_error and CAPTURE_STOP in (result.result or "")
    assert conflicts == [], "loaded settings redirected the Local request"
    assert len(requests) == 1
    request = requests[0]
    assert request["path"].split("?")[0] == "/v1/messages"
    assert request["authorization"] == "Bearer ollama"
    assert not request["api_key"]
    assert request["body"]["model"] == model
    assert request["body"].get("thinking") == thinking
    if family == "gptoss":
        assert request["body"]["output_config"]["effort"] == effort


def test_installed_cli_survives_silent_local_response(assembled_options, capture_servers):
    delay = int(os.getenv("CLAUDE_WEB_TEST_LOCAL_SILENT_SECONDS", "0"))
    if delay < MIN_SILENT_SECONDS:
        pytest.skip(f"set CLAUDE_WEB_TEST_LOCAL_SILENT_SECONDS>={MIN_SILENT_SECONDS}")
    origin, requests = capture_servers(delay)
    conflict_origin, conflicts = capture_servers()
    options = assembled_options(origin, conflict_origin, "project", "qwen3:30b", "qwen3moe", "off")
    started = time.monotonic()
    result = asyncio.run(_query_installed_cli(options, delay + CLI_STARTUP_ALLOWANCE_SECONDS))
    assert time.monotonic() - started >= delay
    assert result.is_error and CAPTURE_STOP in (result.result or "")
    assert conflicts == []
    assert len(requests) == 1, "CLI retried after dropping the original silent connection"
