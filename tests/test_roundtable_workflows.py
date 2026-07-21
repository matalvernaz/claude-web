"""Offline tests for the high-level coding workflow shared by MCP and web."""
from __future__ import annotations

import asyncio
import subprocess

import jsonschema
import pytest

import roundtable.core as core


def _allow_all_participants(monkeypatch) -> None:
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)


def _git_repo(path):
    path.mkdir()
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "initial"],
        cwd=path, check=True,
    )
    return path


def test_coding_profiles_are_stable_and_validate_unknown_tasks():
    profiles = core.roundtable_coding_profiles()
    assert list(profiles) == [
        "general", "debug", "review", "plan", "implement", "test", "explain",
    ]
    assert profiles["review"]["capture_diff"] is True
    assert profiles["review"]["verified_review"] is True
    with pytest.raises(ValueError, match="Unknown coding task"):
        core.roundtable_coding_panel_prompt("rewrite-everything", [])


def test_coding_review_schema_is_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(
        core.roundtable_coding_review_schema()
    )


def test_high_level_coding_workflow_is_registered_as_mcp_tool():
    from roundtable.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert "roundtable_coding_task" in by_name
    assert "roundtable_coding_profiles" in by_name
    schema = by_name["roundtable_coding_task"].inputSchema
    assert {"prompt", "task", "working_directory", "verify_review"} <= set(
        schema["properties"]
    )


def test_panel_prompt_assigns_distinct_task_lenses(monkeypatch):
    _allow_all_participants(monkeypatch)
    prompt = core.roundtable_coding_panel_prompt("debug", ["gemini-pro", "gpt-5"])
    assert "Coding workflow: Debug" in prompt
    assert "Gemini Pro: Root-cause investigator" in prompt
    assert "GPT-5: Hypothesis falsifier" in prompt
    assert "Do not merge" in prompt


def test_review_findings_are_deduplicated_sorted_and_capped(monkeypatch):
    monkeypatch.setattr(core, "_CODING_REVIEW_MAX_FINDINGS", 2)
    results = {
        "gemini-pro": {
            "summary": "two",
            "findings": [
                {"claim": "low issue", "file": "z.py", "line": 9,
                 "proof": "z", "severity": "low", "category": "testing"},
                {"claim": "serious issue", "file": "a.py", "line": 3,
                 "proof": "a", "severity": "high", "category": "correctness"},
            ],
        },
        "gpt-5": {
            "summary": "same plus one",
            "findings": [
                {"claim": " serious   issue ", "file": "a.py", "line": 3,
                 "proof": "duplicate", "severity": "high", "category": "correctness"},
                {"claim": "medium issue", "file": "m.py", "line": 5,
                 "proof": "m", "severity": "medium", "category": "reliability"},
            ],
        },
    }
    out = core.roundtable_coding_review_findings(results)
    assert out["total"] == 3
    assert out["omitted"] == 1
    assert [f["claim"] for f in out["findings"]] == [
        "serious issue", "medium issue",
    ]
    assert out["findings"][0]["reviewer"] == "Gemini Pro"


def test_review_synthesis_contract_uses_verification_ledger():
    prompt = core.roundtable_coding_synthesis_prompt(
        "review", ["Gemini Pro", "GPT-5"], "Claude Opus", True,
        {"status": "completed", "summary": {
            "confirmed": 2, "refuted": 1, "unresolved": 1,
        }},
    )
    assert "2 confirmed, 1 refuted, and 1 unresolved" in prompt
    assert "omit refuted claims" in prompt
    assert "```diff path/to/file.ext" in prompt


