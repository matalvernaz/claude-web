# claude-web

A small, self-hostable web UI for [Claude Code](https://claude.com/claude-code) with native OIDC auth and per-tool permission prompts. Built for **single-user homelab or trusted-team** deployments.

- Streams Claude's responses + tool calls in the browser via SSE.
- Intercepts every tool invocation and asks the browser to allow / allow-for-session / deny.
- Reads sessions directly from `~/.claude/projects/<project>/*.jsonl`, so the UI and the host-shell `claude` CLI share state — start a chat in one, resume it in the other.
- OIDC sign-in (Keycloak, Authentik, Authelia, Auth0, Google, …) with optional email- or group-based allowlists.
- One container, one `.env` file, no separate database.

> ## Trust model — read this first
>
> claude-web **does not sandbox Claude**. Approved tools (Bash, Edit, Write, …) execute with the permissions of the server process and can read, write, or run anything that user can. The per-tool permission prompt is the *only* guardrail between an authenticated user and arbitrary code execution on the host.
>
> Only deploy this to people you would be comfortable handing a shell. For untrusted users, run one isolated container or Unix user per person — the multi-user mode (`CLAUDE_WEB_PER_USER_SESSIONS`) is an *ownership filter*, not a security boundary; sessions from other users can't be listed but their files are still on the same filesystem the model can `Read` or `Bash` to.

---

## Quick start (Docker Compose)

```bash
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
cp .env.example .env
# edit .env: set SESSION_SECRET (random) and the OIDC_* values for your IdP
cp docker-compose.example.yml docker-compose.yml
mkdir -p workspace claude-home claude-web-state
docker compose up -d
```

Open the URL you put in `OIDC_REDIRECT_URI` (minus `/auth/callback`). On first visit the app redirects to **`/setup`** — a one-time, in-browser sign-in flow for the bundled `claude` CLI. Two options:

- **Sign in with a Claude account** — drives `claude auth login` (or `--console` for an Anthropic Console account) as a subprocess. Click the link to claude.com, sign in, copy the one-time code back into the textbox.
- **Or paste an API key** — for headless / shared instances. The key is persisted to `$CLAUDE_WEB_STATE_DIR/anthropic_api_key` (mode 0600) and loaded into `ANTHROPIC_API_KEY` on startup.

Either way, credentials persist in the `claude-home` (or `claude-web-state`) volume across container restarts. The `/setup` page locks itself once a credential is provisioned (`CLAUDE_WEB_ENABLE_SETUP=auto`); to switch accounts later, set `CLAUDE_WEB_ENABLE_SETUP=true` and restart, or shell into the container and run `claude auth login` directly.

If you'd rather sign in from a shell from the start: `docker compose exec claude-web claude auth login`.

## Running from source (no Docker)

Tested on Python 3.11+. You need Node.js for the bundled `@anthropic-ai/claude-code` CLI (the SDK shells out to it).

```bash
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code      # provides the `claude` binary
cp .env.example .env                          # edit values
set -a; source .env; set +a
uvicorn app:app --host 127.0.0.1 --port 3001
```

Then visit `http://localhost:3001/setup` (set `SESSION_COOKIE_INSECURE=true` and `AUTH_MODE=none` in `.env` for local-only testing) to sign Claude in.

### Running from source on Windows

The same source install works on Windows; the prerequisites are the same (Python 3.11+, Node.js + the `claude` CLI), just expressed in PowerShell. Two Windows-specific notes:

```powershell
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code        # provides claude.cmd
Copy-Item .env.example .env                     # edit values
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*)$') { Set-Item "env:$($matches[1])" $matches[2] }
}
uvicorn app:app --host 127.0.0.1 --port 3001
```

- The per-user-credentials feature (`/account`) mirrors `CLAUDE_HOME` into per-user subdirectories using symlinks. On Windows, `os.symlink` requires either **Developer Mode** (Settings → Privacy & security → For developers → "Developer Mode") or an Administrator shell. If neither is available we fall back to NTFS junctions for directories and hardlinks for files, which works without privilege but only on NTFS volumes — the warning will appear in the log if a fallback also fails. The shared slot doesn't need any of this; only the multi-credential view does.
- Click-to-apply diffs in `/roundtable` shell out to GNU `patch`. It isn't installed by default on Windows; the route returns HTTP 501 with a clear message if it's missing. Install it via Git for Windows (it ships `usr\bin\patch.exe`) or any other GNU-utils bundle and the feature lights up.

For a long-running install behind a reverse proxy, a systemd unit looks like:

```ini
# /etc/systemd/system/claude-web.service
[Unit]
Description=claude-web
After=network-online.target

[Service]
Type=simple
User=claude
WorkingDirectory=/opt/claude-web
Environment=HOME=/home/claude
# Systemd strips PATH to a minimal default that won't include ~/.local/bin
# (where `npm install -g` places the claude binary). Include it explicitly.
Environment=PATH=/home/claude/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=/opt/claude-web/.env
ExecStart=/opt/claude-web/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 3001 --proxy-headers --forwarded-allow-ips=* --timeout-graceful-shutdown 3
# always (not on-failure): the in-app drain-restart exits cleanly and relies
# on the supervisor to revive it. --timeout-graceful-shutdown matters for the
# same reason — open SSE streams otherwise block the exit indefinitely.
Restart=always

[Install]
WantedBy=multi-user.target
```

> **Single worker only.** The app keeps in-memory state (active SSE subscribers, pending permission requests, per-session locks) that isn't shared across uvicorn/gunicorn workers. Running with `WEB_CONCURRENCY > 1` will misroute permission prompts and split-brain conversations. The startup checks `WEB_CONCURRENCY` and refuses to boot if it's set to more than one — set `CLAUDE_WEB_ALLOW_MULTI_WORKER=true` only if you've externalised state, which this app does not currently do.

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example) for the full list.

