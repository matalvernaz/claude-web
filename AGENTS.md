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

- **Filesystem layout**: per-user `CLAUDE_CONFIG_DIR = $PERSONAL_HOMES_DIR/<safe_sub>/<id>/` — a symlink skeleton over the shared `CLAUDE_HOME` where **only `.credentials.json` is a real per-user file**. Projects, sessions, skills, settings stay shared, and the transcript jsonl is the same file regardless of slot. Mid-conversation slot toggles cancel + respawn the CLI with `--resume <session_id>` so the conversation picks up unbroken on the new credentials. `<safe_sub>` is `urlsafe_b64(sha256(sub))` — collision-free across distinct OIDC subjects. The previous "strip + truncate to 64 chars" naming could silently collide; a startup migration (`_startup_migrate_personal_homes`) renames legacy directories to the new hash names on first boot.
- **Schema**:
  - `user_credential(id, user_sub, label, created_at)` with `UNIQUE(user_sub, label)`
  - `user_account.active` is a free string `'shared'` or `'cred:<id>'` (legacy `'personal'` rows migrate on startup).
- **API**: `/api/account/credentials` (GET list / POST create / PATCH rename / DELETE) plus per-cred `/oauth/start`, `/oauth/code`, `/oauth/cancel`, `/apikey`, `/signout`, `/status`. All scoped to the caller's OIDC sub — rows owned by other users return **404, not 403**.
- **Env**: `CLAUDE_WEB_PERSONAL_HOMES_DIR` (default `~/.claude-homes`), `CLAUDE_WEB_SHARED_ACCOUNT_LABEL` (default `"Shared"`).

### Personalities

Each user picks a "personality" — a system-prompt voice the spawned CLI runs under. Built-in rows ship for "No persona" (default Claude voice — empty body, no append), Hagrid, Software Architect, Dobby, Kreacher, Hermione Granger, Luna Lovegood, and Tonks; users can create, edit, clone, and delete their own at `/personalities`. The picker dropdown in the chat header is the runtime control. Picking "No persona" makes `_resolve_personality_for_run` return `append=""`, which leaves the `system_prompt` option as a bare `{"type": "preset", "preset": "claude_code"}` (no `--append-system-prompt`).

Personality is bound **per session**, not per user. The original design keyed on `user_personality(user_sub PK, personality_id)` and used a single global mirror file under `CLAUDE_HOME`, which made "two chats with two voices simultaneously" structurally impossible — switching in tab B mutated the user-global row, rewrote the shared mirror, and cancelled tab A's CLI. The current design stores `session_personality(session_id PK, user_sub, personality_id, …)` rows on first send and on each picker change; `user_personality` is consulted **only as the default for fresh chats with no binding yet**.

