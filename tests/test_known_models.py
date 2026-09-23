"""KNOWN_MODELS invariants: picker ordering, uniqueness, and catalog drift.

The ordering test is an accessibility guard, not a tidiness one — see the
comment above KNOWN_MODELS in app.py. Under a screen reader a collapsed
<select> fires "change" on every option arrowed past, and switchKey() reverts
a mid-chat switch onto a spawn-only entry, so one spawn-only entry sitting
between two switchable ones bounces the selection back and traps the user
before they reach the model they wanted.
"""
from __future__ import annotations

from pathlib import Path

import app as app_module

KNOWN_MODELS = app_module.KNOWN_MODELS


def _spawn_only(entry: dict) -> bool:
    """Only request betas still need a fresh spawn.

    The advisor used to as well; it is now its own control and changes on a
    live CLI, which is what emptied this block down to one entry.
    """
    return bool(entry.get("betas"))


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
    # claude-opus-4-7 is deliberately absent from the table above. The catalog
    # now gives it a 1M window (`native_1m`), while claude-web still carries it
    # as 200K plus a separate `claude-opus-4-7-1m` entry behind the 1M beta —
    # a pair that predates the native listing. Reconciling that means deciding
    # whether the beta entry retires, which is a bigger call than a data fix,
    # so it is left alone rather than silently pinned to the wrong number.
    for m in KNOWN_MODELS:
        expected = catalog.get(m["model"])
        if expected is not None and not m.get("betas"):
            assert m["context"] == expected, f"{m['key'] or '(default)'}: {m['model']}"


def test_no_model_appears_twice_in_the_picker() -> None:
    # The advisor combos used to list the same executor a second time, so the
    # picker carried Opus 5 twice and adding one advisor doubled the list. Only
    # a betas variant may repeat a model id now, and it must say so in its
    # label, or two rows read identically under a screen reader.
    def shape(entry: dict) -> tuple:
        # What actually makes two entries on the same model id different runs:
        # the request betas, and the model the run switches to in plan mode.
        # The combos differed in neither -- only in an advisor attachment,
        # which is no longer part of a model entry at all.
        return (tuple(sorted(entry.get("betas") or [])), entry.get("plan_model"))

    seen: dict[str, dict] = {}
    for m in KNOWN_MODELS:
        if not m["key"]:
            continue
        prior = seen.get(m["model"])
        if prior is not None:
            assert shape(m) != shape(prior), f"{m['key']} duplicates {prior['key']}"
        seen[m["model"]] = m


def test_labels_are_unique() -> None:
    labels = [m["label"] for m in KNOWN_MODELS]
    assert len(labels) == len(set(labels))


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
