"""Pure-logic + storage tests for roundtable.core that don't need live APIs.

Complements test_panel_tools.py (the _RepoTools executor + provider tool
loops). Covers transcript rendering, context-cap trimming, participant
resolution, effort validation, artifact versioning/diff, thread forking,
the stored-binding -> ToolUseContext resolver, the _run_turn panel-tools
dispatch decision, and DB-lock concurrency. The hermetic state dir is set
in conftest.py, so these never touch the host's real roundtable DB.
"""
from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path

import pytest

import roundtable.core as core


# ─── transcript rendering (golden) ───────────────────────────────────────

def test_format_transcript_labels_and_you_tag():
    msgs = [
        {"speaker": "orchestrator", "content": "review this", "idx": 0, "ts": 1.0},
        {"speaker": "Gemini Pro", "content": "looks fine", "idx": 1, "ts": 2.0},
    ]
    out = core._format_transcript(msgs, for_participant_label="Gemini Pro")
    assert out == (
        "[orchestrator]:\nreview this\n\n"
        "[Gemini Pro (you)]:\nlooks fine"
    )


# ─── context-cap trimming ────────────────────────────────────────────────

def _msg(speaker, content, idx):
    return {"speaker": speaker, "content": content, "idx": idx, "ts": float(idx)}


def test_trim_under_cap_is_unchanged():
    msgs = [_msg("orchestrator", "a", 0), _msg("Gemini Pro", "b", 1)]
    out = core._trim_messages_to_cap(msgs, 10_000, for_participant_label="Gemini Pro")
    assert [m["content"] for m in out] == ["a", "b"]


def test_trim_drops_whole_middle_messages_with_marker():
    # Many small middles, each well under per_message_cap, so the trimmer
    # drops WHOLE messages (not in-place truncation) once the budget fills —
    # that's the path that emits the system "older messages omitted" marker.
    msgs = (
        [_msg("orchestrator", "FIRST", 0)]
        + [_msg("p", f"middle-{i} " + "m" * 200, i + 1) for i in range(20)]
        + [_msg("orchestrator", "LAST", 21)]
    )
    out = core._trim_messages_to_cap(msgs, 1500, for_participant_label="p")
    contents = [m["content"] for m in out]
    assert contents[0] == "FIRST"
    assert contents[-1] == "LAST"
    assert any(
        m["speaker"] == core._OMITTED_MARKER_SPEAKER
        and "older messages omitted" in m["content"]
        for m in out
    )


def test_trim_truncates_oversized_single_body_in_place():
    msgs = [_msg("orchestrator", "Z" * 20_000, 0)]
    out = core._trim_messages_to_cap(msgs, 1000, for_participant_label="x")
    assert len(out) == 1
    assert "omitted within this message" in out[0]["content"]


# ─── participant resolution ──────────────────────────────────────────────

def test_resolve_participant_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)
    a = core._resolve_participant("Gemini-Pro")
    b = core._resolve_participant("  gemini-pro  ")
    assert a["provider"] == "gemini" and a == b


def test_resolve_unknown_participant_raises_valueerror(monkeypatch):
    monkeypatch.setattr(core, "_participant_provider_available", lambda *_: True)
    with pytest.raises(ValueError):
        core._resolve_participant("does-not-exist")


# ─── effort validation ───────────────────────────────────────────────────

def test_normalise_effort_empty_is_none_and_invalid_raises():
    assert core._normalise_effort("") is None
    assert core._normalise_effort(None) is None
    assert core._normalise_effort("high") == "high"
    with pytest.raises(ValueError):
        core._normalise_effort("higher")


# ─── artifact versioning + diff ──────────────────────────────────────────

def test_artifact_versions_bump_and_old_version_retrievable():
    tid = core.roundtable_create("artifact test")["thread_id"]
    r1 = core.roundtable_set_artifact(tid, "m.py", "v1 content\n")
    r2 = core.roundtable_set_artifact(tid, "m.py", "v2 content\n")
    assert r1["version"] == 1 and r2["version"] == 2
    assert core.roundtable_get_artifact(tid, "m.py", version=1)["content"] == "v1 content\n"
    assert core.roundtable_get_artifact(tid, "m.py", version=0)["content"] == "v2 content\n"
    # Each set appends a synthetic transcript turn announcing the artifact.
    hist = core.roundtable_history(tid)
    assert "m.py" in hist


# ─── thread forking ──────────────────────────────────────────────────────

def test_fork_copies_prefix_with_fresh_contiguous_idx():
    tid = core.roundtable_create("fork src")["thread_id"]
    core.roundtable_post(tid, "m0")
    core.roundtable_post(tid, "m1")
    core.roundtable_post(tid, "m2")
    forked = core.roundtable_fork(tid, upto_idx=1, new_topic="branch")
    assert forked["messages_copied"] == 2
    new_hist = core.roundtable_history(forked["thread_id"])
    assert "m0" in new_hist and "m1" in new_hist and "m2" not in new_hist
    # Source thread is untouched.
    assert "m2" in core.roundtable_history(tid)


# ─── stored-binding -> ToolUseContext resolver ───────────────────────────

def test_effective_tool_context_none_without_binding():
    tid = core.roundtable_create("no binding")["thread_id"]
    assert core._effective_tool_context(tid) is None


def test_effective_tool_context_readonly_binding(tmp_path):
    tid = core.roundtable_create("bound")["thread_id"]
    core.roundtable_bind_repo(tid, str(tmp_path), permission_policy="readonly")
    ctx = core._effective_tool_context(tid)
    assert ctx is not None
    assert str(ctx.working_directory) == str(tmp_path)
    assert ctx.allowed_tools == core._READONLY_TOOLS
    assert ctx.permission_callback is not None


