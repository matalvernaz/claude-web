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
import base64
import datetime
import json
import logging
import os
import re
import time
import traceback
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
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    UserMessage,
)
from claude_agent_sdk.types import (
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth


def _sanitize_project_key(cwd: Path) -> str:
    """Mirror Claude Code's per-project session-dir naming."""
    return str(cwd.resolve()).replace("/", "-")


CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude"))).resolve()


def _configured_projects() -> list[Path]:
    """Allowed working directories for sessions.

    Set CLAUDE_WEB_PROJECT_DIRS=/a,/b,/c to enable the project picker.
    Falls back to the legacy single-CWD behaviour when only CLAUDE_PROJECT_DIR
    (or neither) is set.
    """
    raw = os.getenv("CLAUDE_WEB_PROJECT_DIRS")
    if raw:
        return [Path(p.strip()).resolve() for p in raw.split(",") if p.strip()]
    fallback = os.getenv("CLAUDE_PROJECT_DIR", str(Path.home()))
    return [Path(fallback).resolve()]


PROJECTS: list[Path] = _configured_projects()
DEFAULT_CWD: Path = PROJECTS[0]
PROJECT_KEYS: dict[str, Path] = {_sanitize_project_key(p): p for p in PROJECTS}


def _resolve_project(key: str) -> Path:
    """Map a project key (sanitised path) to its real path, rejecting unknown ones."""
    if not key:
        return DEFAULT_CWD
    cwd = PROJECT_KEYS.get(key)
    if cwd is None:
        raise HTTPException(400, "unknown project")
    return cwd


def _sessions_dir(cwd: Path) -> Path:
    return CLAUDE_HOME / "projects" / _sanitize_project_key(cwd)


USAGE_DIR = Path(os.getenv("CLAUDE_WEB_STATE_DIR", str(Path.home() / ".claude-web"))).resolve()
USAGE_DIR.mkdir(parents=True, exist_ok=True)
USAGE_LOG = USAGE_DIR / "usage.jsonl"
RATE_LIMIT_CACHE = USAGE_DIR / "rate_limit.json"

# Pending permission requests deny themselves after this if the browser never
# answers (closed tab, lost network). Without this the SDK turn pins forever.
PERMISSION_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_WEB_PERMISSION_TIMEOUT", "900"))

# Cap how many synth-message turns can chain off background tool notifications
# before we stop and wait for the human. Prevents a notification-emitting tool
# from looping the driver against the API forever.
MAX_CONSECUTIVE_AUTO_FIRES = int(os.getenv("CLAUDE_WEB_MAX_AUTO_FIRES", "3"))

MAX_TITLE_CHARS = 80
MAX_LISTED_SESSIONS = 100
MAX_SEARCH_RESULTS = 50
SEARCH_SNIPPET_CHARS = 160
TOOL_RESULT_PREVIEW = 200
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_TURN = 10
ALLOWED_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
STATIC_DIR = Path(__file__).parent / "static"

# Models exposed in the UI dropdown. The form sends `key`; the server maps
# that to (`model`, `betas`). Opus 4.7's 1M-context variant is exposed as a
# separate option because it's enabled via a beta flag rather than a distinct
# model id. The empty key ("" → "Default") omits `model=` so the SDK uses
# whatever the CLI defaults to.
KNOWN_MODELS = [
    {"key": "", "model": "", "label": "Default", "betas": []},
    {"key": "claude-opus-4-7", "model": "claude-opus-4-7", "label": "Opus 4.7", "context": 200000, "betas": []},
    {"key": "claude-opus-4-7-1m", "model": "claude-opus-4-7", "label": "Opus 4.7 (1M context)",
     "context": 1000000, "betas": ["context-1m-2025-08-07"]},
    {"key": "claude-sonnet-4-6", "model": "claude-sonnet-4-6", "label": "Sonnet 4.6", "context": 1000000, "betas": []},
    {"key": "claude-haiku-4-5", "model": "claude-haiku-4-5", "label": "Haiku 4.5", "context": 200000, "betas": []},
]
MODELS_BY_KEY = {m["key"]: m for m in KNOWN_MODELS}

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


def session_title_from(path: Path) -> Optional[str]:
    for obj in _iter_jsonl(path):
        if obj.get("type") != "user" or obj.get("isMeta"):
            continue
        text = _extract_text(obj.get("message"))
        if text and _is_user_visible(text):
            stripped = text.strip()
            return (stripped[:MAX_TITLE_CHARS] + "…") if len(stripped) > MAX_TITLE_CHARS else stripped
    return None


def session_title(session_id: str, cwd: Optional[Path] = None) -> Optional[str]:
    """Look up a session's first user line.

    If `cwd` is given, only the matching project's dir is consulted; otherwise
    every configured project is searched.
    """
    candidates = [cwd] if cwd is not None else PROJECTS
    for project in candidates:
        path = _sessions_dir(project) / f"{session_id}.jsonl"
        if path.exists():
            return session_title_from(path)
    return None


def list_sessions() -> list[dict]:
    """All sessions across every configured project, newest first."""
    rows: list[dict] = []
    for project in PROJECTS:
        d = _sessions_dir(project)
        if not d.exists():
            continue
        key = _sanitize_project_key(project)
        for p in d.glob("*.jsonl"):
            try:
                mtime = p.stat().st_mtime
            except FileNotFoundError:
                continue
            rows.append({
                "id": p.stem,
                "project": key,
                "project_path": str(project),
                "mtime": int(mtime),
                "_path": p,
            })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    rows = rows[:MAX_LISTED_SESSIONS]
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "project": r["project"],
            "project_path": r["project_path"],
            "title": session_title_from(r["_path"]) or r["id"][:8],
            "mtime": r["mtime"],
        })
    return out


