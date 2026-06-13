"""Tests for the Layer-2 read-only repo tool executor (_RepoTools).

The executor is the security-critical, fully-deterministic core of the
Gemini/OpenAI repo-grounding feature: permission gating, working-directory
jailing, and result caps. The provider function-calling loops around it need
live APIs to verify; this exercises everything that doesn't.
"""
from __future__ import annotations

import roundtable.core as core


def _allow_all(label, name, args):
    return "allow"


def _deny_all(label, name, args):
    return "deny"


def _mk(tmp_path, cb=_allow_all):
    return core._RepoTools(tmp_path, cb, "gemini")


def test_read_returns_contents(tmp_path):
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    assert _mk(tmp_path).execute("Read", {"path": "a.txt"}) == "hello world"


def test_read_denied_by_permission(tmp_path):
    (tmp_path / "a.txt").write_text("secret", encoding="utf-8")
    out = _mk(tmp_path, _deny_all).execute("Read", {"path": "a.txt"})
    assert "permission denied" in out
    assert "secret" not in out


def test_read_traversal_blocked(tmp_path):
    # A path escaping the root must not resolve, even though the file exists.
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    try:
        out = _mk(tmp_path).execute("Read", {"path": "../outside_secret.txt"})
        assert "TOPSECRET" not in out
        assert "no such file" in out
    finally:
        secret.unlink()


