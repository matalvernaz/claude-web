"""The Tasks section of the page: totals, task numbers, real names, descriptions on request.

The section used to say only "Tasks", swap a task's name for its short
"doing" phrase while it was in progress, number rows by their place instead of
by the task's own number, and never show a description. A page that loaded
got no list at all (Matt, 2026-11-12).
"""
from __future__ import annotations

import json

from tests.test_local_ui import browser, playwright, ui  # noqa: F401  (shared browser harness)

READ = {
    "id": "1", "content": "Read the player code", "activeForm": "Reading the player",
    "status": "completed", "description": "Trace the seek path.",
}
FIX = {
    "id": "2", "content": "Fix the pause between items", "activeForm": "Waiting on CI for PR 322",
    "status": "in_progress", "description": "One rule for every door.",
}
SHIP = {
    "id": "3", "content": "Open the pull request", "activeForm": "Opening the PR",
    "status": "pending", "description": "CI green first.",
}
THREE_TASKS = [READ, FIX, SHIP]


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def _send_list(ui, todos: list[dict]):  # noqa: F811
    """A chat in which Claude has just sent this task list."""
    page, state = ui
    state["chat_sse"] = _sse({"type": "todos_update", "todos": todos}, {"type": "result"})
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Go")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    return page


def _rows(page) -> list[str]:
    return [" ".join(text.split()) for text in page.locator("#todos-list li").all_inner_texts()]


def test_the_heading_says_how_far_along_the_list_is(ui):  # noqa: F811
    page = _send_list(ui, THREE_TASKS)
    playwright.expect(page.locator("#todos-heading")).to_have_text("Tasks: 1 of 3 done, 1 in progress")
    # The section is named by its heading, so the totals are also what a
    # screen reader says on entering it.
    playwright.expect(page.get_by_role("region", name="Tasks: 1 of 3 done, 1 in progress")).to_be_visible()


def test_nothing_in_progress_is_left_out_of_the_totals(ui):  # noqa: F811
    page = _send_list(ui, [READ, SHIP])
    playwright.expect(page.locator("#todos-heading")).to_have_text("Tasks: 1 of 2 done")


def test_a_task_keeps_its_name_while_it_is_in_progress(ui):  # noqa: F811
    page = _send_list(ui, THREE_TASKS)
    playwright.expect(page.locator("#todos-list li")).to_have_count(3)
    assert _rows(page) == [
        "1. Done: Read the player code",
        "2. In progress: Fix the pause between items Now: Waiting on CI for PR 322",
        "3. To do: Open the pull request",
    ]
    playwright.expect(page.locator("#todos-list li").nth(1)).to_have_attribute("aria-current", "step")


def test_a_doing_phrase_that_only_repeats_the_name_is_not_said_twice(ui):  # noqa: F811
    same = {"content": "Read the code", "activeForm": "Read the code", "status": "in_progress"}
    page = _send_list(ui, [same])
    playwright.expect(page.locator("#todos-list li")).to_have_count(1)
    assert _rows(page) == ["1. In progress: Read the code"]


def test_rows_carry_the_task_numbers_the_tool_lines_use(ui):  # noqa: F811
    """After a chat is started again the list can begin at task 13, and the
    conversation's tool lines say "Updated task #14 status"."""
    later = [dict(READ, id="13"), dict(FIX, id="14"), dict(SHIP, id="16")]
    page = _send_list(ui, later)
    playwright.expect(page.locator("#todos-list li")).to_have_count(3)
    assert [row.split(" ")[0] for row in _rows(page)] == ["13.", "14.", "16."]
    # Still a list of three to a screen reader, with the numbers in the rows.
    snapshot = page.locator("#todos-panel").aria_snapshot()
    assert '- listitem: "13. Done: Read the player code"' in snapshot


def test_a_list_with_no_numbers_of_its_own_counts_its_rows(ui):  # noqa: F811
    """TodoWrite and Codex plans send no task numbers and no descriptions."""
    plain = [
        {"content": "Read the code", "activeForm": "Read the code", "status": "completed"},
        {"content": "Write the fix", "activeForm": "Write the fix", "status": "pending"},
    ]
    page = _send_list(ui, plain)
    playwright.expect(page.locator("#todos-list li")).to_have_count(2)
    assert _rows(page) == ["1. Done: Read the code", "2. To do: Write the fix"]
    playwright.expect(page.get_by_label("Show descriptions")).to_be_hidden()


