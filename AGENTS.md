# Agent / contributor notes

Project context for Claude Code (or any agent) working on this repo. Captures the non-obvious design decisions and gotchas that aren't visible from reading a single file in isolation.

## What this is

A Python FastAPI app that gives you a web UI for Claude Code, with the agent's own memory, `CLAUDE.md`, hooks, MCP servers, and skills all attached. Designed to be self-hostable behind an OIDC provider (Keycloak, Auth0, anything authlib can talk to).

Backend is `app.py` (routes, SSE, run lifecycle, permission plumbing, per-user-account API), with `auth.py` (OIDC code+PKCE via authlib) and `setup_flow.py` (in-browser sign-in flows for the bundled CLI, keyed per-credential).

## Architecture: the non-obvious choices

### Why `claude_agent_sdk` and not raw subprocess

We use the Python `claude_agent_sdk` because we need the `can_use_tool` callback to power in-browser permission prompts. The SDK still shells out to the bundled `@anthropic-ai/claude-code` Node CLI under the hood — **Node.js is a runtime requirement**. `setting_sources=["user","project","local"]` plus `system_prompt={"type":"preset","preset":"claude_code"}` are what make memory and `CLAUDE.md` auto-load.

### Permission UI

`can_use_tool` registers a `Future` per request, emits an SSE `permission_request`, the browser POSTs `/api/permission/{id}` to resolve it.

- `SAFE_TOOLS = {"TodoWrite"}` — auto-approved server-side.
- `NO_SESSION_ALLOWLIST_TOOLS = {"Bash"}` — UI hides the "Allow this session" button because `(tool, signature)` granularity is too coarse for Bash. **Server-side enforcement too**: even if a tampered client POSTs a session-allow for Bash, the callback refuses it.

### Sessions read from disk

Session jsonl files at `$CLAUDE_HOME/projects/<sanitized-cwd>/*.jsonl` are the source of truth for transcripts. The web UI and any terminal `claude` CLI share state through these files. Sanitisation is `cwd.replace("/", "-")`.

### Multi-project picker

Set `CLAUDE_WEB_PROJECT_DIRS=/a,/b,/c` to enable the project picker. Legacy single-CWD mode (via `CLAUDE_PROJECT_DIR`) is the fallback.

### Per-user Claude accounts

Each OIDC user can register, label, sign in to, switch between, and delete their own Claude credentials at `/account`. The header dropdown lists their slots (shared + each named cred).

- **Filesystem layout**: per-user `CLAUDE_CONFIG_DIR = $PERSONAL_HOMES_DIR/<safe_sub>/<id>/` — a symlink skeleton over the shared `CLAUDE_HOME` where **only `.credentials.json` is a real per-user file**. Projects, sessions, skills, settings stay shared, and the transcript jsonl is the same file regardless of slot. Mid-conversation slot toggles cancel + respawn the CLI with `--resume <session_id>` so the conversation picks up unbroken on the new credentials.
- **Schema**:
  - `user_credential(id, user_sub, label, created_at)` with `UNIQUE(user_sub, label)`
  - `user_account.active` is a free string `'shared'` or `'cred:<id>'` (legacy `'personal'` rows migrate on startup).
- **API**: `/api/account/credentials` (GET list / POST create / PATCH rename / DELETE) plus per-cred `/oauth/start`, `/oauth/code`, `/oauth/cancel`, `/apikey`, `/signout`, `/status`. All scoped to the caller's OIDC sub — rows owned by other users return **404, not 403**.
- **Env**: `CLAUDE_WEB_PERSONAL_HOMES_DIR` (default `~/.claude-homes`), `CLAUDE_WEB_SHARED_ACCOUNT_LABEL` (default `"Shared"`).

### Personalities

