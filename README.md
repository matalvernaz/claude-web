# claude-web

A small, self-hostable web UI for [Claude Code](https://claude.com/claude-code) with native OIDC auth and per-tool permission prompts. Built for **single-user homelab or trusted-team** deployments.

- Streams Claude's responses + tool calls in the browser via SSE.
- Intercepts every tool invocation and asks the browser to allow / allow-for-session / deny.
- Reads sessions directly from `~/.claude/projects/<project>/*.jsonl`, so the UI and the host-shell `claude` CLI share state — start a chat in one, resume it in the other.
- OIDC sign-in (Keycloak, Authentik, Authelia, Auth0, Google, …) with optional email- or group-based allowlists.
- One container, one `.env` file, no separate database.
- Optional second AI provider: install the OpenAI `codex` CLI and sign in either the shared host slot or personal ChatGPT subscription accounts from `/account`. Codex conversations get the same streamed events, per-command approval prompts, interrupt, resume, mid-chat model switching, and same-chat account switching; commands run with the same trust model as the Claude path (approval prompt, no sandbox).
- Optional local AI provider: connect an Ollama server for coding and writing with the existing file tools, command approvals, and saved conversations.

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
mkdir -p workspace claude-home claude-web-state codex-home claude-homes codex-homes
docker compose up -d
```

Open the URL you put in `OIDC_REDIRECT_URI` (minus `/auth/callback`). On first visit the app redirects to **`/setup`** — a one-time, in-browser sign-in flow for the bundled `claude` CLI. Two options:

- **Sign in with a Claude account** — drives `claude auth login` (or `--console` for an Anthropic Console account) as a subprocess. Click the link to claude.com, sign in, copy the one-time code back into the textbox.
- **Or paste an API key** — for headless / shared instances. The key is persisted to `$CLAUDE_WEB_STATE_DIR/anthropic_api_key` (mode 0600) and loaded into `ANTHROPIC_API_KEY` on startup.

Either way, credentials persist in the `claude-home` (or `claude-web-state`) volume across container restarts. The `/setup` page locks itself once a credential is provisioned (`CLAUDE_WEB_ENABLE_SETUP=auto`); to switch accounts later, set `CLAUDE_WEB_ENABLE_SETUP=true` and restart, or shell into the container and run `claude auth login` directly.

If you'd rather sign in from a shell from the start: `docker compose exec claude-web claude auth login`.

## Local models (Ollama)

Install a tool-capable model on your Ollama server, then configure the app:

```dotenv
CLAUDE_WEB_OLLAMA_URL=http://ollama:11434
CLAUDE_WEB_OLLAMA_MODELS=qwen3.5:35b-32k
```

The model names are a comma-separated allowlist of models already installed on
that server. The app checks their capabilities without downloading or loading
weights. The URL must be the server origin, with no `/v1` suffix. A Claude Code
CLI is still required for tools and permissions; a Claude subscription is not
required for local inference.

Configure the context window in the model's Ollama Modelfile. For example,
`FROM qwen3.5:35b` and `PARAMETER num_ctx 32768` can be saved as a Modelfile and
installed with `ollama create qwen3.5:35b-32k -f Modelfile`. Choose a window that
fits the server; larger contexts require more memory and prompt-processing time.
If Ollama runs in a container or VM with fewer CPUs than the host, add
`PARAMETER num_thread <cpus available>` to the same Modelfile. Ollama sizes its
thread pool from the host's core count, and an oversubscribed pool can slow
generation by more than an order of magnitude.
When no explicit model window is available, the UI leaves its capacity unknown.

After restarting the app, choose **Local (Ollama)**. The default permission mode
uses the same approval cards as Claude. Other permission modes retain their
existing behavior, including the explicit option to bypass approvals. Tools run
on the **Claude web host**; inference runs on the **Ollama server**.

With Ollama 0.33.3 or newer, supported Qwen3/Qwen3.5 models expose a two-position
thinking slider, and GPT-OSS exposes low, medium, and high reasoning effort.
Models without verified controls display an explanation instead of an active
slider. More thinking can take longer; it does not guarantee a better answer.

On CPU-only servers the first reply of a new conversation can take several
minutes while the model reads the full Claude Code prompt; later turns reuse the
server's prompt cache and respond faster. The app raises the CLI's request
timeouts for local runs so slow prompt processing is not reported as an error.
Ollama does not enforce Anthropic thinking-token budgets. Model and effort
changes take effect on the next message through a restarted local process.

Local sessions keep their provider when reopened. Switching between Local and a
cloud provider starts a separate chat. A saved Local selection stays selected
while provider discovery is pending or fails; sending is blocked until it is
available or you explicitly select another provider. Missing models or an unavailable server
produce an error; local runs never use the app's cloud-account failover or
`CLAUDE_WEB_FALLBACK_MODEL`. Cloud model tags are rejected. Local inference does
not make the whole assistant offline: configured MCP tools, hooks, and approved
commands can still access external services.

Local routing and thinking controls take precedence over conflicting CLI user,
project, and local settings. Changing the model or effort waits for the previous
local process to close before resuming the conversation.

To check compatibility after upgrading the Claude CLI or SDK, run the optional
tests against temporary loopback servers (no model inference or cloud requests):

```bash
CLAUDE_WEB_TEST_LOCAL_CLI=1 .venv/bin/pytest -q tests/test_local_cli_contract.py -k 'not silent'
CLAUDE_WEB_TEST_LOCAL_CLI=1 CLAUDE_WEB_TEST_LOCAL_SILENT_SECONDS=420 .venv/bin/pytest -q tests/test_local_cli_contract.py -k silent
```

The second check holds a request silent for seven minutes to catch runtime idle
timeouts. The ordinary suite skips these explicit compatibility checks. Browser
regressions run with Chromium required in the Linux/Python 3.13 CI job; run them
locally with `CLAUDE_WEB_REQUIRE_BROWSER=1 .venv/bin/pytest -q tests/test_local_ui.py`
after installing Chromium with `python -m playwright install chromium`.

## Running from source (no Docker)

Tested on Python 3.11+. You need Node.js for the Claude Code CLI; install the Codex CLI too when using OpenAI accounts.

```bash
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code @openai/codex
cp .env.example .env                          # edit values
set -a; source .env; set +a
uvicorn app:app --host 127.0.0.1 --port 3001
```

Then visit `http://localhost:3001/setup` (set `SESSION_COOKIE_INSECURE=true` and `AUTH_MODE=none` in `.env` for local-only testing) to sign Claude in.

### Running from source on Windows

The standalone desktop bundle also supports a portable layout. Create a
`portable-data` folder next to `claude-web.exe`; the launcher keeps Claude and
OpenAI accounts, app state, conversations, and a workspace inside that folder.
Double-clicking opens the app in your default browser, bound to localhost.
Explicit environment or `.env` settings take precedence. Keep this folder private:
it contains login credentials.

Download the `-full` zip from the release page for a first install. Besides the
app it carries, under `tools/`, the three programs a chat needs, so nothing has
to be installed on the machine: OpenAI's Codex CLI as the official package
layout (with the code-mode host every Codex tool call runs through and
ripgrep), Claude Code's native binary, and Portable Git for the CLI's Bash.
Each lives in `tools/<name>/<version>/` with a `tools/<name>/current` marker,
the layout `portable_tools.py` installs at build time and keeps current at run
time. The lean zip without `tools/` is what the self-updater downloads; a copy
unzipped from it fetches the three programs on its first update pass instead.
The earlier hand-made layouts (`tools/codex.exe`, Portable Git extracted into
`tools/git`) still work and are replaced by managed copies on the first pass.
No batch launcher is needed.

The Windows build updates itself. Every six hours (and on demand from the
banner at the top of the chat page) it checks this repo's GitHub releases; a
newer `vX.Y.Z` release is downloaded and verified in the background, then the
app restarts into it as soon as no conversation is mid-turn, the same drain
logic as the self-restart below. `ZipExtractor.exe` (ravibpatel/AutoUpdater.NET,
built in the release workflow) performs the swap after the process exits and
relaunches it with the same arguments; `portable-data`, `tools` and `.env` are
not in the release zip and are never touched. The files the update changes are
backed up first, and a swap that only half lands is rolled back on the next
start. A copy built by hand from a branch (a `workflow_dispatch` run) updates
to the first versioned release published after it was built. Set
`CLAUDE_WEB_SELF_UPDATE=notify` to be asked first, or `off` to disable;
`claude-web.exe --version` prints what is installed.

The bundled programs update on the same six-hour cadence
(`CLAUDE_WEB_TOOLS_AUTOUPDATE`, default on; `CLAUDE_WEB_TOOLS` picks a subset
of `codex,claude,git`). Each vendor's release feed is asked for its newest
stable version: Codex from the openai/codex GitHub releases (package tarball,
checked against the release digest and its SHA256SUMS), Claude Code from
Anthropic's release bucket (checked against the version's manifest), Git from
the git-for-windows releases. A newer version is unpacked beside the running
one, checked (the program must report the expected version, and a Codex
release must still offer every app-server method claude-web uses, else it is
set aside until the next release), and then pointed at: new conversations
spawn it at once, running ones keep the files they hold, and old directories
are removed later. The previous version stays on disk as a manual rollback.
`GET /api/admin/update-tools` reports the state; `POST` runs a pass now. The
Agent SDK's bundled Claude CLI remains the fallback and a managed copy is only
downloaded when it would be newer than whatever would run otherwise.

