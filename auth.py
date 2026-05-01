"""OIDC authentication for claude-web.

Supports two modes via the AUTH_MODE env var:
  - "oidc": Standard Authorization Code + PKCE against any OIDC provider
    (Keycloak, Authentik, Authelia, Auth0, etc.). The default and recommended
    mode. Configured via OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI. Optional access control via OIDC_ALLOWED_EMAILS
    (comma-separated) or OIDC_ALLOWED_GROUPS (comma-separated, matched against
    the "groups" claim in the ID token).
  - "none": No authentication. Only safe for localhost dev or when an
    upstream proxy is enforcing auth. Set AUTH_MODE=none explicitly to
    opt in -- there is no implicit fallback.

Sessions are signed cookies via Starlette's SessionMiddleware. SESSION_SECRET
must be set in oidc mode.
"""
from __future__ import annotations

import os
import secrets
from typing import Optional
from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware


AUTH_MODE = os.getenv("AUTH_MODE", "oidc").strip().lower()

# Default 24h. The signed cookie carries this max-age and Starlette refuses
# expired payloads, so a stale cookie can't be replayed past the limit.
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_SECONDS", "86400"))


def _split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


ALLOWED_EMAILS = _split_csv(os.getenv("OIDC_ALLOWED_EMAILS", ""))
ALLOWED_GROUPS = _split_csv(os.getenv("OIDC_ALLOWED_GROUPS", ""))


def safe_next(value: Optional[str]) -> str:
    r"""Constrain a post-login redirect to a same-origin path.

    `startswith("/")` alone admits `//evil.com` and `/\evil.com`, which
    browsers parse as protocol-relative or backslash-host URLs.
    """
    if not value or not value.startswith("/"):
        return "/"
    if value.startswith("//") or value.startswith("/\\"):
        return "/"
    return value


_oauth: Optional[OAuth] = None


def configure(app) -> None:
    """Wire session middleware + (in oidc mode) the OAuth client onto the app."""
    if AUTH_MODE == "none":
        return
    if AUTH_MODE != "oidc":
        raise RuntimeError(f"AUTH_MODE must be 'oidc' or 'none', got {AUTH_MODE!r}")

    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET must be set when AUTH_MODE=oidc")

    issuer = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
    client_id = os.getenv("OIDC_CLIENT_ID", "")
    client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    if not (issuer and client_id and client_secret):
        raise RuntimeError(
            "OIDC_ISSUER_URL, OIDC_CLIENT_ID, and OIDC_CLIENT_SECRET are required"
        )

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        same_site="lax",
        https_only=os.getenv("SESSION_COOKIE_INSECURE", "").lower() != "true",
        max_age=SESSION_MAX_AGE,
    )

    global _oauth
    _oauth = OAuth()
    _oauth.register(
        name="oidc",
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_id=client_id,
        client_secret=client_secret,
        client_kwargs={"scope": "openid email profile"},
    )


def _user_allowed(user: dict) -> bool:
    if ALLOWED_EMAILS:
        if (user.get("email") or "").lower() not in {e.lower() for e in ALLOWED_EMAILS}:
            return False
    if ALLOWED_GROUPS:
        groups = set(user.get("groups") or [])
        if not (groups & ALLOWED_GROUPS):
            return False
    return True


def current_user(request: Request) -> Optional[dict]:
    """Return the logged-in user dict or None.

    In AUTH_MODE=none returns a synthetic anonymous user so downstream code
    can keep treating "user" as always-present.
    """
    if AUTH_MODE == "none":
        return {"sub": "anonymous", "email": "anonymous@localhost", "name": "anonymous"}
    return request.session.get("user")


def require_user(request: Request) -> dict:
    """FastAPI dependency: 401/redirect if not logged in."""
    user = current_user(request)
    if user is not None:
        return user

    accept = request.headers.get("accept", "")
    wants_html = "text/html" in accept and "application/json" not in accept
    if wants_html and request.method == "GET":
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        # Re-validate to be safe and percent-encode so an `&next=...` smuggled
        # into the original query can't redefine the login redirect target.
        encoded = urlencode({"next": safe_next(next_url)})
        raise HTTPException(
            status_code=302,
            headers={"location": f"/auth/login?{encoded}"},
        )
    raise HTTPException(status_code=401, detail="authentication required")


def install_routes(app) -> None:
    """Register /auth/login, /auth/callback, /auth/logout."""
    if AUTH_MODE == "none":
        return

    @app.get("/auth/login")
    async def login(request: Request, next: str = "/"):
        request.session["post_login_next"] = safe_next(next)
        redirect_uri = os.getenv("OIDC_REDIRECT_URI") or str(request.url_for("auth_callback"))
        return await _oauth.oidc.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback", name="auth_callback")
    async def callback(request: Request):
        try:
            token = await _oauth.oidc.authorize_access_token(request)
        except OAuthError as e:
            return JSONResponse({"error": "oauth_error", "detail": str(e)}, status_code=400)

        userinfo = token.get("userinfo") or {}
        user = {
            "sub": userinfo.get("sub"),
            "email": userinfo.get("email"),
            "name": userinfo.get("name") or userinfo.get("preferred_username"),
            "groups": userinfo.get("groups") or [],
        }
        if not user["sub"]:
            return JSONResponse({"error": "missing_sub"}, status_code=400)
        if not _user_allowed(user):
            return JSONResponse(
                {"error": "forbidden", "detail": "Account not permitted to use this instance."},
                status_code=403,
            )

        request.session["user"] = user
        # Keep id_token for upstream logout (RP-initiated logout).
        if id_token := token.get("id_token"):
            request.session["id_token"] = id_token

        next_url = safe_next(request.session.pop("post_login_next", "/"))
        return RedirectResponse(url=next_url, status_code=302)

    @app.get("/auth/logout")
    async def logout(request: Request):
        id_token = request.session.get("id_token")
        request.session.clear()

        # If the IdP advertises end_session_endpoint, do RP-initiated logout
        # so the SSO session dies too. Otherwise just bounce home.
        try:
            metadata = await _oauth.oidc.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
        except Exception:
            end_session = None

        if end_session:
            params = {"post_logout_redirect_uri": str(request.base_url)}
            if id_token:
                params["id_token_hint"] = id_token
            return RedirectResponse(url=f"{end_session}?{urlencode(params)}", status_code=302)
        return RedirectResponse(url="/", status_code=302)
