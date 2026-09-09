"""Local model discovery, thinking controls, and subprocess routing isolation."""
import json

import httpx
import pytest

import local_provider


@pytest.fixture(autouse=True)
def local_config(monkeypatch):
    monkeypatch.setattr(local_provider, "BASE_URL", "http://storage.test:11434")
    monkeypatch.setattr(local_provider, "MODEL_NAMES", ("qwen3:30b",))
    monkeypatch.setattr(local_provider, "_MODEL_CACHE", {})


@pytest.fixture
def ollama(monkeypatch):
    client_type = httpx.AsyncClient

    def install(info, *, show_status=200, version="0.33.3", version_status=200):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/api/show":
                assert request.method == "POST"
                assert json.loads(request.content) == {"model": "qwen3:30b"}
                return httpx.Response(show_status, json=info)
            assert request.method == "GET"
            assert request.url.path == "/api/version"
            return httpx.Response(version_status, json={"version": version})

        def client(**kwargs):
            assert kwargs["trust_env"] is False
            return client_type(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(local_provider.httpx, "AsyncClient", client)
        return requests

    return install


def model_info(family="qwen3", *, thinking=True, parameters="", context=262144):
    return {
        "capabilities": ["completion", "tools"] + (["thinking"] if thinking else []),
        "details": {"family": family, "parameter_size": "30B"},
        "model_info": {
            "general.architecture": family,
            f"{family}.context_length": context,
        },
        "parameters": parameters,
    }


@pytest.mark.parametrize("url", [
    "", "ftp://storage.test", "http://", "http://storage.test:invalid",
    "http://storage.test:65536", "http://[::1", "http://storage.test/api",
    "http://user:password@storage.test", "http://storage.test?query=1",
    "http://storage.test#fragment", "https://ollama.com", "https://api.ollama.com",
    "https://OLLAMA.COM.",
])
def test_invalid_or_cloud_origin_is_unavailable(monkeypatch, url):
    monkeypatch.setattr(local_provider, "BASE_URL", url)
    assert local_provider.unavailable_reason()


@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://[::1]:11434", "http://192.168.1.10:11434",
    "https://storage.test", "http://storage.test:11434/",
])
def test_self_hosted_origins_are_available(monkeypatch, url):
    monkeypatch.setattr(local_provider, "BASE_URL", url)
    assert local_provider.unavailable_reason() is None


@pytest.mark.parametrize("names", [(), ("qwen3:cloud",), ("qwen3:30b-CLOUD",)])
def test_missing_or_cloud_models_are_unavailable(monkeypatch, names):
    monkeypatch.setattr(local_provider, "MODEL_NAMES", names)
    assert local_provider.unavailable_reason()


async def test_unknown_model_is_rejected_before_network(monkeypatch):
    def unexpected_client(**kwargs):
        pytest.fail("Unknown models must not reach the server")

    monkeypatch.setattr(local_provider.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="Unknown local model"):
        await local_provider.check_model("other:latest")


@pytest.mark.parametrize(("info", "status", "error"), [
    ({"error": "not found"}, 404, "not installed"),
    ({"capabilities": ["completion"]}, 200, "does not support tool calling"),
    ({"capabilities": ["tools"], "remote_host": "https://ollama.com"}, 200, "Remote Ollama"),
    ({"capabilities": ["tools"], "remote_model": "qwen3:cloud"}, 200, "Remote Ollama"),
])
async def test_unusable_models_are_rejected_and_stale_metadata_removed(ollama, info, status, error):
    key = (local_provider.BASE_URL, "qwen3:30b")
    local_provider._MODEL_CACHE[key] = {"efforts": ["off", "on"]}
    requests = ollama(info, show_status=status)
    with pytest.raises(ValueError, match=error):
        await local_provider.check_model("qwen3:30b")
    assert len(requests) == 1
    assert key not in local_provider._MODEL_CACHE


async def test_http_server_error_is_not_reported_as_model_metadata(ollama):
    ollama({"error": "server unavailable"}, show_status=503)
    with pytest.raises(httpx.HTTPStatusError):
        await local_provider.check_model("qwen3:30b")


