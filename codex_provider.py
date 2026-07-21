"""OpenAI Codex provider plumbing for claude-web.

Drives `codex app-server` — the JSON-RPC 2.0 stdio server that powers the
Codex VS Code extension and desktop app — so Codex conversations get the
same long-lived-run treatment as Claude ones: streamed events, in-browser
approval prompts, interrupt, resume.

Design notes:

- The keyed process pool supports one account-control server per OpenAI slot
  and one short-lived server per active chat. A run-specific process matters
  for account handoff: ``thread/unsubscribe`` leaves a thread cached for up to
  30 minutes, while a fresh process must reload the shared rollout and see
  turns another account appended. Per-slot ``CODEX_HOME`` directories isolate
  ``auth.json`` and local SQLite indexes while app.py shares the rollout
  ``sessions/`` directory. If a process dies, pending requests fail and live
  subscribers get a synthetic ``_codex/server_exited`` notification.

- This module is deliberately free of app.py imports (pure stdlib) so it
  can't create an import cycle. The run driver in app.py owns everything
  that touches ActiveRun / permission gating; this module owns the wire
  protocol and the pure translation from Codex notifications to the SSE
  event dicts the claude-web frontend already renders (the v1 stream-json
  shapes emitted by ``_sdk_message_to_events``).

- OS sandboxing: this host's vendor kernel lacks Landlock, and claude-web's
  trust model is human-in-the-loop approval rather than kernel confinement
  (the Claude path runs unsandboxed behind ``can_use_tool``). Codex runs
  therefore use ``danger-full-access`` and gate on ``approvalPolicy``
  instead, driven by the same per-conversation permission-mode selector the
  Claude side uses (see ``CODEX_PERMISSION_MODES``): "default" maps to
  ``untrusted`` (every non-trivial command prompts in the browser),
  "bypassPermissions" to ``never``, and "acceptEdits" keeps ``untrusted``
  while app.py's approval bridge auto-accepts patch approvals. Both env
  knobs remain as overrides for hosts where the bundled sandbox works.
"""

import asyncio
import collections
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("claude-web.codex")

# ─── Configuration ────────────────────────────────────────────────────────────

CODEX_BIN_ENV = "CLAUDE_WEB_CODEX_BIN"
# Every escalation prompts the user unless codex trusts the command as safe
# (read-only basics). Matches the Claude path's SAFE_TOOLS + gate posture.
APPROVAL_POLICY = os.environ.get("CLAUDE_WEB_CODEX_APPROVAL", "untrusted")
SANDBOX_MODE = os.environ.get("CLAUDE_WEB_CODEX_SANDBOX", "danger-full-access")

# The subset of claude-web permission modes a Codex conversation supports.
# With the sandbox pinned to danger-full-access (no Landlock on this
# kernel), codex's approvalPolicy collapses to two useful notches — ask
# for everything untrusted, or never ask — so "acceptEdits" keeps the
# untrusted policy and relies on app.py's approval bridge to auto-accept
# patch approvals. plan/dontAsk/auto are Claude-only and rejected at send.
CODEX_PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions")


def approval_policy_for_mode(permission_mode: str) -> str:
    """codex approvalPolicy for a claude-web permission mode. Sent on
    thread/start, thread/resume, and every turn/start, so a mid-chat mode
    switch takes effect on the next turn without touching the thread."""
    if permission_mode == "bypassPermissions":
        return "never"
    return APPROVAL_POLICY

# JSON-RPC request timeout. thread/start and model/list do network work on
# first use; generous but bounded so a wedged server surfaces as an error
# instead of a stuck driver.
REQUEST_TIMEOUT_S = 60.0
# thread/read of a long session can return megabytes on one line; the
# default StreamReader limit (64 KiB) would kill the reader with
# LimitOverrunError.
STDOUT_LIMIT_BYTES = 32 * 1024 * 1024
STDERR_RING_LINES = 100
# model/list is stable for the life of a CLI version; cache and refresh
# only when the server respawns.
SERVER_EXITED_METHOD = "_codex/server_exited"

# Tool names the approval bridge presents to the existing permission UI.
# "Bash" deliberately reuses the Claude tool name so the frontend's command
# rendering and the server's NO_SESSION_ALLOWLIST_TOOLS coarse-signature
# rule apply as-is.
TOOL_COMMAND = "Bash"
TOOL_PATCH = "ApplyPatch"
TOOL_WEB_SEARCH = "WebSearch"