Each user picks a "personality" — a system-prompt voice the spawned CLI runs under. Built-in rows ship for "No persona" (default Claude voice — empty body, no append), Hagrid, Software Architect, Dobby, Kreacher, Hermione Granger, Luna Lovegood, and Tonks; users can create, edit, clone, and delete their own at `/personalities`. The picker dropdown in the chat header is the runtime control. Picking "No persona" makes `_resolve_personality_for_run` return `append=""`, which leaves the `system_prompt` option as a bare `{"type": "preset", "preset": "claude_code"}` (no `--append-system-prompt`) and unlinks the auto-memory mirror file.

- **Schema**:
  - `personality(id PK, owner_sub, name, description, system_prompt, is_builtin, created_at, updated_at)` with `UNIQUE(owner_sub, name)`. `owner_sub IS NULL` means a built-in row visible to every user; otherwise it's owned by that OIDC sub and only that user sees it.
  - `user_personality(user_sub PK, personality_id, updated_at)` is the per-user active pick.
  - **SQLite UNIQUE gotcha**: NULL is distinct from every other NULL, so `ON CONFLICT(owner_sub, name)` doesn't detect existing built-in rows (they all have `owner_sub IS NULL`). The seeder uses an explicit `SELECT-then-UPDATE-or-INSERT` instead, keeping row ids stable across restarts so `user_personality.personality_id` pointers don't dangle when content is refreshed.

- **Where the persona content actually lives at runtime** (path-3 architecture):
  - **Mirror file** at `$CLAUDE_HOME/projects/<sanitized-cwd>/memory/active_personality.md`. claude-web writes the active personality's body here (with YAML frontmatter) on every picker change, content edit affecting the caller's active, deletion of the active row, and at startup. The `claude_code` preset loads it as a first-class feedback memory entry — same loading weight any other auto-memory file carries.
  - **MEMORY.md auto-edit**: at startup, `_ensure_memory_index_references_mirror()` strips any line referencing `feedback_persona.md` from `<cwd>/memory/MEMORY.md` and appends a line for `active_personality.md`. The original `feedback_persona.md` file stays on disk untouched; it's just not in the index anymore. **claude-web silently modifies a user-owned file here** — surprising, but it's the cleanest way to make the picker the canonical persona source without touching auto-memory loader internals.
  - **SDK `--append-system-prompt`** still carries the same persona body, defensively. Two signals reinforcing each other against drift.
  - **History-reset directive** (`PERSONA_HISTORY_RESET_DIRECTIVE` in `app.py`) is prepended to the persona body in both the mirror file and the append. It tells the model to disregard voice established by earlier turns of the resumed conversation and to skip Claude's default conversational fillers ("Great question", "I'd be happy to..."). Path 3 made MEMORY.md persona competition go away; this directive closes the remaining conversation-history bias on mid-conversation switches.

- **Built-in bodies are constants**: `_BUILTIN_HAGRID_PROMPT`, `_BUILTIN_ARCHITECT_PROMPT`, `_BUILTIN_DOBBY_PROMPT`, `_BUILTIN_KREACHER_PROMPT`, `_BUILTIN_HERMIONE_PROMPT`, `_BUILTIN_LUNA_PROMPT`, and `_BUILTIN_TONKS_PROMPT` in `app.py` are the source of truth. Every startup overwrites the matching `(owner_sub IS NULL, name)` row's body from the constant, so a repo edit to a built-in lands on next restart with no hand-migration. User-owned rows (`owner_sub IS NOT NULL`) are untouched — clone-then-edit if you want changes to survive. The "No persona" row's "body" is the empty string.

