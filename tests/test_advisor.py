"""Advisor wiring: it is an independent on/off, not a set of model entries.

The advisor used to be baked into KNOWN_MODELS as executor+advisor combo keys,
which meant the picker carried every executor twice. It is now one flag
(ADVISOR_MODEL + the "advisor" field on /api/chat), so these tests pin the
replacement: no entry may reintroduce a per-model advisor, the retired keys
still resolve, and the entitlement check still knows an advisor-on run bills
the advisor's family too.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import app as app_module

from tests.test_fableplan import _FakeClient, _stub_run


def test_advisor_is_one_model_not_a_set_of_combo_entries() -> None:
    assert app_module.ADVISOR_MODEL == "claude-fable-5-1"
    # The advisor must also be pickable as a main model, so "consult Fable"
    # and "run on Fable" are not two different vocabularies.
    assert app_module.ADVISOR_MODEL in {
        m["model"] for m in app_module.KNOWN_MODELS if m["key"]
    }


def test_no_entry_carries_its_own_advisor() -> None:
    # A reintroduced advisor_model would silently win over the checkbox at
    # spawn and bring the duplicate-entry problem back with it.
    for m in app_module.KNOWN_MODELS:
        assert "advisor_model" not in m, m["key"]


def test_model_supports_advisor_follows_the_cli_rank_rule() -> None:
    # The CLI allows an advisor when the executor has a rank at all and the
    # advisor's rank is at least the executor's. Fable 5.1 is rank 5, the top,
    # so every ranked model qualifies.
    for m in app_module.KNOWN_MODELS:
        rank = m.get("advisor_rank")
        expected = rank is not None and rank <= app_module._ADVISOR_MODEL_RANK
        assert app_module.model_supports_advisor(m["key"]) is expected, m["key"]
    # An unknown key reads as "no advisor" rather than raising.
    assert app_module.model_supports_advisor("not-a-model") is False


def test_fableplan_rank_is_the_higher_of_its_two_halves() -> None:
    # The CLI drops the advisor the moment the run switches to a half that
    # outranks it, and it does so silently — so the entry has to advertise the
    # stricter of the two, not the model it spends most of its time on.
    entry = app_module.MODELS_BY_KEY["fableplan"]
    halves = {entry["model"], entry["plan_model"]}
    ranks = {
        m["advisor_rank"] for m in app_module.KNOWN_MODELS
        if m["key"] and m["model"] in halves and m.get("advisor_rank")
    }
    assert entry["advisor_rank"] == max(ranks)


def test_every_retired_combo_key_still_resolves() -> None:
    # These arrive from a browser whose localStorage predates the split, and
    # from run rows already in state.db. Dropping one 400s a real user's next
    # message, so each must land on a live entry with the advisor on.
    assert app_module.LEGACY_MODEL_KEYS, "the legacy map must not be emptied"
    for legacy, (target, advisor) in app_module.LEGACY_MODEL_KEYS.items():
        assert legacy not in app_module.MODELS_BY_KEY, legacy
        assert target in app_module.MODELS_BY_KEY, legacy
        assert advisor is True, legacy
        assert app_module.resolve_model_key(legacy) == (target, True)
        assert app_module.model_supports_advisor(target), legacy


def test_current_keys_resolve_to_themselves_with_no_advisor() -> None:
    for m in app_module.KNOWN_MODELS:
        assert app_module.resolve_model_key(m["key"]) == (m["key"], False)


def test_models_payload_carries_advisor_availability() -> None:
    payload = {m["key"]: m for m in app_module._models_payload()}
    assert payload[""]["advisor_ok"] is True
    assert payload["claude-opus-5-5"]["advisor_ok"] is True
    # The old per-entry advisor id is gone; the browser reads a boolean.
    assert "advisor" not in payload[""]
    assert payload[""]["betas"] == []
    assert payload[""]["efforts"] == app_module.EFFORT_LEVELS


def test_entitlement_families_include_the_advisor_only_when_it_is_on() -> None:
    # An advisor-on run consults Fable mid-turn, so a slot entitled to only the
    # executor's family dies partway through a turn rather than at spawn.
    assert app_module._model_families_for_key("claude-opus-5-5") == {"opus"}
    assert app_module._model_families_for_key(
        "claude-opus-5-5", advisor=True) == {"opus", "fable"}
    # A retired key asks for the Fable entitlement it always did, with no
    # caller having to know it was a combo.
    assert app_module._model_families_for_key(
        "opus55-fable51-advisor") == {"opus", "fable"}


def test_fableplan_families_cover_both_halves_and_the_advisor() -> None:
    assert app_module._model_families_for_key("fableplan") == {"opus", "fable"}
    assert app_module._model_families_for_key(
        "fableplan", advisor=True) == {"opus", "fable"}


def test_form_flag_reads_a_checkbox() -> None:
    for on in ("1", "true", "on", "yes", "TRUE"):
        assert app_module._form_flag(on) is True
    for off in ("", "0", "false", "off", "no", None):
        assert app_module._form_flag(off) is False


def test_split_model_entry_still_drives_plan_model_swaps() -> None:
    # Unchanged behaviour, re-pinned on the surviving key: the combo entry this
    # used to run against no longer exists.
    client = _FakeClient()
    run = _stub_run("fableplan", "plan", client)

    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5"]

    run.permission_mode = "acceptEdits"
    asyncio.run(app_module._sync_plan_model(run))
    assert client.calls == ["claude-fable-5", "claude-opus-4-8"]


# ─── advisor consent refusal at spawn ───────────────────────────────────────

def _consent_stderr(label: str) -> str:
    """The CLI's own refusal, as captured from a 2.1.258 spawn."""
    return (
        f"{label} as the advisor bills to usage credits, which need to be set "
        "up for your account. Run /model fable in an interactive session to "
        "review and enable."
    )


