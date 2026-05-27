"""Launcher entry-point helpers.

The launcher is what users on Windows actually invoke (double-click the
frozen exe). A regression here means people see a flashing console and
nothing else, so the helpers get unit coverage even though they're tiny.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Import launcher with a frozen-style binary dir pointed at a tmp."""
    # Pretend we're a PyInstaller-frozen exe living in tmp_path. The
    # launcher reads ``sys.frozen`` + ``sys.executable`` to decide where
    # to look for .env / .env.example; setting both moves the lookup off
    # the source tree's real ./.env so each test starts hermetic.
    fake_exe = tmp_path / "claude-web.exe"
    fake_exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    # Clear every var the bootstrap branches on so test order can't leak
    # a prior test's setenv into _looks_unconfigured.
    for var in (
        "AUTH_MODE", "SESSION_SECRET",
        "OIDC_ISSUER_URL", "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI",
        "FOO_NEW_KEY", "OVERRIDE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    import importlib
    import launcher as launcher_mod
    importlib.reload(launcher_mod)
    return launcher_mod


def test_binary_dir_is_executable_parent_when_frozen(launcher, tmp_path) -> None:
    assert launcher._binary_dir() == tmp_path


def test_load_dotenv_reads_kv_from_binary_dir(launcher, tmp_path) -> None:
    (tmp_path / ".env").write_text(textwrap.dedent("""
        # comment line
        FOO_NEW_KEY=hello

        OVERRIDE_KEY="quoted value"
        export EXPORTED_KEY=ok
    """).strip())
    loaded = launcher._load_dotenv_files()
    assert tmp_path / ".env" in [Path(p) for p in loaded]
    assert os.environ["FOO_NEW_KEY"] == "hello"
    assert os.environ["OVERRIDE_KEY"] == "quoted value"
    assert os.environ["EXPORTED_KEY"] == "ok"


def test_load_dotenv_respects_existing_env(launcher, tmp_path, monkeypatch) -> None:
    """If the env var is already set in the shell, the file must not
    overwrite it. This is what lets an operator override a bundled
    .env from systemd / docker-compose / a launching script."""
    monkeypatch.setenv("OVERRIDE_KEY", "shell-wins")
    (tmp_path / ".env").write_text("OVERRIDE_KEY=file-loses\n")
    launcher._load_dotenv_files()
    assert os.environ["OVERRIDE_KEY"] == "shell-wins"


def test_looks_unconfigured_true_on_clean_env(launcher) -> None:
    assert launcher._looks_unconfigured() is True


def test_looks_unconfigured_false_when_any_auth_var_set(launcher, monkeypatch) -> None:
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://example/")
    assert launcher._looks_unconfigured() is False


def test_looks_unconfigured_false_when_auth_mode_set(launcher, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    assert launcher._looks_unconfigured() is False
