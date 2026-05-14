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
import shutil
import sqlite3
import time
import traceback
import unicodedata
import uuid as uuid_mod
import weakref
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
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import setup_flow

log = logging.getLogger("claude-web")


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
STATE_DB_PATH = USAGE_DIR / "state.db"
UPLOADS_ROOT = USAGE_DIR / "uploads"
UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)

# Multi-user mode: when true, sessions are scoped to whoever first chatted in
# them. Listing / loading / deleting / exporting requires owner match. Sessions
# created via the host-shell `claude` CLI (no owner) are visible to everyone,
# matching the README's "share state" promise. Default off so a single-user
# homelab keeps working as before.
PER_USER_SESSIONS = os.getenv("CLAUDE_WEB_PER_USER_SESSIONS", "").lower() in ("1", "true", "yes")

# Per-user "personal account" support. Each logged-in user can register their
# own Claude credentials and toggle between the shared (default) account and
# their personal one. Personal credentials live in PERSONAL_HOMES_DIR/<sub>/
# as a directory that's mostly symlinks back to CLAUDE_HOME, with .credentials.json
# as the only real file — so transcripts/sessions/skills all stay shared and
# switching mid-chat is seamless. The slot identifiers in code are 'shared' and
# 'personal' (never 'office'); display labels come from SHARED_ACCOUNT_LABEL
# below and the per-user personal_label column in user_account.
PERSONAL_HOMES_DIR = Path(os.getenv(
    "CLAUDE_WEB_PERSONAL_HOMES_DIR", str(Path.home() / ".claude-homes")
)).resolve()
SHARED_ACCOUNT_LABEL = os.getenv("CLAUDE_WEB_SHARED_ACCOUNT_LABEL", "Shared").strip() or "Shared"

# Comma-separated email allowlist for the destructive /setup endpoints
# (apikey replacement, sign-Claude-out, OAuth re-flow). Only enforced in
# PER_USER_SESSIONS mode — the single-user default trusts whoever logs in.
# Empty list in multi-user mode means no one can run these endpoints from
# the browser; admins must shell into the container.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("CLAUDE_WEB_ADMIN_EMAILS", "").split(",") if e.strip()}


def _require_setup_access(user: dict) -> None:
    """Gate the credential-mutating /setup endpoints.

    Three policies, layered from strictest:
      * ``CLAUDE_WEB_ENABLE_SETUP=false`` → always 403, regardless of user.
      * ``ENABLE_SETUP=auto`` (default) → 403 once a credential is provisioned;
        admin must restart with ``ENABLE_SETUP=true`` to re-auth.
      * In ``PER_USER_SESSIONS`` mode the user's email must additionally be in
        ``CLAUDE_WEB_ADMIN_EMAILS`` so one regular user can't reconfigure
        credentials shared by the whole instance.

    Each rejection logs which gate triggered so a user wondering "why is
    /setup locked" can diagnose from the journal.
    """
    user_id = user.get("email") or user.get("sub") or "?"
    if ENABLE_SETUP == "false":
        log.info("setup-gate reject %s: ENABLE_SETUP=false", user_id)
        raise HTTPException(403, "setup is locked (CLAUDE_WEB_ENABLE_SETUP=false)")
    if ENABLE_SETUP == "auto" and setup_flow.is_configured():
        log.info(
            "setup-gate reject %s: ENABLE_SETUP=auto and Claude already configured",
            user_id,
        )
        raise HTTPException(
            403,
            "setup is locked after first configuration. Set "
            "CLAUDE_WEB_ENABLE_SETUP=true and restart to re-auth.",
        )
    if PER_USER_SESSIONS:
        email = (user.get("email") or "").lower()
        if email not in ADMIN_EMAILS:
            log.info(
                "setup-gate reject %s: not in CLAUDE_WEB_ADMIN_EMAILS", user_id,
            )
            raise HTTPException(403, "admin access required for credential changes")

# How long persisted runs (events + metadata) survive before being purged.
# Default 24h; tune via CLAUDE_WEB_PERSIST_RETENTION. The matching in-memory
# retention (RUN_RETENTION_SECONDS) is set to the same value so the GC pass
# and the sqlite purge stay aligned.
PERSIST_RETENTION_SECONDS = int(os.getenv("CLAUDE_WEB_PERSIST_RETENTION", "86400"))

# Per-run upload directories (uploads/<run_id>/) get GC'd this old. Default
# 7 days, longer than PERSIST_RETENTION_SECONDS so a user can still grab a
# file they uploaded yesterday after the conversation has rolled off.
UPLOAD_RETENTION_SECONDS = int(os.getenv("CLAUDE_WEB_UPLOAD_RETENTION", str(7 * 86400)))

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
# Generic (non-image) attachments. Saved to disk and referenced by absolute
# path in a synthetic prefix so Claude's Read/Bash tools can open them — the
# Anthropic API can't take arbitrary binary blobs as content blocks anyway.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_TURN = 10
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

# Tools where "Allow this session" is intentionally disabled. Their signature
# (first Bash word, etc.) does not capture the actual content being executed,
# so allowlisting `echo` once would also bless `echo "ok" && rm -rf ~`. The
# permission UI hides the session-allow button when the tool is in this set;
# the can_use_tool callback also refuses to extend the allowlist if the
# decision arrives anyway (defense-in-depth against a tampered client).
NO_SESSION_ALLOWLIST_TOOLS: set[str] = set(
    t.strip() for t in os.getenv("NO_SESSION_ALLOWLIST_TOOLS", "Bash").split(",") if t.strip()
)

# Cap on per-subscriber SSE event queue depth. A slow or stuck client used to
# accumulate every event for the run forever; with this cap a 1000-event
# backlog disconnects the slow subscriber instead of growing memory unbounded.
# Active subscribers should never approach this — events are bytes on the wire
# the moment they're queued.
MAX_SUBSCRIBER_QUEUE = int(os.getenv("CLAUDE_WEB_MAX_SUBSCRIBER_QUEUE", "1000"))

# Maximum bytes we'll buffer in memory for a single uploaded image. Smaller
# than MAX_IMAGE_BYTES is fine — we stream and abort when the limit is hit
# rather than reading the whole file just to reject it.
MAX_IMAGE_READ_CHUNKS = 16  # 1MB chunks → 16MB ceiling matched to MAX_IMAGE_BYTES

# Setup-flow lock-down. Three states:
#   "true"  → /setup endpoints accept destructive actions for any signed-in
#             user (subject to PER_USER_SESSIONS admin gating).
#   "false" → /setup destructive actions always 403; admin must shell in.
#   "auto"  → (default) acts like "true" while is_configured() returns False,
#             "false" once a credential has been provisioned. Re-auth requires
#             flipping to "true" and restarting.
ENABLE_SETUP = os.getenv("CLAUDE_WEB_ENABLE_SETUP", "auto").strip().lower()
if ENABLE_SETUP not in ("true", "false", "auto"):
    raise RuntimeError(
        f"CLAUDE_WEB_ENABLE_SETUP must be 'true', 'false', or 'auto'; got {ENABLE_SETUP!r}"
    )

