"""Panel review helpers: commit spotting, working state, verdicts, one round."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

import panel


@pytest.mark.parametrize("command, expected", [
    ("git commit -m x", ["/r"]),
    ("git -C /repo commit -qam msg", ["/repo"]),
    ("cd /repo && git commit -m x", ["/repo"]),
    ("cd sub && git commit", ["/r/sub"]),
    ("git status && git diff", []),
    ("GIT_NO_AUTOPUSH=1 git commit -m x", ["/r"]),
    ("git -c user.name=x commit -m y", ["/r"]),
    ("git commit --amend --no-edit", ["/r"]),
    ("/usr/bin/git commit -m x", ["/r"]),
    ("sudo -u matt git -C /s commit -m x", ["/s"]),
    ("git -C /a status; git -C /b commit -m x", ["/b"]),
    ("echo 'git commit'", []),
    ("git log --grep commit", []),
    ("git -C /repo \\\n  commit -m x", ["/repo"]),
    ("bash -c 'cd /repo && git commit -m x'", ["/repo"]),
    ("git add -A && git commit -F - <<'EOF'\nfix: git commit hook\nmore\nEOF\n", ["/r"]),
    ("git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\"", ["/r"]),
    ("timeout 60 git commit -m x", ["/r"]),
    ("git --work-tree=/w commit -m x", ["/w"]),
])
def test_commit_targets(command, expected):
    assert panel.commit_targets(command, "/r") == expected


@pytest.mark.parametrize("command", [
    "cd - && git commit -m x",
    "git commit -m \"unbalanced",
])
def test_commit_targets_it_cannot_place_ask_for_git_dash_c(command):
    assert panel.commit_targets(command, "/r") is None


def test_unbalanced_quotes_without_a_commit_are_not_a_commit():
    assert panel.commit_targets("echo \"unbalanced", "/r") == []


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "a.py").write_text("a = 1\n")
    (r / "b.py").write_text("b = 1\n")
    (r / ".gitignore").write_text("cache/\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


def test_working_state_covers_every_kind_of_change(repo):
    assert panel.working_state(str(repo)) == {}
    (repo / "a.py").write_text("a = 2\n")
    (repo / "new.py").write_text("n = 1\n")
    (repo / "b.py").unlink()
    (repo / "cache").mkdir()
    (repo / "cache" / "junk").write_text("ignored")
    state = panel.working_state(str(repo))
    assert set(state) == {"a.py", "new.py", "b.py"}
    assert state["b.py"] == "deleted"
    assert state["a.py"] != state["new.py"]


def test_a_staged_rename_lists_both_paths(repo):
    _git(repo, "mv", "a.py", "renamed.py")
    state = panel.working_state(str(repo))
    assert state["a.py"] == "deleted"
    assert state["renamed.py"] not in ("deleted", "dir")


def test_approval_covers_a_split_commit_but_not_a_later_edit(repo):
    (repo / "a.py").write_text("a = 2\n")
    (repo / "b.py").write_text("b = 2\n")
    approved = panel.working_state(str(repo))
    _git(repo, "commit", "-qm", "first half", "a.py")
    assert panel.unapproved(approved, panel.working_state(str(repo))) == []
    (repo / "b.py").write_text("b = 3\n")
    assert panel.unapproved(approved, panel.working_state(str(repo))) == ["b.py"]


def test_repo_root_finds_the_repo_for_a_file_not_created_yet(repo, tmp_path):
    assert panel.repo_root(str(repo / "pkg" / "deep" / "new.py")) == str(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert panel.repo_root(str(outside / "x.txt")) is None
    assert panel.repo_root("relative/path.py") is None


@pytest.mark.parametrize("text, verdict", [
    ("Looks right.\nVERDICT: APPROVE", "approve"),
    ("1. Bug at a.py:3\n**VERDICT:** CHANGES NEEDED", "changes"),
    ("verdict - approved", "approve"),
    ("VERDICT: changes\n...\nOn reflection, VERDICT: APPROVE", "approve"),
    ("I think it's fine.", "unclear"),
    ("VERDICT: REQUEST CHANGES", "changes"),
])
def test_parse_verdict(text, verdict):
    assert panel.parse_verdict(text) == verdict


def test_state_round_trips_and_survives_junk():
    st = panel.PanelState(enabled=True, thread_id=7, plan_ok=True,
                          rounds={"plan": 2, "changes": 1}, approvals={"/r": {"a.py": "x"}})
    assert panel.PanelState.from_json(st.to_json()) == st
    assert panel.PanelState.from_json("not json") == panel.PanelState()
    assert panel.PanelState.from_json('{"enabled": true, "unknown": 1}').enabled is True
    st.end_cycle()
    assert st.plan_ok is False and st.rounds == {"plan": 0, "changes": 0}


def test_user_prefix_strips_back_to_what_the_user_typed():
    sent = panel.user_prefix() + "please fix the login bug"
    assert sent.startswith(panel.USER_PREFIX_MARKER)
    assert panel.strip_user_prefix(sent) == "please fix the login bug"
    assert panel.strip_user_prefix("plain message") == "plain message"


def test_result_message_carries_the_marker_every_reply_and_the_next_step():
    msg = panel.result_message(
        "plan", 1, 3, "changes",
        [{"label": "GPT Sol", "verdict": "changes", "text": "Wrong file.\nVERDICT: CHANGES NEEDED"},
         {"label": "Gemini Pro", "verdict": "approve", "text": "Fine.\nVERDICT: APPROVE"}],
        [{"label": "Claude Sonnet", "error": "TimeoutError: slow"}],
        "Matt",
    )
    assert msg.startswith(panel.MESSAGE_MARKER)
    assert "### GPT Sol: wants changes" in msg
    assert "### Gemini Pro: approves" in msg
    assert "### Claude Sonnet: couldn't answer" in msg
    assert "Don't change files until they approve" in msg


class FakeCore:
    """Just enough of roundtable.core for one round, recording every call."""

    PARTICIPANTS = {
        "gpt-5": {"label": "GPT Sol"},
        "gemini-pro": {"label": "Gemini Pro"},
    }

    def __init__(self, responses=None, errors=None, stream=()):
        self.responses = responses or {}
        self.errors = errors or {}
        self.stream = set(stream)
        self.calls = []

    def roundtable_create(self, topic, participants, house_rules):
        self.calls.append(("create", topic))
        return {"thread_id": 41}

    def roundtable_bind_repo(self, tid, repo, policy):
        self.calls.append(("bind_repo", tid, repo, policy))

    def roundtable_bind_diff(self, tid, repo, base):
        self.calls.append(("bind_diff", tid, repo, base))
        return {"truncated": False}

    def roundtable_post(self, tid, content, speaker):
        self.calls.append(("post", speaker, content))

    def _effective_tool_context(self, tid):
        return "ctx"

    def roundtable_ask_parallel(self, tid, participants, prompt, effort, tool_use_context,
                                on_result=None):
        self.calls.append(("ask", tuple(participants), prompt, tool_use_context))
        for name, text in self.responses.items():
            if on_result and name in self.stream:
                on_result(name, text, None)
        return {"responses": self.responses, "errors": self.errors}


def _round(core, **kw):
    events = []
    threads = []
    args = dict(
        stage="plan", repo="/r", message="the plan", notes=["Matt: do X"],
        thread_id=None, participants=["gpt-5", "gemini-pro"], round_no=1,
        max_rounds=3, plan_reviewed=False, user_label="Matt", topic="t",
        emit=events.append, on_thread=threads.append,
    )
    args.update(kw)
    result = asyncio.run(panel.run_round(core, **args))
    return result, events, threads


def test_a_plan_round_creates_the_thread_posts_both_voices_and_approves():
    core = FakeCore(responses={"gpt-5": "ok\nVERDICT: APPROVE", "gemini-pro": "VERDICT: APPROVE"})
    result, events, threads = _round(core)
    assert threads == [41]
    assert ("bind_repo", 41, "/r", "readonly") in core.calls
    posts = [c for c in core.calls if c[0] == "post"]
    assert posts == [("post", "Matt", "Matt: do X"), ("post", panel.AUTHOR, "the plan")]
    assert result.outcome == "approved"
    assert [e["type"] for e in events] == ["panel_round", "panel_reply", "panel_reply"]
    assert events[0]["participants"] == ["GPT Sol", "Gemini Pro"]
    assert not any(c[0] == "bind_diff" for c in core.calls)


def test_one_objection_means_changes_and_an_error_is_reported_not_counted():
    core = FakeCore(responses={"gpt-5": "bug\nVERDICT: CHANGES NEEDED"},
                    errors={"gemini-pro": "RateLimitError: 429"})
    result, events, _ = _round(core, thread_id=9)
    assert result.outcome == "changes"
    assert result.errors == [{"participant": "gemini-pro", "label": "Gemini Pro", "error": "RateLimitError: 429"}]
    assert not any(c[0] == "create" for c in core.calls)
    assert events[-1]["verdict"] == "error"


def test_nobody_answering_is_unavailable():
    core = FakeCore(errors={"gpt-5": "x", "gemini-pro": "y"})
    result, _, _ = _round(core)
    assert result.outcome == "unavailable"


def test_a_changes_round_attaches_the_diff_and_records_what_was_reviewed(repo):
    (repo / "a.py").write_text("a = 5\n")
    core = FakeCore(responses={"gpt-5": "VERDICT: APPROVE", "gemini-pro": "VERDICT: APPROVE"})
    result, _, _ = _round(core, stage="changes", repo=str(repo), notes=[])
    assert ("bind_diff", 41, str(repo), "HEAD") in core.calls
    assert set(result.reviewed_state) == {"a.py"}
    ask = next(c for c in core.calls if c[0] == "ask")
    assert "No plan was reviewed" in ask[2]


def test_a_repository_the_panel_cannot_open_still_gets_reviewed():
    class FencedCore(FakeCore):
        def roundtable_bind_repo(self, tid, repo, policy):
            raise ValueError("outside ROUNDTABLE_REPO_ROOTS")

    core = FencedCore(responses={"gpt-5": "VERDICT: APPROVE", "gemini-pro": "VERDICT: APPROVE"})
    result, _, _ = _round(core)
    assert result.outcome == "approved"
    ask = next(c for c in core.calls if c[0] == "ask")
    assert "couldn't be opened for you" in ask[2]


def test_replies_stream_as_they_land_and_are_not_shown_twice():
    core = FakeCore(responses={"gpt-5": "VERDICT: APPROVE", "gemini-pro": "VERDICT: APPROVE"},
                    stream={"gemini-pro"})
    _, events, _ = _round(core)
    replies = [e["participant"] for e in events if e["type"] == "panel_reply"]
    assert replies == ["Gemini Pro", "GPT Sol"]