- **Schema**:
  - `personality(id PK, owner_sub, name, description, system_prompt, is_builtin, created_at, updated_at)` with `UNIQUE(owner_sub, name)`. `owner_sub IS NULL` means a built-in row visible to every user; otherwise it's owned by that OIDC sub and only that user sees it.
  - `user_personality(user_sub PK, personality_id, updated_at)` is the per-user default for new chats.
  - `session_personality(session_id PK, user_sub, personality_id, created_at, updated_at)` holds the binding for an in-flight conversation. Written on SDK init (via `ActiveRun.emit`'s session_id assignment hook) and on every picker change that lands at `/api/chat`.
  - **SQLite UNIQUE gotcha**: NULL is distinct from every other NULL, so `ON CONFLICT(owner_sub, name)` doesn't detect existing built-in rows (they all have `owner_sub IS NULL`). The seeder uses an explicit `SELECT-then-UPDATE-or-INSERT` instead, keeping row ids stable across restarts so `user_personality.personality_id` and `session_personality.personality_id` pointers don't dangle when content is refreshed.

- **How persona reaches the model**:
  - **SDK `--append-system-prompt`** carries the persona body. This is the single authoritative signal. The earlier "mirror file" mechanism (an `active_personality.md` written into `$CLAUDE_HOME/projects/<cwd>/memory/` with a matching MEMORY.md index entry) was removed when the per-session binding landed — the mirror was a global shared file fighting the per-session design and was no longer needed once the original competing `feedback_persona.md` was gone from the index.
  - **History-reset directive** (`PERSONA_HISTORY_RESET_DIRECTIVE` in `app.py`) is prepended to the persona body in the append. It tells the model to disregard voice established by earlier turns of the resumed conversation and to skip Claude's default conversational fillers ("Great question", "I'd be happy to..."). Best-effort against a long resumed JSONL; the UI defaults to forking a fresh chat on personality switch to avoid relying on it entirely.

- **Built-in bodies are constants**: `_BUILTIN_HAGRID_PROMPT`, `_BUILTIN_ARCHITECT_PROMPT`, `_BUILTIN_DOBBY_PROMPT`, `_BUILTIN_KREACHER_PROMPT`, `_BUILTIN_HERMIONE_PROMPT`, `_BUILTIN_LUNA_PROMPT`, and `_BUILTIN_TONKS_PROMPT` in `app.py` are the source of truth. Every startup overwrites the matching `(owner_sub IS NULL, name)` row's body from the constant, so a repo edit to a built-in lands on next restart with no hand-migration. User-owned rows (`owner_sub IS NOT NULL`) are untouched — clone-then-edit if you want changes to survive. The "No persona" row's "body" is the empty string.

- **API**: `/api/personalities` (GET list / POST create), `/api/personalities/{id}` (PATCH / DELETE), `/api/personalities/active` (POST with `personality_id` form field — sets the user-global default for *new chats only*, does not affect existing session bindings). All scoped to the caller's OIDC sub. Built-ins are read-only (clone-then-edit is the pattern).

### Personality switching and the input gate

The CLI subprocess bakes its `--append-system-prompt` in at spawn — a picker change after the spawn doesn't reach the live model unless the run is torn down. Two paths feed input into a long-lived conversation, and the picker change can arrive on either side of the next message:

| Endpoint | When | What happens on a personality mismatch |
| --- | --- | --- |
| `POST /api/chat` | Turn-start (new run, or sending a message when no run is live) | `_resolve_personality_for_run(user, session_id, override_personality_id)` reads the session's bound personality (or the client's explicit `personality_id` form field). If the existing run was spawned under a different pid, `_supersede_run(existing, "personality_changed")` flips the run's input gate off, cancels the driver, and falls through to the fresh-spawn path with `--resume <session_id>`. |
| `POST /api/chat/send/{run_id}` | Mid-turn input injection (UI sends a follow-up while a turn is generating, or just queues into the live run while the SSE is open) | Re-resolves the same way; on mismatch returns HTTP 409 with `{"error": "personality_changed"}`. The browser's `sendInExistingRun` catches the 409, aborts the SSE, clears `currentRunId`, and falls back to `/api/chat`. Same flow for `account_changed`. |

The reason both paths exist: `_supersede_run` is the synchronous half. `task.cancel()` is asynchronous — until the driver hits an await point and unwinds, `run.done` stays False and `ACTIVE_RUNS_BY_SESSION` still routes to the dying CLI. Flipping `run.accepting_input = False` and `run.superseded_reason = "personality_changed"` *before* calling `task.cancel()` gives `_inject_user_input` and the 409 path a deterministic signal to refuse new input the instant the supersede returns. Without this, a follow-up message pipelined into the still-open SSE between picker POST and cancel-task wakeup would land in the old persona's stdin.

**Browser side**, the chat-page picker drives session binding via the `personality_id` form field on every `/api/chat` and `/api/chat/send/{run_id}` POST. Switching the picker mid-conversation defaults to forking a fresh chat (the current transcript stays in the sidebar; the next message creates a new session in the new voice). The "Apply to current chat" checkbox next to the picker opts into the legacy best-effort behaviour — keeps the session_id and lets the server respawn the CLI under the new persona on the next message, accepting that earlier turns of the JSONL may leak the old voice through `--resume`. Checkbox state persists in `localStorage` under key `personality-apply-current`.