### Auth

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AUTH_MODE` | yes | `oidc` | `oidc` or `none`. `none` skips auth entirely — only safe behind a trusted proxy or for localhost dev. |
| `SESSION_SECRET` | when `oidc` | — | Random string for cookie signing. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SESSION_MAX_AGE_SECONDS` | no | `86400` | How long the signed session cookie is valid. |
| `SESSION_COOKIE_INSECURE` | no | `false` | Set `true` only when serving plain HTTP (e.g. localhost dev). |
| `OIDC_ISSUER_URL` | when `oidc` | — | Base URL your IdP advertises in `/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID` | when `oidc` | — | OIDC client id. |
| `OIDC_CLIENT_SECRET` | when `oidc` | — | OIDC client secret (confidential client). |
| `OIDC_REDIRECT_URI` | when `oidc` | — | Must match what's registered with the IdP. Usually `https://<your-host>/auth/callback`. Also drives the CSRF middleware's expected origin. |
| `OIDC_ALLOWED_EMAILS` | no | (any) | Comma-separated allowlist. Empty = anyone with a valid token. |
| `OIDC_ALLOWED_GROUPS` | no | (any) | Comma-separated, matched against the `groups` claim. |

### Claude Code

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_PROJECT_DIR` | `$HOME` | The directory Claude treats as its project root. |
| `CLAUDE_WEB_PROJECT_DIRS` | (unset) | Comma-separated list to expose multiple project roots in a picker. |
| `CLAUDE_HOME` | `$HOME/.claude` | Where Claude Code keeps its per-user state (sessions, settings, MCP, hooks). |
| `CLAUDE_WEB_STATE_DIR` | `$HOME/.claude-web` | Where this app keeps usage log, rate-limit cache, persisted runs, and uploads. |
| `SAFE_TOOLS` | `TodoWrite` | Tools auto-approved without prompting. Comma-separated. |
| `NO_SESSION_ALLOWLIST_TOOLS` | `Bash` | Tools where "Allow this session" is disabled because their signature is too coarse to be safe (e.g. allowing `echo` would also bless `echo "ok" && rm -rf ~`). Each call requires explicit per-call approval. |
| `CLAUDE_WEB_FALLBACK_MODEL` | (unset) | Model the CLI retries with when the primary model is overloaded (API 529), e.g. `claude-sonnet-4-6`. Unset = no fallback. |
| `CLAUDE_WEB_MAX_BUDGET_USD` | `0` (off) | Hard per-run API-spend ceiling in USD. Only meaningful for API-key credentials — subscription turns report synthetic costs. |
| `CLAUDE_WEB_PUSHOVER_TOKEN` / `CLAUDE_WEB_PUSHOVER_USER` | (unset) | When both are set, a Pushover notification fires when a turn finishes after running longer than `CLAUDE_WEB_NOTIFY_MIN_SECONDS` (default `120`) — for the walked-away-during-a-long-turn case the in-page earcons can't cover. |
| `CLAUDE_WEB_FILE_CHECKPOINTS` | `true` | The CLI snapshots files before edits so `/rewind [n]` can restore them to before your nth-last message (only while the conversation's CLI is alive, and only between turns). Set `false` to skip the snapshot overhead. |

Chat extras: `/fork [message]` branches the conversation into a new session
(the original stays intact and navigable), `/rewind [n]` undoes file changes,
and assistant replies stream progressively as they're generated. Roundtable
panel runs survive tab close: the work finishes server-side and the page
rejoins (and replays) the run on reload.

#### Identity passed to the CLI

Every spawned Claude CLI subprocess receives three env vars describing the signed-in user, so hooks and `CLAUDE.md` personalities can address people by name:

| Variable | Source | Value when `AUTH_MODE=none` |
|---|---|---|
| `CLAUDE_WEB_USER_SUB` | OIDC `sub` claim | `""` (empty) |
| `CLAUDE_WEB_USER_EMAIL` | OIDC `email` claim | `""` |
| `CLAUDE_WEB_USER_NAME` | OIDC `name` (or `preferred_username`) | `""` |

The schema is stable — keys are always set, only their values are empty when there's no real identity. A typical use is a `SessionStart` hook in `$CLAUDE_HOME/settings.json` that emits an `additionalContext` line like *"Signed-in user: Jocelyn Smith <jocelyn@cobd.ca>"*, which the model then reads at the start of every session.

### Multi-user

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_PER_USER_SESSIONS` | `false` | When `true`, sessions are scoped to whoever first chatted in them. **Not a security boundary** — see the trust-model note above. Sessions created via the host-shell `claude` CLI have no recorded owner and stay visible to everyone. |
| `CLAUDE_WEB_ADMIN_EMAILS` | (empty) | Comma-separated email allowlist for the credential-mutating `/setup` endpoints. Only enforced in `PER_USER_SESSIONS` mode. Empty in multi-user mode means **no one** can mutate credentials from the browser; admin must shell into the container. |