def _summarise_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash" and inp.get("command"):
        return str(inp["command"])[:200]
    for key in ("file_path", "path", "url", "pattern"):
        if key in inp:
            return str(inp[key])[:200]
    return json.dumps(inp)[:200]


def _find_session_path(session_id: str, project_key: str = "") -> Optional[Path]:
    """Locate a session file in the configured projects."""
    if project_key:
        cwd = _resolve_project(project_key)
        path = _sessions_dir(cwd) / f"{session_id}.jsonl"
        return path if path.exists() else None
    for project in PROJECTS:
        path = _sessions_dir(project) / f"{session_id}.jsonl"
        if path.exists():
            return path
    return None


def session_transcript(session_id: str, project_key: str = "") -> list[dict]:
    """Return ordered messages for replay, including tool dance.

    Roles: "user", "assistant", "tool_use" (Claude→world), "tool_result"
    (world→Claude), "tool_use_full" (Edit/Write so the frontend can render a
    diff). Frontend renders tool_use/tool_result as single-line chips so
    reloaded sessions match the live view.
    """
    path = _find_session_path(session_id, project_key)
    if path is None:
        return []
    msgs: list[dict] = []
    for obj in _iter_jsonl(path):
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
                # Collect text + count of attachments so the resumed turn
                # shows the same shape (text + N images) it did live.
                text_parts: list[str] = []
                image_count = 0
                tool_results: list[dict] = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text":
                        t = blk.get("text", "")
                        if t and _is_user_visible(t):
                            text_parts.append(t)
                    elif btype == "image":
                        image_count += 1
                    elif btype == "tool_result":
                        c = blk.get("content")
                        if isinstance(c, list):
                            c = "".join(
                                b.get("text", "")
                                for b in c
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        tool_results.append({
                            "role": "tool_result",
                            "text": str(c or "")[:TOOL_RESULT_PREVIEW],
                            "is_error": bool(blk.get("is_error")),
                        })
                if text_parts or image_count:
                    msgs.append({
                        "role": "user",
                        "text": "\n".join(text_parts),
                        "image_count": image_count,
                    })
                msgs.extend(tool_results)
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
                    name = blk.get("name", "?")
                    inp = blk.get("input", {}) or {}
                    entry = {
                        "role": "tool_use",
                        "name": name,
                        "summary": _summarise_tool_input(name, inp),
                    }
                    if name in ("Edit", "Write"):
                        # Pass enough input through for the frontend to render
                        # a diff on session reload (live view already gets it).
                        entry["input"] = {
                            "file_path": inp.get("file_path") or inp.get("path"),
                            "old_string": inp.get("old_string"),
                            "new_string": inp.get("new_string"),
                            "content": inp.get("content"),
                        }
                    elif name == "Bash":
                        # The export wants the un-truncated command, not the
                        # 200-char summary. The live UI ignores this field
                        # for Bash so the extra payload is export-only cost.
                        entry["input"] = {"command": inp.get("command")}
                    msgs.append(entry)
    return msgs


# Cap on Write content + arbitrary tool input dumps in the markdown export.
# Tool *results* are already truncated by session_transcript to
# TOOL_RESULT_PREVIEW, so they don't need a second cap.
EXPORT_INPUT_MAX_CHARS = 4000


def _truncate_for_export(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated {len(text) - limit} more chars)"


def _md_summary(text: str, limit: int = 60) -> str:
    """Tool-call summary safe for a <details><summary> line."""
    flat = text.replace("\n", " ").strip()
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    # < and ` would break either the summary's HTML or its inline-code render.
    return flat.replace("<", "&lt;").replace("`", "ʼ")


def session_to_markdown(session_id: str, project_key: str = "") -> Optional[str]:
    """Render a session jsonl as a self-contained markdown document.

    Produces a single string with a small frontmatter-y header followed by
    alternating "## You" / "## Claude" sections. Tool calls and results are
    folded into <details> blocks so the document is readable in a fresh
    GitHub issue / forum post but the dance doesn't drown out the text.
    """
    path = _find_session_path(session_id, project_key)
    if path is None:
        return None

    title = session_title_from(path) or session_id[:8]
    # Reverse the sanitised dir name back to a real path when we still have
    # the project configured; otherwise fall back to the sanitised key so
    # the export at least carries *something* identifying.
    parent_key = path.parent.name
    project_path = str(PROJECT_KEYS[parent_key]) if parent_key in PROJECT_KEYS else parent_key
    try:
        started = datetime.datetime.fromtimestamp(path.stat().st_ctime).isoformat(timespec="seconds")
    except OSError:
        started = "?"

    out: list[str] = [
        f"# {title}",
        "",
        f"- **Session:** `{session_id}`",
        f"- **Project:** `{project_path}`",
        f"- **Started:** {started}",
        "",
        "---",
        "",
    ]

    for m in session_transcript(session_id, project_key):
        role = m.get("role")
        if role == "user":
            out.append("## You")
            out.append("")
            text = m.get("text", "")
            if text:
                out.append(text)
                out.append("")
            count = m.get("image_count") or 0
            if count:
                out.append(f"_({count} image{'s' if count != 1 else ''} attached)_")
                out.append("")
        elif role == "assistant":
            out.append("## Claude")
            out.append("")
            out.append(m.get("text", ""))
            out.append("")
        elif role == "tool_use":
            name = m.get("name", "?")
            inp = m.get("input") or {}
            summary = m.get("summary", "")
            heading = f"🔧 {name}"
            if summary:
                heading += f" — `{_md_summary(summary)}`"
            out.append("<details>")
            out.append(f"<summary>{heading}</summary>")
            out.append("")
            if name in ("Edit", "Write") and isinstance(inp, dict):
                file_path = inp.get("file_path") or ""
                if file_path:
                    out.append(f"`{file_path}`")
                    out.append("")
                if name == "Edit":
                    old_s = inp.get("old_string") or ""
                    new_s = inp.get("new_string") or ""
                    out.append("```diff")
                    for ln in old_s.split("\n"):
                        out.append(f"- {ln}")
                    for ln in new_s.split("\n"):
                        out.append(f"+ {ln}")
                    out.append("```")
                else:  # Write
                    content = inp.get("content") or ""
                    out.append("```")
                    out.append(_truncate_for_export(content, EXPORT_INPUT_MAX_CHARS))
                    out.append("```")
            elif name == "Bash" and isinstance(inp, dict) and inp.get("command"):
                out.append("```sh")
                out.append(_truncate_for_export(str(inp["command"]), EXPORT_INPUT_MAX_CHARS))
                out.append("```")
            # No `elif summary:` body — for Read/Grep/WebFetch/etc the path
            # or pattern already lives in the <summary> line, so a duplicate
            # body just adds noise.
            out.append("")
            out.append("</details>")
            out.append("")
        elif role == "tool_result":
            text = m.get("text", "")
            if not text:
                continue
            mark = "❌" if m.get("is_error") else "↩️"
            out.append("<details>")
            out.append(f"<summary>{mark} Result</summary>")
            out.append("")
            out.append("```")
            out.append(text)
            out.append("```")
            out.append("")
            out.append("</details>")
            out.append("")
    return "\n".join(out)


# ─── Permission registry ──────────────────────────────────────────────────────


# request_id → {"future": Future[dict], "owner_sub": str}.
# owner_sub gates which logged-in user is allowed to resolve the request, so
# in a multi-user deployment one user can't approve another user's tool call.
PENDING: dict[str, dict] = {}


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
    """One long-lived conversation backed by a single ClaudeSDKClient.

    Originally per-turn; widened so the bundled CLI subprocess survives
    across user messages and Monitor / TaskNotification events keep flowing
    in between turns. The driver loop reads receive_messages() forever,
    auto-firing follow-up turns when background tools emit notifications.

    Subscribers (HTTP SSE streams) come and go. Buffered events let a
    reconnecting browser replay-then-tail.
    """

    def __init__(self, run_id: str, owner_sub: Optional[str] = None):
        self.run_id = run_id
        self.owner_sub = owner_sub
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.session_id: Optional[str] = None
        self.project_key: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.finished_at: Optional[float] = None
        self.session_allowlist: set[tuple[str, str]] = set()
        # Long-lived conversation state.
        self.user_input_queue: asyncio.Queue = asyncio.Queue()
        self.pending_notifications: list[dict] = []
        self.notification_grace_started_at: Optional[float] = None
        # Tool notifications that arrive between turns auto-fire a synth user
        # message. If the synth's tool calls emit more notifications we'd
        # auto-fire forever; this counter caps the chain.
        self.consecutive_auto_fires: int = 0
        self.last_activity: float = time.time()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                self.session_id = sid
        # Subscriber queues are unbounded, so put_nowait can't fail here.
        for q in list(self.subscribers):
            q.put_nowait(event)

    def subscribe(self, start_index: int = 0) -> asyncio.Queue:
        """Subscribe to events from `start_index` onward.

        Use 0 for "give me everything from the start" (page reload). Use
        len(events)-N for "give me only the newest N events" — used when a
        follow-up POST hits an already-running long-lived run and the
        browser already has the older events rendered.
        """
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events[start_index:]:
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
            q.put_nowait({"type": "_done"})
        self.subscribers.clear()


ACTIVE_RUNS: dict[str, ActiveRun] = {}
ACTIVE_RUNS_BY_SESSION: dict[str, ActiveRun] = {}
RUN_RETENTION_SECONDS = 300  # keep finished runs around so a slow reconnect can still replay
AUTO_FIRE_GRACE_MS = 1500  # buffer late task notifications this long before auto-firing
SESSION_IDLE_TIMEOUT_MS = 10 * 60 * 1000  # close idle conversation after 10 min


def _gc_runs() -> None:
    """Evict completed runs older than retention so the dict doesn't grow."""
    now = time.time()
    stale = [
        rid for rid, r in ACTIVE_RUNS.items()
        if r.done and r.finished_at and (now - r.finished_at) > RUN_RETENTION_SECONDS
    ]
    for rid in stale:
        run = ACTIVE_RUNS.pop(rid, None)
        if run and run.session_id:
            existing = ACTIVE_RUNS_BY_SESSION.get(run.session_id)
            if existing is run:
                ACTIVE_RUNS_BY_SESSION.pop(run.session_id, None)


def _existing_run_for_session(session_id: str) -> Optional[ActiveRun]:
    """Return the live ActiveRun owning this client session, if any."""
    if not session_id:
        return None
    run = ACTIVE_RUNS_BY_SESSION.get(session_id)
    if run is None or run.done or (run.task and run.task.done()):
        return None
    return run


def _require_owner(run: ActiveRun, user: dict) -> None:
    """Reject cross-user access in multi-user deployments.

    Run ids are random UUIDs so this is mostly belt-and-braces, but
    OIDC_ALLOWED_EMAILS advertises shared use and the cost of a check is
    nothing. Runs created before owner tracking existed (owner_sub is None)
    are allowed through.
    """
    owner = run.owner_sub
    if owner and owner != user.get("sub"):
        raise HTTPException(403, "not your run")


# ─── HTTP routes ──────────────────────────────────────────────────────────────


@app.get("/")
async def index(request: Request, user: dict = Depends(auth.require_user)):
    response = templates.TemplateResponse(
        request, "index.html", {
            "sessions": list_sessions(),
            "user": user,
            "projects": [
                {"key": _sanitize_project_key(p), "path": str(p), "name": p.name or str(p)}
                for p in PROJECTS
            ],
            "default_project": _sanitize_project_key(DEFAULT_CWD),
            "models": KNOWN_MODELS,
            "multi_project": len(PROJECTS) > 1,
        }
    )
    # Don't cache the HTML — sidebar contents are time-sensitive.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/projects")
async def api_projects(user: dict = Depends(auth.require_user)):
    return {
        "projects": [
            {"key": _sanitize_project_key(p), "path": str(p), "name": p.name or str(p)}
            for p in PROJECTS
        ],
        "default": _sanitize_project_key(DEFAULT_CWD),
    }


@app.get("/api/sessions")
async def api_sessions(user: dict = Depends(auth.require_user)):
    return list_sessions()


@app.get("/api/sessions/search")
async def api_sessions_search(
    q: str = "",
    user: dict = Depends(auth.require_user),
):
    """Substring search across every configured project's session transcripts.

    Cheap-and-cheerful: line-by-line, case-insensitive, capped at
    MAX_SEARCH_RESULTS hits. The frontend always shows titles for matched
    sessions even when the hit was inside an assistant or tool message.
    """
    query = (q or "").strip().lower()
    if len(query) < 2:
        return {"query": q, "hits": []}

    hits: list[dict] = []
    for project in PROJECTS:
        d = _sessions_dir(project)
        if not d.exists():
            continue
        key = _sanitize_project_key(project)
        for path in d.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            session_hit: Optional[dict] = None
            for obj in _iter_jsonl(path):
                kind = obj.get("type")
                if kind not in ("user", "assistant"):
                    continue
                if kind == "user" and obj.get("isMeta"):
                    continue
                text = _extract_text(obj.get("message")) or ""
                if not text:
                    continue
                low = text.lower()
                idx = low.find(query)
                if idx < 0:
                    continue
                start = max(0, idx - 40)
                end = min(len(text), idx + len(query) + SEARCH_SNIPPET_CHARS - 40)
                snippet = text[start:end].replace("\n", " ").strip()
                if start > 0:
                    snippet = "…" + snippet
                if end < len(text):
                    snippet = snippet + "…"
                session_hit = {
                    "id": path.stem,
                    "project": key,
                    "title": session_title_from(path) or path.stem[:8],
                    "mtime": int(mtime),
                    "snippet": snippet,
                    "role": kind,
                }
                break
            if session_hit:
                hits.append(session_hit)
            if len(hits) >= MAX_SEARCH_RESULTS:
                break
        if len(hits) >= MAX_SEARCH_RESULTS:
            break

    hits.sort(key=lambda h: h["mtime"], reverse=True)
    return {"query": q, "hits": hits}


@app.get("/api/sessions/{sid}")
async def api_session(
    sid: str,
    project: str = "",
    user: dict = Depends(auth.require_user),
):
    sid = _safe_id(sid)
    path = _find_session_path(sid, project)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # path lives at <CLAUDE_HOME>/projects/<sanitized-cwd>/<sid>.jsonl, so the
    # parent dir's name *is* the project key — no need to walk PROJECTS.
    project_key = path.parent.name
    return {
        "id": sid,
        "project": project_key,
        "messages": session_transcript(sid, project_key),
    }


@app.get("/api/sessions/{sid}/export.md")
async def api_session_export(
    sid: str,
    project: str = "",
    user: dict = Depends(auth.require_user),
):
    """Return a markdown rendering of the session for copy/paste or download.

    Same Content-Disposition either way: a fetch() call gets the body and
    can copy to clipboard, while a direct browser navigation downloads it.
    """
    sid = _safe_id(sid)
    md = session_to_markdown(sid, project)
    if md is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="claude-session-{sid[:12]}.md"',
            "Cache-Control": "no-store",
        },
    )