# CSRF: known-safe methods bypass the Origin check entirely. Everything else
# must carry an Origin/Referer that matches our expected origin (computed from
# OIDC_REDIRECT_URI when set, falling back to request.base_url). When set to
# "true", requests with no Origin AND no Referer are rejected — turn off only
# for command-line testing.
CSRF_STRICT = os.getenv("CLAUDE_WEB_CSRF_STRICT", "true").strip().lower() != "false"
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Single-worker enforcement. Module-global state (ACTIVE_RUNS, PENDING,
# _SESSION_LOCKS, etc.) does not survive a multi-worker uvicorn deployment —
# permission requests pinned to one worker won't be visible from another, and
# SSE streams will route at random. Refuse to start with WEB_CONCURRENCY>1
# unless explicitly opted out.
def _enforce_single_worker() -> None:
    raw = os.getenv("WEB_CONCURRENCY", "").strip()
    if not raw:
        return
    try:
        n = int(raw)
    except ValueError:
        return
    if n > 1 and os.getenv("CLAUDE_WEB_ALLOW_MULTI_WORKER", "").lower() != "true":
        raise RuntimeError(
            f"WEB_CONCURRENCY={n} but claude-web requires single-worker mode "
            "(in-process state isn't shared across workers). Set "
            "CLAUDE_WEB_ALLOW_MULTI_WORKER=true to override at your own risk."
        )


_enforce_single_worker()

# Refuse the dangerous combination at startup: PER_USER_SESSIONS is meant to
# isolate users from each other, but in AUTH_MODE=none every visitor is
# `sub="anonymous"`, so isolation collapses. Ship explicit isolation or pick a
# different mode.
if PER_USER_SESSIONS and auth.AUTH_MODE == "none":
    raise RuntimeError(
        "CLAUDE_WEB_PER_USER_SESSIONS=true is incompatible with AUTH_MODE=none — "
        "every visitor would share owner_sub='anonymous'. Either enable OIDC or "
        "disable PER_USER_SESSIONS."
    )

class CSRFMiddleware(BaseHTTPMiddleware):
    """Origin/Referer-based CSRF defense for state-changing requests.

    SameSite=Lax cookies block most cross-site form POSTs in modern browsers,
    but we layer this anyway because (a) older browsers and embedded webviews
    have weaker SameSite enforcement, and (b) the endpoints downstream
    authorize shell execution. The check skips safe methods plus the OIDC
    callback (the IdP issues a top-level GET), and is configurable via
    CLAUDE_WEB_CSRF_STRICT.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _CSRF_SAFE_METHODS:
            return await call_next(request)
        # Auth callback is a top-level GET from the IdP, but if a particular
        # provider ever issues POST we still want it through unchecked.
        if request.url.path.startswith("/auth/"):
            return await call_next(request)

        expected = auth.expected_origin(request)
        origin = request.headers.get("origin", "").rstrip("/")
        referer = request.headers.get("referer", "")
        # Log rejections at WARNING so a user reporting "my browser is blocked"
        # can be diagnosed from the journal without re-running the request —
        # without this you can't tell CSRF rejection from auth failure (both
        # surface as 403). Includes the offending header so a misconfigured
        # reverse proxy or wrong OIDC_REDIRECT_URI is debuggable.
        if origin:
            if origin != expected:
                log.warning(
                    "CSRF reject %s %s: Origin=%r expected=%r",
                    request.method, request.url.path, origin, expected,
                )
                return JSONResponse(
                    {"error": "csrf", "detail": f"bad Origin {origin!r}"}, status_code=403
                )
        elif referer:
            if not referer.startswith(expected + "/") and referer.rstrip("/") != expected:
                log.warning(
                    "CSRF reject %s %s: Referer=%r expected=%r",
                    request.method, request.url.path, referer, expected,
                )
                return JSONResponse(
                    {"error": "csrf", "detail": "bad Referer"}, status_code=403
                )
        elif CSRF_STRICT:
            log.warning(
                "CSRF reject %s %s: no Origin/Referer (strict mode)",
                request.method, request.url.path,
            )
            return JSONResponse(
                {"error": "csrf", "detail": "missing Origin/Referer"}, status_code=403
            )
        return await call_next(request)


# CSP defaults: 'self' for everything, allow data: and blob: for images so the
# attachment thumbnails (FileReader → data URL) keep working, allow inline
# style only because dynamic context-meter colours are set via element.style
# (refactor target). frame-ancestors blocks clickjacking on permission cards;
# X-Frame-Options is the legacy backstop for older browsers.
_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject CSP + a few hardening headers on every response.

    Cheap, no per-route exemptions needed — static assets get the same CSP and
    nothing breaks because they're already same-origin.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP_HEADER_VALUE)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response


app = FastAPI()
auth.configure(app)
# Order matters: SessionMiddleware (added by auth.configure) wraps the app
# innermost so request.session is available to handlers. CSRF + security
# headers wrap outside of it, so they see the request before session lookup
# but after the underlying ASGI server. add_middleware adds outermost-first.
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
auth.install_routes(app)
# Pull a persisted Anthropic API key (from a previous /setup api-key submission)
# into the env so the SDK and CLI both see it without a container restart.
setup_flow.load_api_key_into_env()
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
        # Auto-fire synth messages aren't user input. The live UI hides them
        # via the auto_fire event; this filter keeps the export and resumed
        # transcript from mis-attributing them to the human.
        or text.startswith(AUTO_FIRE_MARKER)
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
    """Return the best display title for a session.

    Preference order:
      1. The most recent ``ai-title`` entry the bundled CLI wrote to the
         JSONL — that's the short summary the host-shell ``claude`` shows
         in its own session list, and the same value users see in the
         terminal title. The CLI rewrites this as the conversation
         evolves, so the last occurrence wins.
      2. First user-visible message text (legacy behaviour).
      3. None.
    """
    ai_title: Optional[str] = None
    first_user_text: Optional[str] = None
    for obj in _iter_jsonl(path):
        kind = obj.get("type")
        if kind == "ai-title":
            t = obj.get("aiTitle")
            if isinstance(t, str) and t.strip():
                ai_title = t.strip()
            continue
        if first_user_text is None and kind == "user" and not obj.get("isMeta"):
            text = _extract_text(obj.get("message"))
            if text and _is_user_visible(text):
                first_user_text = text.strip()
    chosen = ai_title or first_user_text
    if not chosen:
        return None
    return (chosen[:MAX_TITLE_CHARS] + "…") if len(chosen) > MAX_TITLE_CHARS else chosen


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


def list_sessions(user: Optional[dict] = None) -> list[dict]:
    """All sessions across every configured project, newest first.

    In PER_USER_SESSIONS mode, sessions owned by other users are filtered
    out. Sessions with no recorded owner (host-shell `claude` ones) remain
    visible to everyone.
    """
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
    if PER_USER_SESSIONS and user is not None:
        rows = [r for r in rows if _user_can_see_session(r["id"], user)]
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
    # Escape & first so the &lt; / &gt; we add don't get re-escaped. Backtick
    # would break the inline-code render inside the summary.
    return (
        flat.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("`", "ʼ")
    )


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


# ─── State persistence (sqlite-backed run + event store) ─────────────────────
#
# Goal: a `systemctl restart claude-web` doesn't lose the user's transcript.
# We persist run metadata + every emitted event row so a reload's tryResume
# path keeps working: the browser's run_id still resolves to a (now-finished)
# ActiveRun whose events SSE replays. The next user message hits a fresh
# /api/chat with `resume=session_id`, which the SDK uses to pick the
# conversation back up from the underlying jsonl file. We don't try to
# reattach to the killed CLI subprocess — anything in-flight at restart time
# (a partial tool call, a queued auto-fire) is gone, but the conversation
# itself is intact.


_STATE_DB: Optional[sqlite3.Connection] = None


def _state_db() -> sqlite3.Connection:
    """Lazy-open + initialise the sqlite connection. WAL + autocommit."""
    global _STATE_DB
    if _STATE_DB is None:
        conn = sqlite3.connect(str(STATE_DB_PATH), check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            owner_sub TEXT,
            session_id TEXT,
            project_key TEXT,
            created_at REAL NOT NULL,
            finished_at REAL,
            last_activity REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_finished ON runs(finished_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS events (
            run_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(run_id, idx)
        )""")
        # Tracks who owns each web-created session for PER_USER_SESSIONS.
        # Sessions created via host-shell `claude` are absent → visible to all.
        conn.execute("""CREATE TABLE IF NOT EXISTS session_owners (
            session_id TEXT PRIMARY KEY,
            owner_sub TEXT NOT NULL,
            project_key TEXT,
            created_at REAL NOT NULL
        )""")
        # Per-user account preference: which credential slot ('shared' or
        # 'personal') the user's next run should authenticate as, and whether
        # they've actually registered personal credentials. personal_label is
        # an optional user-facing name for their personal account.
        conn.execute("""CREATE TABLE IF NOT EXISTS user_account (
            user_sub TEXT PRIMARY KEY,
            active TEXT NOT NULL CHECK(active IN ('shared','personal')) DEFAULT 'shared',
            has_personal INTEGER NOT NULL DEFAULT 0,
            personal_label TEXT,
            updated_at REAL NOT NULL
        )""")
        _STATE_DB = conn
    return _STATE_DB