Logged as `personality-toggle respawn …` (on `/api/chat`) and `account-toggle respawn …` — note that the `claude-web` logger has no handler attached by default, so these lines don't show up in `journalctl` without explicit `logging.basicConfig`.

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
| `CLAUDE_WEB_MAX_MESSAGE_BYTES` | 1048576 | max bytes for a single `/api/chat` or `/api/chat/send` text body (HTTP 413 above this) |
| `CLAUDE_WEB_LOG_LEVEL` | `INFO` | log level for the `claude-web` application logger |
| `CLAUDE_WEB_ROUNDTABLE_RATE_CAPACITY` | 60 | per-user roundtable bucket capacity (1 token = 1 panellist or synth call) |
| `CLAUDE_WEB_ROUNDTABLE_RATE_REFILL_PER_SEC` | 1.0 | roundtable token refill rate per user |
| `CLAUDE_ROUNDTABLE_REVIEW_MAX_FINDINGS` | 8 | maximum severity-sorted findings sent through grounded review verification |
| `OIDC_ALLOWLIST_MODE` | `all` | `all` (default; user must be in every configured email/group list) or `any` (one match is enough) |

### State / cost files (under `$CLAUDE_WEB_STATE_DIR`, default `~/.claude-web/`)

- `usage.jsonl` — cost log with `account_slot` + `owner_sub` per row
- `state.db` (+ `-wal`, `-shm`) — SQLite, WAL mode
- `uploads/<run_id>/` — file uploads
- `rate_limit.json`
- `anthropic_api_key` (mode 0600 for shared-slot API-key sign-in)

Per-user creds: `$CLAUDE_WEB_PERSONAL_HOMES_DIR/<safe_sub>/<id>/`.

### Roundtable

Roundtable is a multi-AI coding panel where Gemini Pro + GPT-5 answer independently and Claude Opus synthesises a single consolidated reply. It has three surfaces over the same `roundtable/core.py` library and SQLite store:

- **MCP**: `roundtable/mcp_server.py` exposes 25 operations. `roundtable_coding_task` is the primary coding entry point for the chat AI; it composes create → repo bind → task-specific panel → optional review verification → synthesis in one call. The lower-level tools remain available for manually driven debates. MCP tool availability does not force model use — the user should explicitly say "use the roundtable" when consultation is mandatory.
- **Assistant view** (`mode-assistant`, default): task picker (`general`, `debug`, `review`, `plan`, `implement`, `test`, `explain`) + input + file picker. Every task assigns independent panel lenses and a task-specific synthesis contract. A selected project is persistently bound into roundtable core as read-only context in addition to claude-web's ownership mapping.
- **Review workflow**: auto-captures `git diff` against the selected base, asks the panel for schema-valid findings, severity-sorts/de-duplicates/caps them, runs `roundtable_converge` against real cited `file:line` source, posts the confirmed/refuted/unresolved ledger, then synthesises. A clean/non-git tree falls back to permission-gated repo inspection and labels findings unverified instead of using a stale diff artifact.
- **Advanced view** (`mode-advanced`): underlying thread browser + manual-driving panels (post / ask / ask_parallel / attach / close). Same body, different `mode-*` class.

The canonical roundtable package is vendored under `roundtable/` in this repository. The app still guards its import and renders a disabled panel if its provider dependencies are unavailable, but there is no second `roundtable-mcp` checkout to keep in sync.

- **Shared store**: `~/.claude-roundtable/state.db` (SQLite, WAL). Same store the standalone `roundtable-mcp` stdio server uses — threads created via Claude Code MCP tools are visible in the web UI and vice versa. No sync layer.

- **Project binding**: `roundtable_thread_project(thread_id PK, project_key, created_by, created_at)` maps each thread to a `project_key` from `CLAUDE_WEB_PROJECT_DIRS`. Threads created outside claude-web (MCP) have no row and surface under an "Unbound" filter; the assistant view inherits the chat-side project picker for new threads.

- **Ownership / authorization**: `_require_roundtable_thread_access(thread_id, user, *, for_apply=False)` gates every per-thread route (read, post, ask, ask_parallel, attach, close, apply). Policy: a bound thread with a non-NULL `created_by` is private to that OIDC sub and any operator in `CLAUDE_WEB_ADMIN_EMAILS`; bound threads with NULL `created_by` (legacy rows) and unbound threads (MCP / CLI created) stay readable+postable by any authenticated user to preserve interop. **Apply is stricter**: it refuses unbound threads outright and demands a non-NULL `created_by` matching the caller — without that gate, any signed-in user could rewrite files in another user's bound project and ride the next build/test for RCE. Cross-user access returns 404 (not 403) so thread ids aren't enumerable.

