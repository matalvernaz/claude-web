"""setup_flow.is_configured / save_api_key / sign_out behaviour."""
from __future__ import annotations

import os
import importlib

import pytest


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


def test_is_configured_true_with_persisted_api_key(fresh_setup_flow, monkeypatch) -> None:
    """Regression: previously is_configured() returned False between
    save_api_key() and the next load_api_key_into_env(), even though the
    file existed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fresh_setup_flow.save_api_key("sk-stored")
    # Simulate a fresh process: clear the env var that save_api_key set so
    # is_configured has to fall back to the file.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert fresh_setup_flow.is_configured() is True


def test_save_api_key_rejects_empty(fresh_setup_flow) -> None:
    with pytest.raises(ValueError):
        fresh_setup_flow.save_api_key("   ")


def test_save_api_key_writes_mode_600(fresh_setup_flow) -> None:
    fresh_setup_flow.save_api_key("sk-mode")
    mode = oct(fresh_setup_flow.API_KEY_FILE.stat().st_mode)[-3:]
    assert mode == "600"
