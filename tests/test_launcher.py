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


# ─── browser auto-open precedence matrix ──────────────────────────────


def _make_args(open_browser: object) -> object:
    import argparse
    ns = argparse.Namespace()
    ns.open_browser = open_browser
    return ns


def test_resolve_open_browser_cli_flag_wins(launcher, monkeypatch) -> None:
    """--no-browser and CLAUDE_WEB_OPEN_BROWSER=true together: the CLI
    flag wins. Otherwise an env var in the user's shell could silently
    re-enable an opt-out they explicitly requested."""
    monkeypatch.setenv("CLAUDE_WEB_OPEN_BROWSER", "true")
    assert launcher._resolve_open_browser(_make_args(False), first_run=True) is False
    assert launcher._resolve_open_browser(_make_args(True), first_run=False) is True


def test_resolve_open_browser_env_var_overrides_first_run(launcher, monkeypatch) -> None:
    """A persistent CLAUDE_WEB_OPEN_BROWSER=false keeps the browser
    suppressed even on first-run (useful for headless/CI bootstraps)."""
    monkeypatch.setenv("CLAUDE_WEB_OPEN_BROWSER", "false")
    assert launcher._resolve_open_browser(_make_args(None), first_run=True) is False


def test_resolve_open_browser_defaults_open_on_first_run_only(launcher, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_WEB_OPEN_BROWSER", raising=False)
    assert launcher._resolve_open_browser(_make_args(None), first_run=True) is True
    assert launcher._resolve_open_browser(_make_args(None), first_run=False) is False


def test_open_browser_when_ready_calls_webbrowser_on_200(launcher, monkeypatch) -> None:
    """Once /healthz responds 200 the helper must call webbrowser.open
    exactly once with the /setup URL — no retries, no extra hits."""
    calls: list[str] = []

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_urlopen(url, timeout):
        return FakeResponse()

    def fake_open(url, new=0):
        calls.append(url)
        return True

    monkeypatch.setattr(launcher, "urlopen", fake_urlopen)
    monkeypatch.setattr(launcher.webbrowser, "open", fake_open)
    launcher._open_browser_when_ready("127.0.0.1", 3001, "/setup", timeout_s=1.0)
    assert calls == ["http://127.0.0.1:3001/setup"]


def test_open_browser_when_ready_rewrites_wildcard_host(launcher, monkeypatch) -> None:
    """Server binding to 0.0.0.0 means "all interfaces", which isn't a
    valid URL host on Windows. The browser open must rewrite to
    127.0.0.1 so the URL is actually clickable."""
    calls: list[str] = []

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(launcher, "urlopen", lambda url, timeout: FakeResponse())
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url, new=0: calls.append(url) or True)
    launcher._open_browser_when_ready("0.0.0.0", 3001, "/setup", timeout_s=1.0)
    assert calls == ["http://127.0.0.1:3001/setup"]


def test_open_browser_when_ready_times_out_quietly(launcher, monkeypatch) -> None:
    """If /healthz never responds, the helper must give up instead of
    looping forever — daemon thread or not, a stuck poll on shutdown
    masks real errors."""
    from urllib.error import URLError

    def always_fail(url, timeout):
        raise URLError("nope")

    calls: list[str] = []
    monkeypatch.setattr(launcher, "urlopen", always_fail)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url, new=0: calls.append(url) or True)
    launcher._open_browser_when_ready("127.0.0.1", 3001, "/setup", timeout_s=0.5)
    assert calls == []
