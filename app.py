"""Web UI for Claude Code.

Uses claude_agent_sdk so we can intercept tool permissions per call and let
the browser approve or deny. Sessions are read straight from
~/.claude/projects/<sanitized-cwd>/*.jsonl, so they stay interchangeable
with host-shell `claude` invocations.

Configuration is via environment variables -- see README.md and
.env.example. Auth is OIDC by default; see auth.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth


def _sanitize_project_key(cwd: Path) -> str:
    """Mirror Claude Code's per-project session-dir naming."""
    return str(cwd.resolve()).replace("/", "-")


CWD = Path(os.getenv("CLAUDE_PROJECT_DIR", str(Path.home()))).resolve()
CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).resolve()
SESSIONS_DIR = CLAUDE_HOME / "projects" / _sanitize_project_key(CWD)

USAGE_DIR = Path(os.getenv("CLAUDE_WEB_STATE_DIR", str(Path.home() / ".claude-web"))).resolve()
USAGE_DIR.mkdir(parents=True, exist_ok=True)
USAGE_LOG = USAGE_DIR / "usage.jsonl"
RATE_LIMIT_CACHE = USAGE_DIR / "rate_limit.json"

MAX_TITLE_CHARS = 80
MAX_LISTED_SESSIONS = 100
TOOL_RESULT_PREVIEW = 200
STATIC_DIR = Path(__file__).parent / "static"

# Session/run IDs are SDK-generated UUIDs. Validate to keep path-traversal at
# bay before using user input as a filename.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_id(value: str) -> str:
    if not _ID_RE.match(value or ""):
        raise HTTPException(400, "bad id")
    return value

# Tools that are safe enough to auto-approve without showing a permission card.
# TodoWrite is pure bookkeeping; the user sees the todos panel either way.
SAFE_TOOLS = set(
    t.strip() for t in os.getenv("SAFE_TOOLS", "TodoWrite").split(",") if t.strip()
)

app = FastAPI()
auth.configure(app)
auth.install_routes(app)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _asset_version(name: str) -> str:
    """mtime as a cache-busting token for /static/<name>."""
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except FileNotFoundError:
        return "0"


templates.env.globals["asset_version"] = _asset_version


# ─── Session-file parsing (unchanged from v1) ────────────────────────────────


def _extract_text(msg) -> Optional[str]:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p) or None
    return None


def _is_user_visible(text: str) -> bool:
    return not (
        text.startswith("<local-command-caveat>")
        or text.startswith("<command-name>")
        or text.startswith("<system-reminder>")
    )


def _iter_jsonl(path: Path) -> Iterable[dict]:
    try:
        with path.open() as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def session_title(session_id: str) -> Optional[str]:
    path = SESSIONS_DIR / f"{session_id}.jsonl"
    for obj in _iter_jsonl(path):
        if obj.get("type") != "user" or obj.get("isMeta"):
            continue
        text = _extract_text(obj.get("message"))
        if text and _is_user_visible(text):
            stripped = text.strip()
            return (stripped[:MAX_TITLE_CHARS] + "…") if len(stripped) > MAX_TITLE_CHARS else stripped
    return None


def list_sessions() -> list[dict]:
    if not SESSIONS_DIR.exists():
        return []
    files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:MAX_LISTED_SESSIONS]
    return [
        {
            "id": p.stem,
            "title": session_title(p.stem) or p.stem[:8],
            "mtime": int(p.stat().st_mtime),
        }
        for p in files
    ]


def _summarise_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash" and inp.get("command"):
        return str(inp["command"])[:200]
    for key in ("file_path", "path", "url", "pattern"):
        if key in inp:
            return str(inp[key])[:200]
    return json.dumps(inp)[:200]


