"""Tests for the structured-ask / compaction / bind-diff roundtable tools.

All offline: provider calls are monkeypatched at the _call_* / _oneshot_text
layer, and bind-diff runs against throwaway local git repos. The hermetic
state dir comes from conftest.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import roundtable.core as core


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["real", "bogus"]},
        "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["verdict", "confidence"],
}


def _allow_all_participants(monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)


# ─── _validate_structured (unit) ─────────────────────────────────────────

def test_validate_structured_strips_fences_and_parses():
    obj = core._validate_structured(
        '```json\n{"verdict": "real", "confidence": 3}\n```', VERDICT_SCHEMA,
    )
    assert obj == {"verdict": "real", "confidence": 3}


def test_validate_structured_bad_json_raises_repairable_error():
    with pytest.raises(ValueError, match="not valid JSON"):
        core._validate_structured("the bug is real, trust me", VERDICT_SCHEMA)


def test_validate_structured_schema_violation_names_the_path():
    with pytest.raises(ValueError, match=r"\$\['confidence'\]"):
        core._validate_structured(
            '{"verdict": "real", "confidence": 9}', VERDICT_SCHEMA,
        )


def test_validate_structured_accepts_pre_parsed_object():
    obj = core._validate_structured(
        {"verdict": "bogus", "confidence": 1}, VERDICT_SCHEMA,
    )
    assert obj["verdict"] == "bogus"


# ─── roundtable_ask_structured ───────────────────────────────────────────

def test_ask_structured_rejects_bad_schemas(monkeypatch):
    _allow_all_participants(monkeypatch)
    tid = core.roundtable_create("schema gates")["thread_id"]
    with pytest.raises(ValueError, match="object"):
        core.roundtable_ask_structured(tid, ["gpt-5"], {"type": "array"})
    with pytest.raises(ValueError, match="not a valid JSON Schema"):
        core.roundtable_ask_structured(
            tid, ["gpt-5"], {"type": "object", "properties": 12},
        )
    with pytest.raises(ValueError, match="Duplicate"):
        core.roundtable_ask_structured(tid, ["gpt-5", "gpt-5"], VERDICT_SCHEMA)


def test_ask_structured_returns_validated_objects_and_commits_json(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(
        core, "_call_openai_structured",
        lambda *a, **k: core.ProviderResult(
            text='{"verdict": "real", "confidence": 4}',
        ),
    )
    tid = core.roundtable_create("structured ok")["thread_id"]
    out = core.roundtable_ask_structured(
        tid, ["gpt-5"], VERDICT_SCHEMA, prompt="verdict please",
    )
    assert out["errors"] == {}
    assert out["results"]["gpt-5"] == {"verdict": "real", "confidence": 4}
    msgs = core._thread_messages(tid)
    # prompt posted once, then the participant's pretty-printed JSON turn
    assert msgs[-2]["speaker"] == "orchestrator"
    assert json.loads(msgs[-1]["content"]) == {"verdict": "real", "confidence": 4}


def test_ask_structured_repairs_once_with_validator_feedback(monkeypatch):
    _allow_all_participants(monkeypatch)
    calls: list[str] = []

    def _fake(model, sys_p, transcript, instr, effort, schema):
        calls.append(instr)
        if len(calls) == 1:
            return core.ProviderResult(text="not json at all")
        return core.ProviderResult(text='{"verdict": "bogus", "confidence": 2}')

    monkeypatch.setattr(core, "_call_openai_structured", _fake)
    tid = core.roundtable_create("structured repair")["thread_id"]
    out = core.roundtable_ask_structured(tid, ["gpt-5"], VERDICT_SCHEMA)
    assert out["results"]["gpt-5"]["verdict"] == "bogus"
    assert len(calls) == 2
    # The repair instruction must carry the validator's complaint and the
    # offending reply so the model can actually fix it.
    assert "not valid JSON" in calls[1]
    assert "not json at all" in calls[1]


def test_ask_structured_gives_up_after_repair_and_records_error(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(
        core, "_call_openai_structured",
        lambda *a, **k: core.ProviderResult(text="still not json"),
    )
    tid = core.roundtable_create("structured fail")["thread_id"]
    out = core.roundtable_ask_structured(tid, ["gpt-5"], VERDICT_SCHEMA)
    assert out["results"] == {}
    assert "schema-valid" in out["errors"]["gpt-5"]
    assert "[provider error" in core._thread_messages(tid)[-1]["content"]


# ─── roundtable_compact ──────────────────────────────────────────────────

def _make_thread(n_posts: int) -> int:
    tid = core.roundtable_create("compaction target")["thread_id"]
    for i in range(n_posts):
        core.roundtable_post(tid, f"message number {i}")
    return tid


def test_compact_replaces_prefix_with_summary_in_effective_view(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(
        core, "_oneshot_text", lambda info, tr, sp, um: "SUMMARY-OF-PREFIX",
    )
    tid = _make_thread(10)
    res = core.roundtable_compact(tid, keep_last=3)
    assert res["messages_compacted"] == 7
    assert res["kept_verbatim"] == 3

    effective = core._effective_messages(tid)
    assert len(effective) == 4  # synthetic summary + 3 kept
    assert "SUMMARY-OF-PREFIX" in effective[0]["content"]
    assert effective[-1]["content"] == "message number 9"

    # Default history shows the compacted view; raw=True shows originals.
    assert "SUMMARY-OF-PREFIX" in core.roundtable_history(tid)
    assert "message number 0" not in core.roundtable_history(tid)
    assert "message number 0" in core.roundtable_history(tid, raw=True)


def test_compact_reshows_artifacts_from_the_compacted_range(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(core, "_oneshot_text", lambda *a: "S")
    tid = core.roundtable_create("compaction artifacts")["thread_id"]
    core.roundtable_set_artifact(tid, "patch", "OLD-CONTENT")
    core.roundtable_set_artifact(tid, "patch", "PATCH-CONTENT-V2")
    for i in range(6):
        core.roundtable_post(tid, f"chatter {i}")
    res = core.roundtable_compact(tid, keep_last=2)
    assert res["artifacts_reshown"] == 1
    first = core._effective_messages(tid)[0]["content"]
    assert "PATCH-CONTENT-V2" in first      # latest version re-shown
    assert "OLD-CONTENT" not in first        # superseded version is not


def test_compact_rejects_pointless_and_repeat_runs(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(core, "_oneshot_text", lambda *a: "S")
    tid = _make_thread(4)
    with pytest.raises(ValueError, match="keep_last"):
        core.roundtable_compact(tid, keep_last=-1)
    with pytest.raises(ValueError, match="not worth"):
        core.roundtable_compact(tid, keep_last=3)  # prefix of 1
    core.roundtable_compact(tid, keep_last=1)
    with pytest.raises(ValueError, match="nothing new"):
        core.roundtable_compact(tid, keep_last=1)  # tail is just the summary


def test_recompact_chains_off_the_previous_summary(monkeypatch):
    _allow_all_participants(monkeypatch)
    seen: list[str] = []

    def _fake_summary(info, transport, system_prompt, user_msg):
        seen.append(user_msg)
        return f"SUMMARY-{len(seen)}"

    monkeypatch.setattr(core, "_oneshot_text", _fake_summary)
    tid = _make_thread(6)
    core.roundtable_compact(tid, keep_last=2)
    for i in range(4):
        core.roundtable_post(tid, f"later {i}")
    core.roundtable_compact(tid, keep_last=1)
    # Second summariser input starts from the first summary, not from the
    # already-compacted originals.
    assert "SUMMARY-1" in seen[1]
    assert "message number 0" not in seen[1]
    assert core._effective_messages(tid)[0]["content"].count("SUMMARY-2") == 1


def test_ask_sees_the_compacted_view(monkeypatch):
    _allow_all_participants(monkeypatch)
    monkeypatch.setattr(core, "_oneshot_text", lambda *a: "THE-BRIEFING")
    captured: dict = {}

    def _fake_run_turn(thread, info, messages, instruction, *a, **k):
        captured["messages"] = messages
        return core.ProviderResult(text="ack")

    monkeypatch.setattr(core, "_run_turn", _fake_run_turn)
    tid = _make_thread(8)
    core.roundtable_compact(tid, keep_last=2)
    core.roundtable_ask(tid, "gemini-pro", prompt="continue")
    assert "THE-BRIEFING" in captured["messages"][0]["content"]
    # prompt lands after the summary, in the kept tail
    assert captured["messages"][-1]["content"] == "continue"


# ─── roundtable_bind_diff ────────────────────────────────────────────────

def _diff_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def g(*a):
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Test")
    g("add", "-A")
    g("-c", "commit.gpgsign=false", "commit", "-qm", "init")
    return path


def test_bind_diff_captures_tracked_untracked_and_filters_secrets(tmp_path):
    repo = _diff_repo(tmp_path / "wd")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("y = 'BRAND-NEW-LINE'\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=SECRETVALUE\n", encoding="utf-8")

    tid = core.roundtable_create("diff review")["thread_id"]
    res = core.roundtable_bind_diff(tid, str(repo))
    assert res["files_changed"] == 1
    assert res["untracked_included"] == 1
    assert res["artifact_version"] == 1

    art = core.roundtable_get_artifact(tid, "working-diff")["content"]
    assert "-x = 1" in art and "+x = 2" in art
    assert "BRAND-NEW-LINE" in art
    assert "SECRETVALUE" not in art  # untracked secret never surfaces

    binding = core.roundtable_repo_context(tid)
    assert binding is not None
    assert binding["permission_policy"] == "readonly"
    assert binding["working_directory"] == str(repo.resolve())


def test_bind_diff_excludes_tracked_secret_changes(tmp_path):
    repo = _diff_repo(tmp_path / "wd2")

    def g(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    (repo / ".env").write_text("TOKEN=OLD\n", encoding="utf-8")
    g("add", "-A")
    g("-c", "commit.gpgsign=false", "commit", "-qm", "add env")
    (repo / ".env").write_text("TOKEN=NEWSECRET\n", encoding="utf-8")
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")

    tid = core.roundtable_create("diff secrets")["thread_id"]
    res = core.roundtable_bind_diff(tid, str(repo))
    assert res["files_excluded"] == 1
    art = core.roundtable_get_artifact(tid, "working-diff")["content"]
    assert "NEWSECRET" not in art
    assert ".env" in art  # named as excluded so the panel knows it exists
    assert "+x = 3" in art


def test_bind_diff_clean_tree_and_non_repo_raise(tmp_path):
    repo = _diff_repo(tmp_path / "wd3")
    tid = core.roundtable_create("diff clean")["thread_id"]
    with pytest.raises(ValueError, match="clean"):
        core.roundtable_bind_diff(tid, str(repo))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="git work tree"):
        core.roundtable_bind_diff(tid, str(plain))


def test_bind_diff_versions_bump_on_recapture(tmp_path):
    repo = _diff_repo(tmp_path / "wd4")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    tid = core.roundtable_create("diff versions")["thread_id"]
    assert core.roundtable_bind_diff(tid, str(repo))["artifact_version"] == 1
    (repo / "a.py").write_text("x = 5\n", encoding="utf-8")
    assert core.roundtable_bind_diff(tid, str(repo))["artifact_version"] == 2
    assert "+x = 5" in core.roundtable_get_artifact(tid, "working-diff")["content"]