`AUTH_MODE=none` + `PER_USER_SESSIONS=true` is refused at startup (every visitor would share `sub="anonymous"`, breaking isolation entirely).

### Per-user accounts

By default every signed-in user authenticates as the deployment-wide *shared* Claude account whose credentials sit in `$CLAUDE_HOME/.credentials.json`. Optionally, each user can register their own *personal* Claude account and flip between the two from a `<select>` in the header.

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_PERSONAL_HOMES_DIR` | `$HOME/.claude-homes` | Where per-user personal `CLAUDE_CONFIG_DIR` directories are created. Bind-mount this in your compose so personal credentials survive container rebuilds (`./claude-homes:/home/claude/.claude-homes`). |
| `CLAUDE_WEB_SHARED_ACCOUNT_LABEL` | `Shared` | Display name for the shared slot in the UI. Set this per deployment, e.g. `Office`, `Team`, `Workspace`. |

Per-user homes are mostly symlinks back to `CLAUDE_HOME`, with only `.credentials.json` as a real per-user file. That means `projects/`, `sessions/`, `settings.json`, `skills/`, `commands/`, etc. all stay shared — the chat transcript JSONL for a session is the same file regardless of which slot is active, so toggling between accounts mid-conversation does not break or split the user's history.

To register a personal account for a user (admin task — needs shell access to the host):

```bash
# Run on whatever host the claude-web container lives on.
./scripts/add-personal --sub <oidc-sub> [--label "<display name>"]
```

This drops the user into an interactive `claude /login` against their personal directory, then flips `has_personal=1` so the toggle becomes available in their UI. The user can find their `<oidc-sub>` by signing in and reading `GET /api/account` (or any field of their session — the `sub` is the OIDC subject identifier from your IdP).

### Setup-flow lock

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_ENABLE_SETUP` | `auto` | Three values: `true` always allow `/setup` actions; `false` always block them (admin must shell in); `auto` allow during first-run, lock once a credential is configured. |