- **Multi-user caveat**: the mirror file is a single shared path under `CLAUDE_HOME`, so two concurrent users on different active personalities race for last-write-wins on the mirror. The per-spawn `--append-system-prompt` stays accurate per-user even in that race — the auto-memory copy is the only thing that drifts. A per-user `CLAUDE_HOME` would close this gap but introduces session-isolation complexity (new sessions written under a per-user dir don't appear in shared session listings); the symlink-farm idea got shelved for that reason.

- **API**: `/api/personalities` (GET list / POST create), `/api/personalities/{id}` (PATCH / DELETE), `/api/personalities/active` (POST with `personality_id` form field). All scoped to the caller's OIDC sub. Built-ins are read-only (clone-then-edit is the pattern).

### Personality respawn vs `/api/chat/send`

The CLI subprocess bakes its `--append-system-prompt` in at spawn — so a picker change after the spawn doesn't reach the live model unless we tear the run down. Two paths feed into a long-lived conversation, and they don't both check the active personality:

| Endpoint | When | Personality check? |
| --- | --- | --- |
| `POST /api/chat` | Turn-start (new run, or sending a message when no run is live) | Yes — `existing.personality_id != active_personality_id` triggers cancel + respawn with `--resume <session>` |
| `POST /api/chat/send/{run_id}` | Mid-turn input injection (UI sends a follow-up while a turn is generating, or just queues into the live run) | **No** — feeds straight into the existing CLI's stdin |

That asymmetry is closed by `_cancel_runs_for_personality_swap` in the `/api/personalities/active` handler: when the picker changes, claude-web walks `ACTIVE_RUNS_BY_SESSION` for the calling user, finds runs whose `personality_id` differs from the new pick, and cancels them. The driver's cleanup pops the run from the session map, so the next message — whichever endpoint it hits — finds no live run and falls through to the `/api/chat` fresh-spawn path under the new persona. Logged as `personality-swap cancel run=… session=…` (note: the `claude-web` logger has no handler attached by default, so these lines don't show up in `journalctl` without explicit `logging.basicConfig`).

### Identity passed to the spawned CLI

Every CLI subprocess started by claude-web receives three identity env vars from `_identity_env_for(user)` (called by `_resolve_account_for_run`), so `SessionStart` hooks and `CLAUDE.md` personalities can address the signed-in user by name without claude-web touching the model context itself:

| Variable | OIDC claim | `AUTH_MODE=none` value |
| --- | --- | --- |
| `CLAUDE_WEB_USER_SUB` | `sub` | `""` |
| `CLAUDE_WEB_USER_EMAIL` | `email` | `""` |
| `CLAUDE_WEB_USER_NAME` | `name` or `preferred_username` | `""` |

Schema is stable — keys are always set; only values go empty in the anonymous path. Hooks can rely on `os.environ["CLAUDE_WEB_USER_EMAIL"]` existing. The SDK's transport merges `ClaudeAgentOptions.env` over inherited process env (see `claude_agent_sdk/_internal/transport/subprocess_cli.py`), so adding these keys to the shared-slot env dict (which used to be empty) doesn't disturb `PATH`/`HOME`/etc.

Tests in `tests/test_app_helpers.py` (`test_identity_env_for_*`, `test_resolve_account_for_run_shared_carries_identity`) lock down the schema and the shared-slot guarantee that `CLAUDE_CONFIG_DIR` is **not** set (so the spawned CLI uses the shared `CLAUDE_HOME`, not a personal one).

### Setup gate

`CLAUDE_WEB_ENABLE_SETUP=true|false|auto` (default `auto`). The `/setup` page (subprocess-driven `claude auth login` flow, or pasted API key) auto-locks once shared credentials exist. To re-auth shared: flip the env var to `true` and restart, or shell in and run `claude auth login`. **Per-user creds bypass this gate** — every user can always manage their own credentials at `/account`.

### `setup_flow.py` flows dict

`_flows: dict[str, OAuthFlowState]` is keyed by `'shared'` or `'cred:<sub>:<id>'`, so an admin re-authing the shared CLI and a user setting up their personal slot can't trample each other. The CLI subprocess is spawned with `CLAUDE_CONFIG_DIR=home` and **`ANTHROPIC_API_KEY` stripped from the child env** so a shared-slot API key doesn't short-circuit a per-credential OAuth login.

### Usage log split

