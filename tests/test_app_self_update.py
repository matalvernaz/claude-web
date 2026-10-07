"""App-side self-update: state machine, auto vs notify, restart hook, endpoints."""
from __future__ import annotations

import asyncio

import pytest

import app as app_module
import self_update

_ORIGIN = {"Origin": "http://testserver"}


def _reset(monkeypatch, mode: str = "notify", can_replace: bool = True) -> None:
    monkeypatch.setattr(app_module, "SELF_UPDATE_MODE", mode)
    monkeypatch.setattr(app_module, "_SELF_UPDATE_STAGED", None)
    monkeypatch.setattr(app_module, "_SELF_UPDATE_LOCK", asyncio.Lock())
    monkeypatch.setattr(app_module.self_update, "can_self_replace", lambda install=None: can_replace)
    app_module.SELF_UPDATE_STATE.update(
        status="never", available=None, staged_tag=None, checked_at=None,
        source=None, detail="", mode=mode,
    )
    app_module.cancel_restart()


def _info(tag="v0.5.0"):
    return {"tag": tag, "download_url": "http://x/z.zip", "size": 1, "digest": None,
            "release_url": "http://x/rel", "published_at": "2026-10-08T00:00:00Z", "notes": "n"}


def _staged(tag="v0.5.0"):
    return self_update.Staged(tag=tag, flat_zip="f.zip", extractor="z.exe", workdir="w", created_at="t")


def test_notify_mode_reports_without_staging(monkeypatch):
    _reset(monkeypatch, "notify")
    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: _info())
    monkeypatch.setattr(app_module.self_update, "stage_update", lambda *a, **k: pytest.fail("must not stage"))
    out = asyncio.run(app_module._run_self_update("test"))
    assert out["status"] == "available"
    assert out["available"]["tag"] == "v0.5.0"
    assert app_module.RESTART_STATE["pending"] is False
    assert app_module._SELF_UPDATE_STAGED is None


def test_auto_mode_stages_and_requests_restart(monkeypatch):
    _reset(monkeypatch, "auto")
    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: _info())
    monkeypatch.setattr(app_module.self_update, "stage_update", lambda info, *a, **k: _staged(info["tag"]))
    try:
        out = asyncio.run(app_module._run_self_update("timer"))
        assert out["status"] == "staged" and out["staged_tag"] == "v0.5.0"
        assert app_module.RESTART_STATE["pending"] is True
        assert app_module.RESTART_STATE["source"] == "self-update:v0.5.0"
        assert app_module._SELF_UPDATE_STAGED.tag == "v0.5.0"
        # A second pass while staged does nothing new.
        monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: pytest.fail("no recheck"))
        assert asyncio.run(app_module._run_self_update("timer"))["status"] == "staged"
    finally:
        app_module.cancel_restart()
        app_module._SELF_UPDATE_STAGED = None


def test_install_request_stages_in_notify_mode(monkeypatch):
    _reset(monkeypatch, "notify")
    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: _info())
    monkeypatch.setattr(app_module.self_update, "stage_update", lambda info, *a, **k: _staged(info["tag"]))
    try:
        out = asyncio.run(app_module._run_self_update("api:x", install=True))
        assert out["status"] == "staged"
        assert app_module.RESTART_STATE["pending"] is True
    finally:
        app_module.cancel_restart()
        app_module._SELF_UPDATE_STAGED = None


def test_cannot_self_replace_never_stages(monkeypatch):
    _reset(monkeypatch, "auto", can_replace=False)
    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: _info())
    monkeypatch.setattr(app_module.self_update, "stage_update", lambda *a, **k: pytest.fail("must not stage"))
    out = asyncio.run(app_module._run_self_update("timer", install=True))
    assert out["status"] == "available"
    assert app_module.RESTART_STATE["pending"] is False