def _spawn_recorder(monkeypatch, fail_attempts: int,
                    fail_after_connect: bool = False):
    """Patch app.ClaudeSDKClient with a stub that records each attempt's
    extra_args and raises ProcessError for the first `fail_attempts` spawns."""
    attempts: list[dict] = []

    class _Spawn:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            attempts.append(dict(self.options.extra_args or {}))
            if len(attempts) <= fail_attempts and not fail_after_connect:
                raise app_module.ProcessError(
                    "Command failed with exit code 1", exit_code=1,
                )
            return f"client-{len(attempts)}"

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(
        app_module, "ClaudeSDKClient", lambda options: _Spawn(options),
    )
    return attempts


def _spawn_run() -> tuple[SimpleNamespace, list[dict]]:
    events: list[dict] = []
    return SimpleNamespace(run_id="t-run", emit=events.append), events


async def test_sdk_client_respawns_without_advisor_on_consent_refusal(
    monkeypatch,
) -> None:
    attempts = _spawn_recorder(monkeypatch, fail_attempts=1)
    stderr_buf = [_consent_stderr("Fable 5.1")]
    options = app_module.ClaudeAgentOptions(
        extra_args={"advisor": "claude-fable-5-1"},
    )
    run, events = _spawn_run()

    async with app_module._sdk_client(options, run, stderr_buf) as client:
        assert client == "client-2"

    # First spawn carried the flag, the retry dropped it and kept the rest.
    assert attempts == [{"advisor": "claude-fable-5-1"}, {}]
    assert [e["type"] for e in events] == ["advisor_disabled"]
    assert events[0]["advisor"] == "claude-fable-5-1"
    assert "/model fable" in events[0]["message"]
    # The stale refusal must not be re-reported as a later failure's reason.
    assert stderr_buf == []


async def test_sdk_client_keeps_other_extra_args_on_respawn(
    monkeypatch,
) -> None:
    attempts = _spawn_recorder(monkeypatch, fail_attempts=1)
    options = app_module.ClaudeAgentOptions(
        extra_args={"advisor": "claude-fable-5", "some-flag": "x"},
    )
    run, _ = _spawn_run()

    async with app_module._sdk_client(
        options, run, [_consent_stderr("Fable 5")],
    ):
        pass

    assert attempts[1] == {"some-flag": "x"}


async def test_sdk_client_does_not_retry_unrelated_process_error(
    monkeypatch,
) -> None:
    attempts = _spawn_recorder(monkeypatch, fail_attempts=1)
    options = app_module.ClaudeAgentOptions(
        extra_args={"advisor": "claude-fable-5-1"},
    )
    run, events = _spawn_run()

    try:
        async with app_module._sdk_client(
            options, run, ["error: unknown option '--advisor'"],
        ):
            raise AssertionError("spawn should not have succeeded")
    except app_module.ProcessError:
        pass

    assert len(attempts) == 1
    assert events == []


async def test_sdk_client_does_not_retry_without_an_advisor_flag(
    monkeypatch,
) -> None:
    attempts = _spawn_recorder(monkeypatch, fail_attempts=1)
    run, _ = _spawn_run()

    try:
        async with app_module._sdk_client(
            app_module.ClaudeAgentOptions(),
            run, [_consent_stderr("Fable 5.1")],
        ):
            raise AssertionError("spawn should not have succeeded")
    except app_module.ProcessError:
        pass

    assert len(attempts) == 1


async def test_sdk_client_does_not_retry_a_midturn_death(monkeypatch) -> None:
    # A ProcessError raised after the client is in hand is a mid-turn death.
    # Respawning there would replay a turn that already produced output.
    attempts = _spawn_recorder(
        monkeypatch, fail_attempts=0, fail_after_connect=True,
    )
    options = app_module.ClaudeAgentOptions(
        extra_args={"advisor": "claude-fable-5-1"},
    )
    run, events = _spawn_run()

    try:
        async with app_module._sdk_client(
            options, run, [_consent_stderr("Fable 5.1")],
        ):
            raise app_module.ProcessError(
                "Command failed with exit code 1", exit_code=1,
            )
    except app_module.ProcessError:
        pass

    assert len(attempts) == 1
    assert events == []


# ─── error summaries carry the CLI's own reason ─────────────────────────────

def test_with_cli_reason_promotes_the_last_stderr_line() -> None:
    tail = "Ignoring 8 permissions.allow entries\n" + _consent_stderr("Fable 5")
    summary = app_module._with_cli_reason(
        "ProcessError: Command failed with exit code 1\nError output: "
        "Check stderr output for details",
        tail,
    )

    assert summary.startswith(
        "ProcessError: Command failed with exit code 1 Error output:",
    )
    assert "as the advisor bills to usage credits" in summary
    assert "\n" not in summary


def test_with_cli_reason_without_stderr_returns_the_summary() -> None:
    assert app_module._with_cli_reason("Boom: x", "") == "Boom: x"
    assert app_module._with_cli_reason("Boom: x", "\n  \n") == "Boom: x"


def test_with_cli_reason_caps_a_long_reason() -> None:
    summary = app_module._with_cli_reason("Boom: x", "y" * 5000)
    assert len(summary) <= len("Boom: x — ") + app_module._ERROR_REASON_CAP


def test_advisor_disabled_notice_is_rendered_and_spoken() -> None:
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8",
    )
    start = source.index('obj.type === "advisor_disabled"')
    block = source[start:source.index("} else if", start)]

    assert 'className = "msg info"' in block
    assert "announce(advisorNote)" in block
