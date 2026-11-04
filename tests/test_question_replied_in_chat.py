"""A chat message sent while a question card is open answers the card.

The CLI holds every message behind the tool call it is waiting on, so a reply
typed instead of picked sat queued until the card timed out (15 minutes by
default). Now the card is answered "replied in the chat" and the message goes
on as usual. The `unanswered` marker goes only to a CLI whose init lists
ask_user_question_unanswered_v1 (an older one refuses the whole answer).
"""
import asyncio
import contextlib

import pytest
from claude_agent_sdk import PermissionResultAllow, SystemMessage
from starlette.datastructures import FormData
from starlette.requests import Request

import app as app_module

QUESTIONS = [{
    "question": "Which colour should the button be?",
    "header": "Colour",
    "options": [{"label": "Red", "description": "Warm"}, {"label": "Blue", "description": "Calm"}],
    "multiSelect": False,
}]
CAPABLE = ["interrupt_receipt_v1", app_module.UNANSWERED_CAPABILITY]


def _request(path):
    request = Request({"type": "http", "method": "POST", "path": path, "headers": [],
                       "scheme": "http", "server": ("testserver", 80)})
    request._form = FormData()
    return request


@contextlib.asynccontextmanager
async def _chat(monkeypatch, tmp_path, capabilities):
    """A live Claude run on a fake CLI whose init lists *capabilities*."""
    monkeypatch.setattr(app_module, "_STATE_DB", None)
    monkeypatch.setattr(app_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(app_module, "ACTIVE_RUNS", {})
    monkeypatch.setattr(app_module, "ACTIVE_RUNS_BY_SESSION", {})
    monkeypatch.setattr(app_module, "PENDING", {})
    monkeypatch.setattr(app_module, "_notify_turn_complete", lambda run: None)
    monkeypatch.setattr(app_module.setup_flow, "is_configured", lambda *a, **k: True)
    state = {"queries": [], "messages": asyncio.Queue(), "ready": asyncio.Event()}

    @contextlib.asynccontextmanager
    async def sdk_client(options, run, stderr):
        state["run"], state["options"] = run, options

        class Client:
            async def query(self, prompt, **kwargs):
                state["queries"].append(prompt)
                state["ready"].set()

            async def interrupt(self):
                pytest.fail("Answering a card from the chat must not interrupt the turn")

            async def receive_messages(self):
                data = {"session_id": "question-session"}
                if capabilities is not None:
                    data["capabilities"] = capabilities
                yield SystemMessage(subtype="init", data=data)
                while True:
                    yield await state["messages"].get()

        yield Client()

    monkeypatch.setattr(app_module, "_sdk_client", sdk_client)
    await app_module.api_chat(
        request=_request("/api/chat"), message="Make me a button", session_id="", project="",
        model="opus", effort="low", permission_mode="default", fork=False, personality_id=None,
        account_slot="", queue_id="", provider="claude", advisor="", panel_review="",
        user={"sub": "anonymous"},
    )
    await asyncio.wait_for(state["ready"].wait(), 3)
    try:
        yield state
    finally:
        state["run"].task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state["run"].task
        if app_module._STATE_DB is not None:
            app_module._STATE_DB.close()


async def _open_card(state):
    """The model calls AskUserQuestion; wait until its card is up."""
    run = state["run"]
    async with asyncio.timeout(3):
        while not run.cli_capabilities and not any(e.get("type") == "system" for e in run.events):
            await asyncio.sleep(0.01)
    asked = asyncio.ensure_future(
        state["options"].can_use_tool(app_module.QUESTION_TOOL, {"questions": QUESTIONS}, None))
    async with asyncio.timeout(3):
        while not any(e.get("type") == "question_request" for e in run.events):
            await asyncio.sleep(0.01)
    return asked


async def _send(run, message, steer=True):
    return await app_module.api_chat_send(
        request=_request(f"/api/chat/send/{run.run_id}"), run_id=run.run_id, message=message,
        provider="claude", personality_id=None, account_slot="", steer=steer, queue_id="",
        user={"sub": "anonymous"},
    )


async def test_a_chat_message_answers_the_open_card_and_still_arrives(monkeypatch, tmp_path):
    async with _chat(monkeypatch, tmp_path, CAPABLE) as state:
        run = state["run"]
        asked = await _open_card(state)
        response = await _send(run, "Make it green, neither of those")
        assert response.status_code == 202

        result = await asyncio.wait_for(asked, 3)
        assert isinstance(result, PermissionResultAllow)
        question = QUESTIONS[0]["question"]
        answer = result.updated_input["answers"][question]
        assert answer == app_module._replied_in_chat_answer("Make it green, neither of those")
        assert '"Make it green, neither of those"' in answer
        assert result.updated_input["annotations"] == {question: {"unanswered": "replied_in_chat"}}
        assert result.updated_input["questions"] == QUESTIONS
        resolved = [e for e in run.events if e.get("type") == "permission_resolved"]
        assert [e["decision"] for e in resolved] == ["replied_in_chat"]
        # The message itself still goes to the CLI as the user's words.
        async with asyncio.timeout(3):
            while "Make it green, neither of those" not in state["queries"]:
                await asyncio.sleep(0.01)
        assert not app_module.PENDING


async def test_a_cli_without_the_capability_gets_no_marker(monkeypatch, tmp_path):
    for capabilities in (None, ["interrupt_receipt_v1"], "not-a-list"):
        async with _chat(monkeypatch, tmp_path, capabilities) as state:
            asked = await _open_card(state)
            assert (await _send(state["run"], "Green, please")).status_code == 202
            result = await asyncio.wait_for(asked, 3)
            assert result.updated_input["answers"] == {
                QUESTIONS[0]["question"]: app_module._replied_in_chat_answer("Green, please")}
            assert "annotations" not in result.updated_input, capabilities


async def test_other_cards_stay_open(monkeypatch, tmp_path):
    """Permission prompts, plan reviews, the overage gate and the panel's ask
    are decisions, not questions a chat message can answer."""
    async with _chat(monkeypatch, tmp_path, CAPABLE) as state:
        run = state["run"]
        loop = asyncio.get_running_loop()
        gate = loop.create_future()
        app_module.PENDING["gate"] = {"future": gate, "owner_sub": run.owner_sub, "run_id": run.run_id}
        elsewhere = loop.create_future()
        app_module.PENDING["elsewhere"] = {"future": elsewhere, "owner_sub": run.owner_sub,
                                           "run_id": "another-run", "kind": "question",
                                           "questions": QUESTIONS}
        assert (await _send(run, "Carry on")).status_code == 202
        assert not gate.done()
        assert not elsewhere.done()


async def test_a_message_that_is_not_delivered_answers_nothing(monkeypatch, tmp_path):
    async with _chat(monkeypatch, tmp_path, CAPABLE) as state:
        asked = await _open_card(state)

        async def full(*args, **kwargs):
            return "queue_full"

        monkeypatch.setattr(app_module, "_inject_user_input", full)
        assert (await _send(state["run"], "Green")).status_code == 429
        await asyncio.sleep(0.05)
        assert not asked.done()
        asked.cancel()


def test_the_payload_skips_questions_without_text():
    payload = app_module._replied_in_chat_payload(
        [{"question": "A?"}, {"question": ""}, {"header": "no text"}, "junk"], annotate=True, message="B")
    assert payload == {"answers": {"A?": app_module._replied_in_chat_answer("B")},
                       "annotations": {"A?": {"unanswered": "replied_in_chat"}}}
    assert app_module._replied_in_chat_payload("junk", annotate=True) == {"answers": {}}


def test_the_answer_quotes_the_reply_in_one_line_and_caps_it():
    answer = app_module._replied_in_chat_answer('Make it "green"\n\nplease')
    assert '"Make it \\"green\\" please"' in answer
    assert "\n" not in answer
    long_reply = app_module._replied_in_chat_answer("x" * (app_module.REPLIED_IN_CHAT_QUOTE_CHARS + 500))
    assert "x" * app_module.REPLIED_IN_CHAT_QUOTE_CHARS + '..."' in long_reply
    assert "x" * (app_module.REPLIED_IN_CHAT_QUOTE_CHARS + 1) not in long_reply
    assert "(attachments only)" in app_module._replied_in_chat_answer("  ")