def test_read_symlink_escape_blocked(tmp_path):
    import os
    secret = tmp_path.parent / "linked_secret.txt"
    secret.write_text("LINKEDSECRET", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(secret, link)
    except OSError:
        return  # platform without symlink perms — skip
    try:
        out = _mk(tmp_path).execute("Read", {"path": "link.txt"})
        # resolve() collapses the symlink to outside-root → jailed out.
        assert "LINKEDSECRET" not in out
    finally:
        secret.unlink()


def test_read_truncates_large_file(tmp_path):
    big = "x" * (core._TOOL_READ_MAX_BYTES + 5000)
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    out = _mk(tmp_path).execute("Read", {"path": "big.txt"})
    assert "truncated" in out
    assert len(out) <= core._TOOL_READ_MAX_BYTES + 50


def test_grep_finds_matches_with_location(tmp_path):
    (tmp_path / "f.py").write_text("import os\nx = 1\nimport sys\n", encoding="utf-8")
    out = _mk(tmp_path).execute("Grep", {"pattern": r"^import "})
    assert "f.py:1:import os" in out
    assert "f.py:3:import sys" in out
    assert "x = 1" not in out


def test_grep_bad_regex_is_message_not_crash(tmp_path):
    out = _mk(tmp_path).execute("Grep", {"pattern": "(unclosed"})
    assert out.startswith("[grep: bad regex")


def test_grep_respects_permission(tmp_path):
    (tmp_path / "f.py").write_text("needle", encoding="utf-8")
    out = _mk(tmp_path, _deny_all).execute("Grep", {"pattern": "needle"})
    assert "permission denied" in out


def test_glob_lists_relative_paths(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "b.py").write_text("", encoding="utf-8")
    (tmp_path / "c.txt").write_text("", encoding="utf-8")
    out = _mk(tmp_path).execute("Glob", {"pattern": "**/*.py"})
    lines = set(out.splitlines())
    assert "b.py" in lines
    assert "pkg/a.py" in lines
    assert "c.txt" not in lines


def test_unknown_tool(tmp_path):
    assert "unknown tool" in _mk(tmp_path).execute("Bash", {"command": "rm -rf /"})


def test_permission_callback_fault_denies(tmp_path):
    def _raises(label, name, args):
        raise RuntimeError("callback boom")

    (tmp_path / "a.txt").write_text("data", encoding="utf-8")
    out = _mk(tmp_path, _raises).execute("Read", {"path": "a.txt"})
    assert "permission denied" in out


# ─── provider loop control (mocked SDK objects) ──────────────────────────
# These validate the loop mechanics — detect function calls, execute via the
# gated executor, feed results back, terminate on a text turn. They mock the
# SDK response shapes, so they catch loop-control bugs (infinite loop, missing
# termination, dropped result) but NOT a wrong live wire format.
from types import SimpleNamespace


def test_openai_tool_loop_executes_then_finishes(tmp_path, monkeypatch):
    import roundtable.core as core
    (tmp_path / "f.py").write_text("import os\n", encoding="utf-8")

    calls = {"n": 0}

    class _FakeResponses:
        def create(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                fc = SimpleNamespace(
                    type="function_call", name="Grep", call_id="c1",
                    arguments='{"pattern": "import"}',
                )
                return SimpleNamespace(output=[fc], output_text="", usage=None)
            # Second round: the model produced its answer.
            return SimpleNamespace(
                output=[], output_text="Found the import on f.py:1", usage=None,
            )

    monkeypatch.setattr(core, "_openai", SimpleNamespace(responses=_FakeResponses()))
    tools = core._RepoTools(tmp_path, _allow_all, "gpt-5")
    result = core._call_openai_with_tools(
        "gpt-5", "sys", "transcript", "", None, False, tools,
    )
    assert calls["n"] == 2  # one tool round, then the final answer
    assert "f.py:1" in result.text


def test_openai_tool_loop_caps_rounds(tmp_path, monkeypatch):
    """A model that never stops calling tools is bounded by _PANEL_TOOL_MAX_ROUNDS."""
    import roundtable.core as core
    (tmp_path / "f.py").write_text("x\n", encoding="utf-8")
    calls = {"n": 0}

    class _FakeResponses:
        def create(self, **kw):
            calls["n"] += 1
            fc = SimpleNamespace(
                type="function_call", name="Glob", call_id=f"c{calls['n']}",
                arguments='{"pattern": "*.py"}',
            )
            return SimpleNamespace(output=[fc], output_text="", usage=None)

    monkeypatch.setattr(core, "_openai", SimpleNamespace(responses=_FakeResponses()))
    tools = core._RepoTools(tmp_path, _allow_all, "gpt-5")
    core._call_openai_with_tools("gpt-5", "s", "t", "", None, False, tools)
    assert calls["n"] == core._PANEL_TOOL_MAX_ROUNDS


def test_gemini_tool_loop_executes_then_finishes(tmp_path, monkeypatch):
    import roundtable.core as core
    (tmp_path / "f.py").write_text("import os\n", encoding="utf-8")
    calls = {"n": 0}

    class _FakeModels:
        def generate_content(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                fc = SimpleNamespace(name="Read", args={"path": "f.py"})
                part = SimpleNamespace(function_call=fc, text=None)
                cand = SimpleNamespace(content=SimpleNamespace(parts=[part]))
                return SimpleNamespace(candidates=[cand], text=None, usage_metadata=None)
            part = SimpleNamespace(function_call=None, text="done")
            cand = SimpleNamespace(content=SimpleNamespace(parts=[part]))
            return SimpleNamespace(candidates=[cand], text="The file imports os.",
                                   usage_metadata=None)

    monkeypatch.setattr(core, "_gemini", SimpleNamespace(models=_FakeModels()))
    tools = core._RepoTools(tmp_path, _allow_all, "gemini")
    result = core._call_gemini_with_tools(
        "gemini-pro-latest", "sys", "t", "", None, False, tools,
    )
    assert calls["n"] == 2
    assert "imports os" in result.text


def test_grep_symlink_escape_blocked(tmp_path):
    """Grep must not read through an in-repo symlink whose target is outside
    the repo (the rglob enumeration would otherwise leak external content)."""
    import os
    secret = tmp_path.parent / "grep_secret.txt"
    secret.write_text("GREP_LEAKED_SECRET", encoding="utf-8")
    (tmp_path / "real.txt").write_text("nothing here", encoding="utf-8")
    link = tmp_path / "leak.txt"
    try:
        os.symlink(secret, link)
    except OSError:
        return  # no symlink perms — skip
    try:
        out = _mk(tmp_path).execute("Grep", {"pattern": "SECRET"})
        assert "GREP_LEAKED_SECRET" not in out
    finally:
        secret.unlink()
