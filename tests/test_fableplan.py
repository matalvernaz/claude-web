"""Fableplan split-model entry: KNOWN_MODELS invariants + _sync_plan_model."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import app as app_module


class _FakeClient:
    """Records set_model calls; optionally fails to exercise the guard."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str | None] = []
        self.fail = fail

    async def set_model(self, model: str | None) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(model)


def _stub_run(model_key: str | None, mode: str, client: _FakeClient | None):
    events: list[dict] = []
    return SimpleNamespace(
        model=model_key,
        permission_mode=mode,
        client=client,
        client_write_lock=asyncio.Lock(),
        live_sdk_model=None,
        run_id="t-run",
        events=events,
        emit=events.append,
    )


ENTRY = app_module.MODELS_BY_KEY.get("fableplan") or {}


# ─── KNOWN_MODELS invariants ────────────────────────────────────────────────

def test_fableplan_entry_exists_with_split_models() -> None:
    assert ENTRY, "fableplan missing from KNOWN_MODELS"
    assert ENTRY["model"] == "claude-opus-4-8"
    assert ENTRY["plan_model"] == "claude-fable-5"


def test_fableplan_betas_match_default_entry() -> None:
    # set_model can't change request betas mid-chat, so the split entry must
    # share the default entry's beta set or the browser would (rightly)
    # refuse a live switch onto it.
    default = app_module.MODELS_BY_KEY[""]
    assert sorted(ENTRY.get("betas") or []) == sorted(default.get("betas") or [])


def test_fableplan_both_models_resolvable_to_labels() -> None:
    # _sync_plan_model announces the target by label; both halves must map to
    # a picker entry so the announcement never falls back to a raw model id.
    by_model = {m["model"] for m in app_module.KNOWN_MODELS if m["key"]}
    assert ENTRY["model"] in by_model
    assert ENTRY["plan_model"] in by_model


# ─── _sync_plan_model behavior ──────────────────────────────────────────────

def test_sync_swaps_to_plan_model_and_back() -> None:
    client = _FakeClient()
    run = _stub_run("fableplan", "plan", client)

    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5"]
    assert run.live_sdk_model == "claude-fable-5"
    assert run.events[-1]["type"] == "plan_model"
    assert run.events[-1]["active"] is True

    run.permission_mode = "acceptEdits"
    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5", "claude-opus-4-8"]
    assert run.live_sdk_model == "claude-opus-4-8"
    assert run.events[-1]["active"] is False


def test_sync_is_idempotent_per_state() -> None:
    client = _FakeClient()
    run = _stub_run("fableplan", "plan", client)
    asyncio.run(app_module._sync_plan_model(run))
    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5"]
    assert len(run.events) == 1


def test_sync_noop_for_ordinary_entries() -> None:
    for key in ("", "claude-fable-5", None):
        client = _FakeClient()
        run = _stub_run(key, "plan", client)
        asyncio.run(app_module._sync_plan_model(run))
        assert client.calls == []
        assert run.events == []


def test_sync_noop_before_client_exists() -> None:
    run = _stub_run("fableplan", "plan", None)
    asyncio.run(app_module._sync_plan_model(run))
    assert run.events == []
    assert run.live_sdk_model is None


def test_sync_survives_set_model_failure() -> None:
    client = _FakeClient(fail=True)
    run = _stub_run("fableplan", "plan", client)
    asyncio.run(app_module._sync_plan_model(run))  # must not raise
    assert run.live_sdk_model is None
    assert run.events == []
