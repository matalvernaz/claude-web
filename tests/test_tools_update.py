"""The bundled-tools updater in app.py: one pass over codex, Claude Code and
Git, the codex protocol gate, the Claude fallback rule, and the endpoints."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app as app_module
import codex_provider
import portable_tools

_ORIGIN = {"Origin": "http://testserver"}


def _candidate(tool: str, version: str) -> portable_tools.Candidate:
    return portable_tools.Candidate(tool=tool, version=version, label=f"v{version}",
                                    url=f"https://x/{tool}", size=1, sha256=None)


@pytest.fixture
def tools(monkeypatch, tmp_path):
    """A scripted portable_tools: no network, no disk, every call recorded."""
    script = {"installed": {}, "source": {}, "latest": {}, "due": {}, "missing": [],
              "fallback": (None, None), "busy": [], "install_error": None}
    calls: list[tuple] = []
    base = tmp_path / "tools"

    def check(name, base_=None):
        candidate = script["latest"][name]
        return {"tool": name, "installed": script["installed"].get(name),
                "source": script["source"].get(name), "exe": None,
                "latest": candidate, "due": script["due"].get(name, False)}

    def install(name, candidate, base_=None, progress_cb=None):
        calls.append(("install", name, candidate.version))
        if script["install_error"]:
            raise RuntimeError(script["install_error"])
        directory = base / name / candidate.version
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def record(what):
        def _f(name, *args, **kwargs):
            calls.append((what, name) + tuple(a for a in args if not isinstance(a, Path)))
            return [] if what in ("retire_legacy", "cleanup") else True
        return _f

    monkeypatch.setattr(app_module.portable_tools, "check", check)
    monkeypatch.setattr(app_module.portable_tools, "install", install)
    # The host running the suite may itself point CLAUDE_WEB_CODEX_BIN or the
    # Git Bash path somewhere; nothing here is overridden unless a test says so.
    monkeypatch.setattr(app_module.portable_tools, "is_overridden",
                        lambda name, base=None, environ=None: False)
    for what in ("activate", "apply_env", "retire_legacy", "cleanup", "discard"):
        monkeypatch.setattr(app_module.portable_tools, what, record(what))

    async def missing(_binary):
        return list(script["missing"])

    monkeypatch.setattr(codex_provider, "missing_protocol_methods", missing)
    recycled: list[float] = []

    async def recycle(updated_at):
        recycled.append(updated_at)
        return []

    monkeypatch.setattr(app_module, "_recycle_stale_codex_servers", recycle)
    monkeypatch.setattr(app_module, "_busy_runs", lambda: list(script["busy"]))

    async def fallback():
        return script["fallback"]

    monkeypatch.setattr(app_module, "_fallback_claude_version", fallback)
    monkeypatch.setattr(app_module, "PUSHOVER_TOKEN", "")
    monkeypatch.setattr(app_module, "_TOOLS_UPDATE_LOCK", asyncio.Lock())
    monkeypatch.setattr(app_module, "_TOOLS_REJECTED", {})
    for name in portable_tools.TOOL_NAMES:
        app_module.TOOLS_UPDATE_STATE["tools"][name] = app_module._tools_state_blank()
    script["calls"] = calls
    script["recycled"] = recycled
    return script


def _run(names):
    return asyncio.run(app_module._run_tools_update("test", tuple(names)))


def test_a_current_tool_is_left_alone(tools):
    tools["latest"]["git"] = _candidate("git", "2.56.0.2")
    tools["installed"]["git"] = "2.56.0.2"
    tools["source"]["git"] = "managed"
    out = _run(["git"])
    assert out["tools"]["git"]["status"] == "current"
    assert out["tools"]["git"]["latest"]["version"] == "2.56.0.2"
    assert out["tools"]["git"]["version"] == "2.56.0.2"
    assert out["source"] == "test" and out["checked_at"]
    assert tools["calls"] == []


def test_a_newer_codex_is_installed_checked_activated_and_spawned_from(tools):
    tools["latest"]["codex"] = _candidate("codex", "0.161.0")
    tools["installed"]["codex"] = "0.160.1"
    tools["source"]["codex"] = "legacy"
    tools["due"]["codex"] = True
    out = _run(["codex"])
    state = out["tools"]["codex"]
    assert state["status"] == "updated"
    assert (state["previous_version"], state["version"], state["source"]) == ("0.160.1", "0.161.0", "managed")
    assert state["updated_at"]
    assert [c[0] for c in tools["calls"]] == [
        "install", "activate", "apply_env", "retire_legacy", "cleanup"]
    assert tools["calls"][1] == ("activate", "codex", "0.161.0")
    # Idle account servers are recycled so the next model list is the new CLI's.
    assert tools["recycled"] == [state["updated_at"]]


def test_a_codex_that_drops_a_method_is_discarded_and_not_retried(tools):
    tools["latest"]["codex"] = _candidate("codex", "0.170.0")
    tools["installed"]["codex"] = "0.160.1"
    tools["source"]["codex"] = "managed"
    tools["due"]["codex"] = True
    tools["missing"].append("turn/steer")
    state = _run(["codex"])["tools"]["codex"]
    assert state["status"] == "rejected" and "turn/steer" in state["detail"]
    assert [c[0] for c in tools["calls"]] == ["install", "discard"]
    assert tools["calls"][1] == ("discard", "codex", "0.170.0")
    assert tools["recycled"] == []
    # The next pass does not download the same release again.
    tools["calls"].clear()
    state = _run(["codex"])["tools"]["codex"]
    assert state["status"] == "rejected" and tools["calls"] == []
    # A newer release is tried.
    tools["latest"]["codex"] = _candidate("codex", "0.171.0")
    tools["missing"].clear()
    assert _run(["codex"])["tools"]["codex"]["status"] == "updated"


def test_claude_is_not_downloaded_while_the_fallback_copy_is_current(tools):
    tools["latest"]["claude"] = _candidate("claude", "2.1.292")
    tools["due"]["claude"] = True
    tools["fallback"] = ("2.1.292", "bundled")
    state = _run(["claude"])["tools"]["claude"]
    assert state["status"] == "fallback" and "bundled claude 2.1.292" in state["detail"]
    assert tools["calls"] == []
    tools["fallback"] = ("2.1.280", "system")
    state = _run(["claude"])["tools"]["claude"]
    assert state["status"] == "updated" and state["version"] == "2.1.292"
    assert tools["calls"][0] == ("install", "claude", "2.1.292")


def test_a_failed_install_is_reported_and_the_pass_continues(tools):
    tools["latest"]["codex"] = _candidate("codex", "0.161.0")
    tools["due"]["codex"] = True
    tools["install_error"] = "disk full"
    tools["latest"]["git"] = _candidate("git", "2.56.0.2")
    tools["installed"]["git"] = "2.56.0.2"
    out = _run(["codex", "git"])
    assert out["tools"]["codex"]["status"] == "error"
    assert "disk full" in out["tools"]["codex"]["detail"]
    assert out["tools"]["git"]["status"] == "current"


def test_a_feed_outage_is_an_error_not_a_crash(tools, monkeypatch):
    def boom(name, base=None):
        raise OSError("api.github.com unreachable")

    monkeypatch.setattr(app_module.portable_tools, "check", boom)
    state = _run(["git"])["tools"]["git"]
    assert state["status"] == "error" and "unreachable" in state["detail"]


def test_git_cleanup_waits_for_an_idle_moment(tools):
    tools["latest"]["git"] = _candidate("git", "2.57.0.1")
    tools["installed"]["git"] = "2.56.0.2"
    tools["source"]["git"] = "legacy"
    tools["due"]["git"] = True
    tools["busy"].append("run-1")
    state = _run(["git"])["tools"]["git"]
    assert state["status"] == "updated"
    kinds = [c[0] for c in tools["calls"]]
    assert "retire_legacy" in kinds and "cleanup" not in kinds
    assert tools["recycled"] == []  # only codex servers are recycled


def test_update_tools_endpoints(client, monkeypatch):
    r = client.get("/api/admin/update-tools", headers=_ORIGIN)
    assert r.status_code == 200
    body = r.json()
    assert set(body["tools"]) == set(portable_tools.TOOL_NAMES)
    assert body["tools"]["codex"]["status"] in ("never", "current")
    # Not a portable build: nothing to update.
    monkeypatch.setattr(app_module.portable_tools, "enabled", lambda: False)
    assert client.post("/api/admin/update-tools", headers=_ORIGIN).status_code == 409
    monkeypatch.setattr(app_module.portable_tools, "enabled", lambda: True)
    monkeypatch.setattr(app_module.portable_tools, "status", lambda base=None: {"codex": {"version": "0.160.1"}})
    seen = []

    async def fake_run(source, names=None):
        seen.append(source)
        return {"tools": {}, "source": source}

    monkeypatch.setattr(app_module, "_run_tools_update", fake_run)
    r = client.post("/api/admin/update-tools", headers=_ORIGIN)
    assert r.status_code == 200 and seen and seen[0].startswith("api:")
    r = client.get("/api/admin/update-tools", headers=_ORIGIN)
    assert r.json()["on_disk"]["codex"]["version"] == "0.160.1"


def test_an_operators_own_copy_is_never_replaced(tools, monkeypatch):
    tools["latest"]["codex"] = _candidate("codex", "0.161.0")
    tools["due"]["codex"] = True
    monkeypatch.setattr(app_module.portable_tools, "is_overridden", lambda name, base=None, environ=None: name == "codex")
    state = _run(["codex"])["tools"]["codex"]
    assert state["status"] == "unmanaged"
    assert tools["calls"] == []