def session_transcript(session_id: str) -> list[dict]:
    """Return ordered messages for replay, including tool dance.

    Roles: "user", "assistant", "tool_use" (Claude→world), "tool_result"
    (world→Claude). Frontend renders tool_use/tool_result as single-line
    chips so reloaded sessions match the live view.
    """
    msgs: list[dict] = []
    for obj in _iter_jsonl(SESSIONS_DIR / f"{session_id}.jsonl"):
        kind = obj.get("type")
        message = obj.get("message")
        if kind == "user" and not obj.get("isMeta"):
            if isinstance(message, str):
                if _is_user_visible(message):
                    msgs.append({"role": "user", "text": message})
                continue
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                if _is_user_visible(content):
                    msgs.append({"role": "user", "text": content})
            elif isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        text = blk.get("text", "")
                        if text and _is_user_visible(text):
                            msgs.append({"role": "user", "text": text})
                    elif blk.get("type") == "tool_result":
                        c = blk.get("content")
                        if isinstance(c, list):
                            c = "".join(
                                b.get("text", "")
                                for b in c
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        msgs.append({
                            "role": "tool_result",
                            "text": str(c or "")[:TOOL_RESULT_PREVIEW],
                            "is_error": bool(blk.get("is_error")),
                        })
        elif kind == "assistant":
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    text = blk.get("text", "")
                    if text:
                        msgs.append({"role": "assistant", "text": text})
                elif blk.get("type") == "tool_use":
                    msgs.append({
                        "role": "tool_use",
                        "name": blk.get("name", "?"),
                        "summary": _summarise_tool_input(blk.get("name", ""), blk.get("input", {}) or {}),
                    })
    return msgs


# ─── Permission registry ──────────────────────────────────────────────────────


# request_id → asyncio.Future[dict] (resolved by /api/permission)
PENDING: dict[str, asyncio.Future] = {}


def _tool_signature(tool: str, tool_input: dict[str, Any]) -> str:
    """Stable, narrow identifier for "allow this kind of call again" rules."""
    if tool == "Bash":
        cmd = tool_input.get("command", "")
        return cmd.strip().split()[0] if cmd.strip() else ""
    for key in ("file_path", "path", "url", "pattern"):
        if key in tool_input:
            return str(tool_input[key])
    return ""


# ─── Active run tracking ──────────────────────────────────────────────────────


class ActiveRun:
    """One in-flight SDK conversation turn.

    Decouples the SDK task from the originating HTTP request: the task runs
    to completion regardless of whether the browser disconnects. Buffers
    every emitted event so a reconnecting browser gets a replay-then-tail.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.session_id: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.finished_at: Optional[float] = None
        self.session_allowlist: set[tuple[str, str]] = set()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                self.session_id = sid
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:
            q.put_nowait(e)
        if self.done:
            q.put_nowait({"type": "_done"})
        else:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def finish(self) -> None:
        self.done = True
        self.finished_at = time.time()
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "_done"})
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()


ACTIVE_RUNS: dict[str, ActiveRun] = {}
RUN_RETENTION_SECONDS = 300  # keep finished runs around so a slow reconnect can still replay


def _gc_runs() -> None:
    """Evict completed runs older than retention so the dict doesn't grow."""
    now = time.time()
    stale = [
        rid for rid, r in ACTIVE_RUNS.items()
        if r.done and r.finished_at and (now - r.finished_at) > RUN_RETENTION_SECONDS
    ]
    for rid in stale:
        ACTIVE_RUNS.pop(rid, None)


# ─── HTTP routes ──────────────────────────────────────────────────────────────


@app.get("/")
async def index(request: Request, user: dict = Depends(auth.require_user)):
    response = templates.TemplateResponse(
        request, "index.html", {"sessions": list_sessions(), "user": user}
    )
    # Don't cache the HTML — sidebar contents are time-sensitive.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/sessions")
async def api_sessions(user: dict = Depends(auth.require_user)):
    return list_sessions()


@app.get("/api/sessions/{sid}")
async def api_session(sid: str, user: dict = Depends(auth.require_user)):
    sid = _safe_id(sid)
    if not (SESSIONS_DIR / f"{sid}.jsonl").exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": sid, "messages": session_transcript(sid)}


@app.delete("/api/sessions/{sid}")
async def api_delete_session(sid: str, user: dict = Depends(auth.require_user)):
    sid = _safe_id(sid)
    path = SESSIONS_DIR / f"{sid}.jsonl"
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    path.unlink()
    return {"ok": True}


@app.post("/api/permission/{request_id}")
async def api_permission(
    request_id: str,
    decision: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    """Resolve a pending permission request from the browser.

    Accepts decision in {"allow", "allow_session", "deny"}.
    """
    fut = PENDING.get(request_id)
    if fut is None or fut.done():
        raise HTTPException(404, "no such pending request")
    if decision not in {"allow", "allow_session", "deny"}:
        raise HTTPException(400, "bad decision")
    fut.set_result({"decision": decision})
    return {"ok": True}


def _stream_run_response(run: ActiveRun) -> StreamingResponse:
    """Subscribe to an ActiveRun and stream its events as SSE.

    Closing the request just unsubscribes — the SDK task keeps running so
    a reload or new tab can rejoin via /api/chat/stream/{run_id}.
    """
    async def stream() -> AsyncIterator[bytes]:
        q = run.subscribe()
        try:
            while True:
                evt = await q.get()
                if evt.get("type") == "_done":
                    yield b"event: done\ndata: {}\n\n"
                    break
                yield f"data: {json.dumps(evt)}\n\n".encode()
        finally:
            run.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
async def api_chat(
    message: str = Form(...),
    session_id: str = Form(default=""),
    user: dict = Depends(auth.require_user),
):
    """Start a new SDK turn and stream its events as SSE.

    Permission requests are emitted as `permission_request` events; the
    browser POSTs the decision to /api/permission/{id} which unblocks the
    can_use_tool callback below.

    The SDK driver task is detached from the request — see ActiveRun.
    Browser reload reconnects via /api/chat/stream/{run_id}; explicit stop
    happens via /api/chat/stop/{run_id}.
    """
    _gc_runs()
    sid_in = session_id or None
    run_id = str(uuid_mod.uuid4())
    run = ActiveRun(run_id)
    ACTIVE_RUNS[run_id] = run
    # First two events: run_id (so a reload can reconnect) and the user's
    # prompt (so a resumed transcript shows what was asked — the SDK only
    # echoes assistant content and tool results back).
    run.emit({"type": "run_started", "run_id": run_id})
    run.emit({"type": "user_prompt", "text": message})

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
        if tool_name in SAFE_TOOLS:
            return PermissionResultAllow()
        sig = _tool_signature(tool_name, tool_input)
        if (tool_name, sig) in run.session_allowlist:
            return PermissionResultAllow()

        request_id = str(uuid_mod.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        PENDING[request_id] = fut
        try:
            run.emit({
                "type": "permission_request",
                "id": request_id,
                "tool": tool_name,
                "input": tool_input,
                "signature": sig,
            })
            decision = await fut
        finally:
            PENDING.pop(request_id, None)

        d = decision.get("decision")
        if d == "allow":
            return PermissionResultAllow()
        if d == "allow_session":
            run.session_allowlist.add((tool_name, sig))
            return PermissionResultAllow()
        return PermissionResultDeny(message="User denied permission via web UI.")

    options = ClaudeAgentOptions(
        cwd=str(CWD),
        resume=sid_in,
        permission_mode="default",
        can_use_tool=can_use_tool,
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        include_partial_messages=False,
    )

    async def driver():
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(message)
                async for msg in client.receive_response():
                    for evt in _sdk_message_to_events(msg):
                        run.emit(evt)
        except asyncio.CancelledError:
            # Explicit /api/chat/stop or process shutdown — surface as a
            # tidy "stopped" rather than the raw transport error that the
            # SDK would otherwise raise on broken pipes.
            run.emit({"type": "stopped"})
            raise
        except Exception as e:
            run.emit({"type": "error", "message": str(e)})
        finally:
            run.finish()

    run.task = asyncio.create_task(driver())
    return _stream_run_response(run)


@app.get("/api/chat/active")
async def api_chat_active(run_id: str = "", user: dict = Depends(auth.require_user)):
    """Used by the browser on page load to decide whether to resume."""
    if not run_id:
        return {"active": False}
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None:
        return {"active": False}
    return {
        "active": not run.done,
        "run_id": run.run_id,
        "session_id": run.session_id,
        "buffered_events": len(run.events),
    }


@app.get("/api/chat/stream/{run_id}")
async def api_chat_stream(run_id: str, user: dict = Depends(auth.require_user)):
    """Reconnect to an in-flight or recently-finished run."""
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return _stream_run_response(run)


@app.post("/api/chat/stop/{run_id}")
async def api_chat_stop(run_id: str, user: dict = Depends(auth.require_user)):
    """Cancel the SDK task. Idempotent."""
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None or run.done:
        return {"ok": False}
    if run.task and not run.task.done():
        run.task.cancel()
    return {"ok": True}


def _sdk_message_to_events(msg) -> list[dict]:
    """Translate one SDK message into one or more SSE-payload dicts.

    The frontend already knows how to render text / tool_use / tool_result /
    init / result events from the v1 stream-json protocol, so we keep that
    shape rather than inventing a new one.
    """
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init":
            data = msg.data or {}
            rli = data.get("rate_limit_info") or {}
            if rli:
                _save_rate_limit(rli)
            return [{
                "type": "system",
                "subtype": "init",
                "session_id": data.get("session_id"),
                "model": data.get("model"),
                "permissionMode": data.get("permissionMode"),
            }]
        return []
    if isinstance(msg, AssistantMessage):
        out = []
        message_blocks = []
        for blk in msg.content:
            if isinstance(blk, TextBlock):
                message_blocks.append({"type": "text", "text": blk.text})
            elif isinstance(blk, ThinkingBlock):
                message_blocks.append({"type": "thinking", "text": blk.thinking})
            elif isinstance(blk, ToolUseBlock):
                message_blocks.append({
                    "type": "tool_use",
                    "id": blk.id,
                    "name": blk.name,
                    "input": blk.input,
                })
                # Promote TodoWrite to a structured panel update.
                if blk.name == "TodoWrite":
                    todos = (blk.input or {}).get("todos") or []
                    out.append({"type": "todos_update", "todos": todos})
        out.append({
            "type": "assistant",
            "message": {"content": message_blocks},
            "session_id": msg.session_id,
        })
        return out
    if isinstance(msg, UserMessage):
        # Tool results coming back from Claude's tool runs.
        results = []
        for blk in msg.content:
            if isinstance(blk, ToolResultBlock):
                content = blk.content
                if isinstance(content, list):
                    text = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                    )
                else:
                    text = str(content) if content is not None else ""
                results.append({
                    "type": "tool_result",
                    "tool_use_id": blk.tool_use_id,
                    "is_error": bool(blk.is_error),
                    "content": text[:TOOL_RESULT_PREVIEW * 4],
                })
        if results:
            return [{"type": "user", "message": {"content": results}}]
        return []
    if isinstance(msg, ResultMessage):
        _log_usage(msg)
        return [{
            "type": "result",
            "is_error": msg.is_error,
            "result": msg.result,
            "duration_ms": msg.duration_ms,
            "total_cost_usd": msg.total_cost_usd,
            "session_id": msg.session_id,
            "subtype": msg.subtype,
            "stop_reason": msg.stop_reason,
            "num_turns": msg.num_turns,
            "permission_denials": [
                {"tool_name": d.tool_name, "tool_input": d.tool_input}
                for d in (msg.permission_denials or [])
            ],
        }]
    return []


# ─── Usage tracking ───────────────────────────────────────────────────────────


def _save_rate_limit(rli: dict) -> None:
    try:
        RATE_LIMIT_CACHE.write_text(json.dumps({"info": rli, "captured_at": int(_time())}))
    except Exception:
        pass


def _log_usage(msg) -> None:
    """Append one row per completed turn to usage.jsonl."""
    usage = getattr(msg, "usage", None) or {}
    row = {
        "ts": int(_time()),
        "session_id": msg.session_id,
        "duration_ms": msg.duration_ms,
        "total_cost_usd": msg.total_cost_usd,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "is_error": msg.is_error,
    }
    try:
        with USAGE_LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _time() -> float:
    import time
    return time.time()


def _today_window() -> tuple[int, int]:
    """Unix [start, end) of today in local time."""
    import datetime
    now = datetime.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + datetime.timedelta(days=1)).timestamp())


