"""Roundtable participants run each provider's newest model, and a Claude
panellist on a repo-bound thread gets to finish its answer.

Field notes behind these (reference_roundtable_mcp, 2026-09 and 10-04): the
registry sat a generation behind (gpt-5.6-sol, Opus 4.8, Sonnet 4.6, Fable 5)
while gpt-6.1-sol, Opus 5.5, Sonnet 5.5 and Fable 5.1 were available; Claude
panellists died at an 8-turn cap mid-read on almost every repo-bound review;
and a working agentic turn was cut at the 300 s per-request timeout and
retried from scratch.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from roundtable import core

# What OpenAI's /v1/models lists for the roundtable key (trimmed, 2026-10-04),
# plus the variants that must never be picked: -pro, dated snapshots,
# -chat-latest.
OPENAI_IDS = [
    "gpt-5.5", "gpt-5.5-pro", "gpt-5.5-2026-04-23", "gpt-5.6-sol",
    "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra",
    "gpt-6.1-sol", "gpt-6.1-sol-2026-09-30", "gpt-6.1-sol-chat-latest",
    "gpt-6.2-sol-pro", "o3",
]


class _Models:
    def __init__(self, ids, fail=False):
        self.ids, self.fail, self.calls = ids, fail, 0

    def list(self, **_kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("listing failed")
        return [SimpleNamespace(id=i) for i in self.ids]


@pytest.fixture
def fake_openai(monkeypatch):
    def install(ids, fail=False):
        models = _Models(ids, fail)
        monkeypatch.setattr(core, "_openai", SimpleNamespace(models=models))
        monkeypatch.setattr(core, "_OPENAI_LATEST", {"at": 0.0, "by_family": {}})
        return models
    return install


def test_gpt_participants_follow_the_newest_model_of_their_tier(fake_openai) -> None:
    fake_openai(OPENAI_IDS)
    assert core._resolve_participant("gpt-5")["model"] == "gpt-6.1-sol"
    assert core._resolve_participant("gpt-5-mini")["model"] == "gpt-6-luna"
    # No GPT-6 Terra exists; the newest Terra is still 5.6.
    assert core._resolve_participant("gpt-5-terra")["model"] == "gpt-5.6-terra"
    assert core._resolve_participant("gpt-astra")["model"] == "gpt-6-astra"


def test_resolving_never_rewrites_the_registry(fake_openai) -> None:
    # The pinned id is the fallback; a resolve must hand back a copy.
    fake_openai(OPENAI_IDS + ["gpt-7-sol"])
    pinned = core.PARTICIPANTS["gpt-5"]["model"]
    assert core._resolve_participant("gpt-5")["model"] == "gpt-7-sol"
    assert core.PARTICIPANTS["gpt-5"]["model"] == pinned


def test_a_failed_listing_falls_back_to_the_pinned_model(fake_openai) -> None:
    fake_openai([], fail=True)
    assert core._resolve_participant("gpt-5")["model"] == core.PARTICIPANTS["gpt-5"]["model"]


def test_the_listing_is_fetched_once_and_cached(fake_openai) -> None:
    models = fake_openai(OPENAI_IDS)
    for key in ("gpt-5", "gpt-5-mini", "gpt-astra", "gpt-5"):
        core._resolve_participant(key)
    assert models.calls == 1


def test_an_env_pin_overrides_the_newest(fake_openai, monkeypatch) -> None:
    fake_openai(OPENAI_IDS)
    monkeypatch.setenv("CLAUDE_ROUNDTABLE_MODEL_GPT_5", "gpt-5.6-sol")
    assert core._resolve_participant("gpt-5")["model"] == "gpt-5.6-sol"


def test_claude_participants_are_cli_aliases() -> None:
    # The subscription CLI resolves these to the newest model of each family.
    assert core.PARTICIPANTS["claude-opus"]["model"] == "opus"
    assert core.PARTICIPANTS["claude-sonnet"]["model"] == "sonnet"
    assert core.PARTICIPANTS["claude-fable"]["model"] == "fable"


def test_api_transport_turns_an_alias_into_the_newest_listed_id(monkeypatch) -> None:
    listing = [
        SimpleNamespace(id="claude-opus-5", created_at="2026-07-01T00:00:00Z"),
        SimpleNamespace(id="claude-opus-5-5", created_at="2026-09-20T00:00:00Z"),
        SimpleNamespace(id="claude-sonnet-5-5", created_at="2026-10-01T00:00:00Z"),
    ]
    monkeypatch.setattr(core, "_anthropic", SimpleNamespace(
        models=SimpleNamespace(list=lambda **_kw: listing)))
    monkeypatch.setattr(core, "_ANTHROPIC_LATEST", {"at": 0.0, "by_family": {}})
    assert core._anthropic_api_model("opus") == "claude-opus-5-5"
    assert core._anthropic_api_model("sonnet") == "claude-sonnet-5-5"
    # A concrete id passes through untouched.
    assert core._anthropic_api_model("claude-opus-4-8") == "claude-opus-4-8"


def test_api_transport_alias_still_resolves_without_a_listing(monkeypatch) -> None:
    monkeypatch.setattr(core, "_anthropic", None)
    monkeypatch.setattr(core, "_ANTHROPIC_LATEST", {"at": 0.0, "by_family": {}})
    assert core._anthropic_api_model("sonnet").startswith("claude-sonnet-")
    assert core._anthropic_api_model("fable").startswith("claude-fable-")


def test_effort_reaches_every_current_claude_model() -> None:
    # The gate named only opus-4-8, so Opus 5.5 / Fable 5.1 / Sonnet 5.5 on the
    # API transport silently lost the effort setting.
    for model in ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5",
                  "claude-opus-4-8", "claude-sonnet-4-6"):
        assert core._anthropic_supports_effort(model), model
    for model in ("claude-haiku-4-5-20251001", "claude-3-7-sonnet", "claude-opus-4-1"):
        assert not core._anthropic_supports_effort(model), model


def test_labels_carry_no_version_numbers() -> None:
    # A participant is told "You are <label>"; "GPT-5" said that to GPT-6.1.
    for key, info in core.PARTICIPANTS.items():
        assert not re.search(r"\d", info["label"]), key


def test_renamed_labels_still_mark_a_participants_own_turns() -> None:
    # Speakers are stored by label, so threads hold hundreds of "GPT-5" turns.
    label = core.PARTICIPANTS["gpt-5"]["label"]
    msgs = [
        {"speaker": "GPT-5", "content": "old turn"},
        {"speaker": label, "content": "new turn"},
        {"speaker": "Gemini Pro", "content": "other"},
    ]
    out = core._format_transcript(msgs, for_participant_label=label)
    assert "[GPT-5 (you)]" in out
    assert f"[{label} (you)]" in out
    assert "[Gemini Pro]:" in out


# ─── Claude panellists on repo-bound threads ─────────────────────────────────

def _ctx(tmp_path: Path):
    return core.ToolUseContext(
        permission_callback=lambda *_a: "allow",
        working_directory=tmp_path, allowed_tools=["Read", "Grep", "Glob"],
    )


class _FakeSDK:
    """Just enough of claude_agent_sdk for _call_anthropic_sdk_with_tools."""

    class PermissionResultAllow:
        pass

    class PermissionResultDeny:
        def __init__(self, message=""):
            self.message = message

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ResultMessage:
        def __init__(self, result=None, is_error=False, subtype="success"):
            self.result, self.is_error, self.subtype = result, is_error, subtype

    def __init__(self, script):
        self.script = script
        self.options = {}

    def ClaudeAgentOptions(self, **kw):
        self.options = kw
        return SimpleNamespace(**kw)

    def query(self, prompt, options):
        script = self.script

        async def _gen():
            async for _ in prompt:
                pass
            for item in script(self):
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, float):
                    await asyncio.sleep(item)
                    continue
                yield item
        return _gen()


def _run(monkeypatch, tmp_path, script):
    fake = _FakeSDK(script)
    monkeypatch.setattr(core, "_import_agent_sdk", lambda: fake)
    result = core._call_anthropic_sdk_with_tools(
        "opus", "sys", "transcript", "", None, False, _ctx(tmp_path), "Claude Opus",
    )
    return result, fake


def test_claude_panellists_get_room_to_read_the_code(monkeypatch, tmp_path) -> None:
    _result, fake = _run(monkeypatch, tmp_path, lambda sdk: [
        sdk.AssistantMessage([sdk.TextBlock("done")])])
    assert fake.options["max_turns"] >= 30


def test_the_answer_is_the_final_result_not_the_narration(monkeypatch, tmp_path) -> None:
    result, _fake = _run(monkeypatch, tmp_path, lambda sdk: [
        sdk.AssistantMessage([sdk.TextBlock("Let me read app.py first.")]),
        sdk.AssistantMessage([sdk.TextBlock("The bug is on line 3.")]),
        sdk.ResultMessage(result="The bug is on line 3."),
    ])
    assert result.text == "The bug is on line 3."


def test_without_a_result_the_last_message_is_the_answer(monkeypatch, tmp_path) -> None:
    result, _fake = _run(monkeypatch, tmp_path, lambda sdk: [
        sdk.AssistantMessage([sdk.TextBlock("Let me read app.py first.")]),
        sdk.AssistantMessage([sdk.TextBlock("The bug is on line 3.")]),
    ])
    assert result.text == "The bug is on line 3."


def test_a_panellist_stopped_by_the_turn_cap_keeps_what_it_found(monkeypatch, tmp_path) -> None:
    # The CLI sends an error_max_turns result and then exits non-zero, which
    # the SDK raises as "Claude Code returned an error result: Reached
    # maximum number of turns (N)". The findings so far are worth keeping.
    result, _fake = _run(monkeypatch, tmp_path, lambda sdk: [
        sdk.AssistantMessage([sdk.TextBlock("X on line 9 is wrong; still checking Y.")]),
        sdk.ResultMessage(is_error=True, subtype="error_max_turns"),
        RuntimeError("Claude Code returned an error result: Reached maximum number of turns (40)"),
    ])
    assert "X on line 9 is wrong" in result.text
    assert "turn limit" in result.text


def test_a_slow_agentic_turn_is_not_retried_from_scratch(monkeypatch, tmp_path) -> None:
    # A tools turn used to get the 300 s request timeout, and a timeout counts
    # as transient, so a working review was killed and started over.
    monkeypatch.setattr(core, "_TOOLS_TURN_TIMEOUT_SEC", 0.2)
    calls = {"n": 0}

    def script(sdk):
        calls["n"] += 1
        return [sdk.AssistantMessage([sdk.TextBlock("Half way: A is fine.")]), 5.0]

    result, _fake = _run(monkeypatch, tmp_path, script)
    assert calls["n"] == 1
    assert "Half way: A is fine." in result.text
    assert "time limit" in result.text


def test_a_slow_turn_with_nothing_to_show_fails_without_a_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(core, "_TOOLS_TURN_TIMEOUT_SEC", 0.2)
    calls = {"n": 0}

    def script(_sdk):
        calls["n"] += 1
        return [5.0]

    with pytest.raises(core.ProviderWallTimeout):
        _run(monkeypatch, tmp_path, script)
    assert calls["n"] == 1


# ─── Coding workflow defaults ────────────────────────────────────────────────

def test_coding_panel_prompt_states_a_tool_budget() -> None:
    # Gemini, because conftest gives the suite a (fake) Gemini key and nothing else.
    prompt = core.roundtable_coding_panel_prompt("review", ["gemini-pro", "gemini-flash"])
    assert re.search(r"about \d+ tool calls", prompt)


def test_default_coding_panel_includes_a_claude_reviewer(monkeypatch) -> None:
    # Claude reviewers found the real defects in the September and October
    # reviews, and run free on the subscription.
    monkeypatch.setattr(core, "_participant_provider_available", lambda _name: True)
    panel = core._default_coding_panel("claude-opus")
    assert panel == ["gpt-5", "claude-sonnet", "gemini-pro"]
    assert core._default_coding_panel("claude-sonnet") == ["gpt-5", "gemini-pro"]