def codex_binary() -> Optional[str]:
    override = os.environ.get(CODEX_BIN_ENV)
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "codex"
    return str(fallback) if fallback.exists() else None


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def availability(
    home: Optional[Path] = None, *, allow_env_key: bool = True,
) -> dict:
    """Cheap, subprocess-free probe used to decide whether the provider
    combo box offers Codex at all. auth.json (written by `codex login`)
    or an OPENAI_API_KEY in the service env both count as signed in.

    Personal credential homes pass ``allow_env_key=False`` so a shared
    service-level API key cannot silently mask a missing per-user login.
    """
    binary = codex_binary()
    if not binary:
        return {"available": False, "reason": "codex CLI not installed"}
    selected_home = Path(home) if home is not None else codex_home()
    authed = (selected_home / "auth.json").is_file() or (
        allow_env_key and bool(os.environ.get("OPENAI_API_KEY"))
    )
    if not authed:
        return {
            "available": False,
            "reason": (
                "not signed in with ChatGPT"
                if not allow_env_key
                else "not signed in (run `codex login` or set OPENAI_API_KEY)"
            ),
        }
    return {"available": True, "reason": None}


class CodexError(RuntimeError):
    """Base for provider failures surfaced to the run as error events."""


class CodexRPCError(CodexError):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error or {}
        super().__init__(
            f"{method} failed: {self.error.get('message') or self.error}"
        )


# ─── App-server client ────────────────────────────────────────────────────────


