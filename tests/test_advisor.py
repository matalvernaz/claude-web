"""Advisor picker entries: KNOWN_MODELS invariants + _models_payload wiring."""
from __future__ import annotations

import asyncio

import app as app_module

from tests.test_fableplan import _FakeClient, _stub_run

OPUS_ADVISOR = app_module.MODELS_BY_KEY.get("opus-fable-advisor") or {}
COMBO = app_module.MODELS_BY_KEY.get("fableplan-advisor") or {}


def test_advisor_entries_exist() -> None:
    assert OPUS_ADVISOR, "opus-fable-advisor missing from KNOWN_MODELS"
    assert OPUS_ADVISOR["model"] == "claude-opus-4-8"
    assert OPUS_ADVISOR["advisor_model"] == "claude-fable-5"
    assert "plan_model" not in OPUS_ADVISOR

    assert COMBO, "fableplan-advisor missing from KNOWN_MODELS"
    assert COMBO["model"] == "claude-opus-4-8"
    assert COMBO["plan_model"] == "claude-fable-5"
    assert COMBO["advisor_model"] == "claude-fable-5"


def test_models_payload_carries_advisor() -> None:
    payload = {m["key"]: m for m in app_module._models_payload()}
    assert payload["opus-fable-advisor"]["advisor"] == "claude-fable-5"
    assert payload["fableplan-advisor"]["advisor"] == "claude-fable-5"
    # Ordinary entries expose an empty advisor so switchKey() compares "" to
    # "" rather than undefined to a model id.
    assert payload[""]["advisor"] == ""
    assert payload["fableplan"]["advisor"] == ""
    # The pre-advisor fields still ride along for the meter/effort pickers.
    assert payload[""]["betas"] == []
    assert payload[""]["efforts"] == app_module.EFFORT_LEVELS


def test_combo_entry_drives_plan_model_swaps() -> None:
    client = _FakeClient()
    run = _stub_run("fableplan-advisor", "plan", client)

    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5"]

    run.permission_mode = "acceptEdits"
    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5", "claude-opus-4-8"]
