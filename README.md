# claude-web

A small, self-hostable web UI for [Claude Code](https://claude.com/claude-code), with native OIDC auth and per-tool permission prompts.

- Streams Claude's responses + tool calls in the browser via SSE.
- Intercepts every tool invocation and asks the browser to allow / allow-for-session / deny.
- Reads sessions directly from `~/.claude/projects/<project>/*.jsonl`, so the UI and the host-shell `claude` CLI share state — start a chat in one, resume it in the other.
- OIDC sign-in (Keycloak, Authentik, Authelia, Auth0, Google, …) with optional email- or group-based allowlists.
- One container, one `.env` file. No database.

> **Security model.** Anyone who can sign in to your IdP and pass the allowlist gets a Claude that can read, write, and run shell commands inside the mounted workspace. The per-tool permission prompt is the *only* guardrail between the user and arbitrary code execution. Don't expose this to people you wouldn't hand a shell.

## Quick start (Docker Compose)

```bash
git clone https://github.com/YOUR_GH_USER/claude-web.git
cd claude-web
cp .env.example .env
# edit .env: set SESSION_SECRET, OIDC_*, OIDC_REDIRECT_URI
cp docker-compose.example.yml docker-compose.yml
mkdir -p workspace claude-home claude-web-state
docker compose up -d
```

Then sign in to your Claude Code account inside the container once (so the credentials persist in the `claude-home` volume):

```bash
docker compose exec claude-web claude login
```

Open `https://claude.example.com` (whatever you set `OIDC_REDIRECT_URI` to point at) and you're in.

## Configuration

All configuration is via environment variables. See [`.env.example`](.env.example) for the full list with comments.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `AUTH_MODE` | yes | `oidc` | `oidc` or `none`. `none` skips auth entirely — only use behind a trusted proxy or for localhost dev. |
| `SESSION_SECRET` | when `oidc` | — | Random string for cookie signing. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `OIDC_ISSUER_URL` | when `oidc` | — | Base URL your IdP advertises in `/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID` | when `oidc` | — | OIDC client ID. |
| `OIDC_CLIENT_SECRET` | when `oidc` | — | OIDC client secret (confidential client). |
| `OIDC_REDIRECT_URI` | when `oidc` | — | Must match what's registered with the IdP. Usually `https://<your-host>/auth/callback`. |
| `OIDC_ALLOWED_EMAILS` | no | (any) | Comma-separated allowlist. Empty = anyone with a valid token. |
| `OIDC_ALLOWED_GROUPS` | no | (any) | Comma-separated. Matched against the `groups` claim. |
| `SESSION_COOKIE_INSECURE` | no | `false` | Set `true` only when serving over plain HTTP (e.g. localhost). |
| `CLAUDE_PROJECT_DIR` | no | `$HOME` | The directory Claude treats as its project root. |
| `CLAUDE_HOME` | no | `$HOME/.claude` | Where Claude Code keeps its per-user state. |
| `CLAUDE_WEB_STATE_DIR` | no | `$HOME/.claude-web` | Where this app keeps its usage log + cached rate-limit info. |
| `SAFE_TOOLS` | no | `TodoWrite` | Tools auto-approved without prompting. Comma-separated. |

## Setting up OIDC

You need a *confidential* OIDC client at your IdP with:

- **Redirect URI**: exactly what you set as `OIDC_REDIRECT_URI` (e.g. `https://claude.example.com/auth/callback`).
- **Scopes**: `openid email profile` (the defaults — claude-web requests these).
- **Optional**: a `groups` mapper if you plan to use `OIDC_ALLOWED_GROUPS`.

### Keycloak

1. Realm → Clients → **Create client**.
2. Client type **OpenID Connect**, Client ID `claude-web`, Name whatever you like.
3. Capability config: enable **Client authentication** (this makes it confidential). Authorization off. Authentication flow: keep `Standard flow` checked, leave the rest as defaults.
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

## Running from source (no Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
claude login
cp .env.example .env  # edit values
set -a; source .env; set +a
uvicorn app:app --host 127.0.0.1 --port 3001
```

## How sessions work

Claude Code writes per-conversation transcripts to `$CLAUDE_HOME/projects/<sanitized-cwd>/<session-id>.jsonl`. claude-web reads the same files: nothing is duplicated, nothing is migrated. If you exec into the container and run `claude --resume <session-id>`, you'll resume the same conversation the browser was viewing.

The "sanitized cwd" is the absolute path with `/` replaced by `-`. So `CLAUDE_PROJECT_DIR=/workspace` → `~/.claude/projects/-workspace/`.

## Reverse proxy notes

claude-web binds `0.0.0.0:3001` inside the container; expose it however you like. The only thing it needs from upstream is `X-Forwarded-Proto: https` (so cookies are issued with `Secure`) when actually serving HTTPS. Streaming uses Server-Sent Events — make sure your proxy doesn't buffer (`proxy_buffering off` for nginx; Traefik handles SSE fine out of the box).

If you want to layer claude-web *behind* an existing edge SSO (oauth2-proxy, Authelia forward-auth, Cloudflare Access), set `AUTH_MODE=none` and let the upstream do the gating. You lose per-user identity in the cost log but gain a uniform login surface.

## Permissions

Every tool call goes through `can_use_tool`. The browser sees a card with the tool name + serialized input and three buttons:

- **Allow** — this single call.
- **Allow this session** — keyed on tool + a stable signature (first Bash word, file path, URL, etc.). Resets when you start a new chat.
- **Deny** — the SDK gets a `PermissionResultDeny` and Claude moves on.

`SAFE_TOOLS` are auto-approved (default: `TodoWrite`, since it's pure UI bookkeeping).

## License

MIT — see [LICENSE](LICENSE).