class CodexAppServer:
    """Async JSON-RPC client pooled by account-control or run-specific key.

    Line-delimited JSON-RPC 2.0 over stdio. Three inbound message kinds:
    responses (matched to pending request futures), server→client requests
    (approvals — routed to the per-thread handler), and notifications
    (routed to the per-thread subscriber queue).
    """

    # ``_instance`` remains the shared-slot alias for callers/tests that only
    # need a cache probe. New code should use ``get_cached(key)``.
    _instance: Optional["CodexAppServer"] = None
    _instances: dict[str, "CodexAppServer"] = {}
    _instance_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        *,
        key: str = "shared",
        home: Optional[Path] = None,
        isolated_auth: bool = False,
    ) -> None:
        self.key = key
        self.home = Path(home) if home is not None else None
        self.isolated_auth = isolated_auth
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue] = {}
        self._request_handlers: dict[
            str, Callable[[str, dict], Awaitable[dict]]
        ] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.stderr_tail: collections.deque = collections.deque(
            maxlen=STDERR_RING_LINES
        )
        self._models_cache: Optional[list[dict]] = None
        self._models_lock = asyncio.Lock()
        self._login_results: dict[str, dict] = {}
        self.started_at: Optional[float] = None

    # -- lifecycle -------------------------------------------------------------

    @classmethod
    async def get(
        cls,
        key: str = "shared",
        *,
        home: Optional[Path] = None,
        isolated_auth: bool = False,
    ) -> "CodexAppServer":
        """Return the live process for ``key``, spawning it once as needed."""
        lock = cls._instance_locks.setdefault(key, asyncio.Lock())
        async with lock:
            inst = cls._instances.get(key)
            if inst is not None and inst.alive:
                requested_home = Path(home) if home is not None else None
                if inst.home != requested_home or inst.isolated_auth != isolated_auth:
                    raise CodexError(f"codex app-server key {key!r} reused with different settings")
                return inst
            inst = cls(key=key, home=home, isolated_auth=isolated_auth)
            await inst._start()
            cls._instances[key] = inst
            if key == "shared":
                cls._instance = inst
            return inst

    @classmethod
    def get_cached(cls, key: str = "shared") -> Optional["CodexAppServer"]:
        inst = cls._instances.get(key)
        return inst if inst is not None and inst.alive else None

    @classmethod
    def shutdown_key(cls, key: str) -> None:
        inst = cls._instances.pop(key, None)
        cls._instance_locks.pop(key, None)
        if inst is not None:
            inst.shutdown()
        if key == "shared":
            cls._instance = None

    @classmethod
    async def close_key(cls, key: str) -> None:
        """Stop one pooled process and wait until it can no longer write."""
        inst = cls._instances.pop(key, None)
        cls._instance_locks.pop(key, None)
        if key == "shared":
            cls._instance = None
        if inst is None:
            return
        proc = inst.proc
        inst.shutdown()
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    @classmethod
    def shutdown_all(cls) -> None:
        for inst in list(cls._instances.values()):
            inst.shutdown()
        cls._instances.clear()
        cls._instance_locks.clear()
        cls._instance = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _start(self) -> None:
        binary = codex_binary()
        if not binary:
            raise CodexError("codex CLI not installed")
        command = [binary]
        child_env = os.environ.copy()
        if self.home is not None:
            self.home.mkdir(parents=True, exist_ok=True)
            child_env["CODEX_HOME"] = str(self.home)
        if self.isolated_auth:
            # A service-level key must never override a user's ChatGPT-plan
            # credential. The CLI writes/refreshes auth.json in this slot's
            # CODEX_HOME because the credential store is forced to ``file``;
            # forcing the built-in OpenAI provider also blocks a shared
            # config.toml custom provider from changing the billing path.
            for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
                child_env.pop(name, None)
            command.extend([
                "-c", 'cli_auth_credentials_store="file"',
                "-c", 'forced_login_method="chatgpt"',
                "-c", 'model_provider="openai"',
            ])
        command.append("app-server")
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT_BYTES,
            cwd=str(Path.home()),
            env=child_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        self.started_at = time.time()
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "claude-web",
                        "title": "claude-web",
                        "version": "1.0",
                    }
                },
                timeout=REQUEST_TIMEOUT_S,
            )
        except Exception:
            self.shutdown()
            raise
        self._send({"jsonrpc": "2.0", "method": "initialized"})
        log.info("codex app-server started (key=%s pid=%s)", self.key, self.proc.pid)

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
        self._fail_all_pending(CodexError("codex app-server shut down"))

    # -- wire ------------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        if not self.alive or self.proc.stdin is None:
            raise CodexError("codex app-server is not running")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def request(
        self, method: str, params: Optional[dict] = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            self._send(
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params or {}}
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise CodexError(
                f"{method} timed out after {timeout:.0f}s"
            ) from None
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("codex app-server sent non-JSON: %r", line[:200])
                    continue
                self._dispatch(msg)
        finally:
            self._on_exit()

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            self.stderr_tail.append(line.decode(errors="replace").rstrip())

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(CodexRPCError("request", msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method == "account/login/completed":
            login_id = params.get("loginId")
            if isinstance(login_id, str) and login_id:
                self._login_results[login_id] = dict(params)
        thread_id = params.get("threadId") or (
            (params.get("thread") or {}).get("id")
        )
        if "id" in msg:
            # Server→client request (approvals). Answered asynchronously so
            # the read loop keeps draining while the user decides.
            handler = self._request_handlers.get(thread_id or "")
            asyncio.get_running_loop().create_task(
                self._answer_server_request(msg["id"], method, params, handler)
            )
            return
        if thread_id and thread_id in self._thread_queues:
            self._thread_queues[thread_id].put_nowait(
                {"method": method, "params": params}
            )
        # Notifications for unsubscribed threads (or global ones like
        # configWarning) are intentionally dropped.

    async def _answer_server_request(
        self, rpc_id: Any, method: str, params: dict,
        handler: Optional[Callable[[str, dict], Awaitable[dict]]],
    ) -> None:
        if handler is None:
            # No driver owns this thread (races teardown). Decline rather
            # than leave the server waiting until its own timeout.
            log.warning("codex approval %s with no handler — declining", method)
            result: dict = {"decision": "decline"}
        else:
            try:
                result = await handler(method, params)
            except Exception:
                log.exception("codex approval handler failed for %s", method)
                result = {"decision": "decline"}
        try:
            self._send({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        except CodexError:
            pass  # server died while we were asking; nothing to answer

    def _on_exit(self) -> None:
        code = self.proc.returncode if self.proc else None
        log.warning("codex app-server exited (code=%s)", code)
        self._fail_all_pending(
            CodexError(f"codex app-server exited (code={code})")
        )
        for q in self._thread_queues.values():
            q.put_nowait({"method": SERVER_EXITED_METHOD, "params": {}})
        if CodexAppServer._instances.get(self.key) is self:
            CodexAppServer._instances.pop(self.key, None)
        if self.key == "shared" and CodexAppServer._instance is self:
            CodexAppServer._instance = None

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # -- thread routing ----------------------------------------------------------

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._thread_queues[thread_id] = q
        return q

    def unsubscribe(self, thread_id: str) -> None:
        self._thread_queues.pop(thread_id, None)
        self._request_handlers.pop(thread_id, None)

    def set_request_handler(
        self, thread_id: str,
        handler: Callable[[str, dict], Awaitable[dict]],
    ) -> None:
        self._request_handlers[thread_id] = handler

    # -- convenience ---------------------------------------------------------

    def login_result(self, login_id: str) -> Optional[dict]:
        result = self._login_results.get(login_id)
        return dict(result) if result is not None else None

    async def models(self) -> list[dict]:
        """model/list, cached for the life of this server process."""
        async with self._models_lock:
            if self._models_cache is not None:
                return self._models_cache
            resp = await self.request("model/list", {})
            models = []
            for m in (resp or {}).get("data", []):
                if m.get("hidden"):
                    continue
                efforts = [
                    e.get("reasoningEffort")
                    for e in (m.get("supportedReasoningEfforts") or [])
                    if e.get("reasoningEffort")
                ]
                models.append({
                    "key": m.get("id") or m.get("model"),
                    "model": m.get("model") or m.get("id"),
                    "label": m.get("displayName") or m.get("id"),
                    "description": m.get("description") or "",
                    "efforts": efforts,
                    "default_effort": m.get("defaultReasoningEffort"),
                    "is_default": bool(m.get("isDefault")),
                })
            self._models_cache = models
            return models

    async def account_usage(self) -> dict:
        """Account-level usage for the Usage dialog's codex view.

        ``account/rateLimits/read`` and ``account/usage/read`` require
        ChatGPT-subscription auth — on API-key auth the app-server rejects
        them ("chatgpt authentication required"). Each piece is fetched
        independently and a rejection is folded into ``unavailable_reason``
        rather than failing the whole call, so an API-key login still gets
        the account-type line and a clear explanation.

        Shapes are passed through nearly verbatim (camelCase) for the
        frontend to render; see the app-server schema RateLimitSnapshot /
        AccountTokenUsageSummary."""
        out: dict = {
            "account": None, "auth_mode": None,
            "rate_limits": None, "token_usage": None,
            "unavailable_reason": None,
        }
        try:
            acct = await self.request("account/read", {})
            account = (acct or {}).get("account") or {}
            out["account"] = account
            out["auth_mode"] = account.get("type")
        except CodexError as e:
            out["unavailable_reason"] = str(e)
            return out
        try:
            out["rate_limits"] = await self.request("account/rateLimits/read", {})
        except CodexRPCError as e:
            out["unavailable_reason"] = e.error.get("message") or str(e)
        except CodexError as e:
            out["unavailable_reason"] = str(e)
        try:
            out["token_usage"] = await self.request("account/usage/read", {})
        except CodexError:
            # Same auth gate as rate limits; the reason from that call already
            # explains it, so don't overwrite it.
            pass
        return out


# ─── Notification → SSE-event translation ─────────────────────────────────────
# Pure functions: Codex protocol dicts in, the frontend's v1 stream-json
# event dicts out (the exact shapes _sdk_message_to_events produces for the
# Claude path, so app.js renders both providers identically).


def _assistant_event(blocks: list[dict], session_id: Optional[str]) -> dict:
    return {
        "type": "assistant",
        "message": {"content": blocks},
        "session_id": session_id,
    }


def _tool_result_event(tool_use_id: str, content: str, is_error: bool,
                       preview_cap: int) -> dict:
    return {
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "content": (content or "")[:preview_cap],
        }]},
    }


def _command_input(item: dict) -> dict:
    cmd = item.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    out = {"command": str(cmd or "")}
    if item.get("cwd"):
        out["cwd"] = item["cwd"]
    return out


def patch_input(item: dict) -> dict:
    changes = item.get("changes")
    files: list[str] = []
    if isinstance(changes, dict):
        files = list(changes.keys())
    elif isinstance(changes, list):
        for ch in changes:
            if isinstance(ch, dict) and ch.get("path"):
                files.append(str(ch["path"]))
    return {"files": files or ["(unknown)"]}


def item_events(item: dict, *, completed: bool, session_id: Optional[str],
                preview_cap: int) -> list[dict]:
    """Translate one thread item (from item/started or item/completed) into
    SSE events. Started emits the tool_use half; completed emits the
    tool_result half (or the whole message for text-bearing items)."""
    itype = item.get("type")
    item_id = str(item.get("id") or "")

    if itype == "agentMessage":
        if not completed:
            return []  # deltas carry the interim text
        return [_assistant_event(
            [{"type": "text", "text": item.get("text") or ""}], session_id,
        )]

    if itype == "reasoning":
        if not completed:
            return []
        text = item.get("text") or item.get("summary") or ""
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        if not text:
            return []
        return [_assistant_event(
            [{"type": "thinking", "text": str(text)}], session_id,
        )]

    if itype == "commandExecution":
        if not completed:
            return [_assistant_event([{
                "type": "tool_use", "id": item_id, "name": TOOL_COMMAND,
                "input": _command_input(item),
            }], session_id)]
        output = item.get("aggregatedOutput") or item.get("output") or ""
        exit_code = item.get("exitCode")
        failed = item.get("status") == "failed" or (
            isinstance(exit_code, int) and exit_code != 0
        )
        if isinstance(exit_code, int) and exit_code != 0:
            output = f"{output}\n(exit code {exit_code})".strip()
        return [_tool_result_event(item_id, str(output), failed, preview_cap)]

    if itype == "fileChange":
        if not completed:
            return [_assistant_event([{
                "type": "tool_use", "id": item_id, "name": TOOL_PATCH,
                "input": patch_input(item),
            }], session_id)]
        failed = item.get("status") == "failed"
        summary = ", ".join(patch_input(item)["files"])
        return [_tool_result_event(
            item_id,
            f"{'Failed to apply' if failed else 'Applied'} changes: {summary}",
            failed, preview_cap,
        )]

    if itype == "webSearch":
        if not completed:
            return [_assistant_event([{
                "type": "tool_use", "id": item_id, "name": TOOL_WEB_SEARCH,
                "input": {"query": item.get("query") or ""},
            }], session_id)]
        return [_tool_result_event(
            item_id, str(item.get("results") or "done"), False, preview_cap,
        )]

    if itype == "mcpToolCall":
        name = f"mcp__{item.get('server') or '?'}__{item.get('tool') or '?'}"
        if not completed:
            return [_assistant_event([{
                "type": "tool_use", "id": item_id, "name": name,
                "input": item.get("arguments") if isinstance(
                    item.get("arguments"), dict) else {},
            }], session_id)]
        failed = item.get("status") == "failed"
        return [_tool_result_event(
            item_id, json.dumps(item.get("result"))[:preview_cap]
            if item.get("result") is not None else "done",
            failed, preview_cap,
        )]

    if itype == "todoList":
        todos = []
        for t in item.get("items") or []:
            text = t.get("text") or t.get("content") or ""
            todos.append({
                "content": text,
                "activeForm": text,
                "status": "completed" if t.get("completed") else "pending",
            })
        return [{"type": "todos_update", "todos": todos}]

    if itype == "error":
        return [{"type": "error",
                 "message": item.get("message") or "Codex reported an error."}]

    if itype == "userMessage":
        return []  # our own input echoed back; user_prompt already emitted

    return []


def usage_tokens(token_usage: dict) -> dict:
    """Flatten a thread/tokenUsage payload's ``total`` bucket into the
    result-event token fields the frontend reads."""
    total = (token_usage or {}).get("total") or token_usage or {}
    return {
        "input_tokens": total.get("inputTokens"),
        "output_tokens": total.get("outputTokens"),
        "cache_read_input_tokens": total.get("cachedInputTokens"),
        "cache_creation_input_tokens": None,
    }


def thread_transcript(thread: dict, preview_cap: int) -> list[dict]:
    """Translate a thread/read response into the transcript message list
    the session-history endpoint returns (same roles the Claude JSONL
    reader produces: user / assistant / tool_use / tool_result).

    Caveat: as of codex 0.144 the thread/read turn items only include the
    message kinds (userMessage/agentMessage) — command executions and file
    changes aren't in that view, so a reopened Codex session replays the
    conversation text without the tool chips the live stream showed. The
    translation below still handles tool items so richer reads light up
    if a future CLI starts returning them."""
    msgs: list[dict] = []
    turns = (thread or {}).get("turns") or []
    for turn in turns:
        for item in turn.get("items") or []:
            itype = item.get("type")
            if itype == "userMessage":
                parts = []
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(c.get("text") or "")
                text = "\n".join(p for p in parts if p)
                if text:
                    msgs.append({"role": "user", "text": text})
                continue
            if itype == "agentMessage":
                text = item.get("text") or ""
                if text:
                    msgs.append({"role": "assistant", "text": text})
                continue
            for ev in item_events(item, completed=False, session_id=None,
                                  preview_cap=preview_cap):
                for blk in (ev.get("message") or {}).get("content") or []:
                    if blk.get("type") == "tool_use":
                        msgs.append({
                            "role": "tool_use",
                            "name": blk.get("name"),
                            "input": blk.get("input"),
                            "id": blk.get("id"),
                        })
            for ev in item_events(item, completed=True, session_id=None,
                                  preview_cap=preview_cap):
                for blk in (ev.get("message") or {}).get("content") or []:
                    if blk.get("type") == "tool_result":
                        msgs.append({
                            "role": "tool_result",
                            "tool_use_id": blk.get("tool_use_id"),
                            "content": blk.get("content"),
                            "is_error": blk.get("is_error"),
                        })
    return msgs