### Hardening

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_CSRF_STRICT` | `true` | Reject mutating requests without a matching `Origin` or `Referer` header. Set `false` only for command-line testing. |
| `CLAUDE_WEB_MAX_SUBSCRIBER_QUEUE` | `1000` | Bound on in-memory SSE event queue per subscriber; slow clients are dropped (with `_overflow`) instead of growing memory. |

### Retention / GC

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_PERSIST_RETENTION` | `86400` (24h) | Run/event store rows older than this are pruned. |
| `CLAUDE_WEB_UPLOAD_RETENTION` | `604800` (7d) | Per-run upload directories older than this are deleted. |
| `CLAUDE_WEB_PERMISSION_TIMEOUT` | `900` (15m) | Pending permission requests deny themselves after this. |
| `CLAUDE_WEB_MAX_AUTO_FIRES` | `3` | How many synth-message turns can chain off background tool notifications before the driver waits for a human. |

### Self-restart

`POST /api/admin/restart` (or `SIGUSR1` to the server process) requests a
drain-restart: new turns get `503 restart_pending`, in-flight conversations
finish their current turn, then the process exits cleanly for the supervisor
to revive. `DELETE /api/admin/restart` cancels a pending drain. With
`CLAUDE_WEB_ADMIN_EMAILS` set, only those users may call it; unset, any
signed-in user can (single-operator default). **Requires a supervisor that
restarts on clean exit** — systemd `Restart=always`, Docker
`restart: unless-stopped`. Useful when Claude edits claude-web from inside a
claude-web session and needs to pick the changes up without killing its own
turn.

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_RESTART_MAX_WAIT` | `1800` (30m) | Drain ceiling: after this many seconds the restart fires even with busy runs (equivalent to today's hard restart — transcripts survive, mid-tool-call state doesn't). |

## Setting up OIDC

You need a *confidential* OIDC client at your IdP with:

- **Redirect URI:** exactly what you set as `OIDC_REDIRECT_URI` (e.g. `https://claude.example.com/auth/callback`).
- **Scopes:** `openid email profile` (the defaults — claude-web requests these).
- **Optional:** a `groups` mapper if you plan to use `OIDC_ALLOWED_GROUPS`.

### Keycloak