async def test_installed_model_is_discovered_without_inference_and_cached(ollama):
    info = model_info(parameters="temperature 0.6\nnum_ctx 32768")
    info["capabilities"].append("vision")
    requests = ollama(info)
    result = await local_provider.check_model("qwen3:30b")
    assert [request.url.path for request in requests] == ["/api/show", "/api/version"]
    assert result["context"] == 32768
    assert result["max_context"] == 262144
    assert result["vision"] is True
    assert result["parameter_size"] == "30B"
    assert local_provider.models()[0] == result
    result["context"] = 1
    assert local_provider.models()[0]["context"] == 32768


def test_undiscovered_models_do_not_invent_context_or_thinking_controls(monkeypatch):
    monkeypatch.setattr(local_provider, "MODEL_NAMES", ("qwen3:30b", "other:latest"))
    first, second = local_provider.models()
    assert first["is_default"] is True
    assert second["is_default"] is False
    assert first["context"] is None
    assert first["max_context"] is None
    assert first["efforts"] == []
    assert first["default_effort"] is None


@pytest.mark.parametrize(("parameters", "maximum", "expected"), [
    ("", 262144, None),
    ("num_ctx 65536", 262144, 65536),
    ("num_ctx 65536", 32768, 32768),
    ("num_ctx 8192", None, 8192),
    ("num_ctx 0", 262144, None),
    ("num_ctx -1", 262144, None),
    ("num_ctx invalid", 262144, None),
])
def test_context_is_actual_configured_window(parameters, maximum, expected):
    result = local_provider._model_metadata(
        "qwen3:30b", model_info(parameters=parameters, context=maximum), "0.33.3",
    )
    assert result["context"] == expected
    assert result["max_context"] == maximum


@pytest.mark.parametrize("maximum", ["262144", -1, 0, None])
def test_invalid_maximum_context_is_unknown(maximum):
    result = local_provider._model_metadata("qwen3:30b", model_info(context=maximum), "0.33.3")
    assert result["max_context"] is None
    assert result["context"] is None


@pytest.mark.parametrize("family", ["qwen3", "qwen3moe", "qwen35", "qwen35moe"])
def test_verified_qwen_families_offer_only_thinking_toggle(family):
    result = local_provider._model_metadata("qwen3:30b", model_info(family), "0.33.3")
    assert result["efforts"] == ["off", "on"]
    assert result["default_effort"] == "on"
    assert "budgets are not enforced" in result["effort_help"]


@pytest.mark.parametrize("version", ["", "0.33.2", "unknown", "0.33.3-rc1", "0.34.0-dev"])
async def test_unverified_server_versions_offer_no_effort_controls(ollama, version):
    ollama(model_info(), version=version)
    result = await local_provider.check_model("qwen3:30b")
    assert result["thinking"] is True
    assert result["efforts"] == []
    with pytest.raises(ValueError, match="does not support the selected effort"):
        local_provider.sdk_options(result, "off")


async def test_missing_server_version_does_not_block_tool_capable_model(ollama):
    ollama(model_info(), version_status=404)
    result = await local_provider.check_model("qwen3:30b")
    assert result["server_version"] == ""
    assert result["efforts"] == []


def test_unknown_thinking_family_offers_no_verified_controls():
    result = local_provider._model_metadata("other:latest", model_info("other"), "0.33.3")
    assert result["efforts"] == []
    assert "not been verified" in result["effort_help"]


@pytest.mark.parametrize(("effort", "thinking"), [
    ("off", {"type": "disabled"}),
    ("on", {"type": "enabled", "budget_tokens": 1024}),
    ("", {"type": "enabled", "budget_tokens": 1024}),
])
def test_qwen_toggle_preserves_explicit_thinking_on_wire(effort, thinking):
    metadata = local_provider._model_metadata("qwen3:30b", model_info(), "0.33.3")
    options = local_provider.sdk_options(metadata, effort)
    assert options["thinking"] == thinking
    assert "effort" not in options
    assert json.loads(options["env"]["CLAUDE_CODE_EXTRA_BODY"]) == {"thinking": thinking}
    for tier in ("FABLE", "OPUS", "SONNET", "HAIKU"):
        assert options["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL_SUPPORTED_CAPABILITIES"] == "thinking"