def test_check_and_download_failures_are_recorded(monkeypatch):
    _reset(monkeypatch, "auto")

    def bad_check(**k):
        raise OSError("offline")

    monkeypatch.setattr(app_module.self_update, "check_for_update", bad_check)
    out = asyncio.run(app_module._run_self_update("timer"))
    assert out["status"] == "error" and "offline" in out["detail"]

    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: _info())

    def bad_stage(info, *a, **k):
        raise RuntimeError("truncated")

    monkeypatch.setattr(app_module.self_update, "stage_update", bad_stage)
    out = asyncio.run(app_module._run_self_update("timer"))
    assert out["status"] == "error" and "truncated" in out["detail"]
    assert app_module.RESTART_STATE["pending"] is False


def test_up_to_date(monkeypatch):
    _reset(monkeypatch, "auto")
    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: None)
    out = asyncio.run(app_module._run_self_update("timer"))
    assert out["status"] == "current" and out["available"] is None


def test_launch_staged_update_applies_or_reports(monkeypatch):
    _reset(monkeypatch, "auto")
    assert app_module._launch_staged_update() is True  # nothing staged: plain restart
    applied: list[str] = []
    monkeypatch.setattr(app_module, "_SELF_UPDATE_STAGED", _staged())
    monkeypatch.setattr(app_module.self_update, "apply_staged", lambda s, **k: applied.append(s.tag))
    assert app_module._launch_staged_update() is True
    assert applied == ["v0.5.0"]
    assert app_module.SELF_UPDATE_STATE["status"] == "applying"

    def bad_apply(s, **k):
        raise RuntimeError("helper missing")

    monkeypatch.setattr(app_module, "_SELF_UPDATE_STAGED", _staged())
    monkeypatch.setattr(app_module.self_update, "apply_staged", bad_apply)
    assert app_module._launch_staged_update() is False
    assert app_module._SELF_UPDATE_STAGED is None
    assert app_module.SELF_UPDATE_STATE["status"] == "error"


async def test_restart_watcher_cancels_when_helper_fails(monkeypatch):
    """A failed apply must not exit the process: on the portable build there
    is no supervisor, so exiting would just stop the app."""
    _reset(monkeypatch, "auto")
    monkeypatch.setattr(app_module, "_SELF_UPDATE_STAGED", _staged())

    def bad_apply(s, **k):
        raise RuntimeError("helper missing")

    monkeypatch.setattr(app_module.self_update, "apply_staged", bad_apply)
    monkeypatch.setattr(app_module, "_busy_runs", lambda: [])
    monkeypatch.setattr(app_module.os, "kill", lambda *a: pytest.fail("must not exit"))
    monkeypatch.setattr(app_module, "_RESTART_POLL_SECONDS", 0)
    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def counted_sleep(seconds):
        ticks["n"] += 1
        if ticks["n"] > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(app_module.asyncio, "sleep", counted_sleep)
    app_module.request_restart("self-update:v0.5.0")
    try:
        with pytest.raises(asyncio.CancelledError):
            await app_module._restart_watcher_loop()
        assert app_module.RESTART_STATE["pending"] is False
        assert app_module.SELF_UPDATE_STATE["status"] == "error"
    finally:
        app_module.cancel_restart()


def test_endpoints(client, monkeypatch):
    _reset(monkeypatch, "notify", can_replace=False)
    r = client.get("/api/admin/update-app")
    assert r.status_code == 200
    assert r.json()["mode"] == "notify"
    assert r.json()["current_version"] == app_module.build_info.VERSION

    r = client.post("/api/admin/update-app/install", headers=_ORIGIN)
    assert r.status_code == 409

    monkeypatch.setattr(app_module.self_update, "check_for_update", lambda **k: None)
    r = client.post("/api/admin/update-app", headers=_ORIGIN)
    assert r.status_code == 200 and r.json()["status"] == "current"

    monkeypatch.setattr(app_module, "SELF_UPDATE_MODE", "off")
    r = client.post("/api/admin/update-app", headers=_ORIGIN)
    assert r.status_code == 409


def test_update_banner_is_wired_into_the_chat_page():
    """The chat page carries the banner markup and loads the self-contained
    script that drives it (same pattern as the CLI banner)."""
    from pathlib import Path

    root = Path(app_module.__file__).resolve().parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="update-banner"' in html
    assert 'id="update-install-btn"' in html
    assert "update-check.js" in html
    assert (root / "static" / "update-check.js").is_file()