`usage.jsonl` rows carry `account_slot` (`'shared'` or `'personal'`) and `owner_sub`. `/api/usage` reports shared spend aggregated across all users (one bill) and personal spend filtered to just the requesting user. Pre-tagging rows are treated as shared.

### Multi-user / ownership filter

`CLAUDE_WEB_PER_USER_SESSIONS=true` scopes session listing/loading/deleting/exporting to whoever first chatted in them. **This is an ownership filter, not a security boundary** — that's called out explicitly in the README.

- `AUTH_MODE=none` + `PER_USER_SESSIONS=true` is refused at startup (footgun).
- `CLAUDE_WEB_ADMIN_EMAILS` gates shared-slot `/setup` mutations in this mode.

### Single-worker enforcement

Module-global state (`ACTIVE_RUNS`, `PENDING`, `_SESSION_LOCKS`) isn't shared across uvicorn workers. **Startup refuses to boot if `WEB_CONCURRENCY>1`** unless `CLAUDE_WEB_ALLOW_MULTI_WORKER=true`.

### State persistence

SQLite at `$CLAUDE_WEB_STATE_DIR/state.db` (WAL mode) holds run metadata + events + permission requests + `user_account` + `user_credential` tables. A service restart doesn't lose an in-flight conversation, only mid-tool-call state. Session jsonl files on disk remain the source of truth for transcripts.

### CSRF + security headers

- `CSRFMiddleware` rejects mutating requests without a matching `Origin`/`Referer` (computed from `OIDC_REDIRECT_URI`). `CLAUDE_WEB_CSRF_STRICT=false` only for CLI testing.
- `SecurityHeadersMiddleware` sends CSP `default-src 'self'; script-src 'self'; … ; frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`. Don't strip these at the proxy.
- When deployed behind a reverse proxy doing TLS termination, **uvicorn must be launched with `--proxy-headers --forwarded-allow-ips=*`** so `X-Forwarded-Proto: https` is honored. Without it, `request.base_url` resolves to `http://` and the CSRF middleware rejects every POST.

### GC / retention env vars

| var | default | purpose |
| --- | --- | --- |
| `CLAUDE_WEB_PERSIST_RETENTION` | 86400 | how long completed run state lives in `state.db` |
| `CLAUDE_WEB_UPLOAD_RETENTION` | 604800 | uploaded-file retention under `$STATE_DIR/uploads/<run_id>/` |
| `CLAUDE_WEB_PERMISSION_TIMEOUT` | 900 | seconds before an unanswered permission prompt auto-denies |
| `CLAUDE_WEB_MAX_AUTO_FIRES` | 3 | cap on consecutive ScheduleWakeup auto-fires before requiring user input |
| `CLAUDE_WEB_MAX_SUBSCRIBER_QUEUE` | 1000 | SSE per-subscriber queue size before backpressure kicks in |

### State / cost files (under `$CLAUDE_WEB_STATE_DIR`, default `~/.claude-web/`)

- `usage.jsonl` — cost log with `account_slot` + `owner_sub` per row
- `state.db` (+ `-wal`, `-shm`) — SQLite, WAL mode
- `uploads/<run_id>/` — file uploads
- `rate_limit.json`
- `anthropic_api_key` (mode 0600 for shared-slot API-key sign-in)

Per-user creds: `$CLAUDE_WEB_PERSONAL_HOMES_DIR/<safe_sub>/<id>/`.

### Roundtable

`/roundtable` is a multi-AI panel where Gemini Pro + GPT-5 answer in parallel and Claude Opus synthesises a single consolidated reply. Code-related questions can return unified diffs in the synth output that click-to-apply against the bound project. Two surfaces:

- **Assistant view** (`mode-assistant`, default): one input box + file picker + "Ask the panel" button. Each user turn fires `roundtable_ask_parallel` against two paid participants (default `gemini-pro` + `gpt-5`) then `roundtable_ask` against the synthesiser (`claude-opus`, free via subscription CLI when `CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT=cli`). Conversation persists in a roundtable thread.
- **Advanced view** (`mode-advanced`): underlying thread browser + manual-driving panels (post / ask / ask_parallel / attach / close). Same body, different `mode-*` class.

