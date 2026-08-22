"""CLI auto-update: `claude update` runner state machine + admin endpoints."""
from __future__ import annotations

import asyncio
import stat
import sys

import pytest

import app as app_module

_ORIGIN = {"Origin": "http://testserver"}

# The fake CLI is a bash script; Windows can't exec it, so the subprocess
# tests are POSIX-only. The endpoint and no-CLI tests run everywhere.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="fake CLI is a shell script")


def _reset_state() -> None:
    app_module.CLI_UPDATE_STATE.update(
        status="never", version=None, previous_version=None,
        checked_at=None, updated_at=None, source=None, detail="",
    )


def _fake_cli(tmp_path, before: str, after: str,
              update_body: str | None = None) -> str:
    """Executable reporting `before` until its `update` subcommand runs."""
    marker = tmp_path / "updated-marker"
    script = tmp_path / "claude"
    if update_body is None:
        update_body = f'echo "Updating..."; touch "{marker}"; exit 0'
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  if [ -e "{marker}" ]; then echo "{after}"; else echo "{before}"; fi\n'
        "  exit 0\n"
        "fi\n"
        f'if [ "$1" = "update" ]; then {update_body}\nfi\n'
        "exit 2\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _run_update(source: str = "test") -> dict:
    # asyncio primitives bind to the running loop, and each asyncio.run()
    # here is a fresh loop — so hand the module a fresh lock alongside it.
    app_module._CLI_UPDATE_LOCK = asyncio.Lock()
    return asyncio.run(app_module._run_cli_update(source))


@_POSIX_ONLY
def test_update_swaps_version(tmp_path, monkeypatch):
    _reset_state()
    cli = _fake_cli(tmp_path, "2.1.240 (Claude Code)", "2.1.250 (Claude Code)")
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: cli)
    out = _run_update()
    assert out["status"] == "updated"
    assert out["version"] == "2.1.250 (Claude Code)"
    assert out["previous_version"] == "2.1.240 (Claude Code)"
    assert out["updated_at"] is not None
    assert out["source"] == "test"


@_POSIX_ONLY
def test_update_already_current(tmp_path, monkeypatch):
    _reset_state()
    cli = _fake_cli(tmp_path, "2.1.250 (Claude Code)", "2.1.250 (Claude Code)")
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: cli)
    out = _run_update()
    assert out["status"] == "current"
    assert out["version"] == "2.1.250 (Claude Code)"
    assert out["updated_at"] is None
    assert out["previous_version"] is None


@_POSIX_ONLY
def test_update_installer_failure(tmp_path, monkeypatch):
    _reset_state()
    cli = _fake_cli(tmp_path, "2.1.240 (Claude Code)", "2.1.240 (Claude Code)",
                    update_body='echo "boom: no network" >&2; exit 1')
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: cli)
    out = _run_update()
    assert out["status"] == "error"
    assert "boom" in out["detail"]
    assert out["version"] == "2.1.240 (Claude Code)"


def test_update_without_cli(monkeypatch):
    _reset_state()
    monkeypatch.setattr(app_module.shutil, "which", lambda _name: None)
    out = _run_update()
    assert out["status"] == "no_cli"
    assert out["checked_at"] is not None


def test_admin_update_cli_endpoints(client, monkeypatch):
    _reset_state()

    async def fake_update(source: str) -> dict:
        return {"status": "current", "version": "2.1.250 (Claude Code)",
                "source": source}

    monkeypatch.setattr(app_module, "_run_cli_update", fake_update)

    r = client.post("/api/admin/update-cli", headers=_ORIGIN)
    assert r.status_code == 200
    assert r.json()["status"] == "current"
    assert r.json()["source"].startswith("api:")

    # GET reports the module state without triggering an update.
    r = client.get("/api/admin/update-cli", headers=_ORIGIN)
    assert r.status_code == 200
    assert r.json()["status"] == "never"