@app.delete("/api/sessions/{sid}")
async def api_delete_session(
    sid: str,
    project: str = "",
    user: dict = Depends(auth.require_user),
):
    sid = _safe_id(sid)
    path = _find_session_path(sid, project)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    path.unlink()
    return {"ok": True}


@app.get("/api/commands")
async def api_commands(user: dict = Depends(auth.require_user)):
    """Slash-command suggestions for the prompt textarea.

    Built-in commands are hardcoded; user skills/commands are scanned from
    ~/.claude/skills/<name>/SKILL.md and ~/.claude/commands/<name>.md so
    project-specific helpers show up automatically.
    """
    builtins = [
        {"name": "help", "description": "Get help with using Claude Code", "kind": "builtin"},
        {"name": "clear", "description": "Clear conversation history", "kind": "builtin"},
        {"name": "compact", "description": "Compact the conversation", "kind": "builtin"},
        {"name": "cost", "description": "Show usage and cost so far", "kind": "builtin"},
        {"name": "memory", "description": "View or edit the auto-memory store", "kind": "builtin"},
        {"name": "model", "description": "Switch model", "kind": "builtin"},
        {"name": "agents", "description": "List available subagents", "kind": "builtin"},
        {"name": "init", "description": "Initialize a CLAUDE.md file", "kind": "builtin"},
        {"name": "review", "description": "Review a pull request", "kind": "builtin"},
        {"name": "schedule", "description": "Manage scheduled remote agents", "kind": "builtin"},
        {"name": "loop", "description": "Run a prompt or skill on a recurring interval", "kind": "builtin"},
    ]

    def _scan(dir_path: Path, kind: str, name_from_dir: bool) -> list[dict]:
        out: list[dict] = []
        if not dir_path.exists():
            return out
        try:
            entries = sorted(dir_path.iterdir())
        except OSError:
            return out
        for entry in entries:
            if name_from_dir:
                if not entry.is_dir():
                    continue
                name = entry.name
                desc_path = entry / "SKILL.md"
            else:
                if entry.suffix != ".md" or not entry.is_file():
                    continue
                name = entry.stem
                desc_path = entry
            description = ""
            try:
                if desc_path.exists():
                    with desc_path.open() as f:
                        for _ in range(40):
                            line = f.readline()
                            if not line:
                                break
                            stripped = line.strip()
                            if stripped.startswith("description:"):
                                description = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                                break
            except OSError:
                pass
            out.append({"name": name, "description": description, "kind": kind})
        return out

    skills = _scan(CLAUDE_HOME / "skills", "skill", name_from_dir=True)
    user_cmds = _scan(CLAUDE_HOME / "commands", "command", name_from_dir=False)

    # Stable, de-duplicated by name (built-ins win, then skills, then commands).
    seen = {b["name"] for b in builtins}
    extras: list[dict] = []
    for c in skills + user_cmds:
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        extras.append(c)
    return {"commands": builtins + extras}


