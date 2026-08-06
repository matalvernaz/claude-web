"""Auth tests: safe_next open-redirect protection, origin derivation,
allowlist gating, and the rolling session window."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import auth


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/foo", "/foo"),
        ("/foo?bar=1", "/foo?bar=1"),
        # Protocol-relative URL — browsers fetch from `evil.com`.
        ("//evil.com", "/"),
        # Backslash-host trick — some browsers normalise to absolute.
        ("/\\evil.com", "/"),
        # External absolute — must reject.
        ("https://evil.com/", "/"),
        ("javascript:alert(1)", "/"),
        # Browsers strip whitespace from Location headers — `/<TAB>/evil.com`
        # becomes `//evil.com` post-strip, opening a protocol-relative redirect
        # if we only checked the leading "//" form.
        ("/\t/evil.com", "/"),
        ("/\n/evil.com", "/"),
        ("/\r/evil.com", "/"),
        ("/ /evil.com", "/"),  # space at index 1 — same risk
        # urlparse-detected absolute despite leading "/" prefix.
        ("/foo:bar/baz", "/foo:bar/baz"),  # this is fine — no scheme
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert auth.safe_next(value) == expected


def test_expected_origin_uses_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """When OIDC_REDIRECT_URI is configured, expected_origin extracts its
    scheme+host so reverse-proxy deployments don't depend on Host-header
    guessing."""
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://claude.example.com/auth/callback")

    class _Req:
        @property
        def base_url(self):
            class _B:
                def __str__(self_inner):
                    return "http://127.0.0.1:3001/"
            return _B()

    assert auth.expected_origin(_Req()) == "https://claude.example.com"


def test_expected_origin_falls_back_to_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)

    class _Req:
        @property
        def base_url(self):
            class _B:
                def __str__(self_inner):
                    return "http://127.0.0.1:3001/"
            return _B()

    assert auth.expected_origin(_Req()) == "http://127.0.0.1:3001"


# ─── email allowlist requires a verified email ───────────────────────────

def _allowed(monkeypatch, user, emails=("ok@x.com",), require=True, mode="all", groups=()):
    monkeypatch.setattr(auth, "ALLOWED_EMAILS", set(emails))
    monkeypatch.setattr(auth, "ALLOWED_GROUPS", set(groups))
    monkeypatch.setattr(auth, "REQUIRE_VERIFIED_EMAIL", require)
    monkeypatch.setattr(auth, "ALLOWLIST_MODE", mode)
    return auth._user_allowed(user)


def test_allowlisted_email_unverified_rejected(monkeypatch) -> None:
    user = {"email": "ok@x.com", "email_verified": False}
    assert _allowed(monkeypatch, user) is False


def test_allowlisted_email_verified_accepted(monkeypatch) -> None:
    user = {"email": "ok@x.com", "email_verified": True}
    assert _allowed(monkeypatch, user) is True


def test_allowlisted_email_missing_verified_claim_rejected_by_default(monkeypatch) -> None:
    # No email_verified claim at all → treated as unverified when required.
    user = {"email": "ok@x.com"}
    assert _allowed(monkeypatch, user) is False


def test_verified_requirement_can_be_disabled(monkeypatch) -> None:
    user = {"email": "ok@x.com"}  # no claim
    assert _allowed(monkeypatch, user, require=False) is True


def test_non_allowlisted_email_rejected_even_if_verified(monkeypatch) -> None:
    user = {"email": "nope@y.com", "email_verified": True}
    assert _allowed(monkeypatch, user) is False


# ─── Rolling session window ──────────────────────────────────────────────────
# The cookie's max-age is validated against its signing time and Starlette only
# re-signs a *modified* session, so without touch_session a login would expire
# on a fixed clock no matter how much the app was used.

def _fake_request(session: dict):
    """Minimal stand-in exposing just the .session mapping touch_session uses."""
    return SimpleNamespace(session=session)


def test_touch_session_stamps_a_fresh_session() -> None:
    session: dict = {"user": {"sub": "abc"}}
    auth.touch_session(_fake_request(session))
    assert isinstance(session["stamped_at"], int)


def test_touch_session_is_rate_limited_within_the_interval(monkeypatch) -> None:
    """A second call soon after must not rewrite the stamp.

    Rewriting on every request would put Set-Cookie on every response,
    including each SSE stream and poll.
    """
    now = int(time.time())
    session = {"user": {"sub": "abc"}, "stamped_at": now}
    monkeypatch.setattr(auth.time, "time", lambda: now + 5)
    auth.touch_session(_fake_request(session))
    assert session["stamped_at"] == now


def test_touch_session_restamps_after_the_interval(monkeypatch) -> None:
    now = int(time.time())
    session = {"user": {"sub": "abc"}, "stamped_at": now}
    later = now + auth.SESSION_REFRESH_INTERVAL + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    auth.touch_session(_fake_request(session))
    assert session["stamped_at"] == later


def test_touch_session_replaces_a_missing_stamp() -> None:
    """Cookies issued before this behaviour existed carry no stamp.

    They must be adopted into the rolling window rather than left pinned to
    their original signing time.
    """
    session = {"user": {"sub": "abc"}}
    auth.touch_session(_fake_request(session))
    assert "stamped_at" in session


def test_touch_session_replaces_a_future_stamp(monkeypatch) -> None:
    """A stamp ahead of now (clock step) must not suppress refreshes forever."""
    now = int(time.time())
    session = {"user": {"sub": "abc"}, "stamped_at": now + 999_999}
    monkeypatch.setattr(auth.time, "time", lambda: now)
    auth.touch_session(_fake_request(session))
    assert session["stamped_at"] == now


def test_touch_session_replaces_a_non_integer_stamp() -> None:
    session = {"user": {"sub": "abc"}, "stamped_at": "not-a-number"}
    auth.touch_session(_fake_request(session))
    assert isinstance(session["stamped_at"], int)


def test_refresh_interval_is_shorter_than_the_window() -> None:
    """Otherwise the stamp could go stale before it is ever refreshed."""
    assert auth.SESSION_REFRESH_INTERVAL < auth.SESSION_MAX_AGE


def test_authenticated_request_reissues_the_session_cookie(monkeypatch) -> None:
    """End-to-end: the response actually carries a refreshed Set-Cookie.

    This is the behaviour the unit tests above only imply — before
    touch_session, an authenticated read emitted `Vary: Cookie` and no
    Set-Cookie, which is what made the window count down from sign-in.
    """
    monkeypatch.setattr(auth, "AUTH_MODE", "oidc")
    mini = FastAPI()
    mini.add_middleware(
        SessionMiddleware, secret_key="test-secret", max_age=auth.SESSION_MAX_AGE,
    )

    @mini.get("/whoami")
    async def _whoami(user: dict = Depends(auth.require_user)):
        return {"sub": user["sub"]}

    c = TestClient(mini)
    # Seed a logged-in session the way /auth/callback does.
    @mini.get("/seed")
    async def _seed(request: Request):
        request.session["user"] = {"sub": "abc"}
        return {"ok": True}

    assert c.get("/seed").status_code == 200
    first = c.get("/whoami")
    assert first.status_code == 200
    # The seeded cookie already carries a stamp from the /whoami call above,
    # so jump past the interval to force a visible refresh.
    later = int(time.time()) + auth.SESSION_REFRESH_INTERVAL + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    second = c.get("/whoami")
    assert second.status_code == 200
    assert "set-cookie" in {k.lower() for k in second.headers}
