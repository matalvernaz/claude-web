"""Interactive setup for in-container Claude Code authentication.

Two flows are supported:

  * **subscription** — drives `claude auth login` (Claude.ai) or
    `claude auth login --console` (Anthropic Console) as a subprocess.
    The CLI prints "If the browser didn't open, visit: <URL>" and then
    blocks on stdin reading "Paste code here if prompted > ". We scrape
    the URL, surface it to the browser, and pipe the user's pasted code
    back into stdin to complete the exchange.
  * **api_key** — the user pastes an Anthropic API key. We persist it
    inside the target home (``.anthropic_api_key`` next to the OAuth
    credentials) so each credential slot is self-contained.

Multiple flows can be in flight at once, keyed by a caller-chosen string
("shared" for the in-container CLI's default home, "cred:<sub>:<id>"
for a per-user credential). Each flow targets its own
``CLAUDE_CONFIG_DIR`` so a user setting up their personal account never
trips the shared in-container CLI sign-in, and vice versa.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


log = logging.getLogger(__name__)

CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).resolve()
STATE_DIR = Path(os.getenv("CLAUDE_WEB_STATE_DIR", str(Path.home() / ".claude-web"))).resolve()
# Legacy shared-slot API key location. Still read for back-compat; new keys
# go into <home>/.anthropic_api_key so each slot is self-contained.
API_KEY_FILE = STATE_DIR / "anthropic_api_key"
SHARED_FLOW_KEY = "shared"

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


def credentials_path(home: Optional[Path] = None) -> Path:
    return (home or CLAUDE_HOME) / ".credentials.json"


def api_key_path(home: Optional[Path] = None) -> Path:
    """Per-home Anthropic API key file. The shared slot also keeps a
    legacy copy at ``STATE_DIR/anthropic_api_key`` so existing deployments
    keep working after upgrade."""
    return (home or CLAUDE_HOME) / ".anthropic_api_key"


def load_api_key_into_env() -> Optional[str]:
    """Idempotent: read the shared-slot persisted key into
    ``ANTHROPIC_API_KEY`` if set.

    Called once at app startup. An env var passed in at container start
    wins (so users with their own secret manager aren't overridden).
    Only the shared slot's key feeds ``ANTHROPIC_API_KEY`` — per-user
    credential slots are loaded on-demand when a run spawns.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for candidate in (api_key_path(CLAUDE_HOME), API_KEY_FILE):
        try:
            key = candidate.read_text().strip()
        except FileNotFoundError:
            continue
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return key
    return None


def is_configured(home: Optional[Path] = None) -> bool:
    """True if the given slot (default: shared) has any usable credential.

    For the shared slot, the boot-time env var counts as configured (the
    SDK will pick it up regardless of which home the CLI uses). For
    per-user homes, only on-disk credentials count.
    """
    if home is None and os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if api_key_path(home).exists():
        return True
    if home is None and API_KEY_FILE.exists():
        return True
    return credentials_path(home).exists()


def whoami(home: Optional[Path] = None) -> dict:
    """Describe the slot's credential without leaking the token itself."""
    if home is None and (os.environ.get("ANTHROPIC_API_KEY") or API_KEY_FILE.exists()):
        return {"mode": "api_key"}
    if api_key_path(home).exists():
        return {"mode": "api_key"}
    cred = credentials_path(home)
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


# Anthropic API keys start with ``sk-ant-`` followed by ~90 chars of
# url-safe alphabet (``[A-Za-z0-9_-]``). Accept a generous suffix length
# so future format changes don't reject still-valid keys at the boundary,
# but catch obvious paste mistakes (whole bash export line, junk text)
# before they hit the SDK and surface as an opaque 401 to the user.
_API_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")


def _atomic_write_secret(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` at mode 0o600 atomically.

    Creates a sibling temp file with O_CREAT|O_EXCL|0o600 so the bytes
    are never observable at the process umask, then ``os.replace`` to
    the target. Replaces ``path.write_text`` + ``path.chmod(0o600)``,
    which leaves the file at the default umask between those two calls
    — a real window for another local user to read a freshly-written
    API key on a shared host.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        # mkstemp gives 0o600 by default on POSIX; the explicit chmod is
        # defense-in-depth in case a future libc changes that or someone
        # ports this to a platform where mkstemp's default is laxer.
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as outf:
            outf.write(content)
            outf.flush()
            os.fsync(outf.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def save_api_key(key: str, home: Optional[Path] = None) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("api key is empty")
    if not _API_KEY_RE.fullmatch(key):
        raise ValueError(
            "api key doesn't match the expected sk-ant-* format. "
            "Paste only the key, not a whole `export ANTHROPIC_API_KEY=...` line."
        )
    target_home = home or CLAUDE_HOME
    _atomic_write_secret(api_key_path(target_home), key)
    # Only the shared slot feeds the process-wide env var; per-user keys
    # are pulled into the spawned CLI's env on demand.
    if home is None:
        _atomic_write_secret(API_KEY_FILE, key)
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


async def sign_out(home: Optional[Path] = None) -> None:
    """Remove every form of stored Claude credential for the given slot."""
    target_home = home or CLAUDE_HOME
    # Best-effort: ask the CLI to log out so the OAuth refresh token is
    # revoked server-side. If the CLI hangs or fails, kill+reap and proceed.
    proc: Optional[asyncio.subprocess.Process] = None
    try:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(target_home)
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "logout",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
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

    candidates = [credentials_path(target_home), api_key_path(target_home)]
    if home is None:
        candidates.append(API_KEY_FILE)
    for p in candidates:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    if home is None:
        os.environ.pop("ANTHROPIC_API_KEY", None)


# ── OAuth subprocess driver ────────────────────────────────────────────

OAuthVariant = Literal["claudeai", "console"]
FlowStatus = Literal[
    "starting", "awaiting_code", "exchanging", "done", "failed", "cancelled"
]


@dataclass
class OAuthFlowState:
    variant: OAuthVariant
    flow_key: str = SHARED_FLOW_KEY
    home: Path = field(default_factory=lambda: CLAUDE_HOME)
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
# Per-flow state, keyed by caller-supplied identifier. The shared slot
# uses SHARED_FLOW_KEY; per-user credential slots use "cred:<sub>:<id>".
_flows: dict[str, OAuthFlowState] = {}


async def _drive(state: OAuthFlowState) -> None:
    args = ["claude", "auth", "login"]
    if state.variant == "console":
        args.append("--console")

    proc: Optional[asyncio.subprocess.Process] = None
    try:
        try:
            env = dict(os.environ)
            env["CLAUDE_CONFIG_DIR"] = str(state.home)
            # The CLI inherits ANTHROPIC_API_KEY from the parent process,
            # which is set when the *shared* slot has an API key configured.
            # If we leave it set when running login for any slot, the CLI
            # skips OAuth entirely. Strip it so the user can actually sign
            # in interactively.
            env.pop("ANTHROPIC_API_KEY", None)
            state.home.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
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

        if rc == 0 and credentials_path(state.home).exists():
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


async def start_oauth(
    variant: OAuthVariant,
    *,
    flow_key: str = SHARED_FLOW_KEY,
    home: Optional[Path] = None,
) -> OAuthFlowState:
    """Begin a new OAuth flow for the given slot, cancelling any prior
    flow for that same slot.

    Returns once the URL is known (or the flow has already failed), so the
    HTTP response can deliver the link without a second round-trip.
    """
    target_home = home or CLAUDE_HOME
    async with _flow_lock:
        prior = _flows.get(flow_key)
        if prior and prior.status in ("starting", "awaiting_code", "exchanging"):
            prior.status = "cancelled"
            prior.code_event.set()
            if prior.proc is not None:
                await _kill_and_reap(prior.proc)
            if prior.driver_task is not None and not prior.driver_task.done():
                prior.driver_task.cancel()
                try:
                    await asyncio.wait_for(prior.driver_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        state = OAuthFlowState(variant=variant, flow_key=flow_key, home=target_home)
        state.driver_task = asyncio.create_task(_drive(state))
        _flows[flow_key] = state

    for _ in range(300):
        if state.url or state.status in ("failed", "done"):
            break
        await asyncio.sleep(0.1)
    return state


def current_flow(flow_key: str = SHARED_FLOW_KEY) -> Optional[OAuthFlowState]:
    return _flows.get(flow_key)


async def submit_code(code: str, *, flow_key: str = SHARED_FLOW_KEY) -> OAuthFlowState:
    # Capture into a local before any await — a concurrent start_oauth()
    # for the same key could swap _flows[key] for a fresh state.
    flow = _flows.get(flow_key)
    if flow is None:
        raise RuntimeError("no flow in progress")
    if flow.status != "awaiting_code":
        raise RuntimeError(f"flow is in status {flow.status!r}, not awaiting_code")
    flow.code = code
    flow.code_event.set()
    deadline = EXCHANGE_TIMEOUT_SECONDS + 5
    for _ in range(deadline * 10):
        if flow.status in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)
    return flow


async def cancel_flow(flow_key: str = SHARED_FLOW_KEY) -> None:
    flow = _flows.get(flow_key)
    if flow is None:
        return
    if flow.status in ("done", "failed", "cancelled"):
        return
    flow.status = "cancelled"
    flow.code_event.set()
    # _drive's finally clause will _kill_and_reap the subprocess; we don't
    # need to do it here. But the driver may be blocked on stdin/code_event
    # await — set the event (already done) and let it unwind.
