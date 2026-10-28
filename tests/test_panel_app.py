"""Panel review wired into the app: gates, rounds, delivery, the switch."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import uuid

import pytest

import app as app_module
import panel
from tests import panel_support

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="panel review is off on Windows (see app._panel_available)",
)


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
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")
    return r


class FakeCore:
    """The slice of roundtable.core panel review uses, scripted per test."""

    _RepoTools = panel_support.SourceTools

    PARTICIPANTS = {"gpt-5": {"label": "GPT Sol"}, "gemini-pro": {"label": "Gemini Pro"}}

    def __init__(self, answers):
        self.answers = list(answers)  # one {"responses":…, "errors":…} per round
        self.posts = []
        self.asks = 0
        self.closed = {}

    def _default_coding_panel(self, synthesizer):
        return ["gpt-5", "gemini-pro"]

    def _participant_provider_available(self, name):
        return True

    def roundtable_create(self, topic, participants, house_rules):
        return {"thread_id": 77}

    def _thread_row(self, tid):
        return {"id": tid, "closed_at": self.closed.get(tid)}

    def roundtable_bind_repo(self, tid, repo, policy):
        self.root = repo
        return {}

    def roundtable_bind_diff(self, tid, repo, base):
        return {"truncated": False}

    def roundtable_post(self, tid, content, speaker):
        self.posts.append((speaker, content))

    def _effective_tool_context(self, tid):
        return panel_support.context(self.root)

    def roundtable_ask_parallel(self, tid, participants, prompt, effort, tool_use_context,
                                on_result=None, **kwargs):
        self.asks += 1
        result = self.answers.pop(0)
        return {**result, "responses": {p: panel_support.report(t, self.root,
                    kwargs.get("participant_prompts", {}).get(p, ""))
                for p, t in result.get("responses", {}).items()}}


APPROVE_ALL = {"responses": {"gpt-5": "Fine.\nVERDICT: APPROVE", "gemini-pro": "OK\nVERDICT: APPROVE"}}
OBJECT = {"responses": {"gpt-5": "Bug at a.py:1\nVERDICT: CHANGES NEEDED",
                        "gemini-pro": "OK\nVERDICT: APPROVE"}}


@pytest.mark.parametrize("round_no", [1, panel.MAX_ROUNDS])
async def test_corrected_approval_releases_the_gate_without_another_author_round(chat, repo, monkeypatch, round_no):
    run, events, delivered = chat
    state = app_module._panel_load(run.session_id)
    state.rounds["plan"] = round_no - 1
    app_module._panel_save(run.session_id, run.owner_sub, state)
    bad = {"summary": "Looks correct", "checks": [], "findings": [], "resolutions": [],
           "limitations": [], "verdict": "approve"}
    core = FakeCore([
        {"responses": {"gpt-5": "Fine.\nVERDICT: APPROVE", "gemini-pro": json.dumps(bad)}},
        {"responses": {"gemini-pro": "Fine.\nVERDICT: APPROVE"}},
    ])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    await _review(run, stage="plan", repo=str(repo), message="Change a to 2.")
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert core.asks == 2
    assert state.plan_ok and state.rounds["plan"] == 0
    assert f"Round {round_no} of" in delivered[-1] and "approved" in delivered[-1]
    assert [e["type"] for e in events][:4] == [
        "panel_round", "panel_reply", "panel_reply", "panel_verdict",
    ]
    assert not any(e["type"] == "question_request" for e in events)


async def test_user_requirements_remain_citable_without_a_new_user_message(chat, repo, monkeypatch):
    run, _, _ = chat
    prompts = []

    class CaptureCore(FakeCore):
        def roundtable_ask_parallel(self, *args, **kwargs):
            prompts.append(kwargs["participant_prompts"])
            return super().roundtable_ask_parallel(*args, **kwargs)

    monkeypatch.setattr(app_module, "roundtable_core", CaptureCore([OBJECT, APPROVE_ALL]))
    for number, notes in ((1, ["Preserve the user's saved value."]), (2, [])):
        await app_module._panel_round_task(
            run, run.session_id, "plan", str(repo), "The proposed plan.", notes,
            ["gpt-5", "gemini-pro"], number,
        )
    assert "1: Preserve the user's saved value." in prompts[1]["gpt-5"]
    state = app_module._panel_load(run.session_id)
    assert state.review_requirements[str(repo)] == ["Preserve the user's saved value."]


@pytest.fixture
def chat(monkeypatch):
    """A live Claude run with panel review on, its events and deliveries."""
    sid = "panel-" + uuid.uuid4().hex[:8]
    run = app_module.ActiveRun("run-" + sid, owner_sub="anonymous")
    run.provider = "claude"
    run.session_id = sid
    run.project_key = "-tmp-project"
    run.panel_user_label = "Matt"
    app_module.ACTIVE_RUNS_BY_SESSION[sid] = run
    events = []
    real_emit = run.emit

    def emit(event):
        events.append(event)
        real_emit(event)

    run.emit = emit
    delivered = []

    async def fake_inject(target, text, blocks, image_count, file_count, **kw):
        delivered.append(text)
        return None

    monkeypatch.setattr(app_module, "_inject_user_input", fake_inject)
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", True)
    app_module._panel_set_enabled(sid, "anonymous", True)
    yield run, events, delivered
    app_module.ACTIVE_RUNS_BY_SESSION.pop(sid, None)
    task = app_module._PANEL_ROUNDS.pop(sid, None)
    if task is not None:
        task.cancel()


def _hooks(run):
    hooks = app_module._panel_hooks_for_run(run)
    edit = hooks["PreToolUse"][0].hooks[0]
    commit = hooks["PreToolUse"][1].hooks[0]
    after = hooks["PostToolUse"][0].hooks[0]
    return edit, commit, after


def _decision(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


async def test_the_edit_gate_stops_repo_edits_until_a_plan_is_approved(chat, repo, tmp_path):
    run, events, _ = chat
    edit, _, _ = _hooks(run)
    inp = {"tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}
    denied = await edit(inp, "t1", {})
    assert _decision(denied) == "deny"
    assert panel.TOOL_NAME in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert events[-1] == {"type": "panel_gate", "gate": "edit", "repo": str(repo), "_idx": events[-1]["_idx"]}
    # Outside any repository nothing is stopped.
    loose = tmp_path / "notes.txt"
    assert await edit({"tool_input": {"file_path": str(loose)}, "cwd": str(tmp_path)}, "t2", {}) == {}
    # A subagent is told to report back rather than call the panel itself.
    sub = await edit({**inp, "agent_id": "agent-1"}, "t3", {})
    assert "Report back to the main agent" in sub["hookSpecificOutput"]["permissionDecisionReason"]
    # Once the plan is approved, edits go through.
    state = app_module._panel_load(run.session_id)
    state.plan_ok = True
    app_module._panel_save(run.session_id, run.owner_sub, state)
    assert await edit(inp, "t4", {}) == {}


async def test_nothing_is_gated_while_the_panel_is_off(chat, repo):
    run, _, _ = chat
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    edit, commit, _ = _hooks(run)
    assert await edit({"tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}, "t", {}) == {}
    (repo / "a.py").write_text("a = 2\n")
    assert await commit({"tool_input": {"command": f"git -C {repo} commit -am x"}, "cwd": "/"}, "t", {}) == {}


async def test_the_commit_gate_holds_until_the_panel_approved_this_exact_state(chat, repo):
    run, _, _ = chat
    _, commit, after = _hooks(run)
    (repo / "a.py").write_text("a = 2\n")
    inp = {"tool_input": {"command": f"git -C {repo} commit -qam change"}, "cwd": "/"}
    denied = await commit(inp, "c1", {})
    assert _decision(denied) == "deny"
    assert "a.py" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    state = app_module._panel_load(run.session_id)
    state.plan_ok = True
    state.approvals[str(repo)] = panel.working_state(str(repo))
    app_module._panel_save(run.session_id, run.owner_sub, state)
    assert await commit(inp, "c2", {}) == {}

    # A later edit isn't covered by the approval.
    (repo / "a.py").write_text("a = 3\n")
    assert _decision(await commit(inp, "c3", {})) == "deny"

    # A commit that doesn't land (here: never run) leaves the task open.
    (repo / "a.py").write_text("a = 2\n")
    assert await commit(inp, "c4", {}) == {}
    await after({"tool_input": inp["tool_input"]}, "c4", {})
    assert app_module._panel_load(run.session_id).plan_ok is True

    # Commit what was approved, leaving test noise behind: the task is done
    # all the same, and the next change needs a new plan.
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_text("noise")
    state = app_module._panel_load(run.session_id)
    state.approvals[str(repo)] = panel.working_state(str(repo))
    app_module._panel_save(run.session_id, run.owner_sub, state)
    assert await commit(inp, "c5", {}) == {}
    _git(repo, "commit", "-qm", "change", "a.py")
    await after({"tool_input": inp["tool_input"]}, "c5", {})
    state = app_module._panel_load(run.session_id)
    assert state.plan_ok is False
    assert str(repo) in state.approvals  # the noise it saw is still covered


async def test_a_commit_it_cannot_place_is_refused_with_how_to_fix_it(chat):
    run, _, _ = chat
    _, commit, _ = _hooks(run)
    result = await commit({"tool_input": {"command": "cd - && git commit -m x"}, "cwd": "/"}, "c", {})
    assert "git -C" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert await commit({"tool_input": {"command": "git status"}, "cwd": "/"}, "c", {}) == {}


async def _review(run, **args):
    text, is_error = await app_module._panel_review_call(run, args)
    return text, is_error


async def _finish_round(run):
    task = app_module._PANEL_ROUNDS.get(run.session_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=10)


async def test_a_plan_round_runs_in_the_background_and_its_answer_is_delivered(chat, repo, monkeypatch):
    run, events, delivered = chat
    core = FakeCore([APPROVE_ALL])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    run.panel_notes = ["Please fix the greeting"]
    text, is_error = await _review(run, stage="plan", repo=str(repo / "a.py"), message="Change a to 2.")
    assert not is_error
    assert "round 1 of" in text and "End your turn now" in text
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.plan_ok is True and state.thread_id == 77
    assert core.posts == [("Matt", "Please fix the greeting"), (panel.AUTHOR, "Change a to 2.")]
    assert delivered and delivered[0].startswith(panel.MESSAGE_MARKER)
    assert "Round 1 of" in delivered[0] and "approved" in delivered[0]
    kinds = [e["type"] for e in events]
    assert kinds[:4] == ["panel_round", "panel_reply", "panel_reply", "panel_verdict"]
    assert app_module._roundtable_get_project(77) == "-tmp-project"
    assert run.session_id not in app_module._PANEL_ROUNDS


async def test_an_approved_changes_round_lets_exactly_those_changes_commit(chat, repo, monkeypatch):
    run, _, delivered = chat
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))
    (repo / "a.py").write_text("a = 2\n")
    await _review(run, stage="changes", repo=str(repo), message="Changed a.")
    await _finish_round(run)
    _, commit, _ = _hooks(run)
    inp = {"tool_input": {"command": f"git -C {repo} commit -qam x"}, "cwd": "/"}
    assert await commit(inp, "c", {}) == {}
    assert "commit these changes as they are" in delivered[-1]


async def test_a_second_call_while_a_round_runs_is_told_to_wait(chat, repo, monkeypatch):
    run, _, _ = chat
    gate = threading.Event()

    class SlowCore(FakeCore):
        def roundtable_ask_parallel(self, *a, **kw):
            gate.wait(timeout=10)
            return APPROVE_ALL

    monkeypatch.setattr(app_module, "roundtable_core", SlowCore([]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    text, is_error = await _review(run, stage="plan", repo=str(repo), message="Plan again.")
    assert "already running" in text and not is_error
    gate.set()
    await _finish_round(run)


async def test_the_review_tool_refuses_a_path_outside_any_repository(chat, tmp_path):
    run, events, _ = chat
    text, is_error = await _review(run, stage="plan", repo=str(tmp_path), message="x")
    assert is_error and "isn't inside a git repository" in text
    # With the panel off, a bad request is refused before the user is asked.
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    text, is_error = await asyncio.wait_for(
        _review(run, stage="plan", repo=str(tmp_path), message="x"), timeout=5,
    )
    assert is_error and "isn't inside a git repository" in text
    assert not any(e["type"] == "question_request" for e in events)


# ─── Panel review off: Claude asks the user first ───────────────────────────

def _panel_off(run):
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)


async def _card(events, n=1):
    """The n-th question card the chat has shown."""
    for _ in range(250):
        cards = [e for e in events if e["type"] == "question_request"]
        if len(cards) >= n:
            return cards[n - 1]
        await asyncio.sleep(0.02)
    raise AssertionError(f"question card {n} never came")


def _answer(card, label):
    question = card["questions"][0]["question"]
    app_module.PENDING[card["id"]]["future"].set_result(
        {"decision": "answer", "payload": {"answers": {question: label}}},
    )


def _ask(run, **args):
    """A review call that waits on the user's card, as a task."""
    return asyncio.create_task(_review(run, **args))


