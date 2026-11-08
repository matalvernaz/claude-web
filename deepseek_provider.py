"""DeepSeek's hosted models, run through the same Claude Code transport as the Local provider.

DeepSeek serves an Anthropic-compatible API that Claude Code talks to directly, so a DeepSeek chat is an ordinary Claude Code session whose requests go to DeepSeek instead of Anthropic: the same tools, approval cards, plan mode, rewind and saved transcripts. What it leaves out is everything tied to an Anthropic account: failover, plan limits, the advisor and panel review.

DeepSeek is account-scoped like Claude and Codex. Each user links their own API keys on the Accounts page (app.py stores and resolves them); a host can also set a Shared key. Every call here takes the key of the account a run uses. The model list comes live from DeepSeek's /models endpoint, which also reports each model's context window, image support and effort levels, so nothing about the models is hardcoded here.

DeepSeek is off unless the host turns it on, because its privacy policy says data is stored and processed in the People's Republic of China and a host may not want its users able to send work there.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

import local_provider


# The optional Shared account's key, set by the host.
API_KEY = os.getenv("CLAUDE_WEB_DEEPSEEK_API_KEY", "").strip()
# Setting a Shared key turns DeepSeek on by itself, so hosts configured before accounts existed keep working.
ENABLED = (
    os.getenv("CLAUDE_WEB_DEEPSEEK_ENABLED", "").strip().lower() in ("1", "true", "yes")
    or bool(API_KEY)
)
# The API origin. Claude Code's requests go to /anthropic on it; /models and
# /user/balance sit at its root.
BASE_URL = os.getenv("CLAUDE_WEB_DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
LABEL = "DeepSeek (China)"
SHARED_LABEL = os.getenv("CLAUDE_WEB_DEEPSEEK_SHARED_ACCOUNT_LABEL", "Shared DeepSeek").strip() or "Shared DeepSeek"
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
NOT_ENABLED = "DeepSeek is off on this server. Set CLAUDE_WEB_DEEPSEEK_ENABLED=true, then restart claude-web."
BAD_ORIGIN = "CLAUDE_WEB_DEEPSEEK_BASE_URL must be an https server origin, such as https://api.deepseek.com."
NO_KEY = "Link a DeepSeek API key on the Accounts page to use DeepSeek."
REFUSED_KEY = "DeepSeek refused this API key. Check it on platform.deepseek.com and link it again."
# One entry per (origin, key fingerprint): {"fetched_at": float, "models": [...]}.
# The key is hashed so a key whose account is deleted doesn't sit in plaintext
# here until the process restarts.
_models_cache: dict[tuple[str, str], dict] = {}
# The list is small, but a host can see many keys over a long lifetime; keep the
# cache from growing without bound.
MODELS_CACHE_MAX_ENTRIES = 200


def cache_key(api_key: str) -> tuple[str, str]:
    """The _models_cache key for this account: the origin and a hash of the key."""
    digest = hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()
    return (BASE_URL, digest)


def _remember_models(key: tuple[str, str], entry: dict) -> None:
    _models_cache[key] = entry
    while len(_models_cache) > MODELS_CACHE_MAX_ENTRIES:
        oldest = min(_models_cache, key=lambda k: _models_cache[k]["fetched_at"])
        del _models_cache[oldest]


def anthropic_url() -> str:
    """Where Claude Code sends its requests."""
    return f"{BASE_URL}/anthropic"


def unavailable_reason(api_key: Optional[str] = None) -> Optional[str]:
    """Why DeepSeek can't run, or None when it can.

    Without a key this answers for the host: off, or a bad server address. With one (an empty string counts) it also answers for that account, so a user with no key is told how to link one.
    """
    if not ENABLED:
        return NOT_ENABLED
    try:
        parsed = urlsplit(BASE_URL)
        _ = parsed.port
    except ValueError:
        return BAD_ORIGIN
    if (not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        return BAD_ORIGIN
    secure = parsed.scheme == "https"
    loopback = parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
    if not (secure or loopback):
        return BAD_ORIGIN
    if api_key is not None and not api_key.strip():
        return NO_KEY
    return None


async def _get(path: str, api_key: str) -> dict:
    """GET one DeepSeek API path with this account's key, turning a refused key into an error a person can act on."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code in (401, 403):
        raise ValueError(REFUSED_KEY)
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


async def list_models(api_key: str, force: bool = False) -> list[dict]:
    """DeepSeek's current models as this account sees them, fetched live and kept for an hour."""
    reason = unavailable_reason(api_key)
    if reason:
        raise ValueError(reason)
    key = cache_key(api_key)
    cached = _models_cache.get(key)
    now = time.monotonic()
    if not force and cached and now - cached["fetched_at"] < MODELS_CACHE_TTL_SECONDS:
        return [dict(model) for model in cached["models"]]
    payload = await _get("/models", api_key)
    entries = []
    for raw in payload.get("data") or []:
        entry = _model_entry(raw, is_default=not entries)
        if entry is not None:
            entries.append(entry)
    if not entries:
        raise ValueError("DeepSeek listed no models that work with Claude Code.")
    _remember_models(key, {"fetched_at": now, "models": entries})
    return [dict(model) for model in entries]


async def check_model(name: str, api_key: str) -> dict:
    """Metadata for one DeepSeek model, checked against the live list."""
    for model in await list_models(api_key):
        if model["key"] == name:
            return model
    raise ValueError(f"DeepSeek no longer offers {name!r}. Pick another DeepSeek model.")


async def model_for_run(name: str, api_key: Optional[str] = None) -> dict:
    """The model a run should use: the one asked for, or DeepSeek's first listed model when none was."""
    api_key = api_key or ""
    if name:
        return await check_model(name, api_key)
    models = await list_models(api_key)
    return next((model for model in models if model["is_default"]), models[0])


def _cached_context(model: str, api_key: str) -> Optional[int]:
    """The context window last listed for `model` on this account, without a network call."""
    cached = _models_cache.get(cache_key(api_key)) or {}
    for entry in cached.get("models") or []:
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
        # The env's model slots carry "[1m]", but an explicit --model wins over
        # them, so the flag has to carry it too or the CLI sizes this run to (or
        # silently trims) a 200k window.
        model = metadata.get("model")
        if model and context >= ONE_MILLION_TOKENS:
            options["model"] = f"{model}[1m]"
    options["env"] = env
    return options


def wire_model_name(model: str, api_key: Optional[str] = None) -> str:
    """The model name to hand Claude Code: with "[1m]" when the listed window is a million tokens.

    The CLI reads the suffix as "size context handling to a million tokens" and
    strips it before sending. Every path that spawns a run checks the model
    against the live list with the same key first (model_for_run), so the cache
    already holds it here.
    """
    context = _cached_context(model, api_key or "")
    if context and context >= ONE_MILLION_TOKENS:
        return f"{model}[1m]"
    return model


def child_env(model: str, api_key: Optional[str] = None) -> dict[str, str]:
    """Route this subprocess's Claude Code to DeepSeek on this account's key, with every model slot on `model`."""
    return local_provider.routed_child_env(
        anthropic_url(), api_key or "", wire_model_name(model, api_key),
    )


async def balance(api_key: str) -> dict:
    """What's left on the DeepSeek account behind this key, as DeepSeek reports it."""
    reason = unavailable_reason(api_key)
    if reason:
        raise ValueError(reason)
    payload = await _get("/user/balance", api_key)
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


async def validate_key(api_key: str) -> dict:
    """Check a key with DeepSeek before it's saved; returns its balance, raises ValueError if refused."""
    return await balance(api_key)
