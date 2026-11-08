"""The DeepSeek provider reads DeepSeek's live model list and routes Claude Code to DeepSeek's server.

The /models and /user/balance payloads below are the real responses from 2026-11-07, so the parsing is tested against what DeepSeek actually sends.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import deepseek_provider


MODELS_PAYLOAD = {"object": "list", "data": [
    {"id": "deepseek-flash", "object": "model", "owned_by": "deepseek", "name": "DeepSeek-V4.1-Flash",
     "context_window": 1048576, "max_output_tokens": 393216,
     "input_modalities": ["text", "image"], "output_modalities": ["text"],
     "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
     "api_capabilities": {"anthropic_messages": {"system_prompt_update": "in-history",
                                                 "tool_update": "addition-only"}}},
    {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek", "name": "DeepSeek-V4-Pro",
     "context_window": 1048576, "max_output_tokens": 393216,
     "input_modalities": ["text"], "output_modalities": ["text"],
     "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
     "api_capabilities": {"anthropic_messages": {"system_prompt_update": "leading-only",
                                                 "tool_update": "leading-only"}}},
]}
BALANCE_PAYLOAD = {"is_available": True, "balance_infos": [
    {"currency": "USD", "total_balance": "20.00", "granted_balance": "0.00", "topped_up_balance": "20.00"},
]}


@pytest.fixture(autouse=True)
def deepseek_config(monkeypatch):
    monkeypatch.setattr(deepseek_provider, "API_KEY", "sk-test")
    monkeypatch.setattr(deepseek_provider, "BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(deepseek_provider, "_models_cache",
                        {"origin": None, "key": None, "fetched_at": 0.0, "models": []})


@pytest.fixture
def deepseek_api(monkeypatch):
    """Answer DeepSeek API calls from canned payloads and record each request."""
    client_type = httpx.AsyncClient
    state = {"requests": [], "models": MODELS_PAYLOAD, "status": 200}

    def respond(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {deepseek_provider.API_KEY}"
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"message": "nope"}})
        if request.url.path == "/models":
            return httpx.Response(200, json=state["models"])
        assert request.url.path == "/user/balance"
        return httpx.Response(200, json=BALANCE_PAYLOAD)

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(deepseek_provider.httpx, "AsyncClient", client)
    return state


def run(coroutine):
    return asyncio.run(coroutine)


# ── Configuration ─────────────────────────────────────────────────────────────

def test_no_key_says_how_to_turn_it_on(monkeypatch):
    monkeypatch.setattr(deepseek_provider, "API_KEY", "")

    assert "CLAUDE_WEB_DEEPSEEK_API_KEY" in deepseek_provider.unavailable_reason()


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "http://127.0.0.1:8123", "http://localhost:9"])
def test_https_or_a_loopback_stand_in_is_accepted(monkeypatch, url):
    monkeypatch.setattr(deepseek_provider, "BASE_URL", url)

    assert deepseek_provider.unavailable_reason() is None


@pytest.mark.parametrize("url", [
    # Plain http to another machine would send the key in the clear.
    "http://api.deepseek.com",
    "https://api.deepseek.com/anthropic",
    "https://user:pass@api.deepseek.com",
    "ftp://api.deepseek.com",
])
def test_anything_else_is_refused(monkeypatch, url):
    monkeypatch.setattr(deepseek_provider, "BASE_URL", url)

    assert deepseek_provider.unavailable_reason() == deepseek_provider.BAD_ORIGIN


# ── The live model list ───────────────────────────────────────────────────────

def test_the_live_list_becomes_picker_entries(deepseek_api):
    flash, pro = run(deepseek_provider.list_models())

    assert flash["key"] == "deepseek-flash"
    assert flash["label"] == "DeepSeek V4.1 Flash"
    assert flash["context"] == 1048576
    assert flash["efforts"] == ["low", "high", "max"]
    assert flash["default_effort"] == "high"
    assert flash["vision"] is True and flash["thinking"] is True
    assert flash["is_default"] is True
    assert pro["key"] == "deepseek-v4-pro"
    assert pro["vision"] is False
    assert pro["is_default"] is False


def test_a_new_model_appears_without_a_code_change(deepseek_api):
    newest = dict(MODELS_PAYLOAD["data"][0], id="deepseek-v5-flash", name="DeepSeek-V5-Flash")
    deepseek_api["models"] = {"data": [newest] + MODELS_PAYLOAD["data"]}

    keys = [model["key"] for model in run(deepseek_provider.list_models())]

    assert keys == ["deepseek-v5-flash", "deepseek-flash", "deepseek-v4-pro"]


def test_models_without_the_anthropic_endpoint_are_left_out(deepseek_api):
    chat_only = dict(MODELS_PAYLOAD["data"][1], id="deepseek-chat-only",
                     api_capabilities={"openai_chat": {}})
    deepseek_api["models"] = {"data": [chat_only, MODELS_PAYLOAD["data"][0]]}

    models = run(deepseek_provider.list_models())

    assert [model["key"] for model in models] == ["deepseek-flash"]
    assert models[0]["is_default"] is True


def test_effort_levels_the_sdk_cannot_send_are_dropped(deepseek_api):
    odd = dict(MODELS_PAYLOAD["data"][0], effort={"supported_levels": ["low", "ultra"], "default_level": "ultra"})
    deepseek_api["models"] = {"data": [odd]}

    (model,) = run(deepseek_provider.list_models())

    assert model["efforts"] == ["low"]
    assert model["default_effort"] is None


def test_the_list_is_cached_for_an_hour_and_per_key(deepseek_api, monkeypatch):
    run(deepseek_provider.list_models())
    run(deepseek_provider.list_models())
    assert len(deepseek_api["requests"]) == 1

    run(deepseek_provider.list_models(force=True))
    assert len(deepseek_api["requests"]) == 2

    monkeypatch.setattr(deepseek_provider, "API_KEY", "sk-another")
    run(deepseek_provider.list_models())
    assert len(deepseek_api["requests"]) == 3


def test_a_refused_key_says_what_to_check(deepseek_api):
    deepseek_api["status"] = 401

    with pytest.raises(ValueError, match="refused the API key"):
        run(deepseek_provider.list_models())


def test_an_empty_list_is_an_error_not_an_empty_picker(deepseek_api):
    deepseek_api["models"] = {"data": []}

    with pytest.raises(ValueError, match="no models"):
        run(deepseek_provider.list_models())


def test_a_run_gets_the_model_asked_for_or_the_first_listed(deepseek_api):
    assert run(deepseek_provider.model_for_run(""))["key"] == "deepseek-flash"
    assert run(deepseek_provider.model_for_run("deepseek-v4-pro"))["key"] == "deepseek-v4-pro"
    with pytest.raises(ValueError, match="no longer offers"):
        run(deepseek_provider.model_for_run("deepseek-r1"))


# ── What a run is spawned with ────────────────────────────────────────────────

def test_effort_reaches_the_sdk_and_images_follow_the_model(deepseek_api):
    flash, pro = run(deepseek_provider.list_models())

    options = deepseek_provider.sdk_options(flash, "max")
    assert options["effort"] == "max"
    assert options["thinking"] == {"type": "adaptive"}
    assert options["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES"] == (
        "thinking,adaptive_thinking,effort,vision")
    # DeepSeek's own Claude Code guide compacts at 786432 on these models.
    assert options["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "786432"

    pro_options = deepseek_provider.sdk_options(pro, "")
    assert pro_options["effort"] == "high"
    assert "vision" not in pro_options["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES"]


def test_an_effort_the_model_lacks_is_refused(deepseek_api):
    flash, _pro = run(deepseek_provider.list_models())

    with pytest.raises(ValueError, match="does not support the selected effort"):
        deepseek_provider.sdk_options(flash, "medium")


def test_the_child_goes_to_deepseek_with_every_slot_pinned(deepseek_api):
    run(deepseek_provider.list_models())

    env = deepseek_provider.child_env("deepseek-flash")

    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    # Blank, not absent: a leftover Anthropic key would send the request to Anthropic.
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["OPENAI_API_KEY"] == ""
    # The CLI strips "[1m]" before sending and sizes its context handling to one million tokens.
    assert env["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-flash[1m]"
    assert env["CLAUDE_CODE_NO_MODEL_FALLBACK"] == "1"
    # Local's half-hour watchdogs exist for CPU inference; a hung DeepSeek call should fail at the CLI's defaults.
    assert "API_FORCE_IDLE_TIMEOUT" not in env
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in env


def test_a_model_without_a_listed_window_gets_no_suffix():
    assert deepseek_provider.child_env("deepseek-flash")["ANTHROPIC_MODEL"] == "deepseek-flash"


# ── Balance ───────────────────────────────────────────────────────────────────

def test_the_balance_is_read_as_deepseek_reports_it(deepseek_api):
    balance = run(deepseek_provider.balance())

    assert balance == {"sufficient": True, "balances": [
        {"currency": "USD", "total": "20.00", "granted": "0.00", "topped_up": "20.00"},
    ]}
