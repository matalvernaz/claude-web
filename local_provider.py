"""Local Ollama models carried by the existing Claude Agent SDK transport."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

import httpx


BASE_URL = os.getenv("CLAUDE_WEB_OLLAMA_URL", "").strip().rstrip("/")
MODEL_NAMES = tuple(dict.fromkeys(
    name.strip() for name in os.getenv("CLAUDE_WEB_OLLAMA_MODELS", "").split(",")
    if name.strip()
))
PERMISSION_MODES = ["default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"]
# Verified against Ollama's Anthropic converter in this release. Older
# releases turn an explicit disabled thinking setting back into enabled.
THINKING_CONTROL_VERSION = (0, 33, 3)
SDK_THINKING_BUDGET = 1024
# CPU inference can spend many minutes on prompt processing before the first
# streamed byte. Claude Code's watchdogs for a custom base URL default to five
# minutes between bytes and ten minutes per request; the stream limits clamp
# at thirty minutes inside the CLI.
STREAM_TIMEOUT_MS = 30 * 60 * 1000
REQUEST_TIMEOUT_MS = 60 * 60 * 1000
_CAPABILITY_ENV_NAMES = tuple(
    f"ANTHROPIC_DEFAULT_{tier}_MODEL_SUPPORTED_CAPABILITIES"
    for tier in ("FABLE", "OPUS", "SONNET", "HAIKU")
)
_MODEL_CACHE: dict[tuple[str, str], dict] = {}


def unavailable_reason() -> str | None:
    if not BASE_URL or not MODEL_NAMES:
        return "Set CLAUDE_WEB_OLLAMA_URL and CLAUDE_WEB_OLLAMA_MODELS."
    try:
        parsed = urlsplit(BASE_URL)
        _ = parsed.port
    except ValueError:
        return "CLAUDE_WEB_OLLAMA_URL must be an HTTP(S) server origin."
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        return "CLAUDE_WEB_OLLAMA_URL must be an HTTP(S) server origin."
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "ollama.com" or hostname.endswith(".ollama.com"):
        return "The local provider requires your own Ollama server."
    if any("cloud" in name.lower().split(":")[-1] for name in MODEL_NAMES):
        return "Cloud model tags are not supported by the local provider."
    return None


def models() -> list[dict]:
    return [
        dict(_MODEL_CACHE.get((BASE_URL, name), _model_entry(name)),
             is_default=index == 0)
        for index, name in enumerate(MODEL_NAMES)
    ]


def _model_entry(name: str) -> dict:
    return {
        "key": name, "model": name, "label": f"Local · {name}",
        "context": None, "max_context": None, "efforts": [],
        "default_effort": None, "effort_labels": {}, "effort_help": "",
        "thinking": False, "vision": False, "betas": [], "advisor": "",
        "is_default": bool(MODEL_NAMES and name == MODEL_NAMES[0]),
    }


def _model_metadata(name: str, info: dict, version: str) -> dict:
    entry = _model_entry(name)
    caps = info.get("capabilities") or []
    details = info.get("details") or {}
    family = details.get("family", "")
    model_info = info.get("model_info") or {}
    architecture = model_info.get("general.architecture", family)
    max_context = model_info.get(f"{architecture}.context_length")
    if not isinstance(max_context, int) or max_context <= 0:
        max_context = None
    # GGUF context_length is the model's maximum, not Ollama's configured
    # window. The server's automatic default is not exposed by /api/show.
    configured_context = None
    for line in (info.get("parameters") or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
            configured_context = int(parts[1]) or None
    if configured_context and max_context:
        configured_context = min(configured_context, max_context)
    entry.update({
        "context": configured_context, "max_context": max_context,
        "thinking": "thinking" in caps, "vision": "vision" in caps,
        "parameter_size": details.get("parameter_size"), "family": family,
        "server_version": version,
    })
    version_match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    supported = bool(version_match and tuple(
        int(part) for part in version_match.groups()
    ) >= THINKING_CONTROL_VERSION)
    if not entry["thinking"]:
        entry["effort_help"] = "This model does not have a thinking mode."
    elif not supported:
        entry["effort_help"] = "Thinking controls require Ollama 0.33.3 or newer."
    elif family in ("qwen3", "qwen3moe", "qwen35", "qwen35moe"):
        entry.update({
            "efforts": ["off", "on"], "default_effort": "on",
            "effort_labels": {"off": "Thinking off", "on": "Thinking on"},
            "effort_help": "Thinking can be switched on or off; token budgets are not enforced.",
        })
    elif family == "gptoss":
        entry.update({
            "efforts": ["low", "medium", "high"], "default_effort": "medium",
            "effort_labels": {"low": "Low", "medium": "Medium", "high": "High"},
            "effort_help": "Reasoning effort adjusts thinking; it cannot be fully disabled.",
        })
    else:
        entry["effort_help"] = "Adjustable thinking has not been verified for this model family."
    return entry


async def check_model(name: str) -> dict:
    """Verify a configured local model and return metadata without running it."""
    reason = unavailable_reason()
    if reason:
        raise ValueError(reason)
    if name not in MODEL_NAMES:
        raise ValueError("Unknown local model.")
    _MODEL_CACHE.pop((BASE_URL, name), None)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(f"{BASE_URL}/api/show", json={"model": name})
        if response.status_code == 404:
            raise ValueError(f"Ollama model {name!r} is not installed on this server.")
        response.raise_for_status()
        info = response.json()
        if info.get("remote_host") or info.get("remote_model"):
            raise ValueError("Remote Ollama models are not supported by the local provider.")
        if "tools" not in (info.get("capabilities") or []):
            raise ValueError(f"Ollama model {name!r} does not support tool calling.")
        version = ""
        try:
            version_response = await client.get(f"{BASE_URL}/api/version")
            version_response.raise_for_status()
            version = str(version_response.json().get("version") or "")
        except (httpx.HTTPError, ValueError):
            pass
    entry = _model_metadata(name, info, version)
    _MODEL_CACHE[(BASE_URL, name)] = entry
    return dict(entry)


def sdk_options(metadata: dict, effort: str = "") -> dict:
    """Map verified model controls to SDK options, rejecting unsupported effort."""
    if effort and effort not in metadata.get("efforts", []):
        raise ValueError("This local model does not support the selected effort.")
    selected = effort or metadata.get("default_effort")
    if selected == "off" or not metadata.get("thinking"):
        options = {"thinking": {"type": "disabled"}}
    elif selected == "on":
        # The SDK requires a positive budget to emit thinking.type=enabled.
        # Ollama ignores this budget; the only control offered is on/off.
        options = {"thinking": {"type": "enabled", "budget_tokens": SDK_THINKING_BUDGET}}
    elif selected in ("low", "medium", "high"):
        options = {"thinking": {"type": "adaptive"}, "effort": selected}
    else:
        options = {"thinking": {"type": "adaptive"}}
    # Claude Code omits disabled thinking for unknown model names, and
    # promotes enabled thinking to adaptive. Its default effort then turns
    # Ollama thinking back on. Preserve the explicit setting on the wire.
    body = {"thinking": options["thinking"]}
    capabilities = ["thinking"] if metadata.get("thinking") else []
    if "effort" in options:
        body["output_config"] = {"effort": options["effort"]}
        capabilities.extend(("adaptive_thinking", "effort"))
    if metadata.get("vision"):
        capabilities.append("vision")
    options["env"] = {
        "CLAUDE_CODE_EXTRA_BODY": json.dumps(body),
        **dict.fromkeys(_CAPABILITY_ENV_NAMES, ",".join(capabilities)),
    }
    return options


def child_env(model: str) -> dict[str, str]:
    """Override inherited cloud routing and credentials for this subprocess only."""
    env = {
        name: "" for name in (
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
            "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_BETAS", "ANTHROPIC_UNIX_SOCKET",
            "ANTHROPIC_API_HOST", "ANTHROPIC_CUSTOM_MODEL_OPTION",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "CLAUDE_CODE_EFFORT_LEVEL", "MAX_THINKING_TOKENS",
            "CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_EXTRA_METADATA",
            "CLAUDE_CODE_DISABLE_THINKING", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
            "CLAUDE_CODE_MODEL_CATALOG", "CLAUDE_CODE_MODEL_CATALOG_URL",
            *_CAPABILITY_ENV_NAMES,
        )
    }
    env.update({
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        "ANTHROPIC_DEFAULT_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": model,
        "CLAUDE_CODE_AUTO_MODE_MODEL": model,
        "CLAUDE_CODE_BG_CLASSIFIER_MODEL": model,
        "CLAUDE_CODE_NO_MODEL_FALLBACK": "1",
        # The CLI leaves the runtime's own idle timeout on for non-Anthropic
        # routes; it dropped silent connections at six minutes regardless
        # of the stream watchdog settings below.
        "API_FORCE_IDLE_TIMEOUT": "false",
        "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": str(STREAM_TIMEOUT_MS),
        "CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(STREAM_TIMEOUT_MS),
        "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": str(STREAM_TIMEOUT_MS),
        "API_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "CLAUDE_CODE_USE_VERTEX": "0",
        "CLAUDE_CODE_USE_FOUNDRY": "0",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
    })
    return env