@pytest.mark.parametrize("effort", ["", "low", "medium", "high"])
def test_gptoss_maps_effort_to_anthropic_output_config(effort):
    metadata = local_provider._model_metadata("gpt-oss:20b", model_info("gptoss"), "v0.33.3")
    assert metadata["efforts"] == ["low", "medium", "high"]
    options = local_provider.sdk_options(metadata, effort)
    assert options["effort"] == (effort or "medium")
    assert json.loads(options["env"]["CLAUDE_CODE_EXTRA_BODY"]) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort or "medium"},
    }
    assert options["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"] == "thinking,adaptive_thinking,effort"


def test_non_thinking_model_explicitly_disables_thinking():
    metadata = local_provider._model_metadata("coder:latest", model_info(thinking=False), "0.33.3")
    metadata["vision"] = True
    assert metadata["efforts"] == []
    options = local_provider.sdk_options(metadata)
    assert json.loads(options["env"]["CLAUDE_CODE_EXTRA_BODY"]) == {"thinking": {"type": "disabled"}}
    assert options["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"] == "vision"


@pytest.mark.parametrize(("family", "effort"), [("qwen3", "high"), ("gptoss", "off")])
def test_effort_from_different_family_is_rejected(family, effort):
    metadata = local_provider._model_metadata("local:latest", model_info(family), "0.33.3")
    with pytest.raises(ValueError, match="does not support the selected effort"):
        local_provider.sdk_options(metadata, effort)


def test_child_environment_keeps_all_model_routes_on_local_server():
    env = local_provider.child_env("qwen3:30b")
    assert env["ANTHROPIC_BASE_URL"] == "http://storage.test:11434"
    for name in (
        "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
        "CLAUDE_CODE_AUTO_MODE_MODEL", "CLAUDE_CODE_BG_CLASSIFIER_MODEL",
    ):
        assert env[name] == "qwen3:30b"
    assert env["CLAUDE_CODE_NO_MODEL_FALLBACK"] == "1"
    for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        assert env[name] == "0"
    # CPU prompt processing of a full Claude Code prompt exceeds the CLI's
    # five-minute first-byte default and its ten-minute request default.
    for name in ("CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS", "CLAUDE_STREAM_IDLE_TIMEOUT_MS",
                 "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS"):
        assert int(env[name]) == 30 * 60 * 1000
    assert int(env["API_TIMEOUT_MS"]) == 60 * 60 * 1000


def test_child_environment_overrides_inherited_credentials_and_request_options(monkeypatch):
    inherited = {
        "ANTHROPIC_API_KEY": "parent-key",
        "ANTHROPIC_AUTH_TOKEN": "parent-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
        "ANTHROPIC_CUSTOM_HEADERS": "Authorization: parent-secret",
        "OPENAI_API_KEY": "parent-openai",
        "CLAUDE_CODE_EXTRA_BODY": '{"thinking":{"type":"enabled"},"model":"cloud"}',
        "CLAUDE_CODE_EFFORT_LEVEL": "high",
        "MAX_THINKING_TOKENS": "32000",
        "CLAUDE_CODE_MODEL_CATALOG_URL": "https://cloud.test/models",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES": "adaptive_thinking,effort",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    env = inherited | local_provider.child_env("qwen3:30b")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
    for name in inherited.keys() - {"ANTHROPIC_AUTH_TOKEN"}:
        assert env[name] == ""
    metadata = local_provider._model_metadata("qwen3:30b", model_info(), "0.33.3")
    env.update(local_provider.sdk_options(metadata, "off")["env"])
    assert json.loads(env["CLAUDE_CODE_EXTRA_BODY"]) == {"thinking": {"type": "disabled"}}
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"] == "thinking"