1. Realm → Clients → **Create client**.
2. Client type **OpenID Connect**, Client ID `claude-web`.
3. Capability config: enable **Client authentication** (this makes it confidential). Authorization off. Authentication flow: keep `Standard flow` checked.
4. Login settings → **Valid redirect URIs**: `https://claude.example.com/auth/callback`.
5. Save → **Credentials** tab → copy the client secret into `.env` as `OIDC_CLIENT_SECRET`.
6. (If you'll use `OIDC_ALLOWED_GROUPS`.) Client → **Client scopes** → `claude-web-dedicated` → **Add mapper** → **By configuration** → **Group Membership**. Token Claim Name `groups`, Full group path off, Add to ID token + userinfo on.
7. `OIDC_ISSUER_URL` is `https://<keycloak-host>/realms/<realm>`.

### Authentik

1. Applications → Providers → **Create** → OAuth2/OpenID Provider.
2. Client type Confidential. Redirect URI `https://claude.example.com/auth/callback`. Signing key whatever your default is.
3. Applications → Applications → **Create**, link to the provider.
4. The issuer URL is shown on the provider page (`https://auth.example.com/application/o/<slug>/`).

### Auth0

1. Applications → **Create** → Regular Web Application.
2. Allowed Callback URLs `https://claude.example.com/auth/callback`. Allowed Logout URLs `https://claude.example.com/`.
3. Settings → Advanced → Endpoints copies the issuer (`https://your-tenant.auth0.com/`).

## Reverse-proxy deployment

claude-web binds `0.0.0.0:3001` inside the container. Expose it however you like — Traefik, nginx, Caddy, oauth2-proxy in front. The only thing it needs from the upstream:

- `X-Forwarded-Proto: https` so cookies are issued with `Secure` when actually serving HTTPS.
- **No buffering on SSE.** The chat endpoints are `text/event-stream`; nginx needs `proxy_buffering off`, Traefik handles it out of the box. Cloudflare/cloudflared close streams after ~100s of byte-level silence — the app sends a `: ping` comment every 25s to keep them alive.

If you want to layer claude-web *behind* an existing edge SSO (oauth2-proxy, Authelia forward-auth, Cloudflare Access), set `AUTH_MODE=none` and let the upstream do the gating. Note that you lose per-user identity in the cost log and `PER_USER_SESSIONS` becomes meaningless (refused at startup).

### Security headers

The app sends `Content-Security-Policy: default-src 'self'; script-src 'self'; ... ; frame-ancestors 'none'`, plus `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: same-origin` on every response. Don't strip these at the proxy.

## How sessions work

Claude Code writes per-conversation transcripts to `$CLAUDE_HOME/projects/<sanitized-cwd>/<session-id>.jsonl`. claude-web reads the same files: nothing is duplicated, nothing is migrated. If you exec into the container and run `claude --resume <session-id>`, you'll resume the same conversation the browser was viewing.

The "sanitized cwd" is the absolute path with `/` replaced by `-`. So `CLAUDE_PROJECT_DIR=/workspace` → `~/.claude/projects/-workspace/`.

Run-level state (event log, permission requests, uploads) lives in a separate SQLite database at `$CLAUDE_WEB_STATE_DIR/state.db` so a `systemctl restart` doesn't lose an in-flight conversation. Anything in-flight at restart time (a partial tool call, a queued auto-fire) is gone, but the conversation jsonl on disk is intact.

## Permissions

Every tool call goes through `can_use_tool`. The browser sees a card with the tool name + serialized input and three buttons:

- **Deny** — this single call. The default focus for high-risk tools (Bash, Write).
- **Allow once** — this single call. The default focus for everything else. `Esc` always denies; pressing `Enter` activates the focused button (so a Deny-focused card won't accidentally approve).
- **Allow this session** — keyed on tool + a stable signature (file path, URL, etc.). Resets when you start a new chat. **Hidden for tools in `NO_SESSION_ALLOWLIST_TOOLS`** (default: `Bash`) because the signature is too coarse to be safe.

`SAFE_TOOLS` are auto-approved (default: `TodoWrite`, since it's pure UI bookkeeping).

## Backup

The data spans three locations:

- `$CLAUDE_HOME/` — Claude credentials + session jsonl files. Most important; without this the user has to sign in again.
- `$CLAUDE_WEB_STATE_DIR/` — `state.db` (run/event store), `uploads/<run_id>/` (file attachments), `usage.jsonl` (cost log), `rate_limit.json` (rate-limit cache).
- `.env` — auth secrets and config.

A nightly tarball of `$CLAUDE_HOME/` and `$CLAUDE_WEB_STATE_DIR/` is enough for a full restore. SQLite WAL is safe to copy live (`sqlite3 state.db ".backup state.db.bak"` if you want a checkpointed snapshot). The example homelab deployment uses a 14-day rolling tarball.

## Development

```bash
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest                     # unit + smoke tests
ruff check .               # lint
node --check static/app.js # JS syntax
```

The CI workflow at `.github/workflows/ci.yml` runs the same on every push.

Tests focus on security boundaries (CSRF, OIDC redirect protection, upload validation, tool-signature allowlist) rather than full coverage; PRs that touch those areas should bring or update tests.

## License

MIT — see [LICENSE](LICENSE).