async def test_with_the_panel_off_claude_asks_first_and_no_sends_nothing(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    core = FakeCore([])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    call = _ask(run, stage="plan", repo=str(repo), message="Rewrite the engine.",
                reason="It rewrites the engine.")
    card = await _card(events)
    question = card["questions"][0]["question"]
    assert "plan" in question and "repo" in question and "It rewrites the engine." in question
    assert run.session_id in app_module._PANEL_ASKS
    _answer(card, panel.ASK_SKIP)
    text, is_error = await call
    assert not is_error and "nothing was sent" in text
    assert core.asks == 0 and core.posts == []
    assert run.session_id not in app_module._PANEL_ROUNDS
    assert run.session_id not in app_module._PANEL_ASKS
    state = app_module._panel_load(run.session_id)
    assert state.enabled is False and state.granted == [] and state.rounds["plan"] == 0
    assert any(e["type"] == "permission_resolved" and e["id"] == card["id"] for e in events)


async def test_a_yes_runs_an_advisory_round_that_gates_nothing(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    core = FakeCore([OBJECT])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    call = _ask(run, stage="plan", repo=str(repo), message="Plan v1.")
    _answer(await _card(events), panel.ASK_SEND)
    text, is_error = await call
    assert not is_error and "End your turn now" in text
    assert "until they approve" not in text and "tells you to go ahead" in text
    await _finish_round(run)
    assert core.asks == 1
    state = app_module._panel_load(run.session_id)
    assert state.enabled is False and state.granted == ["plan"]
    assert "they want changes" in delivered[-1]
    assert "until they approve" not in delivered[-1]
    assert "tells you to go ahead, go ahead" in delivered[-1]
    # Nothing is gated: edits and commits go straight through.
    edit, commit, _ = _hooks(run)
    assert await edit({"tool_input": {"file_path": str(repo / "a.py")}, "cwd": str(repo)}, "t", {}) == {}
    (repo / "a.py").write_text("a = 2\n")
    inp = {"tool_input": {"command": f"git -C {repo} commit -qam x"}, "cwd": "/"}
    assert await commit(inp, "c", {}) == {}


async def test_one_yes_covers_that_stages_rounds_until_it_resolves(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    core = FakeCore([OBJECT, APPROVE_ALL])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    call = _ask(run, stage="plan", repo=str(repo), message="Plan v1.")
    _answer(await _card(events), panel.ASK_SEND)
    await call
    await _finish_round(run)
    # Round 2 of the same plan goes without a new card.
    text, _ = await asyncio.wait_for(
        _review(run, stage="plan", repo=str(repo), message="Plan v2."), timeout=5,
    )
    assert "round 2 of" in text
    await _finish_round(run)
    assert len([e for e in events if e["type"] == "question_request"]) == 1
    assert "approved" in delivered[-1] and "your call" in delivered[-1]
    state = app_module._panel_load(run.session_id)
    assert state.granted == [] and state.plan_ok is True and state.rounds["plan"] == 0
    # It resolved, so the next review asks again, without the keep-on choice.
    call = _ask(run, stage="changes", repo=str(repo), message="Changed a.")
    card = await _card(events, 2)
    assert [o["label"] for o in card["questions"][0]["options"]] == [panel.ASK_SEND, panel.ASK_SKIP]
    _answer(card, panel.ASK_SKIP)
    await call


async def test_an_approved_advisory_changes_review_ends_the_task(chat, repo, monkeypatch):
    """So a later review isn't told a plan was approved for it."""
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL, APPROVE_ALL]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    _answer(await _card(events), panel.ASK_SEND)
    await call
    await _finish_round(run)
    assert app_module._panel_load(run.session_id).plan_ok is True
    (repo / "a.py").write_text("a = 2\n")
    call = _ask(run, stage="changes", repo=str(repo), message="Changed a.")
    _answer(await _card(events, 2), panel.ASK_SEND)
    await call
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.plan_ok is False and state.granted == []
    assert state.rounds == {"plan": 0, "changes": 0}
    assert "commit these changes" in delivered[-1]


async def test_objections_to_the_last_round_go_to_the_user_in_the_chat_not_a_card(chat, repo, monkeypatch):
    """No gate holds anything, so the decision card has nothing to decide."""
    run, events, delivered = chat
    monkeypatch.setattr(panel, "MAX_ROUNDS", 1)
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    _answer(await _card(events), panel.ASK_SEND)
    await call
    await _finish_round(run)
    assert len([e for e in events if e["type"] == "question_request"]) == 1
    verdict = [e for e in events if e["type"] == "panel_verdict"][-1]
    assert verdict["outcome"] == "unresolved"
    assert "still had objections" in delivered[-1] and "tell Matt" in delivered[-1]
    state = app_module._panel_load(run.session_id)
    assert state.granted == [] and state.rounds["plan"] == 0


async def test_yes_and_keep_review_on_turns_the_switch_on(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    _answer(await _card(events), panel.ASK_KEEP_ON)
    text, _ = await call
    assert "Don't change files until they approve" in text
    assert {"type": "panel_changed", "enabled": True}.items() <= next(
        e for e in events if e["type"] == "panel_changed").items()
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.enabled is True and state.granted == [] and state.plan_ok is True
    assert "Before you commit, send them to the panel" in delivered[-1]


async def test_turning_the_switch_on_during_the_card_runs_an_enforced_round(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    card = await _card(events)
    app_module._panel_set_enabled(run.session_id, run.owner_sub, True)
    _answer(card, panel.ASK_SEND)
    text, _ = await call
    assert "Don't change files until they approve" in text
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.enabled is True and state.granted == []


async def test_a_message_from_the_user_ends_the_yes_but_a_panel_answer_does_not(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan v1.")
    _answer(await _card(events), panel.ASK_SEND)
    await call
    await _finish_round(run)
    assert delivered  # the panel's answer reached Claude...
    assert app_module._panel_load(run.session_id).granted == ["plan"]  # ...and kept the yes
    assert app_module._panel_prefix(run, "/compact") == ""
    assert app_module._panel_load(run.session_id).granted == ["plan"]
    assert app_module._panel_prefix(run, "Hmm, also check the tests.") == ""
    assert app_module._panel_load(run.session_id).granted == []
    # So the revised plan is a fresh ask, and says so.
    call = _ask(run, stage="plan", repo=str(repo), message="Plan v2.")
    card = await _card(events, 2)
    assert "revised plan" in card["questions"][0]["question"]
    _answer(card, panel.ASK_SKIP)
    await call


async def test_flipping_the_switch_ends_the_yes(chat, repo, monkeypatch):
    run, events, _ = chat
    _panel_off(run)
    state = app_module._panel_load(run.session_id)
    state.granted = ["plan"]
    app_module._panel_save(run.session_id, run.owner_sub, state)
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)  # no flip
    assert app_module._panel_load(run.session_id).granted == ["plan"]
    app_module._panel_set_enabled(run.session_id, run.owner_sub, True)
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    assert app_module._panel_load(run.session_id).granted == []


async def test_a_yes_starts_the_review_at_round_one(chat, repo, monkeypatch):
    """A count left by an earlier review would otherwise make the first
    advisory round the last."""
    run, events, delivered = chat
    _panel_off(run)
    state = app_module._panel_load(run.session_id)
    state.rounds["plan"] = 2
    app_module._panel_save(run.session_id, run.owner_sub, state)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    _answer(await _card(events), panel.ASK_SEND)
    text, _ = await call
    assert "round 1 of" in text
    await _finish_round(run)
    assert "they want changes" in delivered[-1]


async def test_a_round_that_started_while_the_card_was_up_wins(chat, repo, monkeypatch):
    run, events, _ = chat
    _panel_off(run)
    core = FakeCore([])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    card = await _card(events)
    other = asyncio.get_running_loop().create_future()
    app_module._PANEL_ROUNDS[run.session_id] = other
    try:
        _answer(card, panel.ASK_SEND)
        text, is_error = await call
        assert not is_error and "already running" in text
        assert core.asks == 0
        assert app_module._PANEL_ROUNDS[run.session_id] is other
    finally:
        other.cancel()


def test_a_follow_up_sent_into_a_live_chat_ends_the_yes(client, monkeypatch):
    """The same through the endpoint a typed follow-up arrives on."""
    import types
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", True)
    run = app_module.ActiveRun(str(uuid.uuid4()), owner_sub="anonymous", account_slot="shared")
    run.provider = "claude"
    run.session_id = "follow-" + uuid.uuid4().hex[:8]
    run.requested_account_slot = "shared"
    run.model = "claude-fable-5-1"
    run.between_turns = True
    run.task = types.SimpleNamespace(done=lambda: False, cancel=lambda: None)
    run.personality_id = app_module._resolve_personality_for_run({"sub": "anonymous"})["id"]
    app_module.ACTIVE_RUNS[run.run_id] = run
    app_module._panel_save(run.session_id, "anonymous", panel.PanelState(granted=["plan"]))
    headers = {"Origin": "http://testserver"}
    try:
        r = client.post(f"/api/chat/send/{run.run_id}",
                        data={"message": "Also check the tests.", "account_slot": "shared"},
                        headers=headers)
        assert r.status_code == 202, r.text
        assert app_module._panel_load(run.session_id).granted == []
    finally:
        app_module.ACTIVE_RUNS.pop(run.run_id, None)


async def test_a_second_call_while_the_card_is_open_is_told_to_wait(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    core = FakeCore([APPROVE_ALL])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    first = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    card = await _card(events)
    text, is_error = await asyncio.wait_for(
        _review(run, stage="plan", repo=str(repo), message="Plan, again."), timeout=5,
    )
    assert not is_error and "already asked" in text
    assert len([e for e in events if e["type"] == "question_request"]) == 1
    _answer(card, panel.ASK_SEND)
    await first
    await _finish_round(run)
    assert core.asks == 1


async def test_a_stopped_turn_puts_no_card_up_and_a_card_up_resolves_on_stop(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([]))
    run.interrupting = True
    text, _ = await asyncio.wait_for(
        _review(run, stage="plan", repo=str(repo), message="Plan."), timeout=5,
    )
    assert "nothing was sent" in text
    assert not any(e["type"] == "question_request" for e in events)
    run.interrupting = False
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    await _card(events)
    assert app_module._resolve_pending_permissions(run, "user stopped") == 1
    text, _ = await call
    assert "nothing was sent" in text
    assert run.session_id not in app_module._PANEL_ASKS
    assert run.session_id not in app_module._PANEL_ROUNDS


@pytest.mark.parametrize("label", [panel.ASK_SEND, panel.ASK_KEEP_ON])
async def test_a_stop_right_after_a_yes_sends_nothing(chat, repo, monkeypatch, label):
    """The yes resolves the card's future, so Stop has nothing left to
    resolve: the call must see the stop itself before starting anything."""
    run, events, delivered = chat
    _panel_off(run)
    core = FakeCore([APPROVE_ALL])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    card = await _card(events)
    _answer(card, label)  # no await between these three
    run.interrupting = True
    app_module._resolve_pending_permissions(run, "user stopped")
    text, _ = await call
    assert "nothing was sent" in text
    assert core.asks == 0 and run.session_id not in app_module._PANEL_ROUNDS
    state = app_module._panel_load(run.session_id)
    assert state.enabled is False and state.granted == [] and state.rounds["plan"] == 0
    assert not any(e["type"] == "panel_changed" for e in events)


async def test_go_ahead_after_the_box_went_off_ends_the_task(chat, repo, monkeypatch):
    """A deadlock card raised with the box on, answered after it went off:
    nothing will end the task at a commit any more, so the answer does."""
    run, events, delivered = chat
    monkeypatch.setattr(panel, "MAX_ROUNDS", 1)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    state = app_module._panel_load(run.session_id)
    state.plan_ok = True
    app_module._panel_save(run.session_id, run.owner_sub, state)
    (repo / "a.py").write_text("a = 2\n")
    await _review(run, stage="changes", repo=str(repo), message="Changed a.")
    card = await _card(events)
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    _answer(card, "Go ahead anyway")
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.enabled is False and state.plan_ok is False
    assert state.rounds == {"plan": 0, "changes": 0}
    assert "commit these changes" in delivered[-1]


async def test_no_answer_in_time_or_an_answer_of_its_own_sends_nothing(chat, repo, monkeypatch):
    run, events, delivered = chat
    _panel_off(run)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([]))
    monkeypatch.setattr(app_module, "PANEL_DECISION_TIMEOUT_SECONDS", 0.2)
    text, _ = await _review(run, stage="plan", repo=str(repo), message="Plan.")
    assert "didn't answer" in text and "nothing was sent" in text
    assert any(e["type"] == "permission_timeout" for e in events)
    monkeypatch.setattr(app_module, "PANEL_DECISION_TIMEOUT_SECONDS", 30)
    call = _ask(run, stage="plan", repo=str(repo), message="Plan.")
    _answer(await _card(events, 2), "Only if it's quick")
    text, _ = await call
    assert "nothing was sent" in text and "Only if it's quick" in text
    assert app_module._panel_load(run.session_id).granted == []


async def test_three_rounds_of_objections_ask_the_user_and_go_ahead_approves(chat, repo, monkeypatch):
    run, events, delivered = chat
    core = FakeCore([OBJECT, OBJECT, OBJECT])
    monkeypatch.setattr(app_module, "roundtable_core", core)
    for n in range(1, panel.MAX_ROUNDS):
        await _review(run, stage="plan", repo=str(repo), message=f"Plan v{n}.")
        await _finish_round(run)
        assert "they want changes" in delivered[-1]
    await _review(run, stage="plan", repo=str(repo), message="Final plan.")
    for _ in range(200):
        card = next((e for e in events if e["type"] == "question_request"), None)
        if card:
            break
        await asyncio.sleep(0.02)
    assert card is not None
    assert card["questions"][0]["options"][0]["label"] == "Go ahead anyway"
    question = card["questions"][0]["question"]
    app_module.PENDING[card["id"]]["future"].set_result(
        {"decision": "allow", "payload": {"answers": {question: "Go ahead anyway"}}},
    )
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.plan_ok is True
    assert "decided to go ahead" in delivered[-1]
    verdict = [e for e in events if e["type"] == "panel_verdict"][-1]
    assert verdict["outcome"] == "go_ahead"


async def test_a_panel_nobody_can_answer_steps_aside(chat, repo, monkeypatch):
    run, _, delivered = chat
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([
        {"responses": {}, "errors": {"gpt-5": "RateLimitError", "gemini-pro": "TimeoutError"}},
    ]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)
    assert app_module._panel_load(run.session_id).plan_ok is True
    assert "stepped aside" in delivered[-1]
    assert "### GPT Sol: couldn't answer" in delivered[-1]


async def test_an_outage_cannot_erase_an_objection_from_the_previous_round(chat, repo, monkeypatch):
    run, _, delivered = chat
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([
        OBJECT, {"responses": {}, "errors": {"gpt-5": "Timeout", "gemini-pro": "Timeout"}},
    ]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)
    before = app_module._panel_load(run.session_id).review_ledger
    assert before[str(repo)]["plan"]["gpt-5"][0]["status"] == "open"
    await _review(run, stage="plan", repo=str(repo), message="Please reconsider.")
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert not state.plan_ok
    assert state.review_ledger == before
    assert "Earlier objections remain unresolved" in delivered[-1]


async def test_a_crash_cannot_turn_a_prior_objection_into_approval(chat, repo, monkeypatch):
    run, _, _ = chat
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)

    class BrokenCore(FakeCore):
        def roundtable_ask_parallel(self, *args, **kwargs):
            raise RuntimeError("failed round")

    monkeypatch.setattr(app_module, "roundtable_core", BrokenCore([]))
    await _review(run, stage="plan", repo=str(repo), message="No changes.")
    await _finish_round(run)
    assert not app_module._panel_load(run.session_id).plan_ok


async def test_a_crashing_round_steps_aside_instead_of_stranding_claude(chat, repo, monkeypatch):
    run, _, delivered = chat

    class BrokenCore(FakeCore):
        def roundtable_ask_parallel(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(app_module, "roundtable_core", BrokenCore([]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)
    assert app_module._panel_load(run.session_id).plan_ok is True
    assert "RuntimeError: boom" in delivered[-1]


async def test_an_answer_with_no_live_chat_waits_for_the_next_message(chat, repo, monkeypatch):
    run, events, _ = chat
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([APPROVE_ALL]))

    async def finished(*a, **kw):
        return "run_finished"

    monkeypatch.setattr(app_module, "_inject_user_input", finished)
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)
    pending = app_module._panel_take_pending(run.session_id, run.owner_sub)
    assert pending.startswith(panel.MESSAGE_MARKER)
    assert app_module._panel_take_pending(run.session_id, run.owner_sub) == ""
    assert any(e["type"] == "panel_parked" for e in events)


async def test_the_users_messages_carry_the_instruction_and_reach_the_panel(chat):
    run, _, _ = chat
    assert app_module._panel_prefix(run, "fix it").startswith(panel.USER_PREFIX_MARKER)
    assert run.panel_notes == ["fix it"]
    # A slash command must stay first in the message or it stops working.
    assert app_module._panel_prefix(run, "/compact") == ""
    assert run.panel_notes == ["fix it"]
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    assert app_module._panel_prefix(run, "and this") == ""


def test_the_switch_endpoint_turns_it_on_and_off(client, monkeypatch):
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", True)
    sid = "switch-" + uuid.uuid4().hex[:8]
    headers = {"Origin": "http://testserver"}
    r = client.post("/api/chat/panel", data={"session_id": sid, "enabled": "1"}, headers=headers)
    assert r.status_code == 200 and r.json()["panel"]["enabled"] is True
    r = client.post("/api/chat/panel", data={"session_id": sid, "enabled": ""}, headers=headers)
    assert r.json()["panel"]["enabled"] is False
    monkeypatch.setattr(app_module, "ROUNDTABLE_AVAILABLE", False)
    r = client.post("/api/chat/panel", data={"session_id": sid, "enabled": "1"}, headers=headers)
    assert r.status_code == 409


def test_a_reopened_chat_shows_the_panel_as_the_panel(tmp_path, monkeypatch):
    sid = "transcript-" + uuid.uuid4().hex[:8]
    proj = tmp_path / "proj"
    proj.mkdir()
    lines = [
        {"type": "user", "message": {"role": "user", "content": panel.user_prefix() + "fix the bug"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": panel.TOOL_NAME,
             "input": {"stage": "plan", "repo": "/r", "message": "My plan, in full."}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "Sent to the panel"},
        ]}},
        {"type": "user", "message": {"role": "user", "content":
            panel.MESSAGE_MARKER + " This message is from claude-web's review panel.\n\nRound 1"}},
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    monkeypatch.setattr(app_module, "_find_session_path", lambda s, p="": proj / f"{sid}.jsonl")
    msgs = app_module.session_transcript(sid)
    assert msgs[0] == {"role": "user", "text": "fix the bug"}
    assert msgs[1]["name"] == panel.TOOL_NAME
    assert msgs[1]["input"]["message"] == "My plan, in full."
    assert msgs[3]["role"] == "panel_result"


async def test_switching_off_while_the_user_decides_is_not_undone(chat, repo, monkeypatch):
    run, events, _ = chat
    monkeypatch.setattr(panel, "MAX_ROUNDS", 1)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    for _ in range(200):
        card = next((e for e in events if e["type"] == "question_request"), None)
        if card:
            break
        await asyncio.sleep(0.02)
    assert card is not None
    # The user turns panel review off while the card waits, then answers it.
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    question = card["questions"][0]["question"]
    app_module.PENDING[card["id"]]["future"].set_result(
        {"decision": "answer", "payload": {"answers": {question: "Go ahead anyway"}}},
    )
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.enabled is False
    assert state.plan_ok is True and state.thread_id == 77


async def test_a_drain_restart_waits_for_a_panel_round(chat, repo, monkeypatch):
    run, _, _ = chat
    gate = threading.Event()

    class SlowCore(FakeCore):
        def roundtable_ask_parallel(self, *a, **kw):
            gate.wait(timeout=10)
            return APPROVE_ALL

    monkeypatch.setattr(app_module, "roundtable_core", SlowCore([]))
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    assert f"panel-review:{run.session_id}" in app_module._busy_runs()
    gate.set()
    await _finish_round(run)
    assert f"panel-review:{run.session_id}" not in app_module._busy_runs()



async def test_the_commit_gate_fails_closed_when_git_errors(chat, repo, monkeypatch):
    run, _, _ = chat
    _, commit, _ = _hooks(run)
    (repo / "a.py").write_text("a = 2\n")

    def broken(root):
        raise RuntimeError("index.lock exists")

    monkeypatch.setattr(panel, "working_state", broken)
    result = await commit({"tool_input": {"command": f"git -C {repo} commit -qam x"}, "cwd": "/"}, "c", {})
    assert _decision(result) == "deny"
    assert "index.lock exists" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_a_commit_sharing_its_command_with_an_edit_is_refused(chat, repo):
    run, _, _ = chat
    _, commit, _ = _hooks(run)
    state = app_module._panel_load(run.session_id)
    state.approvals[str(repo)] = panel.working_state(str(repo))
    app_module._panel_save(run.session_id, run.owner_sub, state)
    cmd = f"sed -i s/1/9/ {repo}/a.py && git -C {repo} commit -qam x"
    result = await commit({"tool_input": {"command": cmd}, "cwd": "/"}, "c", {})
    assert "has to run on its own" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_staged_content_the_panel_never_saw_is_refused(chat, repo):
    run, _, _ = chat
    _, commit, _ = _hooks(run)
    (repo / "a.py").write_text("a = 2\n")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("a = 3\n")
    state = app_module._panel_load(run.session_id)
    state.approvals[str(repo)] = panel.working_state(str(repo))  # what's on disk: 3
    app_module._panel_save(run.session_id, run.owner_sub, state)
    result = await commit({"tool_input": {"command": f"git -C {repo} commit -qm x"}, "cwd": "/"}, "c", {})
    assert "what's staged differs" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_go_ahead_on_a_deadlock_approves_what_the_panel_saw_not_later_edits(chat, repo, monkeypatch):
    run, events, delivered = chat
    monkeypatch.setattr(panel, "MAX_ROUNDS", 1)
    monkeypatch.setattr(app_module, "roundtable_core", FakeCore([OBJECT]))

    async def answer(label):
        for _ in range(200):
            cards = [e for e in events if e["type"] == "question_request"
                     and e["id"] in app_module.PENDING]
            if cards:
                break
            await asyncio.sleep(0.02)
        card = cards[-1]
        app_module.PENDING[card["id"]]["future"].set_result(
            {"decision": "answer", "payload": {"answers": {card["questions"][0]["question"]: label}}})

    (repo / "a.py").write_text("a = 2\n")
    await _review(run, stage="changes", repo=str(repo), message="Changed a.")
    await answer("Stop")
    await _finish_round(run)
    assert "asked you to stop" in delivered[-1]
    # Claude edits again, then asks; the user waves it through.
    (repo / "a.py").write_text("a = 3\n")
    text, _ = await _review(run, stage="changes", repo=str(repo), message="Changed a again.")
    assert "being asked to decide" in text
    await answer("Go ahead anyway")
    await _finish_round(run)
    _, commit, _ = _hooks(run)
    result = await commit({"tool_input": {"command": f"git -C {repo} commit -qam x"}, "cwd": "/"}, "c", {})
    assert _decision(result) == "deny"  # a = 3 was never in front of the panel


async def test_a_closed_panel_thread_is_replaced_not_crashed_into(chat, repo, monkeypatch):
    run, _, _ = chat
    core = FakeCore([APPROVE_ALL])
    core.closed[5] = 1234.0
    monkeypatch.setattr(app_module, "roundtable_core", core)
    state = app_module._panel_load(run.session_id)
    state.thread_id = 5
    app_module._panel_save(run.session_id, run.owner_sub, state)
    await _review(run, stage="plan", repo=str(repo), message="Plan.")
    await _finish_round(run)
    state = app_module._panel_load(run.session_id)
    assert state.thread_id == 77 and state.plan_ok is True


def test_a_parked_answer_and_the_users_message_come_back_apart():
    text = (panel.MESSAGE_MARKER + " answer body" + panel.PARKED_SEPARATOR
            + panel.user_prefix() + "my next message")
    entries = app_module._user_turn_entries(text)
    assert entries == [
        {"role": "panel_result", "text": panel.MESSAGE_MARKER + " answer body"},
        {"role": "user", "text": "my next message"},
    ]