@app.get("/api/usage")
async def api_usage(user: dict = Depends(auth.require_user)):
    """Aggregate today's usage and return whatever rate-limit info we last saw."""
    start, end = _today_window()
    today_rows: list[dict] = []
    for row in _iter_jsonl(USAGE_LOG):
        ts = row.get("ts")
        if ts is None or ts < start or ts >= end:
            continue
        today_rows.append(row)

    by_session: dict[str, dict] = {}
    for r in today_rows:
        sid = r.get("session_id") or "?"
        slot = by_session.setdefault(sid, {"turns": 0, "cost": 0.0, "input": 0, "output": 0})
        slot["turns"] += 1
        slot["cost"] += float(r.get("total_cost_usd") or 0.0)
        slot["input"] += int(r.get("input_tokens") or 0)
        slot["output"] += int(r.get("output_tokens") or 0)

    sessions = []
    for sid, s in sorted(by_session.items(), key=lambda kv: kv[1]["cost"], reverse=True):
        sessions.append({
            "session_id": sid,
            "title": session_title(sid) or sid[:8],
            "turns": s["turns"],
            "cost_usd": round(s["cost"], 4),
            "input_tokens": s["input"],
            "output_tokens": s["output"],
        })

    total_cost = round(sum(s["cost_usd"] for s in sessions), 4)
    total_input = sum(s["input_tokens"] for s in sessions)
    total_output = sum(s["output_tokens"] for s in sessions)

    rate_limit = None
    try:
        if RATE_LIMIT_CACHE.exists():
            rate_limit = json.loads(RATE_LIMIT_CACHE.read_text())
    except Exception:
        rate_limit = None

    return {
        "today": {
            "turns": sum(s["turns"] for s in sessions),
            "cost_usd": total_cost,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "sessions": sessions[:20],
        },
        "rate_limit": rate_limit,
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}