@app.post("/api/permission/{request_id}")
async def api_permission(
    request_id: str,
    decision: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    """Resolve a pending permission request from the browser.

    Accepts decision in {"allow", "allow_session", "deny"}.
    """
    entry = PENDING.get(request_id)
    if entry is None:
        raise HTTPException(404, "no such pending request")
    fut: asyncio.Future = entry["future"]
    if fut.done():
        raise HTTPException(404, "no such pending request")
    owner = entry.get("owner_sub")
    if owner and owner != user.get("sub"):
        raise HTTPException(403, "not your permission request")
    if decision not in {"allow", "allow_session", "deny"}:
        raise HTTPException(400, "bad decision")
    fut.set_result({"decision": decision})
    return {"ok": True}


def _stream_run_response(run: ActiveRun, start_index: int = 0) -> StreamingResponse:
    """Subscribe to an ActiveRun and stream its events as SSE.

    `start_index` controls how much history the new subscriber replays —
    0 for full reconnect, len(events)-N for "only events I'm about to emit".
    Closing the request just unsubscribes — the SDK task keeps running so
    a reload or new tab can rejoin via /api/chat/stream/{run_id}.
    """
    async def stream() -> AsyncIterator[bytes]:
        q = run.subscribe(start_index=start_index)
        try:
            while True:
                # SSE comment heartbeat: Cloudflare/cloudflared close response
                # streams after ~100s of byte-level silence, which Firefox
                # surfaces as "Error in input stream" mid-conversation while
                # we wait for the user's next prompt. Colon-prefixed lines are
                # ignored by EventSource/fetch consumers per the SSE spec.
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
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


def _validate_image(upload: UploadFile, data: bytes) -> str:
    """Return the media type, raising HTTPException if the upload is rejected.

    Trusts the client's content-type hint but cross-checks the magic bytes so
    a renamed `.png` blob can't sneak past the allowlist.
    """
    media_type = (upload.content_type or "").lower()
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise HTTPException(400, f"unsupported image type: {media_type!r}")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, f"image too large (>{MAX_IMAGE_BYTES} bytes)")
    sniffed: Optional[str] = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        sniffed = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        sniffed = "image/webp"
    if sniffed and sniffed != media_type:
        raise HTTPException(400, f"image bytes don't match content-type {media_type!r}")
    return media_type


