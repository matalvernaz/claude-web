"""Opt-in checks of the installed Claude CLI's Local wire contract.

Run the fast cases (Local and DeepSeek) after a CLI/SDK upgrade:
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
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage

import app as app_module
import deepseek_provider
import local_provider
from tests.test_deepseek_provider import MODELS_PAYLOAD as DEEPSEEK_MODELS


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
                request = {
                    "path": self.path, "body": payload,
                    "authorization": self.headers.get("Authorization"),
                    "api_key": self.headers.get("X-Api-Key"),
                    "arrived": time.monotonic(), "answered": None,
                }
                requests.append(request)
                # Only the first request goes silent. The CLI's own tries after
                # the 400 are answered at once, so they can't run the clock out;
                # one that comes while the first is still silent is the failure.
                if stopping.wait(delay if len(requests) == 1 else 0):
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
                request["answered"] = time.monotonic()

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
def contract_app(client, monkeypatch, tmp_path):
    """An app with isolated state whose spawns are captured instead of run."""
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
    # The captured run ends before the test starts the real CLI with its
    # options, and a finished run deletes its settings overlay. In a live run
    # the CLI starts while the overlay exists, so keep it here too.
    monkeypatch.setattr(app_module, "ROUTED_SETTINGS_DIR", tmp_path / "routed-settings")
    monkeypatch.setattr(app_module, "_remove_routed_settings", lambda run_id: None)
    account_for_run = app_module._routed_account_for_run

    def isolated_account(user, provider, model, account=None):
        account = account_for_run(user, provider, model, account)
        account["env"]["CLAUDE_CONFIG_DIR"] = str(config)
        return account

    monkeypatch.setattr(app_module, "_routed_account_for_run", isolated_account)
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
    yield SimpleNamespace(project=project, config=config, captured=captured)
    if app_module._STATE_DB is not None:
        app_module._STATE_DB.close()


def _write_conflicting_settings(app, source, conflict_origin):
    """Settings files that try to send the request somewhere else; routing must win."""
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
        "user": app.config / "settings.json",
        "project": app.project / ".claude" / "settings.json",
        "local": app.project / ".claude" / "settings.local.json",
    }[source]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings))


def _block_incidental_network(monkeypatch, conflict_origin):
    # Block incidental CLI network requests through a loopback proxy too.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, conflict_origin)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def assembled_options(contract_app, client, monkeypatch):
    captured = contract_app.captured

    def build(origin, conflict_origin, source, model, family, effort):
        _write_conflicting_settings(contract_app, source, conflict_origin)
        monkeypatch.setattr(local_provider, "BASE_URL", origin)
        monkeypatch.setattr(local_provider, "MODEL_NAMES", (model,))
        _block_incidental_network(monkeypatch, conflict_origin)

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

    return build


@pytest.fixture
def deepseek_options(contract_app, client, monkeypatch):
    """Options /api/chat assembles for a DeepSeek run, pointed at a loopback stand-in."""
    captured = contract_app.captured

    def build(origin, conflict_origin, source, model, effort):
        _write_conflicting_settings(contract_app, source, conflict_origin)
        _block_incidental_network(monkeypatch, conflict_origin)
        # Runs on the Shared account, so no per-user key files are needed here.
        monkeypatch.setattr(deepseek_provider, "ENABLED", True)
        monkeypatch.setattr(deepseek_provider, "API_KEY", "contract-key")
        monkeypatch.setattr(deepseek_provider, "BASE_URL", origin)
        entries = [
            deepseek_provider._model_entry(raw, is_default=index == 0)
            for index, raw in enumerate(DEEPSEEK_MODELS["data"])
        ]
        monkeypatch.setattr(deepseek_provider, "_models_cache", {
            (origin, "contract-key"): {"fetched_at": time.monotonic(), "models": entries},
        })
        response = client.post("/api/chat", headers={"Origin": "http://testserver"}, data={
            "message": "Say OK.", "provider": "deepseek", "model": model, "effort": effort,
        })
        assert response.status_code == 200, response.text
        assert "options" in captured, response.text
        return captured["options"]

    return build


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
    # The capture server stops the CLI with a 400, and CLI 2.1.318 tries a 400
    # again (up to three more times, the later ones with its whole tool list),
    # so how many requests arrive is the CLI's business. What each one carries
    # is the contract: every attempt must stay Local and keep the thinking.
    assert requests
    for request in requests:
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
    # Tries after the 400 are the CLI's own (see above); one that arrived while
    # the first request was still silent means the CLI gave up on it.
    assert requests[0]["answered"] is not None
    assert all(r["arrived"] >= requests[0]["answered"] for r in requests[1:]), \
        "CLI retried after dropping the original silent connection"


@pytest.mark.parametrize("source", ["user", "project", "local"])
@pytest.mark.parametrize("model,effort", [
    ("deepseek-flash", "low"),
    ("deepseek-flash", "max"),
    ("deepseek-v4-pro", "high"),
])
def test_installed_cli_sends_deepseek_requests_to_deepseek(
    deepseek_options, capture_servers, source, model, effort,
):
    origin, requests = capture_servers()
    conflict_origin, conflicts = capture_servers()
    options = deepseek_options(origin, conflict_origin, source, model, effort)
    result = asyncio.run(_query_installed_cli(options, CLI_STARTUP_ALLOWANCE_SECONDS))
    assert result.is_error and CAPTURE_STOP in (result.result or "")
    assert conflicts == [], "loaded settings redirected the DeepSeek request"
    assert requests
    for request in requests:
        assert request["path"].split("?")[0] == "/anthropic/v1/messages"
        assert request["authorization"] == "Bearer contract-key"
        assert not request["api_key"]
        # The "[1m]" suffix that sizes the CLI's context handling never reaches DeepSeek.
        assert request["body"]["model"] == model
        assert request["body"]["thinking"]["type"] == "adaptive"
        assert request["body"]["output_config"]["effort"] == effort
