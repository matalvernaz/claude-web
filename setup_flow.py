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
import json
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
    """True if the bundled CLI has any usable Claude credential.

    Checked in priority order: live env var, persisted API key file (the env
    var only gets populated by ``load_api_key_into_env`` during boot — without
    this branch, a fresh container ships with the file but reports
    ``unconfigured`` until first request), then OAuth credentials.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if API_KEY_FILE.exists():
        return True
    return credentials_path().exists()


def whoami() -> dict:
    """Describe the active credential without leaking the token itself.

    Mirrors ``is_configured``'s priority: the env-var/persisted-key path
    wins over OAuth, since that's what the SDK will pick up. Used by the
    /setup page to tell the user what's connected.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or API_KEY_FILE.exists():
        return {"mode": "api_key"}
    cred = credentials_path()
    if not cred.exists():
        return {"mode": "none"}
    try:
        data = json.loads(cred.read_text())
    except (OSError, ValueError):
        return {"mode": "oauth"}
    oauth = data.get("claudeAiOauth") or {}
    return {
        "mode": "oauth",
        "subscription_type": oauth.get("subscriptionType"),
        "expires_at": oauth.get("expiresAt"),
    }


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


async def _kill_and_reap(proc: asyncio.subprocess.Process, *, reap_timeout: float = 5.0) -> None:
    """Send SIGKILL and wait for the process to exit so we don't leave zombies.

    asyncio.subprocess.Process needs an explicit wait() after kill() — without
    it the OS keeps the entry around until the parent reaps it, which never
    happens for an orphaned background subprocess. Idempotent on
    already-finished processes.
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=reap_timeout)
    except asyncio.TimeoutError:
        log.warning("subprocess %s did not exit after kill within %ss", proc.pid, reap_timeout)


async def sign_out() -> None:
    """Remove every form of stored Claude credential."""
    # Best-effort: ask the CLI to log out so the OAuth refresh token is
    # revoked server-side. If the CLI hangs or fails, kill+reap and proceed.
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "logout",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("claude auth logout timed out; killing")
            await _kill_and_reap(proc)
    except FileNotFoundError:
        log.warning("claude CLI not on PATH; skipping logout subprocess")
    except Exception as e:
        log.warning("claude auth logout failed: %r", e)
        if proc is not None:
            await _kill_and_reap(proc)

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

    proc: Optional[asyncio.subprocess.Process] = None
    try:
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

        # Read until we see the "Paste code" prompt. We deliberately don't
        # extract the URL incrementally — a 256-byte chunk can split the URL
        # in half and we'd capture a truncated string. Once the prompt has
        # arrived, the full URL line is guaranteed to be in the buffer.
        buf = bytearray()
        saw_prompt = False
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=30)
            except asyncio.TimeoutError:
                if state.status != "cancelled":
                    state.status = "failed"
                    state.error = "claude auth login stalled before printing the sign-in URL"
                return
            if not chunk:
                break
            buf.extend(chunk)
            if _PROMPT_MARKER in buf:
                saw_prompt = True
                break

        # An external cancel_flow may have flipped state.status to "cancelled"
        # while we were reading. Don't overwrite it with a "failed" verdict
        # derived from the EOF we now see.
        if state.status == "cancelled":
            return

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
            return

        if state.status == "cancelled":
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
            rc = -1

        if state.status == "cancelled":
            return

        if rc == 0 and credentials_path().exists():
            state.status = "done"
            return

        state.status = "failed"
        tail = buf.decode("utf-8", errors="replace").strip()
        state.error = tail or f"claude auth login exited with code {rc}"
    finally:
        # Single point of subprocess cleanup. Reached on every exit path
        # (normal return, exception, CancelledError), so a CancelledError
        # raised mid-spawn or mid-read can't leak the process. Idempotent
        # on already-finished processes.
        if proc is not None:
            await _kill_and_reap(proc)


async def start_oauth(variant: OAuthVariant) -> OAuthFlowState:
    """Begin a new OAuth flow, cancelling any one already in progress.

    Returns once the URL is known (or the flow has already failed), so the
    HTTP response can deliver the link without a second round-trip.
    """
    global _current
    async with _flow_lock:
        prior = _current
        if prior and prior.status in ("starting", "awaiting_code", "exchanging"):
            prior.status = "cancelled"
            prior.code_event.set()
            if prior.proc is not None:
                await _kill_and_reap(prior.proc)
            # Cancel the driver task directly so we don't depend on it
            # noticing the status change. Awaiting it here keeps shutdown
            # deterministic — without this, the new flow could race the old
            # task's tail-end output handling.
            if prior.driver_task is not None and not prior.driver_task.done():
                prior.driver_task.cancel()
                try:
                    await asyncio.wait_for(prior.driver_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        state = OAuthFlowState(variant=variant)
        state.driver_task = asyncio.create_task(_drive(state))
        _current = state

    # Wait up to ~30s for the URL or a terminal status. The driver itself
    # gives the CLI 30s per stdout chunk; if we returned earlier the HTTP
    # response would carry url=None and the browser would have to poll for
    # it via /api/setup/status anyway. The polling loop in setup.js handles
    # both cases, but a synchronous URL on the first response is friendlier.
    for _ in range(300):
        if state.url or state.status in ("failed", "done"):
            break
        await asyncio.sleep(0.1)
    return state


def current_flow() -> Optional[OAuthFlowState]:
    return _current


async def submit_code(code: str) -> OAuthFlowState:
    # Capture the global into a local before any await — without this, a
    # concurrent start_oauth() (which swaps `_current` for a fresh state
    # object) could cause us to write the code into the new flow's
    # code_event before it reaches `awaiting_code`, instantly failing the
    # new flow with stale code.
    flow = _current
    if flow is None:
        raise RuntimeError("no flow in progress")
    if flow.status != "awaiting_code":
        raise RuntimeError(f"flow is in status {flow.status!r}, not awaiting_code")
    flow.code = code
    flow.code_event.set()
    # Block the HTTP response until the exchange is done, so the browser
    # can redirect immediately on success.
    deadline = EXCHANGE_TIMEOUT_SECONDS + 5
    for _ in range(deadline * 10):
        if flow.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    return flow


async def cancel_flow() -> None:
    flow = _current
    if flow is None:
        return
    if flow.status in ("done", "failed", "cancelled"):
        return
    flow.status = "cancelled"
    flow.code_event.set()
    # _drive's finally clause will _kill_and_reap the subprocess; we don't
    # need to do it here. But the driver may be blocked on stdin/code_event
    # await — set the event (already done) and let it unwind.