The same source install works on Windows; the prerequisites are the same (Python 3.11+, Node.js + the `claude` CLI), just expressed in PowerShell. Two Windows-specific notes:

```powershell
git clone https://github.com/matalvernaz/claude-web.git
cd claude-web
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code @openai/codex
Copy-Item .env.example .env                     # edit values
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*)$') { Set-Item "env:$($matches[1])" $matches[2] }
}
uvicorn app:app --host 127.0.0.1 --port 3001
```

- The per-user-credentials feature (`/account`) mirrors `CLAUDE_HOME` into per-user subdirectories using symlinks. On Windows, `os.symlink` requires either **Developer Mode** (Settings → Privacy & security → For developers → "Developer Mode") or an Administrator shell. If neither is available we fall back to NTFS junctions for directories and hardlinks for files, which works without privilege but only on NTFS volumes — the warning will appear in the log if a fallback also fails. The shared slot doesn't need any of this; only the multi-credential view does. `CLAUDE_HOME/projects` is created before the mirror is built, so every credential's `projects` is always a link to the one shared transcript store; an install whose credentials already have private `projects` directories (from a first chat that ran before the shared directory existed) is merged into the shared store on the next start.
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
| `SESSION_MAX_AGE_SECONDS` | no | `86400` | How long a login survives with no activity. Rolling — measured from the last request, so active use never logs you out. No server-side store, so a leaked cookie stays valid this long. |
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
| `CLAUDE_WEB_FALLBACK_MODEL` | (unset) | Model the CLI retries with when the primary model is overloaded (API 529), e.g. `claude-sonnet-5`. A comma-separated list is tried in order, and the primary is retried at the start of each user turn. Unset = no fallback. |
| `CLAUDE_WEB_CLI_MODELS_FETCH` | `true` | The model picker is the installed CLI's own `/model` list, read at boot, after each CLI update and every `CLAUDE_WEB_CLI_UPDATE_INTERVAL` seconds, and cached in the state dir. Its `opus` / `fable` / `sonnet` / `haiku` rows always run the newest model of the family. `false` keeps the cached list (or, with none, just those four rows). |
| `CLAUDE_WEB_SELF_UPDATE` | `auto` | Portable Windows build only: `auto` downloads a newer GitHub release and restarts into it when no conversation is mid-turn, `notify` shows it with an Install button, `off` never checks. `CLAUDE_WEB_SELF_UPDATE_INTERVAL` (default 21600 s) sets the check cadence; `CLAUDE_WEB_SELF_UPDATE_PRERELEASE=true` also offers prereleases. |
| `CLAUDE_WEB_TOOLS_AUTOUPDATE` | `true` | Portable Windows build only: keep the bundled Codex, Claude Code and Git under `tools/` at their vendors' newest stable releases, checked every `CLAUDE_WEB_CLI_UPDATE_INTERVAL` seconds. `CLAUDE_WEB_TOOLS` (default `codex,claude,git`) limits which are managed; `CLAUDE_WEB_TOOLS_DIR` points a source run at a tools directory. |
| `CLAUDE_WEB_MAX_BUDGET_USD` | `0` (off) | Hard per-run API-spend ceiling in USD. Only meaningful for API-key credentials — subscription turns report synthetic costs. |
| `CLAUDE_WEB_PUSHOVER_TOKEN` / `CLAUDE_WEB_PUSHOVER_USER` | (unset) | When both are set, a Pushover notification fires when a turn finishes after running longer than `CLAUDE_WEB_NOTIFY_MIN_SECONDS` (default `120`) — for the walked-away-during-a-long-turn case the in-page earcons can't cover. |
| `CLAUDE_WEB_FILE_CHECKPOINTS` | `true` | The CLI snapshots files before edits so `/rewind [n]` can restore them to before your nth-last message (only while the conversation's CLI is alive, and only between turns). Set `false` to skip the snapshot overhead. |

Chat extras: `/fork [message]` branches the conversation into a new session
(the original stays intact and navigable), `/rewind [n]` undoes file changes,
and assistant replies stream progressively as they're generated. Roundtable
panel runs survive tab close: the work finishes server-side and the page
rejoins (and replays) the run on reload.

#### Roundtable coding workflows

Roundtable is available through both `/roundtable` and the `roundtable` MCP
server. Both surfaces call the vendored `roundtable/core.py` package and share
the SQLite thread store, so a debate started by Claude through MCP is visible in
the browser.

The primary MCP operation is `roundtable_coding_task`. It composes the lower
level thread tools into one coding workflow: select `general`, `debug`,
`review`, `plan`, `implement`, `test`, or `explain`; bind the active repository
read-only; ask independent panel roles; and have a synthesizer return one
answer. Review mode additionally captures the working diff, requests
schema-valid findings, and checks cited `file:line` locations against source
before synthesis. Claude chooses whether to invoke MCP tools, so ask it to
"use the roundtable" when consultation is mandatory rather than optional.

The `/roundtable` assistant exposes the same task modes in a picker. Select a
project to enable repository grounding. In Review changes mode, the advanced
options control the diff base, working-diff capture, and grounded verification.

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

### Per-user Claude and OpenAI accounts

`/account` lets every signed-in user add, label, sign in to, switch, and remove their own Claude and OpenAI accounts. Rows and filesystem homes are scoped to the caller's OIDC subject; a guessed credential id owned by another user returns 404.

Claude defaults to the deployment-wide shared account in `$CLAUDE_HOME/.credentials.json`. Personal Claude homes mirror `CLAUDE_HOME`, with only the credential files kept private. The transcript JSONL is therefore the same file under every slot, so a credential change can resume the current conversation.

A claude.ai sign-in lasts about four weeks from the moment you authorize, however
much it is used. Pick "long-lived token" in the sign-in form instead and the same
browser steps mint a token that lasts about a year (`claude setup-token`); the slot
then runs on `CLAUDE_CODE_OAUTH_TOKEN` and never needs the four-week refresh. The
token carries only the inference scope, so claude.ai-side extras tied to the
account (hosted connectors, plugin sync) may not work on such a slot, and the
Usage dialog cannot query Anthropic's usage service for it (that needs the
profile scope). The dialog's plan-window table does not depend on that: Anthropic
answers every message with its window headers, the CLI relays them, and the
dialog shows the 5-hour and 7-day percentages and reset times from the account's
most recent message. The per-model weekly buckets (Fable's, for one) and the
extra-usage balance need a four-week sign-in on the slot as well. Runs on the
long-lived token never refresh that sign-in's eight-hour key, so opening Usage
does: claude-web starts the CLI for a prompt-free handshake on the slot (nothing
billed, no session file), and the CLI refreshes the key itself. That keeps the
buckets available until the four-week sign-in ends, which the dialog names.

Slots with `auto_email` configured get a "Get sign-in link" button when the host
sets `CLAUDE_WEB_MAILBOX_POLL_CMD`. A server-side browser asks claude.ai to send
the sign-in email and waits on the login page; the mailbox reader picks the link
out of the email and the account page shows it. Open the link in your own
browser: because the email was requested elsewhere, claude.ai shows you a short
verification code instead of signing you in. Type it into the account page and
the server browser finishes the sign-in and the authorization itself. The
browser never opens the link (claude.ai meets it with a puzzle). If anything
fails, the page falls back to the ordinary sign-in link and long-code form.
Cancelling or retrying stops the previous request. Set
`CLAUDE_WEB_SIGNIN_DEBUG_DIR` to keep per-stage page text and screenshots
(URLs stripped of query and fragment) in a private directory when claude.ai's
pages change. An OAuth file with empty tokens is shown as signed out; an expired
access token with a refresh token still counts as configured.

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_PERSONAL_HOMES_DIR` | `$HOME/.claude-homes` | Where per-user personal `CLAUDE_CONFIG_DIR` directories are created. Bind-mount this in your compose so personal credentials survive container rebuilds (`./claude-homes:/home/claude/.claude-homes`). |
| `CLAUDE_WEB_SHARED_ACCOUNT_LABEL` | `Shared` | Display name for the shared slot in the UI. Set this per deployment, e.g. `Office`, `Team`, `Workspace`. |

OpenAI has a separate slot set. The shared slot uses `$CODEX_HOME/auth.json` or `OPENAI_API_KEY`; personal slots use Codex's ChatGPT device-code flow and support subscription plans such as Plus and Pro.

Codex questions offer suggested answers and free text. You can answer while Codex
keeps working or after its response finishes; reopening the conversation restores
unanswered question forms. Submitting sends the answer to that conversation.

| Variable | Default | Notes |
|---|---|---|
| `CODEX_HOME` | `$HOME/.codex` | Shared Codex configuration, rollout history, and optional host-level login. Bind-mount this in a container (`./codex-home:/home/claude/.codex`). |
| `CLAUDE_WEB_CODEX_PERSONAL_HOMES_DIR` | `$HOME/.codex-homes` | Private per-user `auth.json` and Codex SQLite indexes. Bind-mount this too (`./codex-homes:/home/claude/.codex-homes`). |
| `CLAUDE_WEB_CODEX_SHARED_ACCOUNT_LABEL` | `Shared OpenAI` | Display name for the host-level OpenAI slot. |
| `CLAUDE_WEB_CODEX_AUTOUPDATE` | `true` | Keep the codex CLI current, since OpenAI only lists its newest models to newer CLIs. Only an npm install under a user-writable `--prefix` is updated here; the portable Windows build's own copy is handled by `CLAUDE_WEB_TOOLS_AUTOUPDATE`. A release that drops an app-server method claude-web uses is rolled back. |

Codex personal homes link only `sessions/` and user configuration such as `config.toml` and `skills/` back to `CODEX_HOME`. Authentication and SQLite files remain private. On an account change, claude-web interrupts any old writer, terminates that chat's app-server process, and resumes the same thread id under the new account in a fresh process. This avoids both a split chat and the corruption risk of opening one SQLite database through multiple symlink paths.

The ownership filter is not an OS security boundary. All model processes still run as the same Unix user with the configured Codex sandbox posture; use separate containers or Unix users for mutually untrusted people.

### Setup-flow lock

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_ENABLE_SETUP` | `auto` | Three values: `true` always allow `/setup` actions; `false` always block them (admin must shell in); `auto` allow during first-run, lock once a credential is configured. |

### Hardening

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_CSRF_STRICT` | `true` | Reject mutating requests without a matching `Origin` or `Referer` header. Set `false` only for command-line testing. |
| `CLAUDE_WEB_MAX_SUBSCRIBER_QUEUE` | `1000` | Bound on in-memory SSE event queue per subscriber. A replay longer than this overflows and the client resumes from the `next_index` the marker carries. |
| `CLAUDE_WEB_CHILD_ENV_SCRUB` | *(empty)* | Extra env var names to strip from every spawned child, comma-separated. This app's own secrets (`SESSION_SECRET`, `OIDC_CLIENT_SECRET`, `OIDC_CLIENT_ID`, `CLAUDE_WEB_PUSHOVER_*`) are always stripped. Provider keys are not, by default — MCP servers registered without their own `env` inherit them from the child. |

A spawned agent runs shell commands, so treat anything in the server's
environment as readable by the model and by anything it executes.
`SESSION_SECRET` is the only input to the auth-cookie signer, so it is stripped
unconditionally; if you add secrets of your own to the service environment, add
their names to `CLAUDE_WEB_CHILD_ENV_SCRUB`.

### Retention / GC

| Variable | Default | Notes |
|---|---|---|
| `CLAUDE_WEB_PERSIST_RETENTION` | `86400` (24h) | Run/event store rows older than this are pruned. |
| `CLAUDE_WEB_UPLOAD_RETENTION` | `604800` (7d) | Per-run upload directories older than this are deleted. |
| `CLAUDE_WEB_CONVERSATION_RETENTION` | `2592000` (30d) | Canonical conversation log (`conversation*` tables) older than this is pruned. Deliberately much longer than the run store: this is what a mid-chat provider switch replays. A conversation with a live run is never pruned. |
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

The "sanitized cwd" is the resolved absolute path with every character outside `A-Z`, `a-z`, `0-9` replaced by `-` — the rule the CLI and `claude_agent_sdk.project_key_for_directory` apply, including a hash suffix for paths over 200 characters. So `CLAUDE_PROJECT_DIR=/workspace` → `~/.claude/projects/-workspace/`, and a Windows path such as `C:\Users\me\My Project` → `C--Users-me-My-Project/`. Keys that earlier versions stored in `state.db` under their old rule (only `/`, `\` and `:` replaced) are rewritten to this form on startup.

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
