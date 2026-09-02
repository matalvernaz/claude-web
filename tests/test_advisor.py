"""Advisor wiring: KNOWN_MODELS invariants, _models_payload, and the
spawn-time fallback for an advisor the account can't consent to."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import app as app_module

from tests.test_fableplan import _FakeClient, _stub_run

OPUS_ADVISOR = app_module.MODELS_BY_KEY.get("opus-fable-advisor") or {}
COMBO = app_module.MODELS_BY_KEY.get("fableplan-advisor") or {}
OPUS5_ADVISOR = app_module.MODELS_BY_KEY.get("opus5-fable-advisor") or {}
OPUS5_FABLE51_ADVISOR = app_module.MODELS_BY_KEY.get("opus5-fable51-advisor") or {}


def test_advisor_entries_exist() -> None:
    assert OPUS_ADVISOR, "opus-fable-advisor missing from KNOWN_MODELS"
    assert OPUS_ADVISOR["model"] == "claude-opus-4-8"
    assert OPUS_ADVISOR["advisor_model"] == "claude-fable-5"
    assert "plan_model" not in OPUS_ADVISOR

    assert COMBO, "fableplan-advisor missing from KNOWN_MODELS"
    assert COMBO["model"] == "claude-opus-4-8"
    assert COMBO["plan_model"] == "claude-fable-5"
    assert COMBO["advisor_model"] == "claude-fable-5"


def test_opus5_advisor_entry_exists() -> None:
    assert OPUS5_ADVISOR, "opus5-fable-advisor missing from KNOWN_MODELS"
    assert OPUS5_ADVISOR["model"] == "claude-opus-5"
    assert OPUS5_ADVISOR["advisor_model"] == "claude-fable-5"
    assert "plan_model" not in OPUS5_ADVISOR
    assert OPUS5_ADVISOR["efforts"] == app_module.EFFORT_LEVELS


def test_opus5_fable51_advisor_entry_exists() -> None:
    assert OPUS5_FABLE51_ADVISOR, "opus5-fable51-advisor missing from KNOWN_MODELS"
    assert OPUS5_FABLE51_ADVISOR["model"] == "claude-opus-5"
    assert OPUS5_FABLE51_ADVISOR["advisor_model"] == "claude-fable-5-1"
    assert "plan_model" not in OPUS5_FABLE51_ADVISOR
    assert OPUS5_FABLE51_ADVISOR["efforts"] == app_module.EFFORT_LEVELS


def test_fable_5_1_switchable_entry() -> None:
    entry = app_module.MODELS_BY_KEY.get("claude-fable-5-1") or {}
    assert entry, "claude-fable-5-1 missing from KNOWN_MODELS"
    assert entry["model"] == "claude-fable-5-1"
    assert entry["context"] == 1000000
    assert entry["betas"] == []
    assert entry["efforts"] == app_module.EFFORT_LEVELS
    assert "advisor_model" not in entry


def test_models_payload_carries_advisor() -> None:
    payload = {m["key"]: m for m in app_module._models_payload()}
    assert payload["opus-fable-advisor"]["advisor"] == "claude-fable-5"
    assert payload["fableplan-advisor"]["advisor"] == "claude-fable-5"
    assert payload["opus5-fable-advisor"]["advisor"] == "claude-fable-5"
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