def test_descriptions_stay_out_of_the_way_until_asked_for(ui):  # noqa: F811
    page = _send_list(ui, THREE_TASKS)
    box = page.get_by_label("Show descriptions")
    playwright.expect(box).to_be_visible()
    playwright.expect(box).not_to_be_checked()
    playwright.expect(page.locator("#todos-list .task-description")).to_have_count(0)
    box.check()
    playwright.expect(page.locator("#todos-list .task-description")).to_have_text([
        "Trace the seek path.", "One rule for every door.", "CI green first.",
    ])
    assert _rows(page)[2] == "3. To do: Open the pull request CI green first."
    # The choice is remembered for the next page load.
    assert page.evaluate("localStorage.getItem('claude-web.tasks.descriptions')") == "1"
    box.uncheck()
    playwright.expect(page.locator("#todos-list .task-description")).to_have_count(0)


def test_a_task_whose_name_was_never_said_reads_as_unknown_not_blank(ui):  # noqa: F811
    nameless = {"id": "7", "content": "", "activeForm": "", "status": "in_progress", "description": ""}
    page = _send_list(ui, [nameless])
    playwright.expect(page.locator("#todos-list li")).to_have_count(1)
    assert _rows(page) == ["7. In progress: (name not known)"]


def test_the_panel_goes_once_everything_is_done(ui):  # noqa: F811
    page, state = ui
    finished = [dict(task, status="completed") for task in THREE_TASKS]
    state["chat_sse"] = _sse(
        {"type": "todos_update", "todos": THREE_TASKS},
        {"type": "todos_update", "todos": finished},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "All three are done."}]}},
        {"type": "result"},
    )
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Go")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    playwright.expect(page.locator("#transcript .body").last).to_have_text("All three are done.")
    playwright.expect(page.locator("#todos-panel")).to_be_hidden()
    playwright.expect(page.locator("#todos-heading")).to_have_text("Tasks")


def _saved_chat(state, **extra):
    state["session"] = {
        "provider": "claude",
        "messages": [{"role": "user", "text": "Start on build A"}],
        **extra,
    }


def test_a_page_load_draws_the_saved_list_and_says_nothing_about_it(ui):  # noqa: F811
    page, state = ui
    _saved_chat(state, tasks=THREE_TASKS)
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#todos-list li")).to_have_count(3)
    assert _rows(page)[1] == "2. In progress: Fix the pause between items Now: Waiting on CI for PR 322"
    # The chat is not doing anything: the task in progress must not start the
    # status line, or be read out in place of "Conversation loaded."
    playwright.expect(page.locator("#status-announcer")).to_have_text("Conversation loaded.")
    playwright.expect(page.locator("#status")).to_have_text("")


def test_a_page_load_from_a_server_that_sends_no_list_changes_nothing(ui):  # noqa: F811
    """The page's files go live before the server restarts with the new code."""
    page, state = ui
    _saved_chat(state)
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#status-announcer")).to_have_text("Conversation loaded.")
    playwright.expect(page.locator("#todos-panel")).to_be_hidden()


def test_a_page_that_joins_a_running_chat_has_the_list_before_anything_new_arrives(ui):  # noqa: F811
    page, state = ui
    _saved_chat(state, tasks=THREE_TASKS, live_run={
        "run_id": "live-run", "active": True, "between_turns": False, "next_idx": 41,
    })
    # The stream stays open with nothing new in it, as a turn in flight does.
    page.add_init_script("""(() => {
      const realFetch = window.fetch;
      window.fetch = (url, options) => {
        if (!String(url).includes('/api/chat/stream/live-run')) return realFetch(url, options);
        window.joinedAt = String(url);
        const body = new ReadableStream({start() {}});
        return Promise.resolve(new Response(body, {headers: {'Content-Type': 'text/event-stream'}}));
      };
    })();""")
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#todos-list li")).to_have_count(3)
    assert page.evaluate("window.joinedAt").endswith("?start_index=41")
    playwright.expect(page.locator("#todos-heading")).to_have_text("Tasks: 1 of 3 done, 1 in progress")
