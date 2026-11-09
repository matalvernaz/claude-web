"""Exercise DeepSeek through the real Responses SDK without making paid calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
import pytest
from openai import OpenAI

from roundtable import core


@pytest.mark.parametrize("source", ["dedicated", "shared", "file", "missing", "empty", "binary"])
def test_deepseek_startup_credentials(tmp_path, source):
    env = os.environ.copy()
    for name in ("DEEPSEEK_API_KEY", "CLAUDE_WEB_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_FILE",
                 "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    env["PATH"] = ""  # Keep a host's Claude CLI out of provider availability.
    key_file = tmp_path / "private-key-file"
    env["DEEPSEEK_API_KEY_FILE"] = str(key_file)
    expected = ""
    if source == "dedicated":
        env["DEEPSEEK_API_KEY"] = expected = "test-dedicated"
        env["CLAUDE_WEB_DEEPSEEK_API_KEY"] = "test-lower-priority"
    elif source == "shared":
        env["CLAUDE_WEB_DEEPSEEK_API_KEY"] = expected = "test-shared"
    elif source == "file":
        expected = "test-file"
        key_file.write_text(expected + "\n", encoding="utf-8")
    elif source == "empty":
        key_file.write_text("\n", encoding="utf-8")
    elif source == "binary":
        key_file.write_bytes(b"\xff\xfe")
    script = """
import sys
from roundtable import core
expected = sys.argv[1]
assert core.providers_configured() == bool(expected)
assert core._participant_provider_available('deepseek-pro') == bool(expected)
if expected:
    core.ensure_routable()
    assert core._deepseek.api_key == expected
"""
    result = subprocess.run([sys.executable, "-c", script, expected], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert str(key_file) not in result.stderr
    assert "test-" not in result.stderr


@pytest.fixture
def deepseek(monkeypatch):
    requests = []
    responses = []

    def handle(request):
        assert request.url.host == "api.deepseek.com"
        assert request.url.path == "/responses"
        assert request.headers["authorization"] == "Bearer test-deepseek"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = OpenAI(api_key="test-deepseek", base_url="https://api.deepseek.com",
                    max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(core, "_deepseek", client)
    monkeypatch.setattr(core, "_openai", None)
    monkeypatch.setattr(core, "PANEL_TOOLS_ENABLED", True)
    yield requests, responses
    client.close()


def _response(text, output=None):
    return {
        "id": "response-test", "object": "response", "created_at": 0,
        "status": "completed", "model": "deepseek-flash",
        "output": output if output is not None else [{
            "type": "message", "id": "message-test", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
            "status": "completed",
        }],
        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
    }


def test_deepseek_text_and_usage_use_its_own_key(deepseek):
    requests, responses = deepseek
    responses.append(_response("Review complete"))
    info = core._resolve_participant("deepseek-flash")
    result = core._run_turn({"topic": "review"}, info, [], "Check it", effort="medium")
    assert result.text == "Review complete"
    assert result.usage["input_tokens"] == 20
    assert requests[0]["reasoning"] == {"effort": "high"}
    assert "max_output_tokens" not in requests[0]  # DeepSeek's thinking-mode default (64K) applies
    assert "prompt_cache_key" not in requests[0]
    assert core._participant_provider_available("gpt-5") is False


@pytest.mark.parametrize("permission", ["allow", "deny"])
def test_deepseek_repo_tools_preserve_reasoning_and_permissions(deepseek, tmp_path, permission):
    requests, responses = deepseek
    (tmp_path / "subject.py").write_text("secret_source = 7\n")
    reasoning = {"type": "reasoning", "id": "reason-test", "summary": [],
                 "content": [{"type": "reasoning_text", "text": "Inspect the file"}]}
    call = {"type": "function_call", "id": "call-test", "call_id": "call-test",
            "name": "Read", "arguments": json.dumps({"path": "subject.py"})}
    responses.extend([_response("", [reasoning, call]), _response("Checked")])
    context = core.ToolUseContext(working_directory=tmp_path, permission_callback=lambda *args: permission)
    result = core._run_turn({"topic": "review"}, core._resolve_participant("deepseek-flash"), [],
                            "Check subject.py", tool_use_context=context)
    assert result.text == "Checked"
    continued = requests[1]["input"]
    assert any(item.get("type") == "reasoning" for item in continued)
    tool_result = next(item for item in continued if item.get("type") == "function_call_output")
    assert ("secret_source" in tool_result["output"]) == (permission == "allow")


def test_deepseek_structured_review_validates_and_repairs(deepseek):
    requests, responses = deepseek
    responses.extend([_response('{"wrong": true}'), _response('{"answer": "checked"}')])
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    result = core._structured_turn({"topic": "review"}, core._resolve_participant("deepseek-flash"),
                                   [], "Check it", "low", schema)
    assert result.structured == {"answer": "checked"}
    assert len(requests) == 2
    assert requests[0]["text"]["format"]["type"] == "json_schema"


def test_deepseek_incomplete_structured_reply_names_the_reason(deepseek):
    # Reasoning can eat the whole output budget; the reply is then empty, not malformed JSON.
    requests, responses = deepseek
    incomplete = _response("", [])
    incomplete["status"] = "incomplete"
    incomplete["incomplete_details"] = {"reason": "max_output_tokens"}
    responses.append(incomplete)
    schema = {"type": "object", "properties": {"answer": {"type": "string"}},
              "required": ["answer"], "additionalProperties": False}
    with pytest.raises(RuntimeError, match="max_output_tokens"):
        core._structured_turn({"topic": "review"}, core._resolve_participant("deepseek-pro"),
                              [], "Check it", "high", schema)
    assert len(requests) == 1


def test_deepseek_can_verify_and_summarize(deepseek):
    requests, responses = deepseek
    responses.append(_response("VERDICT: confirmed"))
    text = core._oneshot_text(core._resolve_participant("deepseek-flash"), "auto", "Verify", "evidence")
    assert text == "VERDICT: confirmed"
    assert len(requests) == 1


def test_deepseek_flash_rejects_unavailable_web_search(deepseek):
    with pytest.raises(ValueError, match="web search"):
        core._run_turn({"topic": "review"}, core._resolve_participant("deepseek-flash"), [],
                       "Search the web", web_search=True)