Optional editable dependency on the `roundtable` package — `pip install -e /home/matt/roundtable-mcp` (or wherever the operator put it). If the import fails the route renders a "not installed" panel. The wrapper at `roundtable/` in this repo is a thin shim re-exporting from the installed package.

- **Shared store**: `~/.claude-roundtable/state.db` (SQLite, WAL). Same store the standalone `roundtable-mcp` stdio server uses — threads created via Claude Code MCP tools are visible in the web UI and vice versa. No sync layer.

- **Project binding**: `roundtable_thread_project(thread_id PK, project_key, created_by, created_at)` maps each thread to a `project_key` from `CLAUDE_WEB_PROJECT_DIRS`. Threads created outside claude-web (MCP) have no row and surface under an "Unbound" filter; the assistant view inherits the chat-side project picker for new threads.

- **Streaming SSE** on `POST /api/roundtable/assistant`. Event order per turn: `created` → `attached` (per file) → `prompt_posted` → `panel_start` → `panel_done` (with per-panelist char counts) → `synth_start` → `done` (full payload incl. patches). The browser uses `fetch().body.getReader()` + a small SSE parser; aria-live announcements track each step for NVDA. Per-token streaming of the synth is a future improvement — would need streaming added to `roundtable.core`.

- **Markdown rendering on synth turns**: `marked.min.js` + `purify.min.js` are loaded on the roundtable page. AI output goes through `marked.parse()` → `DOMPurify.sanitize()` before insertion. Falls back to plain `<pre>` if either lib is missing.

