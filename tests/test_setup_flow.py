"""setup_flow.is_configured / save_api_key / sign_out behaviour."""
from __future__ import annotations

import os
import importlib

import pytest


_IS_WINDOWS = os.name == "nt"


@pytest.fixture
def fresh_setup_flow(tmp_path, monkeypatch):
    """Re-import setup_flow with a fresh STATE_DIR so tests don't share state."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    state.mkdir()
    home.mkdir()
    monkeypatch.setenv("CLAUDE_WEB_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import setup_flow
    importlib.reload(setup_flow)
    return setup_flow


def test_is_configured_false_when_nothing_present(fresh_setup_flow) -> None:
    assert fresh_setup_flow.is_configured() is False
    assert fresh_setup_flow.whoami() == {"mode": "none"}


def test_is_configured_true_with_env_var(fresh_setup_flow, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert fresh_setup_flow.is_configured() is True
    assert fresh_setup_flow.whoami() == {"mode": "api_key"}


# Realistic-looking key shape (sk-ant- + 90 url-safe chars). The format
# validator only checks structure; the value never reaches the network in
# tests, so any well-shaped string works.
_FAKE_KEY = "sk-ant-" + "a" * 90


def test_is_configured_true_with_persisted_api_key(fresh_setup_flow, monkeypatch) -> None:
    """Regression: previously is_configured() returned False between
    save_api_key() and the next load_api_key_into_env(), even though the
    file existed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    # Simulate a fresh process: clear the env var that save_api_key set so
    # is_configured has to fall back to the file.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert fresh_setup_flow.is_configured() is True


def test_save_api_key_rejects_empty(fresh_setup_flow) -> None:
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("   ")


def test_save_api_key_rejects_malformed(fresh_setup_flow) -> None:
    """A pasted bash export line or junk text should be rejected at the
    boundary rather than failing the first API call with an opaque 401."""
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("not-a-real-key")
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("export ANTHROPIC_API_KEY=sk-ant-abc")


@pytest.mark.skipif(_IS_WINDOWS, reason="POSIX file modes don't apply on NTFS")
def test_save_api_key_writes_mode_600(fresh_setup_flow) -> None:
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    mode = oct(fresh_setup_flow.API_KEY_FILE.stat().st_mode)[-3:]
    assert mode == "600"
    # The per-home copy must also be 0o600 — that's the one the SDK reads
    # for per-credential slots, where a permissive mode would matter on a
    # shared host.
    home_copy = fresh_setup_flow.api_key_path(fresh_setup_flow.CLAUDE_HOME)
    assert oct(home_copy.stat().st_mode)[-3:] == "600"


def test_save_api_key_persists_on_windows(fresh_setup_flow) -> None:
    """The atomic-write path must still produce a readable file on Windows
    even though the chmod step is skipped. The directory permissions on
    %USERPROFILE% are the real access boundary there."""
    fresh_setup_flow.save_api_key(_FAKE_KEY)
    assert fresh_setup_flow.API_KEY_FILE.read_text() == _FAKE_KEY
    home_copy = fresh_setup_flow.api_key_path(fresh_setup_flow.CLAUDE_HOME)
    assert home_copy.read_text() == _FAKE_KEY
