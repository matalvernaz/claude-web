"""DeepSeek's hosted models, run through the same Claude Code transport as the Local provider.

DeepSeek serves an Anthropic-compatible API that Claude Code talks to directly, so a DeepSeek chat is an ordinary Claude Code session whose requests go to DeepSeek instead of Anthropic: the same tools, approval cards, plan mode, rewind and saved transcripts. What it leaves out is everything tied to an Anthropic account: sign-in slots, failover, plan limits, the advisor and panel review.

One API key, set by the host in CLAUDE_WEB_DEEPSEEK_API_KEY, serves every user who picks DeepSeek. The model list comes live from DeepSeek's /models endpoint, which also reports each model's context window, image support and effort levels, so nothing about the models is hardcoded here.
"""
from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

import local_provider


API_KEY = os.getenv("CLAUDE_WEB_DEEPSEEK_API_KEY", "").strip()
# The API origin. Claude Code's requests go to /anthropic on it; /models and
# /user/balance sit at its root.
BASE_URL = os.getenv("CLAUDE_WEB_DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
LABEL = "DeepSeek (China)"
PERMISSION_MODES = list(local_provider.PERMISSION_MODES)
# DeepSeek's model list changes when it ships a model, not from one request to the next.
MODELS_CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 10
# The effort levels the Agent SDK accepts. DeepSeek's own list is filtered through this, so a level DeepSeek adds later can't reach the SDK and fail its validation.
SDK_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Claude Code reads a "[1m]" suffix on a model name as "this model has a one-million-token window". It strips the suffix before sending (verified on CLI 2.1.319) and sizes its context handling to match; without it, the CLI assumes a 200k window for a model it doesn't know.
ONE_MILLION_TOKENS = 1_000_000
# DeepSeek's own Claude Code guide compacts at 786432 tokens on its 1048576-token models, which is three quarters of the window.
AUTO_COMPACT_SHARE = 0.75
# Plain http would send the API key in the clear, so it's only accepted for a stand-in server on this machine (the tests use one).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
BAD_ORIGIN = "CLAUDE_WEB_DEEPSEEK_BASE_URL must be an https server origin, such as https://api.deepseek.com."
_models_cache: dict = {"origin": None, "key": None, "fetched_at": 0.0, "models": []}


def anthropic_url() -> str:
    """Where Claude Code sends its requests."""
    return f"{BASE_URL}/anthropic"


def unavailable_reason() -> Optional[str]:
    """Why DeepSeek can't be offered on this host, or None when it can."""
    if not API_KEY:
        return "Set CLAUDE_WEB_DEEPSEEK_API_KEY to a DeepSeek API key, then restart claude-web."
    try:
        parsed = urlsplit(BASE_URL)
        _ = parsed.port
    except ValueError:
        return BAD_ORIGIN
    if (not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        return BAD_ORIGIN
    if parsed.scheme == "https":
        return None
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return None
    return BAD_ORIGIN


async def _get(path: str) -> dict:
    """GET one DeepSeek API path and return its JSON, turning a refused key into an error a person can act on."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {API_KEY}"},
        )
    if response.status_code in (401, 403):
        raise ValueError(
            "DeepSeek refused the API key. Check CLAUDE_WEB_DEEPSEEK_API_KEY, then restart claude-web."
        )
    response.raise_for_status()
    return response.json()


def _model_entry(raw: dict, is_default: bool) -> Optional[dict]:
    """Turn one /models record into a model-picker entry, or None when Claude Code can't use it."""
    key = str(raw.get("id") or "").strip()
    if not key:
        return None
    # Only models DeepSeek serves on its Anthropic endpoint work through Claude Code. A record without the field at all is let through, so a change to DeepSeek's listing can't silently empty the picker.
    api_capabilities = raw.get("api_capabilities")
    if isinstance(api_capabilities, dict) and "anthropic_messages" not in api_capabilities:
        return None
    context = raw.get("context_window")
    if not isinstance(context, int) or context <= 0:
        context = None
    effort = raw.get("effort") or {}
    offered = [
        level for level in (effort.get("supported_levels") or [])
        if level in SDK_EFFORT_LEVELS
    ]
    default_effort = effort.get("default_level")
    if default_effort not in offered:
        default_effort = None
    # DeepSeek's names read as "DeepSeek-V4.1-Flash"; with spaces a screen reader says them as words.
    label = str(raw.get("name") or key).replace("-", " ")
    return {
        "key": key, "model": key, "label": label,
        "context": context, "max_context": context,
        "efforts": offered, "default_effort": default_effort,
        "effort_labels": {level: level.capitalize() for level in offered},
        "effort_help": "",
        "thinking": bool(offered),
        "vision": "image" in (raw.get("input_modalities") or []),
        "betas": [], "advisor": "",
        "is_default": is_default,
    }


async def list_models(force: bool = False) -> list[dict]:
    """DeepSeek's current models, fetched live and kept for an hour."""
    reason = unavailable_reason()
    if reason:
        raise ValueError(reason)
    cache = _models_cache
    now = time.monotonic()
    fresh = (
        not force and cache["models"]
        and cache["origin"] == BASE_URL and cache["key"] == API_KEY
        and now - cache["fetched_at"] < MODELS_CACHE_TTL_SECONDS
    )
    if fresh:
        return [dict(model) for model in cache["models"]]
    payload = await _get("/models")
    entries = []
    for raw in payload.get("data") or []:
        entry = _model_entry(raw, is_default=not entries)
        if entry is not None:
            entries.append(entry)
    if not entries:
        raise ValueError("DeepSeek listed no models that work with Claude Code.")
    cache.update(origin=BASE_URL, key=API_KEY, fetched_at=now, models=entries)
    return [dict(model) for model in entries]


async def check_model(name: str) -> dict:
    """Metadata for one DeepSeek model, checked against the live list."""
    for model in await list_models():
        if model["key"] == name:
            return model
    raise ValueError(f"DeepSeek no longer offers {name!r}. Pick another DeepSeek model.")


async def model_for_run(name: str) -> dict:
    """The model a run should use: the one asked for, or DeepSeek's first listed model when none was."""
    if name:
        return await check_model(name)
    models = await list_models()
    return next((model for model in models if model["is_default"]), models[0])


def _cached_context(model: str) -> Optional[int]:
    """The context window last listed for `model`, without a network call."""
    for entry in _models_cache["models"]:
        if entry["key"] == model:
            return entry.get("context")
    return None


def sdk_options(metadata: dict, effort: str = "") -> dict:
    """Map the picked effort to SDK options, rejecting a level the model doesn't offer."""
    offered = metadata.get("efforts") or []
    if effort and effort not in offered:
        raise ValueError("This DeepSeek model does not support the selected effort.")
    selected = effort or metadata.get("default_effort") or ""
    thinks = bool(metadata.get("thinking"))
    # DeepSeek treats adaptive thinking as switched on and has no thinking budget to set; the effort level is the only dial.
    options: dict = {"thinking": {"type": "adaptive" if thinks else "disabled"}}
    if selected:
        options["effort"] = selected
    capabilities = ["thinking", "adaptive_thinking", "effort"] if thinks else []
    if metadata.get("vision"):
        capabilities.append("vision")
    env = dict.fromkeys(local_provider.CAPABILITY_ENV_NAMES, ",".join(capabilities))
    context = metadata.get("context")
    if context:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(context * AUTO_COMPACT_SHARE))
    options["env"] = env
    return options


def child_env(model: str) -> dict[str, str]:
    """Route this subprocess's Claude Code to DeepSeek, with every model slot on `model`.

    The "[1m]" suffix depends on the model's listed window. Every path that spawns a run checks the model against the live list first (model_for_run), so the cache already holds it here.
    """
    context = _cached_context(model)
    wire_model = model
    if context and context >= ONE_MILLION_TOKENS:
        wire_model = f"{model}[1m]"
    return local_provider.routed_child_env(anthropic_url(), API_KEY, wire_model)


async def balance() -> dict:
    """What's left on the DeepSeek account, as DeepSeek reports it."""
    reason = unavailable_reason()
    if reason:
        raise ValueError(reason)
    payload = await _get("/user/balance")
    balances = [
        {
            "currency": str(item.get("currency") or ""),
            "total": str(item.get("total_balance") or "0"),
            "granted": str(item.get("granted_balance") or "0"),
            "topped_up": str(item.get("topped_up_balance") or "0"),
        }
        for item in payload.get("balance_infos") or []
    ]
    # is_available is DeepSeek saying whether the balance can pay for a request.
    return {"sufficient": bool(payload.get("is_available")), "balances": balances}
