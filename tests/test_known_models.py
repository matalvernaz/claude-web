"""The Claude picker is the CLI's own model list.

claude-web used to carry the list by hand and fell behind (Sonnet 5.5 shipped
in CLI 2.1.289 and never appeared). It now reads the rows the CLI's SDK
initialize response lists, the same ones its /model picker shows. The fixture
is that list from a real 2.1.289, and conftest preloads it as the cache, so
KNOWN_MODELS here is what a host serves after its first fetch.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

import app as app_module
import claude_models

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "cli_models_2.1.289.json").read_text(encoding="utf-8"),
)
ROWS = FIXTURE["rows"]
KNOWN_MODELS = app_module.KNOWN_MODELS


@pytest.fixture
def restore_models():
    saved = copy.deepcopy(list(app_module.KNOWN_MODELS))
    yield
    app_module.KNOWN_MODELS[:] = saved
    app_module.MODELS_BY_KEY.clear()
    app_module.MODELS_BY_KEY.update({m["key"]: m for m in saved})


def test_the_picker_is_the_cli_list_with_aliases_first() -> None:
    keys = [m["key"] for m in KNOWN_MODELS]
    assert keys[:6] == ["", "opus", "fable", "fableplan", "sonnet", "haiku"]
    assert keys[6:] == [
        "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    ]


def test_alias_rows_reach_the_cli_as_the_alias() -> None:
    # The point of the change: "opus" spawns and switches as `opus`, which the
    # CLI resolves to the newest Opus on every run, whatever this list says.
    for alias in claude_models.FAMILY_ALIASES:
        assert app_module.MODELS_BY_KEY[alias]["model"] == alias
    assert app_module.MODELS_BY_KEY["opus"]["resolved"] == "claude-opus-5-5"
    assert app_module.MODELS_BY_KEY["sonnet"]["label"] == "Sonnet 5.5"
    assert app_module.MODELS_BY_KEY["fable"]["label"] == "Fable 5.1"


def test_default_entry_names_the_model_it_runs() -> None:
    # "Default (recommended)" tells a screen-reader user nothing about what
    # will answer; the sibling row's name does.
    default = app_module.MODELS_BY_KEY[""]
    assert default["label"] == "Default (Opus 5.5)"
    # Never spawned with (an empty key sends no --model); it sizes the
    # failover entitlement check.
    assert default["model"] == "claude-opus-5-5"
    assert app_module._model_families_for_key("") == {"opus"}


def test_fableplan_tracks_the_newest_fable_and_opus() -> None:
    entry = app_module.MODELS_BY_KEY["fableplan"]
    assert (entry["model"], entry["plan_model"]) == ("opus", "fable")
    assert entry["label"] == "Fableplan (Fable 5.1 plans, Opus 5.5 builds)"


def test_effort_levels_come_from_the_cli() -> None:
    # Opus 4.7 and Sonnet 4.6 took effort all along; the hand-kept list said
    # they did not and hid the control.
    assert app_module.MODELS_BY_KEY["haiku"]["efforts"] == []
    assert app_module.MODELS_BY_KEY["claude-sonnet-4-6"]["efforts"] == [
        "low", "medium", "high", "max"]
    assert app_module.MODELS_BY_KEY["claude-opus-4-7"]["efforts"] == app_module.EFFORT_LEVELS


def test_keys_and_labels_are_unique() -> None:
    # MODELS_BY_KEY silently keeps the last of a duplicate pair, and two rows
    # that read the same under a screen reader are a trap.
    keys = [m["key"] for m in KNOWN_MODELS]
    labels = [m["label"] for m in KNOWN_MODELS]
    assert len(keys) == len(set(keys)) == len(app_module.MODELS_BY_KEY)
    assert len(labels) == len(set(labels))


def test_a_repeated_label_gets_its_value_appended() -> None:
    rows = [{"value": "opus", "displayName": "Opus"},
            {"value": "claude-opus-9", "displayName": "Opus"}]
    labels = [e["label"] for e in claude_models.build_entries(rows)]
    assert labels == ["Opus", "Opus (claude-opus-9)"]


def test_every_entry_switches_mid_chat() -> None:
    # The spawn-only block held one row, Opus 4.7 behind the 1M beta; the CLI
    # now runs Opus 4.7 at 1M natively, so no entry needs a fresh spawn.
    assert all(not m.get("betas") for m in KNOWN_MODELS)
    assert "claude-opus-4-7-1m" not in app_module.MODELS_BY_KEY


@pytest.mark.parametrize("saved, expected", [
    ("claude-opus-5-5", ("opus", False)),
    ("claude-sonnet-5-5", ("sonnet", False)),
    ("claude-fable-5-1", ("fable", False)),
    # The CLI resolves Haiku to a dated id; a saved bare id must still match.
    ("claude-haiku-4-5", ("haiku", False)),
    ("claude-opus-4-8", ("claude-opus-4-8", False)),
    ("claude-opus-4-7-1m", ("claude-opus-4-7", False)),
    # Retired combo keys chain onward through the explicit id they named.
    ("opus55-fable51-advisor", ("opus", True)),
    ("opus-fable-advisor", ("claude-opus-4-8", True)),
    # An id the CLI no longer lists falls to the newest of its family.
    ("claude-opus-4-1", ("opus", False)),
    # Not a Claude model: left for the caller to reject.
    ("gpt-6.1-sol", ("gpt-6.1-sol", False)),
    ("", ("", False)),
])
def test_resolve_model_key_maps_old_keys_onto_the_current_list(saved, expected) -> None:
    assert app_module.resolve_model_key(saved) == expected


def test_the_fallback_list_is_aliases_only(restore_models) -> None:
    # Before the first fetch (or with no CLI to ask) the picker still offers
    # each family's newest model, never a pinned one.
    app_module._apply_cli_model_rows([])
    keys = [m["key"] for m in app_module.KNOWN_MODELS]
    assert keys == ["", "opus", "fable", "fableplan", "sonnet", "haiku"]
    assert app_module.MODELS_BY_KEY[""]["label"] == "Default"
    assert app_module.MODELS_BY_KEY["opus"]["label"] == "Opus"


def test_a_refresh_rebuilds_the_list_in_place(restore_models) -> None:
    before = app_module.KNOWN_MODELS
    rows = copy.deepcopy(ROWS)
    # A future CLI whose `opus` alias has moved on.
    for row in rows[:2]:
        row["resolvedModel"] = "claude-opus-6"
    rows[1]["displayName"] = "Opus 6"
    app_module._apply_cli_model_rows(rows)

    assert app_module.KNOWN_MODELS is before
    assert app_module.MODELS_BY_KEY["opus"]["label"] == "Opus 6"
    assert app_module.MODELS_BY_KEY[""]["label"] == "Default (Opus 6)"
    assert app_module.MODELS_BY_KEY["fableplan"]["label"] == (
        "Fableplan (Fable 5.1 plans, Opus 6 builds)")
    # A saved pick of the Opus it used to be follows the alias forward.
    assert app_module.resolve_model_key("claude-opus-6") == ("opus", False)


def test_disabled_and_malformed_rows_are_skipped() -> None:
    rows = [{"value": "default"}, {"value": "opus", "disabled": True},
            {"nope": 1}, "x", {"value": "sonnet"}, {"value": "sonnet"}]
    assert [r["value"] for r in claude_models.usable_rows(rows)] == ["default", "sonnet"]


def test_cache_round_trip(tmp_path) -> None:
    path = tmp_path / "cli_models.json"
    claude_models.save_cache(path, ROWS, "2.1.289 (Claude Code)")
    data = claude_models.load_cache(path)
    assert data["rows"] == ROWS
    assert data["cli_version"] == "2.1.289 (Claude Code)"
    path.write_text("{not json", encoding="utf-8")
    assert claude_models.load_cache(path) is None


def test_turn_usage_teaches_the_meter_each_window(restore_models, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "MODEL_CONTEXT_CACHE", tmp_path / "ctx.json")
    usage = {
        "claude-sonnet-5-5": {"contextWindow": 1000000, "canonicalModel": "claude-sonnet-5-5"},
        # The small model the CLI runs housekeeping on is listed too.
        "claude-haiku-4-5-20251001": {"contextWindow": 200000, "canonicalModel": "claude-haiku-4-5"},
    }
    windows = app_module._note_context_windows(usage)

    assert windows == {"claude-sonnet-5-5": 1000000, "claude-haiku-4-5": 200000}
    assert app_module.MODELS_BY_KEY["sonnet"]["context"] == 1000000
    assert app_module.MODELS_BY_KEY["haiku"]["context"] == 200000
    # Kept for the next boot, whose list has no windows of its own.
    app_module._apply_cli_model_rows(ROWS)
    assert app_module.MODELS_BY_KEY["haiku"]["context"] == 200000


def test_result_event_carries_the_turns_context_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "MODEL_CONTEXT_CACHE", tmp_path / "ctx.json")
    msg = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s-1", result="PONG",
        model_usage={"claude-opus-5-5": {"contextWindow": 1000000,
                                         "canonicalModel": "claude-opus-5-5"}},
    )
    (event,) = app_module._sdk_message_to_events(msg)
    assert event["context_windows"] == {"claude-opus-5-5": 1000000}


def test_payload_tells_the_browser_what_each_row_resolves_to() -> None:
    payload = {m["key"]: m for m in app_module._models_payload()}
    assert payload["opus"]["resolved"] == "claude-opus-5-5"
    assert payload[""]["resolved"] == "claude-opus-5-5"
    assert payload["haiku"]["resolved"] == "claude-haiku-4-5-20251001"


def test_frontend_gates_the_advisor_on_a_capability_not_a_provider_name() -> None:
    """The send path must ask what a provider can do, not who it is.

    Same rule test_codex_provider enforces for permission modes: a hardcoded
    provider name there is how Codex ended up silently starting in the wrong
    permission mode. The advisor is Claude-only today, but that is a fact about
    capabilities, so it travels as one.
    """
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",  # Windows defaults to cp1252 and chokes on the JS
    )
    send_one = source.index("async function sendOne")
    start = source.index("const provider = ", send_one)
    end = source.index("if (effortSelect", start)
    form_block = source[start:end]

    assert "providerCapabilities(provider).advisor" in form_block
    assert 'fd.append("advisor", "1")' in form_block


def test_frontend_migrates_every_retired_key() -> None:
    """The browser's legacy map must cover the server's, or a saved pick is
    dropped silently: the restore discards any value that is no longer an
    <option>, with no announcement, and the user lands on Default."""
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",
    )
    block = source[source.index("const LEGACY_MODEL_KEYS"):]
    block = block[:block.index("};")]
    for legacy, (target, _advisor) in app_module.LEGACY_MODEL_KEYS.items():
        assert f'"{legacy}": "{target}"' in block, legacy
    retired = source[source.index("const RETIRED_MODEL_KEYS"):]
    retired = retired[:retired.index("};")]
    for old, new in app_module.RETIRED_MODEL_KEYS.items():
        assert f'"{old}": "{new}"' in retired, old


def test_frontend_maps_a_saved_id_through_resolved() -> None:
    # A saved explicit id ("claude-opus-5-5") is no longer an <option> once
    # the opus alias row covers it; the restore must follow `resolved`, or
    # every existing browser drops back to Default on the first load.
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",
    )
    fn = source[source.index("function resolveSavedModel("):]
    fn = fn[:fn.index("\n  }\n")]
    assert "resolved" in fn
    assert "LEGACY_MODEL_KEYS" in fn and "RETIRED_MODEL_KEYS" in fn
