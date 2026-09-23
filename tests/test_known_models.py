"""KNOWN_MODELS invariants: picker ordering, uniqueness, and catalog drift.

The ordering test is an accessibility guard, not a tidiness one — see the
comment above KNOWN_MODELS in app.py. Under a screen reader a collapsed
<select> fires "change" on every option arrowed past, and switchKey() reverts
a mid-chat switch onto a spawn-only entry, so one spawn-only entry sitting
between two switchable ones bounces the selection back and traps the user
before they reach the model they wanted.
"""
from __future__ import annotations

import app as app_module

KNOWN_MODELS = app_module.KNOWN_MODELS


def _spawn_only(entry: dict) -> bool:
    """Request betas and an advisor attachment can only be set at spawn."""
    return bool(entry.get("betas")) or bool(entry.get("advisor_model"))


def test_keys_are_unique() -> None:
    keys = [m["key"] for m in KNOWN_MODELS]
    assert len(keys) == len(set(keys))
    # MODELS_BY_KEY silently keeps the last of a duplicate pair; the picker
    # would show both rows but only one would be selectable.
    assert len(app_module.MODELS_BY_KEY) == len(KNOWN_MODELS)


def test_spawn_only_entries_are_contiguous_at_the_end() -> None:
    flags = [_spawn_only(m) for m in KNOWN_MODELS]
    first_spawn_only = flags.index(True)
    assert all(flags[first_spawn_only:]), (
        "a switchable entry sits below a spawn-only one; see the module "
        "comment above KNOWN_MODELS for why that traps screen-reader users"
    )


def test_every_entry_is_fully_specified() -> None:
    for m in KNOWN_MODELS:
        assert m["model"], m["key"]
        assert isinstance(m["context"], int) and m["context"] > 0, m["key"]
        assert m["label"], m["key"]
        assert set(m["efforts"]) <= set(app_module.EFFORT_LEVELS), m["key"]


def test_context_windows_match_the_cli_model_catalog() -> None:
    # Read out of the CLI's own model catalog (`claude` 2.1.280 binary, the
    # `context: {window: …}` field). The context meter and the threshold
    # announcements divide by this, so an inflated value silences the warning
    # that a session is about to overflow.
    catalog = {
        "claude-opus-5-5": 1000000,
        "claude-opus-5": 1000000,
        "claude-fable-5-1": 1000000,
        "claude-fable-5": 1000000,
        "claude-opus-4-8": 1000000,
        "claude-sonnet-5": 1000000,
        "claude-sonnet-4-6": 200000,
        "claude-haiku-4-5": 200000,
    }
    for m in KNOWN_MODELS:
        # claude-opus-4-7 is listed twice: natively 200K here and again under
        # the 1M-context beta, so it is checked by key rather than model id.
        expected = catalog.get(m["model"])
        if expected is not None and not m.get("betas"):
            assert m["context"] == expected, f"{m['key'] or '(default)'}: {m['model']}"


def test_new_5_5_generation_is_offered() -> None:
    opus55 = app_module.MODELS_BY_KEY.get("claude-opus-5-5") or {}
    assert opus55, "claude-opus-5-5 missing from KNOWN_MODELS"
    assert opus55["model"] == "claude-opus-5-5"
    assert opus55["efforts"] == app_module.EFFORT_LEVELS
    assert not _spawn_only(opus55), "Opus 5.5 must be switchable mid-chat"

    sonnet5 = app_module.MODELS_BY_KEY.get("claude-sonnet-5") or {}
    assert sonnet5, "claude-sonnet-5 missing from KNOWN_MODELS"
    assert sonnet5["model"] == "claude-sonnet-5"
    assert sonnet5["efforts"] == app_module.EFFORT_LEVELS
    assert not _spawn_only(sonnet5)


def test_default_entry_tracks_the_cli_opus_alias() -> None:
    # An empty key sends no --model, so this id is never spawned with; it is
    # what _model_families_for_key reads to decide which usage family the run
    # will bill, and the failover preflight ranks credential slots on that.
    default = app_module.MODELS_BY_KEY[""]
    assert app_module._model_families_for_key("") == {"opus"}
    assert default["model"] in {m["model"] for m in KNOWN_MODELS if m["key"]}