def _claim_session_owner(session_id: str, owner_sub: Optional[str], project_key: Optional[str]) -> None:
    """First-write-wins ownership claim. Idempotent on re-claims."""
    if not session_id or not owner_sub:
        return
    try:
        _state_db().execute(
            "INSERT OR IGNORE INTO session_owners(session_id, owner_sub, project_key, created_at)"
            " VALUES(?, ?, ?, ?)",
            (session_id, owner_sub, project_key, time.time()),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("claim_session_owner failed: %s", e)


def _session_owner(session_id: str) -> Optional[str]:
    if not session_id:
        return None
    try:
        row = _state_db().execute(
            "SELECT owner_sub FROM session_owners WHERE session_id = ?", (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _user_account(user_sub: Optional[str]) -> dict:
    """Return {active, has_personal, personal_label} for a user.

    Defaults to the shared slot for users who've never touched the toggle
    or who aren't logged in (AUTH_MODE=none → sub='anonymous').
    """
    default = {"active": "shared", "has_personal": False, "personal_label": None}
    if not user_sub:
        return default
    try:
        row = _state_db().execute(
            "SELECT active, has_personal, personal_label FROM user_account WHERE user_sub = ?",
            (user_sub,),
        ).fetchone()
    except sqlite3.Error:
        return default
    if not row:
        return default
    return {
        "active": row[0],
        "has_personal": bool(row[1]),
        "personal_label": row[2],
    }


def _set_user_active(user_sub: str, active: str) -> None:
    """Flip a user's active slot. Caller must validate active ∈ {'shared','personal'}."""
    if active not in ("shared", "personal"):
        raise ValueError(f"invalid active slot: {active!r}")
    try:
        _state_db().execute(
            """INSERT INTO user_account(user_sub, active, has_personal, personal_label, updated_at)
               VALUES(?, ?, 0, NULL, ?)
               ON CONFLICT(user_sub) DO UPDATE SET
                   active=excluded.active,
                   updated_at=excluded.updated_at""",
            (user_sub, active, time.time()),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("set_user_active failed: %s", e)


def _mark_personal_registered(user_sub: str, label: Optional[str] = None) -> None:
    """Record that a user has provisioned personal credentials. Idempotent;
    setting label to None leaves any existing label intact."""
    try:
        _state_db().execute(
            """INSERT INTO user_account(user_sub, active, has_personal, personal_label, updated_at)
               VALUES(?, 'shared', 1, ?, ?)
               ON CONFLICT(user_sub) DO UPDATE SET
                   has_personal=1,
                   personal_label=COALESCE(excluded.personal_label, user_account.personal_label),
                   updated_at=excluded.updated_at""",
            (user_sub, label, time.time()),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("mark_personal_registered failed: %s", e)


def _safe_sub(user_sub: str) -> str:
    """Sanitise an OIDC subject for use as a directory name. Subs are
    typically opaque UUIDs but we don't trust the IdP to keep them out of
    `/` / `..` territory."""
    sanitised = re.sub(r"[^A-Za-z0-9_-]", "_", user_sub or "")[:64]
    return sanitised or "anonymous"


def _personal_home_path(user_sub: str) -> Path:
    return PERSONAL_HOMES_DIR / _safe_sub(user_sub)


def _ensure_personal_home(user_sub: str) -> Path:
    """Create or refresh the per-user CLAUDE_CONFIG_DIR.

    The personal home is a directory that mirrors CLAUDE_HOME via symlinks,
    *except* for .credentials.json which is the user's real (personal)
    credential file. The symlinks mean projects/, sessions/, settings.json,
    skills/, etc. all resolve back to the shared home — so a user's
    transcript history is identical regardless of which slot is active and
    switching between slots mid-conversation does not move the JSONL file
    the CLI is appending to.

    Idempotent. Existing entries are never overwritten, so the user's
    real .credentials.json is safe.
    """
    home = _personal_home_path(user_sub)
    home.mkdir(parents=True, exist_ok=True)
    try:
        entries = list(CLAUDE_HOME.iterdir())
    except FileNotFoundError:
        return home
    for entry in entries:
        # Never symlink the credentials file — it must be a real per-user
        # file or the personal slot would auth as the shared account.
        if entry.name == ".credentials.json":
            continue
        link = home / entry.name
        # is_symlink() guards the "broken symlink pointing into the shared
        # home" case where exists() returns False but the link is set up.
        if link.is_symlink() or link.exists():
            continue
        try:
            link.symlink_to(entry.resolve())
        except OSError as e:
            logging.getLogger("claude-web").warning(
                "personal-home symlink %s → %s failed: %s", link, entry, e
            )
    return home


def _resolve_account_for_run(user: dict) -> dict:
    """Pick the credential slot for the user's next run.

    Returns ``{"slot": "shared"|"personal", "env": dict[str,str], "label": str}``.
    ``env`` is empty for shared (CLAUDE_HOME wins naturally) and carries
    CLAUDE_CONFIG_DIR for personal. If the user is marked has_personal but
    their .credentials.json is missing, falls back to shared rather than
    spawning a CLI that would crash on first API call.
    """
    sub = (user or {}).get("sub")
    state = _user_account(sub)
    if state["active"] == "personal" and state["has_personal"] and sub:
        home = _ensure_personal_home(sub)
        if (home / ".credentials.json").exists():
            return {
                "slot": "personal",
                "env": {"CLAUDE_CONFIG_DIR": str(home)},
                "label": state["personal_label"] or "My account",
            }
        logging.getLogger("claude-web").warning(
            "personal creds missing for sub=%s; falling back to shared", sub
        )
    return {"slot": "shared", "env": {}, "label": SHARED_ACCOUNT_LABEL}


def _user_can_see_session(session_id: str, user: dict) -> bool:
    """In PER_USER_SESSIONS mode, hide other users' sessions. Sessions with
    no recorded owner (host-shell `claude` ones, pre-feature ones) stay
    visible to everyone — that's the documented "share with the shell" path.
    """
    if not PER_USER_SESSIONS:
        return True
    owner = _session_owner(session_id)
    if owner is None:
        return True
    return owner == (user or {}).get("sub")


def _persist_run_meta(run: "ActiveRun") -> None:
    """Upsert the runs row. COALESCE preserves session_id/project_key once set
    so a later emit() that doesn't carry them can't blank them out."""
    try:
        _state_db().execute(
            """
            INSERT INTO runs(run_id, owner_sub, session_id, project_key,
                             created_at, finished_at, last_activity)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                session_id=COALESCE(excluded.session_id, runs.session_id),
                project_key=COALESCE(excluded.project_key, runs.project_key),
                finished_at=excluded.finished_at,
                last_activity=excluded.last_activity
            """,
            (
                run.run_id, run.owner_sub, run.session_id, run.project_key,
                run.created_at, run.finished_at, run.last_activity,
            ),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("persist_run_meta failed: %s", e)


def _persist_event(run_id: str, idx: int, event: dict) -> None:
    try:
        payload = json.dumps(event)
    except (TypeError, ValueError):
        return
    try:
        _state_db().execute(
            "INSERT OR REPLACE INTO events(run_id, idx, payload) VALUES(?, ?, ?)",
            (run_id, idx, payload),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("persist_event failed: %s", e)


def _purge_old_persisted(now: float) -> None:
    cutoff = now - PERSIST_RETENTION_SECONDS
    try:
        db = _state_db()
        db.execute(
            "DELETE FROM events WHERE run_id IN ("
            " SELECT run_id FROM runs WHERE COALESCE(finished_at, last_activity) < ?"
            ")",
            (cutoff,),
        )
        db.execute(
            "DELETE FROM runs WHERE COALESCE(finished_at, last_activity) < ?",
            (cutoff,),
        )
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("purge_old_persisted failed: %s", e)


_LAST_UPLOAD_PURGE = 0.0
_UPLOAD_PURGE_INTERVAL_SECONDS = 600  # don't rescan the dir on every request
_LAST_DB_PURGE = 0.0
_DB_PURGE_INTERVAL_SECONDS = 3600  # one sqlite DELETE pass per hour is plenty


def _purge_old_uploads(now: float) -> None:
    """Drop per-run upload directories older than UPLOAD_RETENTION_SECONDS.

    Wired into _gc_runs (which fires on every /api/chat) but throttled to
    one scan every ten minutes so a chatty session doesn't statvfs-storm
    the uploads dir. Names that don't match the run-id regex are ignored.
    """
    global _LAST_UPLOAD_PURGE
    if now - _LAST_UPLOAD_PURGE < _UPLOAD_PURGE_INTERVAL_SECONDS:
        return
    _LAST_UPLOAD_PURGE = now
    cutoff = now - UPLOAD_RETENTION_SECONDS
    try:
        entries = list(UPLOADS_ROOT.iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if not _ID_RE.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        # Use rmtree so a stray subdirectory (created by an external process
        # or a future feature that nests uploads) doesn't crash the GC pass
        # with IsADirectoryError. ignore_errors=True so a transient permission
        # blip doesn't take the whole sweep down.
        try:
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def _restore_persisted_runs() -> None:
    """At boot, hydrate ACTIVE_RUNS from sqlite as already-finished entries.

    Each restored run is marked done so the in-memory state is consistent
    with "the live SDK turn is gone". Runs that died mid-flight (finished_at
    NULL on disk) get a synthetic `restarted_during_run` event appended so
    the user knows what happened, and the synth is itself persisted so the
    next restart sees a clean already-finished row.
    """
    now = time.time()
    _purge_old_persisted(now)
    try:
        rows = _state_db().execute(
            "SELECT run_id, owner_sub, session_id, project_key, created_at,"
            " finished_at, last_activity FROM runs ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error as e:
        logging.getLogger("claude-web").warning("restore_persisted_runs read failed: %s", e)
        return

    log = logging.getLogger("claude-web")
    restored = 0
    interrupted = 0
    for run_id, owner_sub, session_id, project_key, created_at, finished_at, last_activity in rows:
        run = ActiveRun(run_id, owner_sub=owner_sub)
        run.created_at = created_at or now
        run.last_activity = last_activity or now
        run.session_id = session_id
        run.project_key = project_key
        try:
            evt_rows = _state_db().execute(
                "SELECT payload FROM events WHERE run_id = ? ORDER BY idx", (run_id,),
            ).fetchall()
        except sqlite3.Error:
            evt_rows = []
        # Track permission_requests that never got resolved/timed out on
        # disk, so we can synthesize a timeout for them after replay. Without
        # this, a browser reconnect would render the permission card and the
        # user's click would 404 (PENDING is in-process state, blown away by
        # the restart).
        unresolved_perms: dict[str, int] = {}
        for (payload,) in evt_rows:
            try:
                evt = json.loads(payload)
            except (TypeError, ValueError):
                continue
            # Backfill _idx for events persisted before emit() started
            # tagging — keeps replay dedup consistent across mixed batches.
            evt.setdefault("_idx", len(run.events))
            run.events.append(evt)
            etype = evt.get("type")
            eid = evt.get("id")
            if etype == "permission_request" and eid:
                unresolved_perms[eid] = len(run.events) - 1
            elif etype == "permission_timeout" and eid:
                unresolved_perms.pop(eid, None)
        # Append a synthetic timeout for each orphaned request so the
        # browser disables its card on resume instead of letting a click
        # 404 silently.
        for pid in unresolved_perms:
            synth = {
                "type": "permission_timeout",
                "id": pid,
                "tool": None,
                "timeout_seconds": 0,
                "reason": "server_restart",
                "_idx": len(run.events),
            }
            run.events.append(synth)
            _persist_event(run_id, len(run.events) - 1, synth)
        was_killed = finished_at is None
        if was_killed:
            synth = {
                "type": "restarted_during_run",
                "message": (
                    "Server restarted while this turn was running. The "
                    "conversation is intact — send a new message and Claude "
                    "will pick up from here."
                ),
                "ts": now,
                "_idx": len(run.events),
            }
            run.events.append(synth)
            _persist_event(run_id, len(run.events) - 1, synth)
            interrupted += 1
        run.done = True
        run.finished_at = finished_at or now
        ACTIVE_RUNS[run_id] = run
        if was_killed:
            _persist_run_meta(run)
        restored += 1

    if restored:
        log.info("Restored %d run(s) from %s (%d interrupted)",
                 restored, STATE_DB_PATH, interrupted)


# ─── Active run tracking ─────────────────────────────────────────────────────


class ActiveRun:
    """One long-lived conversation backed by a single ClaudeSDKClient.

    Originally per-turn; widened so the bundled CLI subprocess survives
    across user messages and Monitor / TaskNotification events keep flowing
    in between turns. The driver loop reads receive_messages() forever,
    auto-firing follow-up turns when background tools emit notifications.

    Subscribers (HTTP SSE streams) come and go. Buffered events let a
    reconnecting browser replay-then-tail.
    """

    def __init__(self, run_id: str, owner_sub: Optional[str] = None,
                 account_slot: str = "shared"):
        self.run_id = run_id
        self.owner_sub = owner_sub
        # Which credential slot this run's CLI subprocess was spawned with.
        # The CLI reads .credentials.json once at startup, so we can't change
        # the auth identity mid-run — api_chat compares this against the
        # user's current toggle and respawns the run when they differ.
        self.account_slot = account_slot
        self.events: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.session_id: Optional[str] = None
        self.project_key: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        now = time.time()
        self.created_at: float = now
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
        self.last_activity: float = now
        # The live SDK client, published once the driver has finished the
        # initial query. Held only as a debug breadcrumb / capability
        # marker — handlers no longer write to it directly. All user input
        # flows through ``user_input_queue`` and is dequeued by the driver
        # while ``between_turns`` is True, which serialises every CLI write
        # through one writer (the driver). The previous direct-write
        # design caused queued messages to land in the CLI's stdin
        # mid-turn and be silently consumed/discarded.
        self.client: Optional[Any] = None  # ClaudeSDKClient
        self.client_write_lock: asyncio.Lock = asyncio.Lock()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        idx = len(self.events) - 1
        # Tag with monotonic per-run index so the browser can dedupe if the
        # same event reaches its DOM twice (e.g. two open SSE subscribers
        # against this run, or a stream resume that replays). Cheaper and
        # more reliable than client-side content hashing.
        event["_idx"] = idx
        meta_changed = False
        if event.get("type") == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid and self.session_id != sid:
                old = self.session_id
                self.session_id = sid
                # Re-index ACTIVE_RUNS_BY_SESSION too, since the SDK can
                # report a different session_id than the resume= we passed
                # (rare, but the resume protocol allows it).
                if old and ACTIVE_RUNS_BY_SESSION.get(old) is self:
                    ACTIVE_RUNS_BY_SESSION.pop(old, None)
                ACTIVE_RUNS_BY_SESSION[sid] = self
                meta_changed = True
                # First-write-wins claim so PER_USER_SESSIONS mode knows who
                # owns this transcript. Idempotent; no-op when disabled.
                _claim_session_owner(sid, self.owner_sub, self.project_key)
        if event.get("type") == "run_started":
            meta_changed = True
        self.last_activity = time.time()
        # Subscriber queues are bounded (MAX_SUBSCRIBER_QUEUE) — a slow client
        # that lets its queue fill up gets disconnected rather than growing
        # the run's memory unbounded. The browser will fall back to /api/
        # chat/stream/<run_id> + the persisted event log to catch up.
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(q)
                log.warning(
                    "dropping slow SSE subscriber for run %s (queue at %d)",
                    self.run_id, q.qsize(),
                )
                # The queue is full by definition (that's why put_nowait
                # raised). Evict one buffered event to make room for the
                # overflow signal — without this, the consumer reads the
                # backlog, blocks on the next get(), and never learns that
                # it was dropped. The frontend handles _overflow by
                # reconnecting via /api/chat/stream/<run_id> + the
                # persisted store, so the dropped event is recovered there.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait({"type": "_overflow"})
                except asyncio.QueueFull:
                    pass
        # Persist after fan-out so a slow disk write never blocks the live
        # browser update. Failures are logged and swallowed.
        _persist_event(self.run_id, idx, event)
        if meta_changed:
            _persist_run_meta(self)

    def subscribe(self, start_index: int = 0) -> asyncio.Queue:
        """Subscribe to events from `start_index` onward.

        Use 0 for "give me everything from the start" (page reload). Use
        len(events)-N for "give me only the newest N events" — used when a
        follow-up POST hits an already-running long-lived run and the
        browser already has the older events rendered.

        Queue depth is capped at MAX_SUBSCRIBER_QUEUE; a slow subscriber that
        falls behind will be dropped and emit() sends "_overflow" so the
        client can reconnect cleanly via /api/chat/stream/<run_id>.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_SUBSCRIBER_QUEUE)
        # The replay can exceed maxsize for an already-finished run with a
        # huge event log. Use unbounded put for the initial drain — once
        # tailing live, subsequent put_nowait calls in emit() will respect
        # the cap.
        backlog = self.events[start_index:]
        for e in backlog:
            try:
                q.put_nowait(e)
            except asyncio.QueueFull:
                # Replay too large to fit; signal the consumer so it knows
                # to fetch the rest via the persisted store.
                try:
                    q.put_nowait({"type": "_overflow"})
                except asyncio.QueueFull:
                    pass
                break
        if self.done:
            try:
                q.put_nowait({"type": "_done"})
            except asyncio.QueueFull:
                pass
        else:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def finish(self) -> None:
        self.done = True
        self.finished_at = time.time()
        self.last_activity = self.finished_at
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "_done"})
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()
        # Drop the session-id mapping so a follow-up POST doesn't pin against
        # a finished run. The run object stays in ACTIVE_RUNS until _gc_runs
        # purges it (we still want /api/chat/stream/<run_id> reconnects to
        # work for late-arriving SSE clients during the retention window).
        if self.session_id and ACTIVE_RUNS_BY_SESSION.get(self.session_id) is self:
            ACTIVE_RUNS_BY_SESSION.pop(self.session_id, None)
        _persist_run_meta(self)


ACTIVE_RUNS: dict[str, ActiveRun] = {}
ACTIVE_RUNS_BY_SESSION: dict[str, ActiveRun] = {}
# Per-session locks for /api/chat. The SDK only sets run.session_id once it
# receives the init SystemMessage, so without this two near-simultaneous POSTs
# for the same resumed session_id (multi-tab / fast double-submit) both miss
# _existing_run_for_session and spawn separate runs — two CLI subprocesses
# writing to the same jsonl. The lock only guards the find-or-create step;
# we release before any long awaits on the SDK.
#
# WeakValueDictionary so a lock disappears once no live request holds a
# reference. The previous size-bounded GC sweep had a race: a request that
# called _session_lock() and yielded before .acquire() (the inevitable
# `await` below) could have its lock evicted by a concurrent _gc_runs call,
# letting a second request for the same session create a *different* lock.
# Both would then "acquire" non-overlapping locks and corrupt the
# find-or-create invariant. With WeakValueDictionary, the lock survives as
# long as any task holds it on the stack — exactly the lifetime we need.
_SESSION_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _session_lock(session_id: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock


# Match PERSIST_RETENTION_SECONDS so a finished run that's still on disk is
# also still in memory — saves a "load on demand from sqlite" code path.
RUN_RETENTION_SECONDS = PERSIST_RETENTION_SECONDS
AUTO_FIRE_GRACE_MS = 1500  # buffer late task notifications this long before auto-firing
SESSION_IDLE_TIMEOUT_MS = 10 * 60 * 1000  # close idle conversation after 10 min

# Hydrate from sqlite at module load so uvicorn's first request already sees
# whatever state survived the last restart.
_restore_persisted_runs()


def _gc_runs() -> None:
    """Evict completed runs older than retention so the dict doesn't grow."""
    global _LAST_DB_PURGE
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
    _purge_old_uploads(now)
    if now - _LAST_DB_PURGE >= _DB_PURGE_INTERVAL_SECONDS:
        _LAST_DB_PURGE = now
        _purge_old_persisted(now)


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


async def _query_iter_with_blocks(text: str, blocks: list[dict]):
    """Async iterable that yields a single user message dict.

    Used by _send_to_client when image blocks are attached: client.query()
    only accepts blocks via the iterable form, not the plain-string form.
    """
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


async def _send_to_client(client, text: str, blocks: list[dict]) -> None:
    """Forward one user input into a live SDK client."""
    if blocks:
        await client.query(_query_iter_with_blocks(text, blocks))
    else:
        await client.query(text or "")


async def _inject_user_input(run: ActiveRun, text: str, blocks: list[dict]) -> bool:
    """Queue user input for the driver to deliver to the CLI.

    Originally this also wrote directly to ``client.query()`` from the HTTP
    handler when the driver was running, in pursuit of "binary-style
    steerability" — push input mid-turn and let the bundled CLI's
    concurrent-query queue handle it. In practice, writing to stdin while
    the CLI was still generating a turn caused the message to be silently
    discarded — bytes landed in the OS pipe but the CLI's state machine
    wasn't polling stdin for new turns, so it consumed and dropped them.
    Symptom (reported by user): queued message after a long task gets
    "Accepted" 202 from the server but no Claude response.

    The fix is to always enqueue. The driver only pops from this queue
    when ``between_turns`` is True (i.e., the CLI is idle and ready),
    serializing every write through one writer. Returns ``True`` because
    queue.put on an unbounded queue can't fail; kept the bool return for
    call-site compatibility.
    """
    await run.user_input_queue.put({"text": text, "image_blocks": blocks})
    return True


# ─── HTTP routes ──────────────────────────────────────────────────────────────


@app.get("/")
async def index(request: Request, user: dict = Depends(auth.require_user)):
    if not setup_flow.is_configured():
        return RedirectResponse(url="/setup", status_code=302)
    response = templates.TemplateResponse(
        request, "index.html", {
            "sessions": list_sessions(user),
            "user": user,
            "projects": [
                {"key": _sanitize_project_key(p), "path": str(p), "name": p.name or str(p)}
                for p in PROJECTS
            ],
            "default_project": _sanitize_project_key(DEFAULT_CWD),
            "models": KNOWN_MODELS,
            # Drop SDK-internal fields and expose only what the JS needs.
            "models_json": json.dumps([
                {"key": m["key"], "label": m["label"], "context": m.get("context")}
                for m in KNOWN_MODELS
            ]),
            "multi_project": len(PROJECTS) > 1,
            "account": _account_payload(user),
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
    return list_sessions(user)


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
            if not _user_can_see_session(path.stem, user):
                continue
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
    if not _user_can_see_session(sid, user):
        # Mimic 404 rather than 403 so we don't leak existence to non-owners.
        return JSONResponse({"error": "not found"}, status_code=404)
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
    if not _user_can_see_session(sid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
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
    if not _user_can_see_session(sid, user):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _find_session_path(sid, project)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Refuse if there's a live run writing to this jsonl. Otherwise the SDK
    # subprocess keeps the unlinked file open and silently re-creates it on
    # next write, leaving the user wondering why the session keeps coming back.
    active = _existing_run_for_session(sid)
    if active is not None:
        raise HTTPException(409, "session is active — stop the run before deleting")
    path.unlink()
    # Drop the ownership row too so we don't leak rows for deleted sessions.
    try:
        _state_db().execute("DELETE FROM session_owners WHERE session_id = ?", (sid,))
    except sqlite3.Error:
        pass
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


def _account_payload(user: dict) -> dict:
    state = _user_account((user or {}).get("sub"))
    return {
        "active": state["active"],
        "has_personal": state["has_personal"],
        "personal_label": state["personal_label"] or "My account",
        "shared_label": SHARED_ACCOUNT_LABEL,
    }


@app.get("/api/account")
async def api_account_get(user: dict = Depends(auth.require_user)):
    return _account_payload(user)


@app.post("/api/account/active")
async def api_account_set_active(
    active: str = Form(...),
    user: dict = Depends(auth.require_user),
):
    sub = user.get("sub")
    if not sub:
        raise HTTPException(401, "no user identity")
    if active not in ("shared", "personal"):
        raise HTTPException(400, "invalid slot")
    if active == "personal" and not _user_account(sub)["has_personal"]:
        # Refuse to flip to personal when no credentials exist — the spawn
        # path would just fall back to shared anyway, which would confuse
        # the toggle UI.
        raise HTTPException(400, "no personal credentials registered")
    _set_user_active(sub, active)
    return _account_payload(user)


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

    Trusts the client's content-type hint but requires magic bytes to match a
    known image format. Previously, an upload claiming ``image/png`` whose
    bytes matched no known signature would slip through; now we require both.
    """
    media_type = (upload.content_type or "").lower()
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise HTTPException(400, f"unsupported image type: {media_type!r}")
    # Note: the size cap is already enforced in _read_with_cap before this is
    # called. We only need to verify content-type vs magic bytes here.
    sniffed: Optional[str] = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        sniffed = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        sniffed = "image/webp"
    if sniffed is None:
        raise HTTPException(400, f"image bytes don't match any known format for {media_type!r}")
    if sniffed != media_type:
        raise HTTPException(400, f"image bytes don't match content-type {media_type!r}")
    return media_type


async def _read_with_cap(upload: UploadFile, cap: int) -> bytes:
    """Read an UploadFile in chunks, aborting once we exceed ``cap`` bytes.

    Starlette's UploadFile already spools to disk for large bodies, but a
    direct ``await upload.read()`` still pulls every byte into memory before
    we can check. Streaming lets us reject a 1GB upload after one chunk
    instead of materialising it whole.
    """
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 1024  # 1 MiB
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(400, f"upload too large (>{cap} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# Strict media-type token. RFC 6838 limits these to ASCII letters, digits,
# and a few punctuation characters; we additionally allow `;` + space + `=`
# so a `; charset=utf-8` parameter passes through. Anything else (newlines,
# control chars, brackets) is rejected — those would otherwise confuse the
# prompt-injection prefix Claude reads via _file_attachment_prefix.
_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9.+/=;\- ]{1,128}$")


def _safe_content_type(value: Optional[str]) -> str:
    """Return a safe ASCII media type or 'application/octet-stream'."""
    if value and _CONTENT_TYPE_RE.match(value):
        return value
    return "application/octet-stream"


def _safe_filename(name: str) -> str:
    """Strip path separators / NULs / non-ASCII so the upload can't escape its
    per-run directory or trip the OS on weird unicode. NFKD-normalises first so
    "résumé.pdf" survives as "resume.pdf" instead of becoming "r_sum_.pdf".
    Falls back to "upload" if nothing usable is left."""
    base = os.path.basename(name or "")
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    cleaned = _FILENAME_RE.sub("_", base).strip("._") or "upload"
    return cleaned[:120]


async def _save_uploaded_files(files: list[UploadFile], run_id: str) -> list[dict]:
    """Persist non-image attachments under uploads/<run_id>/ and return metadata.

    The Anthropic API can't accept arbitrary binary blobs in user content, so
    instead of inlining we write to disk and let Claude's filesystem tools
    (Read, Bash) open the path. ``run_id`` namespaces the directory; cleanup
    happens with the rest of the run's persisted state.
    """
    if not files:
        return []
    real = [f for f in files if f and f.filename]
    if not real:
        return []
    if len(real) > MAX_FILES_PER_TURN:
        raise HTTPException(400, f"too many files (max {MAX_FILES_PER_TURN})")
    target_dir = UPLOADS_ROOT / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for f in real:
        # Stream-and-cap so a 100GB upload doesn't spike memory before the
        # size check rejects it.
        data = await _read_with_cap(f, MAX_UPLOAD_BYTES)
        if not data:
            continue
        base = _safe_filename(f.filename)
        target = target_dir / base
        # Disambiguate name collisions within the same run. Bounded so a
        # filename pile-up can't loop forever; we'd rather fail loudly with
        # a clear error than spin doing stat() calls.
        n = 1
        while target.exists():
            if n > 1000:
                raise HTTPException(409, f"too many name collisions for '{base}'")
            stem, dot, ext = base.rpartition(".")
            base2 = f"{stem}-{n}.{ext}" if dot else f"{base}-{n}"
            target = target_dir / base2
            n += 1
        target.write_bytes(data)
        out.append({
            "filename": target.name,
            "path": str(target),
            "size": len(data),
            "content_type": _safe_content_type(f.content_type),
        })
    return out


def _file_attachment_prefix(metas: list[dict]) -> str:
    if not metas:
        return ""
    lines = ["[Attached files for this turn — open with the Read tool (or Bash):]"]
    for m in metas:
        size_kb = max(1, round(m["size"] / 1024))
        lines.append(f"- {m['path']} ({size_kb} KB, {m['content_type']})")
    return "\n".join(lines) + "\n\n"


async def _read_uploaded_images(images: list[UploadFile]) -> tuple[list[dict], list[dict]]:
    """Validate and base64-encode uploaded image files."""
    if images and len(images) > MAX_IMAGES_PER_TURN:
        raise HTTPException(400, f"too many images (max {MAX_IMAGES_PER_TURN})")
    blocks: list[dict] = []
    meta: list[dict] = []
    for img in images or []:
        if not img or not img.filename:
            continue
        # Stream-and-cap so an oversized image is rejected after the first
        # chunk over the limit, not after we've already materialised every
        # byte into the worker's RAM.
        data = await _read_with_cap(img, MAX_IMAGE_BYTES)
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


AUTO_FIRE_MARKER = "<auto-injected-background-events>"


def _compose_auto_fire_message(events: list[dict]) -> str:
    """Render buffered task notifications into a synthetic user message.

    The model sees this and decides what to do — the same as if you had
    typed it. Keep it terse so it doesn't crowd the agent's context.

    Wrapped in AUTO_FIRE_MARKER so the JSONL replay can recognise it and
    avoid rendering the synth as a "You" bubble (the live UI hides it via
    the auto_fire event; the export and resumed view need this marker).
    """
    lines = [AUTO_FIRE_MARKER, "Background events from your tools (auto-injected):"]
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
    files: list[UploadFile] = File(default_factory=list),
    user: dict = Depends(auth.require_user),
):
    """Send a user message into a (possibly already-running) conversation.

    If an ActiveRun exists for this session_id, the message is enqueued onto
    its driver — the bundled CLI subprocess and any in-flight Monitor stay
    alive across turns. Otherwise we spawn a fresh driver.

    Refuses with 503 if Claude Code hasn't been signed in yet, so the
    frontend can bounce the user to /setup instead of waiting for a 401
    from Anthropic's API.

    Permission requests come back as `permission_request` events, resolved
    via /api/permission/{id}. Reconnect via /api/chat/stream/{run_id}.
    """
    if not setup_flow.is_configured():
        return JSONResponse(
            {"error": "claude_not_configured", "setup_url": "/setup"},
            status_code=503,
        )
    _gc_runs()

    # Validate any provided session_id: it ends up as `resume=<id>` on the
    # bundled CLI subprocess and as part of dict keys and audit logs. A
    # `../` here would have the SDK write a session file outside its
    # configured directory.
    if session_id:
        session_id = _safe_id(session_id)

    cwd = _resolve_project(project)
    if model and model not in MODELS_BY_KEY:
        raise HTTPException(400, "unknown model")
    selected_model = MODELS_BY_KEY.get(model, {}) if model else {}

    image_blocks, _image_meta = await _read_uploaded_images(images)

    # Reuse an existing long-lived run for this session if we can. The lock
    # ensures two near-simultaneous POSTs with the same session_id can't both
    # miss _existing_run_for_session and spawn duplicate runs (only relevant
    # for resumed sessions; a fresh session_id="" is unique per request).
    sess_lock = _session_lock(session_id) if session_id else None
    if sess_lock:
        await sess_lock.acquire()
    # Resolve the credential slot once per request. Used both to decide
    # whether to reuse an existing run (whose CLI was spawned with possibly
    # different creds) and to pass into the SDK on fresh spawns.
    account = _resolve_account_for_run(user)

    try:
        existing = _existing_run_for_session(session_id) if session_id else None
        if existing is not None and existing.account_slot != account["slot"]:
            # User toggled their account between turns. The CLI subprocess
            # bound its credentials at startup, so we can't just keep using
            # it; cancel the driver and fall through to spawning a fresh run
            # with `resume=session_id`. The session JSONL is in the shared
            # projects/ directory (personal home symlinks back to it), so
            # the conversation continues unbroken from the new CLI.
            log.info(
                "account-toggle respawn session=%s run=%s %s→%s",
                session_id, existing.run_id, existing.account_slot, account["slot"],
            )
            _require_owner(existing, user)
            if existing.task and not existing.task.done():
                existing.task.cancel()
            existing = None
        if existing is not None:
            _require_owner(existing, user)
            file_metas = await _save_uploaded_files(files, existing.run_id)
            effective = _file_attachment_prefix(file_metas) + message
            # Subscribe BEFORE we emit the new user_prompt so the new subscriber
            # only sees events from this turn forward, not the entire prior
            # history that the browser already rendered.
            start_index = len(existing.events)
            # Pending notifications get superseded by explicit user input —
            # the user is now driving, no need to auto-fire. Reset the chain
            # counter so the user gets a fresh budget if their reply itself
            # triggers background work later.
            existing.pending_notifications.clear()
            existing.notification_grace_started_at = None
            existing.consecutive_auto_fires = 0
            # Only persist the user_prompt event AFTER we know the input
            # actually reached the CLI — otherwise a broken pipe would leave
            # the transcript saying "you said X" when Claude never saw it.
            delivered = await _inject_user_input(existing, effective, image_blocks)
            if delivered:
                existing.emit({
                    "type": "user_prompt",
                    "text": message,
                    "image_count": len(image_blocks),
                    "file_count": len(file_metas),
                })
            return _stream_run_response(existing, start_index=start_index)

        sid_in = session_id or None
        run_id = str(uuid_mod.uuid4())
        run = ActiveRun(run_id, owner_sub=user.get("sub"), account_slot=account["slot"])
        run.project_key = _sanitize_project_key(cwd)
        # Multi-user ownership check before any upload work.
        if session_id and PER_USER_SESSIONS:
            owner = _session_owner(session_id)
            if owner is not None and owner != user.get("sub"):
                raise HTTPException(403, "not your session")
        # Save uploads inside the session lock so upload validation failures
        # never leave an orphan ActiveRun pinned in ACTIVE_RUNS_BY_SESSION.
        # If we registered first and then awaited the upload, a concurrent
        # request for the same session could find the orphan, queue input
        # into its (driver-less) queue, and report success while the message
        # silently disappears when the original request raises.
        file_metas = await _save_uploaded_files(files, run_id)
        ACTIVE_RUNS[run_id] = run
        if session_id:
            # Eager-claim the session id so a concurrent POST sees this run
            # and reuses it instead of spawning a parallel one. The driver
            # may overwrite this with whatever the SDK reports in init —
            # usually the same value but the resume protocol allows new ids.
            run.session_id = session_id
            ACTIVE_RUNS_BY_SESSION[session_id] = run
            _claim_session_owner(session_id, user.get("sub"), run.project_key)
    finally:
        if sess_lock:
            sess_lock.release()
    effective_message = _file_attachment_prefix(file_metas) + message
    # First two events: run_id (so a reload can reconnect) and the user's
    # prompt (so a resumed transcript shows what was asked — the SDK only
    # echoes assistant content and tool results back).
    run.emit({"type": "run_started", "run_id": run_id, "project": run.project_key, "model": model or None})
    run.emit({
        "type": "user_prompt",
        "text": message,
        "image_count": len(image_blocks),
        "file_count": len(file_metas),
    })

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context):
        # Audit-trail logging: every tool invocation produces exactly one
        # decision log line so "who allowed this command at 3am" is
        # answerable from the journal without grepping the SSE event store.
        # Includes run_id + owner_sub for cross-correlation with errors.
        owner = run.owner_sub or "?"
        if tool_name in SAFE_TOOLS:
            log.info(
                "perm safe-auto %s tool=%s run=%s owner=%s", tool_name,
                tool_name, run.run_id, owner,
            )
            return PermissionResultAllow()
        sig = _tool_signature(tool_name, tool_input)
        # Tools in NO_SESSION_ALLOWLIST_TOOLS bypass the per-session allowlist
        # entirely — their signature is too coarse to be safe (e.g. Bash maps
        # every command to its first word, so allowlisting `echo` would
        # bless `echo "ok" && rm -rf ~`).
        allow_session_supported = tool_name not in NO_SESSION_ALLOWLIST_TOOLS
        if allow_session_supported and (tool_name, sig) in run.session_allowlist:
            log.info(
                "perm session-allowlist tool=%s sig=%r run=%s owner=%s",
                tool_name, sig, run.run_id, owner,
            )
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
                "allow_session_supported": allow_session_supported,
            })
            try:
                decision = await asyncio.wait_for(fut, timeout=PERMISSION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.info(
                    "perm timeout tool=%s sig=%r run=%s owner=%s after=%ss",
                    tool_name, sig, run.run_id, owner, PERMISSION_TIMEOUT_SECONDS,
                )
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
        log.info(
            "perm decision=%s tool=%s sig=%r run=%s owner=%s",
            d, tool_name, sig, run.run_id, owner,
        )
        if d == "allow":
            return PermissionResultAllow()
        if d == "allow_session":
            # Defense-in-depth: refuse to extend the allowlist for tools that
            # opt out, even if a tampered client posted allow_session anyway.
            # Treat it as allow-once.
            if allow_session_supported:
                run.session_allowlist.add((tool_name, sig))
            else:
                log.info(
                    "perm allow_session-downgraded tool=%s sig=%r run=%s "
                    "(signature unsafe to allowlist)",
                    tool_name, sig, run.run_id,
                )
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
    if account["env"]:
        # CLAUDE_CONFIG_DIR points the spawned CLI at the user's per-user
        # home (mostly symlinks to CLAUDE_HOME, real .credentials.json) so
        # this run authenticates as their personal account.
        options_kwargs["env"] = account["env"]
    options = ClaudeAgentOptions(**options_kwargs)

    async def _send_user_message(client: ClaudeSDKClient, text: str, blocks: list[dict]) -> None:
        """Forward one user input into the live SDK client (driver path).

        Acquires the per-run write lock so concurrent mid-turn injections
        from request handlers don't interleave bytes on the CLI's stdin.
        """
        async with run.client_write_lock:
            await _send_to_client(client, text, blocks)

    def _drain_pending_for_auto_fire() -> dict:
        events = run.pending_notifications[:]
        run.pending_notifications = []
        run.notification_grace_started_at = None
        run.consecutive_auto_fires += 1
        synth = _compose_auto_fire_message(events)
        run.emit({"type": "auto_fire", "events": events})
        return {"synth": synth}

    def _drop_pending_capped() -> None:
        dropped = run.pending_notifications[:]
        run.pending_notifications = []
        run.notification_grace_started_at = None
        run.emit({
            "type": "auto_fire_capped",
            "events": dropped,
            "limit": MAX_CONSECUTIVE_AUTO_FIRES,
        })

    async def driver():
        # The CLI's stdout flows continuously: assistant text, tool calls,
        # tool results, ResultMessage at end-of-turn, AND any background
        # task events (Monitor / run_in_background Bash / etc.) in between
        # turns. We want all of those to dispatch to the UI immediately,
        # not just whatever happened to fit between user keystrokes.
        #
        # Architecture: a "pump" coroutine owns the SDK iterator and feeds
        # messages into our own bounded queue. The driver's main loop
        # races that queue against user input and a timeout. When a
        # background TaskNotification arrives during what used to be the
        # idle wait, msg_queue fires, the message dispatches, and the
        # auto-fire grace timer arms naturally.
        #
        # Why a pump task instead of racing aiter.__anext__() directly:
        # cancelling __anext__ mid-yield can drop a message. asyncio.Queue
        # doesn't have that hazard — the pump owns the iterator
        # exclusively and never cancels mid-yield.
        msg_queue: asyncio.Queue = asyncio.Queue()
        pump_done = object()  # sentinel for "iterator exhausted"

        async def _pump_messages(client) -> None:
            try:
                async for msg in client.receive_messages():
                    await msg_queue.put(msg)
            finally:
                await msg_queue.put(pump_done)

        try:
            async with ClaudeSDKClient(options=options) as client:
                pump_task = asyncio.create_task(_pump_messages(client))
                try:
                    # Send the initial query, then publish run.client so
                    # handlers can enqueue follow-up messages. Anything that
                    # was already queued during the run-creation window stays
                    # in the queue — the driver loop's user_get branch will
                    # pop them in order once we're between turns.
                    async with run.client_write_lock:
                        await _send_to_client(client, effective_message, image_blocks)
                        run.client = client

                    # `between_turns` flips to True on each ResultMessage and
                    # back to False whenever we send something to the CLI
                    # (user input or auto-fire synth). Auto-fire only arms
                    # while between_turns — we never auto-fire while an LLM
                    # turn is still in flight, and we likewise never pop a
                    # queued user message into a busy CLI (writing to the
                    # CLI's stdin mid-turn caused the message to be silently
                    # consumed-and-discarded; queueing serialises everything
                    # through one writer so that bug can't recur).
                    between_turns = False

                    while True:
                        cap_reached = run.consecutive_auto_fires >= MAX_CONSECUTIVE_AUTO_FIRES

                        # Compute the wait timeout. Three regimes:
                        #   1. Notifications buffered + between turns + not
                        #      capped → grace remaining (auto-fire when it
                        #      expires).
                        #   2. Capped notifications between turns → drop
                        #      them once and fall through to idle timeout.
                        #   3. Otherwise → idle timeout.
                        if (between_turns and run.pending_notifications
                                and run.notification_grace_started_at is not None
                                and not cap_reached):
                            elapsed = time.time() - run.notification_grace_started_at
                            timeout = max(0.0, AUTO_FIRE_GRACE_MS / 1000 - elapsed)
                        else:
                            if (between_turns and cap_reached
                                    and run.pending_notifications):
                                _drop_pending_capped()
                            timeout = SESSION_IDLE_TIMEOUT_MS / 1000

                        msg_get = asyncio.create_task(msg_queue.get())
                        # Only race on user_input_queue when the CLI is idle.
                        # If we polled it while a turn was still streaming,
                        # we'd write user input into a CLI that isn't ready
                        # to consume it, with the same disappear-into-the-
                        # void failure mode the queue is designed to avoid.
                        waitables: set[asyncio.Task] = {msg_get}
                        user_get: Optional[asyncio.Task] = None
                        if between_turns:
                            user_get = asyncio.create_task(run.user_input_queue.get())
                            waitables.add(user_get)
                        try:
                            done, _pending = await asyncio.wait(
                                waitables,
                                timeout=timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            # Queue.get() is cancellation-safe — the awaited
                            # future is just a notification, not a payload-
                            # carrying transfer. The pump still owns the SDK
                            # iterator exclusively, so we never lose a
                            # message by cancelling msg_get here.
                            for t in (msg_get, user_get):
                                if t is not None and not t.done():
                                    t.cancel()

                        if msg_get in done:
                            msg = msg_get.result()
                            if msg is pump_done:
                                # SDK iterator exhausted — CLI subprocess
                                # closed. Nothing more will arrive; exit.
                                break
                            run.last_activity = time.time()
                            for evt in _sdk_message_to_events(msg, run):
                                # emit() also keeps ACTIVE_RUNS_BY_SESSION in
                                # sync whenever an init event reveals (or
                                # changes) the SDK session id, so follow-up
                                # /api/chat lookups can find this run.
                                run.emit(evt)

                            if isinstance(msg, (TaskStartedMessage,
                                                TaskProgressMessage,
                                                TaskNotificationMessage)):
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
                                between_turns = True
                            elif isinstance(msg, (AssistantMessage, UserMessage)):
                                # Active LLM turn / tool dance. We're not
                                # between-turns again until the next Result.
                                between_turns = False
                            # Task* and Init messages are out-of-band — they
                            # don't change between_turns. A TaskNotification
                            # arriving between turns must keep between_turns
                            # = True so the grace timer can arm.
                            continue

                        if user_get is not None and user_get in done:
                            item = user_get.result()
                            run.consecutive_auto_fires = 0
                            await _send_user_message(
                                client,
                                item.get("text") or "",
                                item.get("image_blocks") or [],
                            )
                            between_turns = False
                            continue

                        # Timeout fired — three sub-cases:
                        if (between_turns and run.pending_notifications
                                and not cap_reached):
                            # Grace period elapsed with notifications buffered:
                            # auto-fire a synth user message.
                            action = _drain_pending_for_auto_fire()
                            async with run.client_write_lock:
                                await client.query(action["synth"])
                            between_turns = False
                            continue
                        if between_turns and run.pending_notifications:
                            _drop_pending_capped()
                            # Don't exit yet — give the user another idle
                            # window to send something before tearing down.
                            continue
                        # Idle timeout with nothing pending — exit cleanly.
                        break
                finally:
                    run.client = None
                    if not pump_task.done():
                        pump_task.cancel()
                        try:
                            await pump_task
                        except (asyncio.CancelledError, Exception):
                            pass

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
    files: list[UploadFile] = File(default_factory=list),
    user: dict = Depends(auth.require_user),
):
    """Inject a user message into an already-running long-lived run.

    The browser uses this when its SSE subscription is still open from a
    prior turn — avoids opening a second stream just to deliver the input.
    Goes straight to the bundled CLI's stdin so its concurrent-query queue
    can inject between tool calls (binary-style steerability), instead of
    waiting on our own end-of-turn boundary.
    Returns 202 Accepted; the existing stream emits the new events.
    """
    _safe_id(run_id)
    run = ACTIVE_RUNS.get(run_id)
    if run is None or run.done:
        raise HTTPException(404, "no such run")
    _require_owner(run, user)
    image_blocks, _meta = await _read_uploaded_images(images)
    file_metas = await _save_uploaded_files(files, run_id)
    effective = _file_attachment_prefix(file_metas) + message
    run.pending_notifications.clear()
    run.notification_grace_started_at = None
    run.consecutive_auto_fires = 0
    delivered = await _inject_user_input(run, effective, image_blocks)
    if delivered:
        run.emit({
            "type": "user_prompt",
            "text": message,
            "image_count": len(image_blocks),
            "file_count": len(file_metas),
        })
    else:
        # Surface the failure as a 502 so the frontend can fall back to
        # opening a fresh run instead of believing the message was queued.
        return JSONResponse(
            {"ok": False, "error": "delivery_failed"}, status_code=502,
        )
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


def _sdk_message_to_events(msg, run: Optional["ActiveRun"] = None) -> list[dict]:
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
        _log_usage(
            msg,
            account_slot=(run.account_slot if run is not None else "shared"),
            owner_sub=(run.owner_sub if run is not None else None),
        )
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
    """Atomic write so a concurrent finish from another turn can't leave the
    file half-written; the read in /api/usage would then JSON-fail and silently
    drop the rate-limit panel."""
    try:
        payload = json.dumps({"info": rli, "captured_at": int(time.time())})
        tmp = RATE_LIMIT_CACHE.with_suffix(RATE_LIMIT_CACHE.suffix + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, RATE_LIMIT_CACHE)
    except Exception:
        # Log so a permission/disk issue is debuggable, but don't propagate
        # — rate-limit caching is non-critical relative to serving the turn.
        log.exception("save_rate_limit failed")


def _log_usage(msg, *, account_slot: str = "shared", owner_sub: Optional[str] = None) -> None:
    """Append one row per completed turn to usage.jsonl.

    ``account_slot`` records which credential slot this turn authenticated as
    ('shared' or 'personal'), and ``owner_sub`` records which logged-in user
    spawned the run. Both are written so /api/usage can break out personal
    spend per user from the deployment-wide shared spend.
    """
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
        "account_slot": account_slot,
        "owner_sub": owner_sub,
    }
    try:
        with USAGE_LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        log.exception("log_usage failed")


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
        agg = by_session.setdefault(sid, {"turns": 0, "cost": 0.0, "input": 0, "output": 0})
        agg["turns"] += 1
        agg["cost"] += float(r.get("total_cost_usd") or 0.0)
        agg["input"] += int(r.get("input_tokens") or 0)
        agg["output"] += int(r.get("output_tokens") or 0)

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

    # Slot breakdown: shared is deployment-wide (everyone shares one bill);
    # personal is filtered to the current user so each user sees only their
    # own spend on their own credentials. Rows from before the slot-tagging
    # change (missing account_slot) are treated as 'shared'.
    user_sub = user.get("sub")
    slot_totals = {
        "shared": {"turns": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        "personal": {"turns": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
    }
    for r in today_rows:
        slot = r.get("account_slot") or "shared"
        if slot not in slot_totals:
            continue
        if slot == "personal" and r.get("owner_sub") != user_sub:
            continue
        bucket = slot_totals[slot]
        bucket["turns"] += 1
        bucket["cost_usd"] += float(r.get("total_cost_usd") or 0.0)
        bucket["input_tokens"] += int(r.get("input_tokens") or 0)
        bucket["output_tokens"] += int(r.get("output_tokens") or 0)
    for bucket in slot_totals.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 4)

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
            "by_slot": slot_totals,
        },
        "rate_limit": rate_limit,
    }


@app.get("/setup")
async def setup_page(request: Request, user: dict = Depends(auth.require_user)):
    """Setup screen for the in-container `claude` CLI sign-in.

    Always reachable (even when already configured) so users can re-auth
    without first signing out. The template adjusts copy based on
    ``configured``.
    """
    response = templates.TemplateResponse(
        request, "setup.html", {
            "user": user,
            "configured": setup_flow.is_configured(),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/setup/status")
async def api_setup_status(user: dict = Depends(auth.require_user)):
    flow = setup_flow.current_flow()
    return {
        "configured": setup_flow.is_configured(),
        "flow": flow.to_public() if flow else None,
        "whoami": setup_flow.whoami(),
    }


@app.get("/api/setup/whoami")
async def api_setup_whoami(user: dict = Depends(auth.require_user)):
    return setup_flow.whoami()


@app.post("/api/setup/oauth/start")
async def api_setup_oauth_start(
    request: Request,
    user: dict = Depends(auth.require_user),
):
    _require_setup_access(user)
    body = await request.json()
    variant = body.get("variant", "claudeai")
    if variant not in ("claudeai", "console"):
        raise HTTPException(400, "variant must be 'claudeai' or 'console'")
    state = await setup_flow.start_oauth(variant)
    return state.to_public()


@app.post("/api/setup/oauth/code")
async def api_setup_oauth_code(
    request: Request,
    user: dict = Depends(auth.require_user),
):
    _require_setup_access(user)
    body = await request.json()
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "code is required")
    if len(code) > 200_000:
        raise HTTPException(400, "code too long")
    try:
        state = await setup_flow.submit_code(code)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {
        "configured": setup_flow.is_configured(),
        "flow": state.to_public(),
    }


@app.post("/api/setup/oauth/cancel")
async def api_setup_oauth_cancel(user: dict = Depends(auth.require_user)):
    _require_setup_access(user)
    await setup_flow.cancel_flow()
    return {"ok": True}


@app.post("/api/setup/apikey")
async def api_setup_apikey(
    request: Request,
    user: dict = Depends(auth.require_user),
):
    _require_setup_access(user)
    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")
    try:
        setup_flow.save_api_key(api_key)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"configured": setup_flow.is_configured()}


@app.post("/api/setup/signout")
async def api_setup_signout(user: dict = Depends(auth.require_user)):
    """Forget all stored Claude credentials. Forward-auth (Keycloak) login
    is unaffected — this only signs the in-container Claude CLI out."""
    _require_setup_access(user)
    await setup_flow.sign_out()
    return {"configured": setup_flow.is_configured()}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
