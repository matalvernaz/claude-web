"""Panel review wired into the app: gates, rounds, delivery, the switch."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import uuid

import pytest

import app as app_module
import panel


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

    PARTICIPANTS = {"gpt-5": {"label": "GPT Sol"}, "gemini-pro": {"label": "Gemini Pro"}}

    def __init__(self, answers):
        self.answers = list(answers)  # one {"responses":…, "errors":…} per round
        self.posts = []
        self.asks = 0

    def _default_coding_panel(self, synthesizer):
        return ["gpt-5", "gemini-pro"]

    def _participant_provider_available(self, name):
        return True

    def roundtable_create(self, topic, participants, house_rules):
        return {"thread_id": 77}

    def roundtable_bind_repo(self, tid, repo, policy):
        return {}

    def roundtable_bind_diff(self, tid, repo, base):
        return {"truncated": False}

    def roundtable_post(self, tid, content, speaker):
        self.posts.append((speaker, content))

    def _effective_tool_context(self, tid):
        return None

    def roundtable_ask_parallel(self, tid, participants, prompt, effort, tool_use_context,
                                on_result=None):
        self.asks += 1
        return self.answers.pop(0)


APPROVE_ALL = {"responses": {"gpt-5": "Fine.\nVERDICT: APPROVE", "gemini-pro": "OK\nVERDICT: APPROVE"}}
OBJECT = {"responses": {"gpt-5": "Bug at a.py:1\nVERDICT: CHANGES NEEDED",
                        "gemini-pro": "OK\nVERDICT: APPROVE"}}


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


async def test_the_review_tool_refuses_when_the_panel_is_off_or_the_repo_is_wrong(chat, tmp_path):
    run, _, _ = chat
    text, is_error = await _review(run, stage="plan", repo=str(tmp_path), message="x")
    assert is_error and "isn't inside a git repository" in text
    app_module._panel_set_enabled(run.session_id, run.owner_sub, False)
    text, is_error = await _review(run, stage="plan", repo=str(tmp_path), message="x")
    assert is_error and "off in this chat" in text


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
