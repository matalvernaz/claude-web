"""OpenAI Codex provider plumbing for claude-web.

Drives `codex app-server` — the JSON-RPC 2.0 stdio server that powers the
Codex VS Code extension and desktop app — so Codex conversations get the
same long-lived-run treatment as Claude ones: streamed events, in-browser
approval prompts, interrupt, resume.

Design notes:

- One shared app-server process serves every Codex conversation. The
  protocol is thread-scoped (every notification/server-request carries a
  ``threadId``), so a singleton multiplexes cleanly — this mirrors how the
  VS Code extension uses it, and keeps auth/model-list state in one place.
  If the process dies, pending requests fail, per-thread subscribers get a
  synthetic ``_codex/server_exited`` notification, and the next use
  respawns it (conversations resume via ``thread/resume``).

- This module is deliberately free of app.py imports (pure stdlib) so it
  can't create an import cycle. The run driver in app.py owns everything
  that touches ActiveRun / permission gating; this module owns the wire
  protocol and the pure translation from Codex notifications to the SSE
  event dicts the claude-web frontend already renders (the v1 stream-json
  shapes emitted by ``_sdk_message_to_events``).

- OS sandboxing: this host's vendor kernel lacks Landlock, and claude-web's
  trust model is human-in-the-loop approval rather than kernel confinement
  (the Claude path runs unsandboxed behind ``can_use_tool``). Codex runs
  therefore default to ``danger-full-access`` + ``approvalPolicy=untrusted``,
  which routes every non-trivial command through the browser approval UI —
  the same posture as the Claude side. Both knobs have env overrides for
  hosts where the bundled bubblewrap sandbox works.
"""

import asyncio
import collections
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


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def availability() -> dict:
    """Cheap, subprocess-free probe used to decide whether the provider
    combo box offers Codex at all. auth.json (written by `codex login`)
    or an OPENAI_API_KEY in the service env both count as signed in."""
    binary = codex_binary()
    if not binary:
        return {"available": False, "reason": "codex CLI not installed"}
    authed = (_codex_home() / "auth.json").exists() or bool(
        os.environ.get("OPENAI_API_KEY")
    )
    if not authed:
        return {
            "available": False,
            "reason": "not signed in (run `codex login` or set OPENAI_API_KEY)",
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
    """Singleton asyncio JSON-RPC client for one `codex app-server` process.

    Line-delimited JSON-RPC 2.0 over stdio. Three inbound message kinds:
    responses (matched to pending request futures), server→client requests
    (approvals — routed to the per-thread handler), and notifications
    (routed to the per-thread subscriber queue).
    """

    _instance: Optional["CodexAppServer"] = None
    _instance_lock: Optional[asyncio.Lock] = None

    def __init__(self) -> None:
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
        self.started_at: Optional[float] = None

    # -- lifecycle -------------------------------------------------------------

    @classmethod
    async def get(cls) -> "CodexAppServer":
        """Return the live singleton, spawning it on first use or after a
        crash. Serialized so concurrent first requests spawn one process."""
        if cls._instance_lock is None:
            cls._instance_lock = asyncio.Lock()
        async with cls._instance_lock:
            inst = cls._instance
            if inst is not None and inst.alive:
                return inst
            inst = cls()
            await inst._start()
            cls._instance = inst
            return inst

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _start(self) -> None:
        binary = codex_binary()
        if not binary:
            raise CodexError("codex CLI not installed")
        self.proc = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT_BYTES,
            cwd=str(Path.home()),
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
        log.info("codex app-server started (pid=%s)", self.proc.pid)

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
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
            raise CodexError(f"{method} timed out after {timeout:.0f}s")
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
        if CodexAppServer._instance is self:
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