def test_general_coding_task_binds_repo_and_runs_panel_then_synth(
    tmp_path, monkeypatch,
):
    _allow_all_participants(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: dict = {}

    def fake_parallel(thread_id, participants, prompt="", **kwargs):
        calls["panel_prompt"] = prompt
        calls["tool_context"] = kwargs["tool_use_context"]
        return {"responses": {"gemini-pro": "panel answer"}, "errors": {}}

    def fake_ask(thread_id, participant, prompt="", **kwargs):
        calls["synth_prompt"] = prompt
        return "final answer"

    monkeypatch.setattr(core, "roundtable_ask_parallel", fake_parallel)
    monkeypatch.setattr(core, "roundtable_ask", fake_ask)

    out = core.roundtable_coding_task(
        "How should this be implemented?", task="general",
        working_directory=str(repo), participants=["gemini-pro"],
        synthesizer="claude-opus",
    )

    assert out["synthesis"] == "final answer"
    assert out["grounding"]["repo_bound"] is True
    assert core.roundtable_repo_context(out["thread_id"])["working_directory"] == str(repo)
    assert "Coding workflow: General" in calls["panel_prompt"]
    assert calls["tool_context"].working_directory == str(repo)
    assert "synthesizing the General coding workflow" in calls["synth_prompt"]


def test_review_coding_task_captures_diff_and_verifies_findings(
    tmp_path, monkeypatch,
):
    _allow_all_participants(monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    seen: dict = {}

    def fake_structured(thread_id, participants, schema, prompt="", effort=""):
        seen["schema"] = schema
        seen["panel_prompt"] = prompt
        return {
            "results": {
                "gemini-pro": {
                    "summary": "one bug",
                    "findings": [{
                        "claim": "the changed value breaks the contract",
                        "file": "app.py", "line": 1, "proof": "value = 2",
                        "severity": "high", "category": "correctness",
                    }],
                },
            },
            "errors": {},
        }

    def fake_converge(thread_id, findings, verifier, transport):
        seen["findings"] = findings
        assert verifier == "claude-opus"
        assert transport == "auto"
        return {
            "ledger": [{
                "claim": findings[0]["claim"], "file": "app.py", "line": 1,
                "proof": "value = 2", "severity": "high",
                "verifier": "Claude Opus", "verdict": "confirmed",
                "evidence": "line 1",
            }],
            "summary": {"confirmed": 1, "refuted": 0, "unresolved": 0},
        }

    monkeypatch.setattr(core, "roundtable_ask_structured", fake_structured)
    monkeypatch.setattr(core, "roundtable_converge", fake_converge)
    monkeypatch.setattr(core, "roundtable_ask", lambda *a, **k: "review synthesis")

    out = core.roundtable_coding_task(
        "Review my changes", task="review", working_directory=str(repo),
        participants=["gemini-pro"], synthesizer="claude-opus",
    )

    assert out["grounding"]["diff"]["artifact_version"] == 1
    assert "working-diff" in core.roundtable_history(out["thread_id"])
    assert seen["schema"]["additionalProperties"] is False
    assert seen["findings"][0]["reviewer"] == "Gemini Pro"
    assert out["verification"]["summary"]["confirmed"] == 1
    assert out["verification"]["ledger"][0]["category"] == "correctness"
    assert "Grounded review verification ledger" in core.roundtable_history(
        out["thread_id"]
    )


def test_clean_review_falls_back_to_tool_grounded_freeform_panel(
    tmp_path, monkeypatch,
):
    _allow_all_participants(monkeypatch)
    repo = _git_repo(tmp_path / "repo")
    called = {"parallel": 0}
    tid = core.roundtable_create(
        "older review", participants=["claude-opus", "gemini-pro"],
    )["thread_id"]
    core.roundtable_set_artifact(tid, "working-diff", "STALE-DIFF")

    def fake_parallel(*args, **kwargs):
        called["parallel"] += 1
        return {"responses": {"gemini-pro": "clean-tree review"}, "errors": {}}

    monkeypatch.setattr(core, "roundtable_ask_parallel", fake_parallel)
    monkeypatch.setattr(
        core, "roundtable_ask_structured",
        lambda *a, **k: pytest.fail("stale artifact selected structured review"),
    )
    monkeypatch.setattr(core, "roundtable_ask", lambda *a, **k: "final")

    out = core.roundtable_coding_task(
        "Review this repository", task="review", working_directory=str(repo),
        thread_id=tid, participants=["gemini-pro"], synthesizer="claude-opus",
    )

    assert called["parallel"] == 1
    assert out["grounding"]["repo_bound"] is True
    assert "clean against" in out["grounding"]["warning"]
    assert "do not treat an older working-diff artifact" in core.roundtable_history(tid)
    assert out["verification"] == {
        "status": "skipped",
        "reason": "no review artifact was available for structured findings",
    }