- **Rate limiting**: per-OIDC-sub token bucket via `_roundtable_rate_limit_check(user, weight=…)`. `ask` consumes 1, `ask_parallel` consumes one per participant, the assistant route consumes `len(panel) + 1`, and review verification consumes one additional token per checked finding. Bucket sizing is `CLAUDE_WEB_ROUNDTABLE_RATE_CAPACITY` / `CLAUDE_WEB_ROUNDTABLE_RATE_REFILL_PER_SEC`. If the second-stage review charge is unavailable, synthesis continues with verification explicitly marked skipped.

- **Streaming SSE** on `POST /api/roundtable/assistant`. Event order per turn: `created` → `grounded` → `attached` (per file) → `prompt_posted` → `panel_start` → `panel_done` → optional `verify_start` / `verify_done` → `synth_start` → optional `synth_delta` → `done`. The detached producer survives tab close; reload rejoins by stream id and replays buffered events. Aria-live announcements track milestones without speaking token deltas.

- **Markdown rendering on synth turns**: `marked.min.js` + `purify.min.js` are loaded on the roundtable page. AI output goes through `marked.parse()` → `DOMPurify.sanitize()` before insertion. Falls back to plain `<pre>` if either lib is missing.

- **Click-to-apply diff**: synth system prompt asks for unified diffs in ` ```diff <filename>` fences when fixes are warranted (only when a code artifact is attached AND the panel reached agreement). Server-side regex `_DIFF_FENCE_RE` parses fences out; `_extract_patches()` returns `[{target, diff}]`. Each detected patch gets an "Apply" button in the UI; click POSTs to `/api/roundtable/assistant/apply` with `{thread_id, target, diff}`.
  - **Safety rails**: target must resolve inside the thread's bound project (unbound threads are refused outright by the ownership gate, see "Ownership / authorization" above). Path traversal returns HTTP 400. `patch --dry-run` validates the hunks before any write; failure returns 422 with stderr tail. Original is saved alongside as `<target>.rt-orig` before apply. Tries `-p0` first, falls back to `-p1` for `a/foo b/foo`-style headers. Requires GNU `patch` on `PATH`.

- **Routes**: `/roundtable` (HTML); `GET /api/roundtable/threads` (list, filterable by `open_only` / `limit` / `project`, including `project=__unbound__`); `GET /api/roundtable/threads/{id}` (structured detail with `project_key`); `POST /api/roundtable/threads` (create, optional `project_key`); `POST /api/roundtable/threads/{id}/{post,ask,ask_parallel,artifact,close}` (manual driving); `POST /api/roundtable/assistant` (streaming); `POST /api/roundtable/assistant/apply`; `GET /api/roundtable/participants`. All gated by `auth.require_user`.

- **Env config**: `GEMINI_API_KEY`, `OPENAI_API_KEY` for the paid panel; `CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT=cli` to bill Claude participants through the subscription CLI (no `ANTHROPIC_API_KEY` needed); optional `CLAUDE_WEB_ROUNDTABLE_ASSISTANT_MAX_BYTES` (default 2 MiB per upload).

- **Cost picture**: normal task = 1 Gemini Pro call + 1 GPT-5 call (paid) + 1 Claude Opus synthesis (subscription in CLI transport mode). Verified review adds up to `CLAUDE_ROUNDTABLE_REVIEW_MAX_FINDINGS` verifier calls; the default verifier is the selected Claude synthesiser with `transport=auto`.

### Codex provider (OpenAI)

The header has an AI-provider picker next to the model picker. It stays hidden until `/api/providers` reports a second available provider; "Codex (OpenAI)" appears when the `codex` CLI is on PATH (or `CLAUDE_WEB_CODEX_BIN`) **and** either `~/.codex/auth.json` exists (`codex login`) or `OPENAI_API_KEY` is set in the service env. Providers are fixed per conversation — switching the picker with a chat open starts a new chat; models switch mid-chat freely (Codex takes the model per turn).

- **Transport**: `codex app-server` over JSON-RPC 2.0 stdio (the same server the Codex VS Code extension drives). The keyed pool keeps one account-control process per OpenAI slot for login/models/usage and one process per active chat for execution. Run-specific processes are intentional: app-server keeps an unsubscribed thread loaded for 30 minutes, so a process boundary is the only safe way to force A→B→A to reload B's appended turns. If a process dies, the live run gets an error event and the next message starts a fresh process and `thread/resume`s.
- **Why app-server and not `codex exec` / the `openai-codex-sdk` pip package**: interactive approvals. The exec path bakes an approval policy in at spawn; the app-server sends `item/commandExecution/requestApproval` / `item/fileChange/requestApproval` as JSON-RPC server→client requests mid-turn. These bridge into the same permission-Future scaffolding the Claude gate uses — `_await_permission_decision` emits the `permission_request` card and returns the raw browser choice — but codex answers in **its own** vocabulary via `_codex_gate_decision`: allow→`accept`, allow-session→`acceptForSession`, deny/timeout→`decline`. Unlike the Claude path (where "Bash" is in `NO_SESSION_ALLOWLIST_TOOLS` and the session button is hidden because the first-word signature is too coarse), codex DOES offer "Allow this session": codex keeps its own per-command approval cache with finer matching, so `acceptForSession` safely suppresses future prompts for that command without claude-web tracking an allowlist. Command approvals still reuse the tool name "Bash" for the card's command rendering.
- **Driver**: `_codex_driver` in app.py mirrors the Claude driver's external contract (consumes `run.user_input_queue` with cancel/ack semantics, flips `between_turns`, publishes `run.client` so `/api/chat/stop` works — the shim's `interrupt()` issues `turn/interrupt`). Mid-turn user input goes through `turn/steer`. Events translate in `codex_provider.item_events` to the exact v1 stream-json SSE shapes `_sdk_message_to_events` emits, so app.js renders both providers identically (`agentMessage` deltas → `partial_text`, `commandExecution` → Bash `tool_use`/`tool_result`, `todoList` → `todos_update`, `turn/completed` + `thread/tokenUsage/updated` → the `result` usage line and the context endpoint).
- **Models**: fetched live from `model/list` per account and cached for that app-server process's life — nothing hardcoded, so new GPT releases appear without a deploy and plan-specific model lists stay accurate. Reasoning efforts ride the same effort picker (per-model lists, including codex-only levels like `ultra`).
- **Sessions and account handoff**: codex thread ids double as claude-web session ids. `codex_session` (state.db) is the registry the sidebar/list/reopen/delete paths use; ownership/visibility reuses `session_owners` via the shared `emit()` init hook. Personal `CODEX_HOME` directories keep `auth.json` and SQLite files private but link `sessions/` to the host Codex home. On a slot change the old turn is interrupted, its run-specific app-server is terminated, and the new account calls `thread/resume` with the same id in a fresh process. Never symlink Codex SQLite state across homes; separate paths can create separate WAL files for one database. Reopen history comes from `thread/read` — **codex 0.144 returns only message items there**, so a reopened codex session replays conversation text without tool chips (live streams show everything). `/api/chat` infers `provider=codex` from the registry when the field is missing, and 400s explicit cross-provider mismatches so the wrong backend can never resume a transcript.
- **Sandbox posture**: `approvalPolicy=untrusted` + `sandbox=danger-full-access` by default — the same trust model as the Claude path (no OS sandbox, per-command human gating). Env overrides: `CLAUDE_WEB_CODEX_APPROVAL`, `CLAUDE_WEB_CODEX_SANDBOX`, `CLAUDE_WEB_CODEX_BIN`.
- **OpenAI accounts**: `codex_credential` + `user_codex_account` are provider-specific parallels to the Claude tables. Homes live at `CLAUDE_WEB_CODEX_PERSONAL_HOMES_DIR/<safe_sub>/<id>/`; browser sign-in uses stable app-server v2 `account/login/start` with `type=chatgptDeviceCode`, polling `account/read` and the `account/login/completed` notification. Personal app-servers strip service-level OpenAI keys and force `cli_auth_credentials_store="file"`, `forced_login_method="chatgpt"`, and `model_provider="openai"`, so the slot cannot silently fall back to shared API billing or a custom provider from shared config. Cross-user ids return 404.
- **Not supported with Codex** (capability-gated in the UI, 400 with a clear message server-side): plan mode, fork, file rewind/checkpoints, MCP page (configure in `~/.codex/config.toml`), background tasks, advisor combos. Codex supports the permission-mode subset `default`, `acceptEdits`, and `bypassPermissions`. Personalities carry over via `developerInstructions` at thread start/resume.
- **Usage dialog**: the header Usage button stays visible for codex and opens a provider-aware view (`GET /api/codex/usage` → `codex_provider.CodexAppServer.account_usage`). `account/rateLimits/read` + `account/usage/read` are **ChatGPT-auth only** — on API-key auth the app-server rejects them ("chatgpt authentication required"), so the dialog degrades to the account-type line plus the live conversation's cumulative token totals (folded in via `?session_id=`) and a pointer to the OpenAI dashboard. On a ChatGPT-plan login it shows the real primary/secondary rate-limit windows and daily token buckets. The running header **cost** figure ($-spend) stays hidden for codex (the `usage` capability gates only that — codex reports no per-turn cost).

### Portability

All paths are env-driven: `CLAUDE_PROJECT_DIR`, `CLAUDE_WEB_PROJECT_DIRS`, `CLAUDE_HOME`, `CODEX_HOME`, `CLAUDE_WEB_STATE_DIR`, `CLAUDE_WEB_PERSONAL_HOMES_DIR`, `CLAUDE_WEB_CODEX_PERSONAL_HOMES_DIR`, `CLAUDE_WEB_SHARED_ACCOUNT_LABEL`, `CLAUDE_WEB_CODEX_SHARED_ACCOUNT_LABEL`, `CLAUDE_WEB_SITE_TITLE`, `AUTH_MODE=oidc|none`. **Don't hardcode an operator's home directory back into the source.**

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
- **Restarting from inside a claude-web chat session**: do NOT run
  `systemctl restart claude-web` directly — it SIGTERMs the whole cgroup,
  including the CLI subprocess running your own conversation, killing the
  in-flight turn (the command dies with exit 144 before the restart lands).
  Instead request a drain-restart, which waits until every conversation is
  between turns and then exits for systemd to revive:
  `kill -USR1 $(systemctl show -p MainPID --value claude-web)`
  (same-user signal, no sudo needed). Your turn finishes normally; the
  restart fires after it ends. Cancel a pending drain with
  `DELETE /api/admin/restart`. Requires `Restart=always` on the unit
  (drop-in at `/etc/systemd/system/claude-web.service.d/self-restart.conf`)
  AND `--timeout-graceful-shutdown` on the uvicorn ExecStart — without it,
  open SSE streams block uvicorn's graceful exit indefinitely and the site
  502s until someone force-restarts (observed 2026-06-09: 9-minute hang).

## Source-file map

| file | what's in it |
| --- | --- |
| `app.py` | routes, SSE, permission Future plumbing, `state.db`, run lifecycle, per-user-account API, personality CRUD + mirror writer, roundtable assistant + apply, picker-driven run cancellation, MEMORY.md auto-edit at startup, `_codex_driver` |
| `codex_provider.py` | OpenAI Codex provider: `codex app-server` JSON-RPC client (singleton), availability probe, notification→SSE translation, thread/read transcript reader; stdlib-only, no app.py imports |
| `auth.py` | OIDC code+PKCE via authlib |
| `setup_flow.py` | in-browser sign-in flows for the bundled CLI, keyed per-credential |
| `roundtable/` | canonical vendored roundtable core + FastMCP server; high-level coding workflows, providers, repo tools, structured review, convergence, storage |
| `templates/{index,account,setup,personalities,roundtable}.html` | UI |
| `static/{app,account,setup,personalities,roundtable}.js` | client JS |
| `static/{style,setup,roundtable}.css` | styles |
| `static/{marked,purify}.min.js` | markdown rendering + sanitisation for roundtable synth output |
| `tests/` | pytest suite |