async def _read_uploaded_images(images: list[UploadFile]) -> tuple[list[dict], list[dict]]:
    """Validate and base64-encode uploaded image files."""
    if images and len(images) > MAX_IMAGES_PER_TURN:
        raise HTTPException(400, f"too many images (max {MAX_IMAGES_PER_TURN})")
    blocks: list[dict] = []
    meta: list[dict] = []
    for img in images or []:
        if not img or not img.filename:
            continue
        data = await img.read()
        if not data:
            continue
        media_type = _validate_image(img, data)
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
        meta.append({"filename": img.filename, "media_type": media_type, "size": len(data)})
    return blocks, meta


def _compose_auto_fire_message(events: list[dict]) -> str:
    """Render buffered task notifications into a synthetic user message.

    The model sees this and decides what to do — the same as if you had
    typed it. Keep it terse so it doesn't crowd the agent's context.
    """
    lines = ["Background events from your tools (auto-injected):"]
    for e in events:
        kind = e.get("kind") or "event"
        task_id = e.get("task_id") or "?"
        bits = [f"- [{kind} task={task_id}]"]
        if e.get("status"):
            bits.append(f"status={e['status']}")
        if e.get("summary"):
            bits.append(str(e["summary"]))
        if e.get("description"):
            bits.append(str(e["description"]))
        if e.get("output_file"):
            bits.append(f"output_file={e['output_file']}")
        lines.append(" ".join(bits))
    lines.append("Please respond as appropriate, or stay quiet if nothing needs doing.")
    return "\n".join(lines)


