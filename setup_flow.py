"""Interactive setup for the in-container Claude Code authentication.

Until the bundled `claude` CLI has credentials, the main UI redirects to
/setup. Two flows are supported:

  * **subscription** — drives `claude auth login` (Claude.ai) or
    `claude auth login --console` (Anthropic Console) as a subprocess.
    The CLI prints "If the browser didn't open, visit: <URL>" and then
    blocks on stdin reading "Paste code here if prompted > ". We scrape
    the URL, surface it to the browser, and pipe the user's pasted code
    back into stdin to complete the exchange.
  * **api_key** — the user pastes an Anthropic API key. We persist it to
    ``$CLAUDE_WEB_STATE_DIR/anthropic_api_key`` (mode 0600) and load it
    into ``ANTHROPIC_API_KEY`` at app startup so the CLI/SDK pick it up.

Detection (``is_configured``) is the union of both: a credentials file
written by ``claude auth login``, an env var, or our persisted key file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


log = logging.getLogger(__name__)

CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).resolve()
STATE_DIR = Path(os.getenv("CLAUDE_WEB_STATE_DIR", str(Path.home() / ".claude-web"))).resolve()
API_KEY_FILE = STATE_DIR / "anthropic_api_key"

# `claude auth login` prints the URL on its own line. Match the first
# https:// run of non-whitespace, which is correct because the URL has no
# spaces (only %-encoded query separators).
_URL_RE = re.compile(r"https://\S+")

# Marker the CLI prints when it's done emitting the URL and is now blocking
# on stdin. Stable across both --claudeai and --console variants as of
# claude 2.1.x.
_PROMPT_MARKER = b"Paste code"

# How long the user has to paste the code before we tear down the flow.
CODE_TIMEOUT_SECONDS = 600
# How long the post-code exchange may take (network round-trip to Anthropic).
EXCHANGE_TIMEOUT_SECONDS = 60


def credentials_path() -> Path:
    return CLAUDE_HOME / ".credentials.json"


def load_api_key_into_env() -> Optional[str]:
    """Idempotent: read the persisted key into ``ANTHROPIC_API_KEY`` if set.

    Called once at app startup. An env var passed in at container start
    wins (so users with their own secret manager aren't overridden).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        key = API_KEY_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        return key
    return None


def is_configured() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    return credentials_path().exists()


def save_api_key(key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("api key is empty")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(key)
    try:
        API_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    os.environ["ANTHROPIC_API_KEY"] = key


async def sign_out() -> None:
    """Remove every form of stored Claude credential."""
    # Best-effort: ask the CLI to log out so the OAuth refresh token is
    # revoked server-side. If that fails (CLI already in a bad state, no
    # network, etc.) we still nuke the local files below.
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "logout",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
    except Exception as e:
        log.warning("claude auth logout failed: %r", e)

    for p in (credentials_path(), API_KEY_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    os.environ.pop("ANTHROPIC_API_KEY", None)


# ── OAuth subprocess driver ────────────────────────────────────────────

OAuthVariant = Literal["claudeai", "console"]
FlowStatus = Literal[
    "starting", "awaiting_code", "exchanging", "done", "failed", "cancelled"
]


@dataclass
class OAuthFlowState:
    variant: OAuthVariant
    status: FlowStatus = "starting"
    url: Optional[str] = None
    error: Optional[str] = None
    proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    driver_task: Optional[asyncio.Task] = field(default=None, repr=False)
    code_event: asyncio.Event = field(default_factory=asyncio.Event)
    code: Optional[str] = None

    def to_public(self) -> dict:
        return {
            "variant": self.variant,
            "status": self.status,
            "url": self.url,
            "error": self.error,
        }


_flow_lock = asyncio.Lock()
_current: Optional[OAuthFlowState] = None


async def _drive(state: OAuthFlowState) -> None:
    args = ["claude", "auth", "login"]
    if state.variant == "console":
        args.append("--console")

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        state.status = "failed"
        state.error = "claude CLI not found on PATH"
        return
    state.proc = proc
    assert proc.stdout is not None and proc.stdin is not None

    # Read until we see the "Paste code" prompt. We deliberately don't try
    # to extract the URL incrementally — a 256-byte chunk can split the URL
    # in half and we'd capture a truncated string. Once the prompt has
    # arrived, the full URL line is guaranteed to be in the buffer.
    buf = bytearray()
    saw_prompt = False
    while True:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=30)
        except asyncio.TimeoutError:
            state.status = "failed"
            state.error = "claude auth login stalled before printing the sign-in URL"
            proc.kill()
            return
        if not chunk:
            break
        buf.extend(chunk)
        if _PROMPT_MARKER in buf:
            saw_prompt = True
            break

    m = _URL_RE.search(buf.decode("utf-8", errors="replace"))
    if m:
        state.url = m.group(0).rstrip(".,)")

    if state.url is None:
        state.status = "failed"
        state.error = (
            buf.decode("utf-8", errors="replace").strip()
            or "claude auth login exited without printing a sign-in URL"
        )
        return

    if not saw_prompt:
        state.status = "failed"
        state.error = (
            buf.decode("utf-8", errors="replace").strip()
            or "claude auth login closed before requesting an auth code"
        )
        return

    state.status = "awaiting_code"

    try:
        await asyncio.wait_for(state.code_event.wait(), timeout=CODE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        state.status = "failed"
        state.error = "Timed out waiting for the auth code from the browser"
        if proc.returncode is None:
            proc.kill()
        return

    if state.status == "cancelled":
        if proc.returncode is None:
            proc.kill()
        return

    state.status = "exchanging"
    code = ((state.code or "").strip() + "\n").encode()
    try:
        proc.stdin.write(code)
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    # Drain remaining stdout so an error message can surface.
    try:
        rest = await asyncio.wait_for(proc.stdout.read(), timeout=EXCHANGE_TIMEOUT_SECONDS)
        if rest:
            buf.extend(rest)
    except asyncio.TimeoutError:
        pass

    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        rc = -1

    if rc == 0 and credentials_path().exists():
        state.status = "done"
        return

    state.status = "failed"
    tail = buf.decode("utf-8", errors="replace").strip()
    state.error = tail or f"claude auth login exited with code {rc}"


async def start_oauth(variant: OAuthVariant) -> OAuthFlowState:
    """Begin a new OAuth flow, cancelling any one already in progress.

    Returns once the URL is known (or the flow has already failed), so the
    HTTP response can deliver the link without a second round-trip.
    """
    global _current
    async with _flow_lock:
        if _current and _current.status in ("starting", "awaiting_code", "exchanging"):
            _current.status = "cancelled"
            _current.code_event.set()
            if _current.proc and _current.proc.returncode is None:
                try:
                    _current.proc.kill()
                except ProcessLookupError:
                    pass
        state = OAuthFlowState(variant=variant)
        state.driver_task = asyncio.create_task(_drive(state))
        _current = state

    for _ in range(100):  # up to ~10s
        if state.url or state.status in ("failed", "done"):
            break
        await asyncio.sleep(0.1)
    return state


def current_flow() -> Optional[OAuthFlowState]:
    return _current


async def submit_code(code: str) -> OAuthFlowState:
    if _current is None:
        raise RuntimeError("no flow in progress")
    if _current.status != "awaiting_code":
        raise RuntimeError(f"flow is in status {_current.status!r}, not awaiting_code")
    _current.code = code
    _current.code_event.set()
    # Block the HTTP response until the exchange is done, so the browser
    # can redirect immediately on success.
    deadline = EXCHANGE_TIMEOUT_SECONDS + 5
    for _ in range(deadline * 10):
        if _current.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    return _current


async def cancel_flow() -> None:
    if _current is None:
        return
    if _current.status in ("done", "failed", "cancelled"):
        return
    _current.status = "cancelled"
    _current.code_event.set()
    if _current.proc and _current.proc.returncode is None:
        try:
            _current.proc.kill()
        except ProcessLookupError:
            pass