- **Click-to-apply diff**: synth system prompt asks for unified diffs in ` ```diff <filename>` fences when fixes are warranted (only when a code artifact is attached AND the panel reached agreement). Server-side regex `_DIFF_FENCE_RE` parses fences out; `_extract_patches()` returns `[{target, diff}]`. Each detected patch gets an "Apply" button in the UI; click POSTs to `/api/roundtable/assistant/apply` with `{thread_id, target, diff}`.
  - **Safety rails**: target must resolve inside the thread's bound project (or, for unbound threads, inside any configured project root); path traversal returns HTTP 400. `patch --dry-run` validates the hunks before any write; failure returns 422 with stderr tail. Original is saved alongside as `<target>.rt-orig` before apply. Tries `-p0` first, falls back to `-p1` for `a/foo b/foo`-style headers. Requires GNU `patch` on `PATH`.

- **Routes**: `/roundtable` (HTML); `GET /api/roundtable/threads` (list, filterable by `open_only` / `limit` / `project`, including `project=__unbound__`); `GET /api/roundtable/threads/{id}` (structured detail with `project_key`); `POST /api/roundtable/threads` (create, optional `project_key`); `POST /api/roundtable/threads/{id}/{post,ask,ask_parallel,artifact,close}` (manual driving); `POST /api/roundtable/assistant` (streaming); `POST /api/roundtable/assistant/apply`; `GET /api/roundtable/participants`. All gated by `auth.require_user`.

- **Env config**: `GEMINI_API_KEY`, `OPENAI_API_KEY` for the paid panel; `CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT=cli` to bill Claude participants through the subscription CLI (no `ANTHROPIC_API_KEY` needed); optional `CLAUDE_WEB_ROUNDTABLE_ASSISTANT_MAX_BYTES` (default 2 MiB per upload).

- **Cost picture**: per "Ask the panel" turn = 1 Gemini Pro call + 1 GPT-5 call (paid) + 1 Claude Opus call (subscription, free in CLI transport mode). Typical 20-40s round-trip.

### Portability

All paths are env-driven: `CLAUDE_PROJECT_DIR`, `CLAUDE_WEB_PROJECT_DIRS`, `CLAUDE_HOME`, `CLAUDE_WEB_STATE_DIR`, `CLAUDE_WEB_PERSONAL_HOMES_DIR`, `CLAUDE_WEB_SHARED_ACCOUNT_LABEL`, `CLAUDE_WEB_SITE_TITLE`, `AUTH_MODE=oidc|none`. **Don't hardcode an operator's home directory back into the source.**

## Known bugs (open)

### ScheduleWakeup pre-empts queued user message (2026-05-18)

If the model has a `ScheduleWakeup` pending and the user queues a message while the turn is ending, the harness dispatches the fired wakeup's sentinel (`<<autonomous-loop-dynamic>>` or `<<autonomous-loop>>`) into the next prompt slot **instead of** the user's queued message. The user sees a "1 QUEUED" indicator that never drains and has to manually cancel.

This is a claude-web bug, not upstream Claude Code — the next-prompt picker after a turn ends needs to prefer a queued user message over a fired scheduled wakeup. Fix is most likely in the run-lifecycle / agent-loop code in `app.py` that decides what to feed the SDK next.

### Per-conversation personality binding (design issue, not a crash)

The personality picker is **per-user** (`user_personality(user_sub PK, personality_id)`), not per-conversation. Switching it in chat A immediately changes the active pick for every other chat that user owns; closing chat A doesn't reset anything because chat A's pick was never bound to its session.

What you'd actually want: per-conversation binding so that new chats pick up your default, switching the picker in chat A doesn't bleed into chat B, and reopening a closed chat restores its last personality. Fix sketch: new table `session_personality(session_id PK, personality_id, updated_at)`, picker reads/writes that on the current session, the user-level `user_personality` row stays as the seed for new-chat defaults, page-load JS reads from `session_personality` to populate the picker per-chat. ~30 lines, unsplit between `app.py` and `static/app.js`.

### Application-level logs don't reach `journalctl`

The `claude-web` logger (`logging.getLogger("claude-web")`) has no handler attached — uvicorn captures stdout for access logs but the application logger drops its records on the floor. So lines like `personality-swap cancel run=…` and `perm decision=…` exist in the code but never reach the journal, making in-prod debugging surprising. Fix is a one-liner `logging.basicConfig(level=logging.INFO, format=…)` near the top of `app.py` (or a proper `dictConfig` in `setup_flow.py`). Until then, debugging in-prod state often requires inspecting `ps`, the SQLite store, or the file system rather than the log stream.

## Local dev / CI

- Python deps in `requirements.txt`, dev-only in `requirements-dev.txt`.
- Lint/test locally (same checks CI runs across Python 3.11/3.12/3.13):
  - `.venv/bin/pytest -q`
  - `.venv/bin/ruff check . --select=F,E,W,B --ignore=E501,B008`
  - `node --check static/app.js`
- Tests live in `tests/` (pytest fixtures pre-set env vars at import time; smoke + CSRF + auth + setup_flow + helpers). CI workflow at `.github/workflows/ci.yml`.
- No `--reload` in production — restart the service after edits.

## Source-file map

| file | what's in it |
| --- | --- |
| `app.py` | routes, SSE, permission Future plumbing, `state.db`, run lifecycle, per-user-account API, personality CRUD + mirror writer, roundtable assistant + apply, picker-driven run cancellation, MEMORY.md auto-edit at startup |
| `auth.py` | OIDC code+PKCE via authlib |
| `setup_flow.py` | in-browser sign-in flows for the bundled CLI, keyed per-credential |
| `roundtable/` | thin shim re-exporting the installed `roundtable` package (`pip install -e ../roundtable-mcp`); absent imports degrade `/roundtable` to a "not installed" panel |
| `templates/{index,account,setup,personalities,roundtable}.html` | UI |
| `static/{app,account,setup,personalities,roundtable}.js` | client JS |
| `static/{style,setup,roundtable}.css` | styles |
| `static/{marked,purify}.min.js` | markdown rendering + sanitisation for roundtable synth output |
| `tests/` | pytest suite |