@app.post("/api/chat")
async def api_chat(
    request: Request,
    message: str = Form(...),
    session_id: str = Form(default=""),
    project: str = Form(default=""),
    model: str = Form(default=""),
    images: list[UploadFile] = File(default_factory=list),
    user: dict = Depends(auth.require_user),
):
    """Send a user message into a (possibly already-running) conversation.

    If an ActiveRun exists for this session_id, the message is enqueued onto
    its driver — the bundled CLI subprocess and any in-flight Monitor stay
    alive across turns. Otherwise we spawn a fresh driver.

    Permission requests come back as `permission_request` events, resolved
    via /api/permission/{id}. Reconnect via /api/chat/stream/{run_id}.
    """
    _gc_runs()

    cwd = _resolve_project(project)
    if model and model not in MODELS_BY_KEY:
        raise HTTPException(400, "unknown model")
    selected_model = MODELS_BY_KEY.get(model, {}) if model else {}

    image_blocks, _image_meta = await _read_uploaded_images(images)

    # Reuse an existing long-lived run for this session if we can.
    existing = _existing_run_for_session(session_id) if session_id else None
    if existing is not None:
        _require_owner(existing, user)
        # Subscribe BEFORE we emit the new user_prompt so the new subscriber
        # only sees events from this turn forward, not the entire prior
        # history that the browser already rendered.
        start_index = len(existing.events)
        existing.emit({"type": "user_prompt", "text": message, "image_count": len(image_blocks)})
        # Pending notifications get superseded by explicit user input — the
        # user is now driving, no need to auto-fire. Reset the chain counter
        # so the user gets a fresh budget if their reply itself triggers
        # background work later.
        existing.pending_notifications.clear()
        existing.notification_grace_started_at = None
        existing.consecutive_auto_fires = 0
        await existing.user_input_queue.put({"text": message, "image_blocks": image_blocks})
        return _stream_run_response(existing, start_index=start_index)

    sid_in = session_id or None
    run_id = str(uuid_mod.uuid4())
    run = ActiveRun(run_id, owner_sub=user.get("sub"))
    run.project_key = _sanitize_project_key(cwd)
    ACTIVE_RUNS[run_id] = run
    # First two events: run_id (so a reload can reconnect) and the user's
    # prompt (so a resumed transcript shows what was asked — the SDK only
    # echoes assistant content and tool results back).
    run.emit({"type": "run_started", "run_id": run_id, "project": run.project_key, "model": model or None})
    run.emit({"type": "user_prompt", "text": message, "image_count": len(image_blocks)})

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
        if tool_name in SAFE_TOOLS:
            return PermissionResultAllow()
        sig = _tool_signature(tool_name, tool_input)
        if (tool_name, sig) in run.session_allowlist:
            return PermissionResultAllow()

        request_id = str(uuid_mod.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        PENDING[request_id] = {"future": fut, "owner_sub": run.owner_sub}
        try:
            run.emit({
                "type": "permission_request",
                "id": request_id,
                "tool": tool_name,
                "input": tool_input,
                "signature": sig,
                "timeout_seconds": PERMISSION_TIMEOUT_SECONDS,
            })
            try:
                decision = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                run.emit({
                    "type": "permission_timeout",
                    "id": request_id,
                    "tool": tool_name,
                    "timeout_seconds": PERMISSION_TIMEOUT_SECONDS,
                })
                return PermissionResultDeny(
                    message=(
                        f"Permission request timed out after "
                        f"{PERMISSION_TIMEOUT_SECONDS}s with no user response."
                    ),
                )
        finally:
            PENDING.pop(request_id, None)

        d = decision.get("decision")
        if d == "allow":
            return PermissionResultAllow()
        if d == "allow_session":
            run.session_allowlist.add((tool_name, sig))
            return PermissionResultAllow()
        return PermissionResultDeny(message="User denied permission via web UI.")

    # Buffer the CLI subprocess's stderr so we can include it in any error
    # event we emit. Without this the SDK just surfaces "Error in input
    # stream" with no clue why (rate limit? OOM? bad arg?).
    stderr_buf: list[str] = []

    def _capture_stderr(line: str) -> None:
        if len(stderr_buf) < 200:
            stderr_buf.append(line)

    options_kwargs: dict[str, Any] = dict(
        cwd=str(cwd),
        resume=sid_in,
        permission_mode="default",
        can_use_tool=can_use_tool,
        setting_sources=["user", "project", "local"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        # Default-None hides every installed skill from the model. "all"
        # mirrors the host-shell `claude` CLI so /security-review,
        # /init, /loop, /skill <name>, etc. actually run.
        skills="all",
        include_partial_messages=False,
        stderr=_capture_stderr,
    )
    sdk_model = selected_model.get("model") or ""
    if sdk_model:
        options_kwargs["model"] = sdk_model
    sdk_betas = list(selected_model.get("betas") or [])
    if sdk_betas:
        # The CLI surfaces an unsupported beta as "Error in input stream", so
        # only set this when the picked variant actually wants it (currently
        # only Opus 4.7's 1M-context option).
        options_kwargs["betas"] = sdk_betas
    options = ClaudeAgentOptions(**options_kwargs)

    async def _query_iter_for(text: str, blocks: list[dict]):
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(blocks)
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    async def _send_user_message(client: ClaudeSDKClient, text: str, blocks: list[dict]) -> None:
        """Forward one user input into the live SDK client.

        Strings can't carry image blocks, so we only swap to the iterable
        form when we actually have an image attachment.
        """
        if blocks:
            await client.query(_query_iter_for(text, blocks))
        else:
            await client.query(text or "")

    async def _wait_for_next_action(client: ClaudeSDKClient) -> Optional[dict]:
        """After ResultMessage, decide whether to send another turn or exit.

        - Queued user input (immediate) → return it.
        - Pending task notifications + AUTO_FIRE_GRACE_MS settle → auto-fire,
          unless the auto-fire chain has already hit MAX_CONSECUTIVE_AUTO_FIRES
          (then we drop the notifications and wait for the human).
        - Neither for SESSION_IDLE_TIMEOUT_MS → return None (driver exits,
          subprocess closes, monitors die).
        """
        cap_reached = run.consecutive_auto_fires >= MAX_CONSECUTIVE_AUTO_FIRES

        def _drain_pending_for_auto_fire() -> dict:
            events = run.pending_notifications[:]
            run.pending_notifications = []
            run.notification_grace_started_at = None
            run.consecutive_auto_fires += 1
            synth = _compose_auto_fire_message(events)
            run.emit({"type": "auto_fire", "events": events})
            return {"kind": "auto_fire", "synth": synth}

        def _drop_pending_capped() -> None:
            dropped = run.pending_notifications[:]
            run.pending_notifications = []
            run.notification_grace_started_at = None
            run.emit({
                "type": "auto_fire_capped",
                "events": dropped,
                "limit": MAX_CONSECUTIVE_AUTO_FIRES,
            })

        # Auto-fire path: short timeout, then synth message.
        if run.pending_notifications and run.notification_grace_started_at and not cap_reached:
            grace_remaining = AUTO_FIRE_GRACE_MS / 1000 - (time.time() - run.notification_grace_started_at)
            if grace_remaining <= 0:
                return _drain_pending_for_auto_fire()
            timeout = grace_remaining
        else:
            if cap_reached and run.pending_notifications:
                _drop_pending_capped()
            timeout = SESSION_IDLE_TIMEOUT_MS / 1000

        try:
            item = await asyncio.wait_for(run.user_input_queue.get(), timeout=timeout)
            run.consecutive_auto_fires = 0
            return {"kind": "user", "item": item}
        except asyncio.TimeoutError:
            if run.pending_notifications and not cap_reached:
                return _drain_pending_for_auto_fire()
            if run.pending_notifications:
                _drop_pending_capped()
            return None

    async def driver():
        try:
            async with ClaudeSDKClient(options=options) as client:
                # Initial user message — already enqueued for transcript;
                # send into the SDK now.
                await _send_user_message(client, message, image_blocks)

                async for msg in client.receive_messages():
                    run.last_activity = time.time()
                    for evt in _sdk_message_to_events(msg):
                        run.emit(evt)

                    # Index by SDK session id once we know it, so a follow-up
                    # /api/chat with the same session can find this run.
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        sid = (msg.data or {}).get("session_id")
                        if sid and run.session_id is None:
                            run.session_id = sid
                            ACTIVE_RUNS_BY_SESSION[sid] = run

                    if isinstance(msg, (TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage)):
                        run.pending_notifications.append({
                            "task_id": msg.task_id,
                            "kind": type(msg).__name__,
                            "description": getattr(msg, "description", None),
                            "summary": getattr(msg, "summary", None),
                            "status": getattr(msg, "status", None),
                            "output_file": getattr(msg, "output_file", None),
                        })
                        run.notification_grace_started_at = time.time()

                    if isinstance(msg, ResultMessage):
                        next_action = await _wait_for_next_action(client)
                        if next_action is None:
                            break
                        if next_action["kind"] == "user":
                            item = next_action["item"]
                            await _send_user_message(client, item.get("text") or "", item.get("image_blocks") or [])
                        else:  # auto_fire
                            await client.query(next_action["synth"])

        except asyncio.CancelledError:
            # Explicit /api/chat/stop or process shutdown — surface as a
            # tidy "stopped" rather than the raw transport error that the
            # SDK would otherwise raise on broken pipes.
            run.emit({"type": "stopped"})
            raise
        except Exception as e:
            tb = traceback.format_exc()
            tail = "\n".join(stderr_buf[-30:]).strip()
            logging.getLogger("claude-web").error(
                "driver error: %s\nstderr:\n%s\ntraceback:\n%s", e, tail, tb,
            )
            payload = {"type": "error", "message": f"{type(e).__name__}: {e}"}
            detail_parts: list[str] = []
            if tail:
                detail_parts.append("--- CLI stderr ---\n" + tail)
            detail_parts.append("--- traceback ---\n" + tb)
            payload["stderr"] = "\n\n".join(detail_parts)
            run.emit(payload)
        finally:
            run.finish()

    run.task = asyncio.create_task(driver())
    return _stream_run_response(run)


@app.post("/api/chat/send/{run_id}")
async def api_chat_send(
    run_id: str,
    message: str = Form(...),
    images: list[UploadFile] = File(default_factory=list),
    user: dict = Depends(auth.require_user),
):
    """Enqueue a user message into an already-running long-lived run.

    The browser uses this when its SSE subscription is still open from a
    prior turn — avoids opening a second stream just to deliver the input.
    Returns 202 Accepted; the existing stream emits the new events.
    """
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None or run.done:
        raise HTTPException(404, "no such run")
    _require_owner(run, user)
    image_blocks, _meta = await _read_uploaded_images(images)
    run.emit({"type": "user_prompt", "text": message, "image_count": len(image_blocks)})
    run.pending_notifications.clear()
    run.notification_grace_started_at = None
    run.consecutive_auto_fires = 0
    await run.user_input_queue.put({"text": message, "image_blocks": image_blocks})
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/api/chat/active")
async def api_chat_active(run_id: str = "", user: dict = Depends(auth.require_user)):
    """Used by the browser on page load to decide whether to resume."""
    if not run_id:
        return {"active": False}
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None:
        return {"active": False}
    # Don't leak existence/state of someone else's run; report inactive so the
    # browser falls back to a fresh session.
    if run.owner_sub and run.owner_sub != user.get("sub"):
        return {"active": False}
    return {
        "active": not run.done,
        "run_id": run.run_id,
        "session_id": run.session_id,
        "project": run.project_key,
        "buffered_events": len(run.events),
    }


@app.get("/api/chat/stream/{run_id}")
async def api_chat_stream(run_id: str, user: dict = Depends(auth.require_user)):
    """Reconnect to an in-flight or recently-finished run."""
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    _require_owner(run, user)
    return _stream_run_response(run)


@app.post("/api/chat/stop/{run_id}")
async def api_chat_stop(run_id: str, user: dict = Depends(auth.require_user)):
    """Cancel the SDK task. Idempotent for runs already finished."""
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    _require_owner(run, user)
    if run.done:
        return {"ok": True, "already_done": True}
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
    if isinstance(msg, TaskStartedMessage):
        return [{
            "type": "task_started",
            "task_id": msg.task_id,
            "description": msg.description,
            "task_type": msg.task_type,
            "tool_use_id": msg.tool_use_id,
        }]
    if isinstance(msg, TaskProgressMessage):
        return [{
            "type": "task_progress",
            "task_id": msg.task_id,
            "description": msg.description,
            "last_tool_name": msg.last_tool_name,
            "tool_use_id": msg.tool_use_id,
        }]
    if isinstance(msg, TaskNotificationMessage):
        return [{
            "type": "task_notification",
            "task_id": msg.task_id,
            "status": msg.status,
            "summary": msg.summary,
            "output_file": msg.output_file,
            "tool_use_id": msg.tool_use_id,
        }]
    if isinstance(msg, ResultMessage):
        _log_usage(msg)
        usage = msg.usage or {}
        return [{
            "type": "result",
            "is_error": msg.is_error,
            "result": msg.result,
            "errors": list(msg.errors or []),
            "duration_ms": msg.duration_ms,
            "total_cost_usd": msg.total_cost_usd,
            "session_id": msg.session_id,
            "subtype": msg.subtype,
            "stop_reason": msg.stop_reason,
            "num_turns": msg.num_turns,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "permission_denials": [
                {"tool_name": d.tool_name, "tool_input": d.tool_input}
                for d in (msg.permission_denials or [])
            ],
        }]
    return []


# ─── Usage tracking ───────────────────────────────────────────────────────────


def _save_rate_limit(rli: dict) -> None:
    try:
        RATE_LIMIT_CACHE.write_text(json.dumps({"info": rli, "captured_at": int(time.time())}))
    except Exception:
        pass


def _log_usage(msg) -> None:
    """Append one row per completed turn to usage.jsonl."""
    usage = getattr(msg, "usage", None) or {}
    row = {
        "ts": int(time.time()),
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


def _today_window() -> tuple[int, int]:
    """Unix [start, end) of today in local time."""
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
