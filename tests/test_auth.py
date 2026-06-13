"""Auth helper tests: safe_next open-redirect protection + origin derivation."""
from __future__ import annotations

import pytest

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
