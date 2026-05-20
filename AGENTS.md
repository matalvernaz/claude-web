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

### Portability

All paths are env-driven: `CLAUDE_PROJECT_DIR`, `CLAUDE_WEB_PROJECT_DIRS`, `CLAUDE_HOME`, `CLAUDE_WEB_STATE_DIR`, `CLAUDE_WEB_PERSONAL_HOMES_DIR`, `CLAUDE_WEB_SHARED_ACCOUNT_LABEL`, `CLAUDE_WEB_SITE_TITLE`, `AUTH_MODE=oidc|none`. **Don't hardcode an operator's home directory back into the source.**

## Known bugs (open)

### ScheduleWakeup pre-empts queued user message (2026-05-18)

If the model has a `ScheduleWakeup` pending and the user queues a message while the turn is ending, the harness dispatches the fired wakeup's sentinel (`<<autonomous-loop-dynamic>>` or `<<autonomous-loop>>`) into the next prompt slot **instead of** the user's queued message. The user sees a "1 QUEUED" indicator that never drains and has to manually cancel.

This is a claude-web bug, not upstream Claude Code — the next-prompt picker after a turn ends needs to prefer a queued user message over a fired scheduled wakeup. Fix is most likely in the run-lifecycle / agent-loop code in `app.py` that decides what to feed the SDK next.

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
| `app.py` | routes, SSE, permission Future plumbing, `state.db`, run lifecycle, per-user-account API |
| `auth.py` | OIDC code+PKCE via authlib |
| `setup_flow.py` | in-browser sign-in flows for the bundled CLI, keyed per-credential |
| `templates/{index,account,setup}.html` | UI |
| `static/{app,account,setup}.js` | client JS |
| `tests/` | pytest suite |
