"""The codex CLI keeps itself current, and refuses an update that would break
the app-server protocol claude-web speaks.

OpenAI lists models to the app-server by client version: gpt-6.1-sol, the new
default, was invisible to codex 0.157.1 and appeared on 0.160.0 with no other
change (2026-10-04). Without an updater the newest OpenAI model only arrives
when someone notices.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import app as app_module
import codex_provider


def _layout(tmp_path: Path) -> Path:
    """An npm --prefix install like ~/.local: bin/codex -> lib/node_modules/..."""
    prefix = tmp_path / "prefix"
    script = prefix / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (prefix / "bin").mkdir()
    os.symlink("../lib/node_modules/@openai/codex/bin/codex.js", prefix / "bin" / "codex")
    return prefix


def test_npm_prefix_finds_a_user_install(tmp_path) -> None:
    prefix = _layout(tmp_path)
    assert codex_provider.npm_prefix(str(prefix / "bin" / "codex")) == prefix


def test_npm_prefix_refuses_anything_else(tmp_path) -> None:
    standalone = tmp_path / "usr" / "bin" / "codex"
    standalone.parent.mkdir(parents=True)
    standalone.write_text("binary", encoding="utf-8")
    assert codex_provider.npm_prefix(str(standalone)) is None


def _schema_dir(tmp_path: Path, drop: str = "") -> Path:
    out = tmp_path / "schema"
    out.mkdir()
    for fname, methods in codex_provider.REQUIRED_PROTOCOL.items():
        enum = [m for m in methods if m != drop] + ["something/else"]
        (out / f"{fname}.json").write_text(json.dumps({
            "oneOf": [{"properties": {"method": {"enum": enum}}}],
        }), encoding="utf-8")
    return out


def test_a_complete_schema_misses_nothing(tmp_path) -> None:
    assert codex_provider.missing_from_schema_dir(_schema_dir(tmp_path)) == []


def test_a_dropped_method_is_reported(tmp_path) -> None:
    out = _schema_dir(tmp_path, drop="turn/steer")
    assert codex_provider.missing_from_schema_dir(out) == ["turn/steer"]


async def test_the_schema_comes_from_the_cli_itself(tmp_path) -> None:
    # A stand-in CLI that writes the schema files where it is told.
    source = _schema_dir(tmp_path, drop="item/fileChange/requestApproval")
    fake = tmp_path / "codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import shutil, sys\n"
        "out = sys.argv[sys.argv.index('--out') + 1]\n"
        f"shutil.copytree({str(source)!r}, out, dirs_exist_ok=True)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    if os.name == "nt":
        # Windows does not execute a Python shebang. Keep a real child
        # process in this test by launching the script through a cmd shim.
        shim = tmp_path / "codex.cmd"
        shim.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        fake = shim
    assert await codex_provider.missing_protocol_methods(str(fake)) == [
        "item/fileChange/requestApproval"]


# ─── The update pass ─────────────────────────────────────────────────────────

class _Shell:
    """Stands in for app._codex_proc: scripted outputs, recorded calls."""

    def __init__(self, before: str, latest: str, after: str, install_rc: int = 0):
        self.before, self.latest, self.after = before, latest, after
        self.install_rc = install_rc
        self.calls: list[tuple] = []
        self.installed = False

    async def __call__(self, *args, timeout):
        self.calls.append(args)
        if args[1:] == ("--version",):
            return 0, f"codex-cli {self.after if self.installed else self.before}"
        if args[1:3] == ("view", "@openai/codex"):
            return 0, self.latest + "\n"
        if args[1] == "install":
            self.installed = args[-1].endswith(self.after)
            return self.install_rc, "added 2 packages"
        raise AssertionError(args)


@pytest.fixture
def update_env(monkeypatch, tmp_path):
    prefix = _layout(tmp_path)
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: str(prefix / "bin" / "codex"))
    monkeypatch.setattr(app_module.shutil, "which",
                        lambda name, *a, **k: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(app_module, "PUSHOVER_TOKEN", "")
    missing: list[str] = []

    async def _missing(_binary):
        return list(missing)

    monkeypatch.setattr(codex_provider, "missing_protocol_methods", _missing)
    recycled: list[float] = []

    async def _recycle(updated_at):
        recycled.append(updated_at)
        return []

    monkeypatch.setattr(app_module, "_recycle_stale_codex_servers", _recycle)

    def install(shell):
        monkeypatch.setattr(app_module, "_codex_proc", shell)
        return shell

    return {"prefix": prefix, "missing": missing, "recycled": recycled, "install": install}


async def test_a_current_cli_is_left_alone(update_env) -> None:
    shell = update_env["install"](_Shell("0.160.0", "0.160.0", "0.160.0"))
    state = await app_module._run_codex_update("test")
    assert state["status"] == "current"
    assert not any(c[1] == "install" for c in shell.calls)


async def test_a_newer_release_is_installed_under_the_same_prefix(update_env) -> None:
    shell = update_env["install"](_Shell("0.157.1", "0.160.0", "0.160.0"))
    state = await app_module._run_codex_update("test")
    assert state["status"] == "updated"
    assert (state["previous_version"], state["version"]) == ("0.157.1", "0.160.0")
    install = next(c for c in shell.calls if c[1] == "install")
    assert install[1:] == ("install", "-g", "--prefix", str(update_env["prefix"]),
                           "@openai/codex@0.160.0")
    # Idle account servers are recycled so the next model list is the new CLI's.
    assert update_env["recycled"] == [state["updated_at"]]


async def test_an_update_that_drops_a_method_is_rolled_back(update_env) -> None:
    update_env["missing"].append("turn/steer")
    shell = update_env["install"](_Shell("0.157.1", "0.170.0", "0.170.0"))
    state = await app_module._run_codex_update("test")
    assert state["status"] == "rolled_back"
    assert "turn/steer" in state["detail"]
    installs = [c[-1] for c in shell.calls if c[1] == "install"]
    assert installs == ["@openai/codex@0.170.0", "@openai/codex@0.157.1"]
    assert update_env["recycled"] == []


async def test_an_older_registry_version_is_never_installed(update_env) -> None:
    # A locally installed pre-release can be ahead of npm's latest tag.
    shell = update_env["install"](_Shell("0.161.0", "0.160.0", "0.160.0"))
    state = await app_module._run_codex_update("test")
    assert state["status"] == "current"
    assert not any(c[1] == "install" for c in shell.calls)


async def test_a_failed_install_reports_an_error(update_env) -> None:
    update_env["install"](_Shell("0.157.1", "0.160.0", "0.160.0", install_rc=1))
    state = await app_module._run_codex_update("test")
    assert state["status"] == "error"


async def test_an_install_it_cannot_manage_is_skipped(monkeypatch, tmp_path) -> None:
    standalone = tmp_path / "codex"
    standalone.write_text("binary", encoding="utf-8")
    monkeypatch.setattr(codex_provider, "codex_binary", lambda: str(standalone))
    calls = []

    async def _never(*args, timeout):
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(app_module, "_codex_proc", _never)
    state = await app_module._run_codex_update("test")
    assert state["status"] == "unmanaged"
    assert calls == []


# ─── Recycling after an update ───────────────────────────────────────────────

class _Proc:
    returncode = None


def _server(key: str, started_at: float) -> codex_provider.CodexAppServer:
    inst = codex_provider.CodexAppServer(key=key)
    inst.proc = _Proc()
    inst.started_at = started_at
    return inst


async def test_only_idle_account_servers_from_before_the_update_are_recycled(monkeypatch) -> None:
    updated_at = time.time()
    servers = {
        "shared": _server("shared", updated_at - 100),
        "shared:run:abc": _server("shared:run:abc", updated_at - 100),
        "cred:x:1": _server("cred:x:1", updated_at - 100),
        "cred:x:2": _server("cred:x:2", updated_at + 5),
    }
    servers["cred:x:1"]._pending[1] = object()  # a request in flight
    monkeypatch.setattr(codex_provider.CodexAppServer, "_instances", servers)
    closed: list[str] = []

    async def _close(key):
        closed.append(key)

    monkeypatch.setattr(codex_provider.CodexAppServer, "close_key", _close)
    assert await app_module._recycle_stale_codex_servers(updated_at) == ["shared"]
    assert closed == ["shared"]


def test_a_server_waiting_on_a_device_code_login_is_not_idle() -> None:
    inst = _server("shared", 0)
    assert inst.idle()
    inst._login_started_at = time.time()
    assert not inst.idle()


async def test_the_model_list_is_refetched_after_its_ttl(monkeypatch) -> None:
    inst = _server("shared", 0)
    calls = []

    async def _request(method, params=None, timeout=None):
        calls.append(method)
        return {"data": [{"id": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol"}]}

    monkeypatch.setattr(inst, "request", _request)
    await inst.models()
    await inst.models()
    assert calls == ["model/list"]
    inst._models_cached_at -= codex_provider.MODELS_CACHE_TTL_S + 1
    await inst.models()
    assert calls == ["model/list", "model/list"]