# ─── _run_turn panel-tools dispatch (the C1 wiring) ──────────────────────

def _thread_dict(tid):
    return {"id": tid, "topic": "t", "participants": ["gemini-pro"], "house_rules": ""}


def _gemini_info():
    return {"provider": "gemini", "model": "gemini-pro-latest", "label": "Gemini Pro"}


def _ctx(tmp_path):
    return core.ToolUseContext(
        permission_callback=lambda *a: "allow",
        working_directory=tmp_path,
        allowed_tools=list(core._READONLY_TOOLS),
    )


def test_run_turn_routes_to_tools_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PANEL_TOOLS_ENABLED", True)
    fired = {"tools": False, "plain": False}
    monkeypatch.setattr(core, "_call_gemini_with_tools",
                        lambda *a, **k: fired.__setitem__("tools", True) or core.ProviderResult(text="tools"))
    monkeypatch.setattr(core, "_call_gemini",
                        lambda *a, **k: fired.__setitem__("plain", True) or core.ProviderResult(text="plain"))
    out = core._run_turn(_thread_dict(1), _gemini_info(), [_msg("orchestrator", "hi", 0)],
                         "", None, False, tool_use_context=_ctx(tmp_path))
    assert fired["tools"] and not fired["plain"]
    assert out.text == "tools"


def test_run_turn_skips_tools_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PANEL_TOOLS_ENABLED", False)
    fired = {"tools": False, "plain": False}
    monkeypatch.setattr(core, "_call_gemini_with_tools",
                        lambda *a, **k: fired.__setitem__("tools", True) or core.ProviderResult(text="tools"))
    monkeypatch.setattr(core, "_call_gemini",
                        lambda *a, **k: fired.__setitem__("plain", True) or core.ProviderResult(text="plain"))
    out = core._run_turn(_thread_dict(1), _gemini_info(), [_msg("orchestrator", "hi", 0)],
                         "", None, False, tool_use_context=_ctx(tmp_path))
    assert fired["plain"] and not fired["tools"]
    assert out.text == "plain"


# ─── DB-lock concurrency (reconstructs the deleted test_concurrency.py) ──

def test_concurrent_posts_get_contiguous_indices():
    tid = core.roundtable_create("concurrency")["thread_id"]
    workers, per = 8, 15

    def _spam(w):
        for i in range(per):
            core.roundtable_post(tid, f"w{w}-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_spam, range(workers)))

    idxs = sorted(m["idx"] for m in core._thread_messages(tid))
    assert idxs == list(range(workers * per))  # no dupes, no gaps


# ─── roundtable_bind_github (offline, via file:// clone) ─────────────────

def _local_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "hello.py").write_text("VALUE = 42\n", encoding="utf-8")

    def g(*a):
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Test")
    g("add", "-A")
    g("-c", "commit.gpgsign=false", "commit", "-qm", "init")
    return path


def test_bind_github_clones_strips_git_and_binds_readonly(tmp_path):
    src = _local_git_repo(tmp_path / "src")
    tid = core.roundtable_create("gh ok")["thread_id"]
    res = core.roundtable_bind_github(tid, f"file://{src}")
    assert len(res["commit_sha"]) == 40
    assert res["file_count"] == 1
    ctx = core._effective_tool_context(tid)
    assert ctx is not None and ctx.allowed_tools == core._READONLY_TOOLS
    root = Path(res["working_directory"])
    assert (root / "hello.py").is_file()
    assert not (root / ".git").exists()  # VCS metadata stripped


def test_bind_github_bad_repo_raises_and_leaves_no_binding(tmp_path):
    tid = core.roundtable_create("gh fail")["thread_id"]
    with pytest.raises(Exception):
        core.roundtable_bind_github(tid, f"file://{tmp_path}/nope")
    assert core.roundtable_repo_context(tid) is None


# ─── roundtable_repo_pack ────────────────────────────────────────────────

def test_repo_pack_injects_tree_and_file_contents(tmp_path):
    src = tmp_path / "repo"
    (src / "pkg").mkdir(parents=True)
    (src / "README.md").write_text("hello readme\n", encoding="utf-8")
    (src / "pkg" / "mod.py").write_text("def f():\n    return 'NEEDLE'\n", encoding="utf-8")
    tid = core.roundtable_create("pack test")["thread_id"]
    core.roundtable_bind_repo(tid, str(src), permission_policy="readonly")
    res = core.roundtable_repo_pack(tid, query="NEEDLE")
    assert res["files_included"] == 2
    hist = core.roundtable_history(tid)
    assert "README.md" in hist and "pkg/mod.py" in hist
    assert "NEEDLE" in hist  # file contents are inlined, not just the tree


def test_repo_pack_requires_a_binding():
    tid = core.roundtable_create("pack nobind")["thread_id"]
    with pytest.raises(RuntimeError):
        core.roundtable_repo_pack(tid)


# ─── CLI failure diagnostics ─────────────────────────────────────────────

def test_cli_failure_surfaces_stdout_when_stderr_empty(monkeypatch):
    """The silent exit=1 a concurrent roundtable hit had an empty stderr;
    the real error must still surface from stdout."""
    from types import SimpleNamespace
    monkeypatch.setattr(core, "_CLAUDE_CLI", "/bin/false")

    def fake_run(*a, **k):
        return SimpleNamespace(returncode=1, stdout="CONCURRENT_SESSION_CONFLICT", stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as ei:
        core._call_anthropic_cli("claude-x", "sys", "transcript", "", None, False)
    assert "CONCURRENT_SESSION_CONFLICT" in str(ei.value)
