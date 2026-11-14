"""The sessions list stays current without reloading the page.

Matt, 2026-11-14: "when you click the sessions list, it only shows sessions up
to the point you refreshed the page". The list was fetched when the page
loaded and when a turn in this tab ended, nothing else. It is fetched again
when it is opened, when the tab or window comes back, and once a minute while
it is on screen; and a refresh updates it in place, so keyboard focus and a
screen reader's place in it survive.
"""
from __future__ import annotations

import json

from tests.test_catch_up_sounds import HARNESS
from tests.test_local_ui import browser, playwright, ui  # noqa: F401  (shared browser harness)


def _chat(sid: str, title: str, mtime: int) -> dict:
    return {"id": sid, "project": "-home-matt", "project_path": "/home/matt",
            "title": title, "mtime": mtime, "provider": "claude"}


OLD = _chat("old", "Old chat", 1_700_000_000)
OTHER = _chat("other", "Other chat", 1_699_000_000)
NEW = _chat("new", "Chat from the phone", 1_700_000_500)


def _serve_sessions(page, state, first: list[dict]):
    """Answer /api/sessions from state["sessions"], counting the requests."""
    state["sessions"] = first
    state["session_fetches"] = 0

    def answer(route):
        state["session_fetches"] += 1
        route.fulfill(json=state["sessions"])

    page.route("**/api/sessions", answer)


def _titles(page) -> list[str]:
    return page.locator("#session-list .session-title").all_inner_texts()


def _open_page(ui, first: list[dict], url: str = "http://local-ui.test/"):  # noqa: F811
    page, state = ui
    _serve_sessions(page, state, first)
    page.add_init_script(HARNESS)
    page.goto(url)
    playwright.expect(page.locator("#session-list .session-title")).to_have_count(len(first))
    return page, state


def test_opening_the_list_fetches_it_again(ui):  # noqa: F811
    page, state = _open_page(ui, [OLD])
    state["sessions"] = [NEW, OLD]
    toggle = page.locator("#toggle-sessions")
    toggle.click()  # hides the list
    toggle.click()  # shows it again
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(
        ["Chat from the phone", "Old chat"])


def test_coming_back_to_the_tab_fetches_it_again(ui):  # noqa: F811
    page, state = _open_page(ui, [OLD])
    state["sessions"] = [NEW, OLD]
    page.evaluate("window.__timeShift = 10000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(
        ["Chat from the phone", "Old chat"])


def test_the_list_is_checked_once_a_minute_while_it_is_showing(ui):  # noqa: F811
    page, state = ui
    _serve_sessions(page, state, [OLD])
    page.clock.install()
    page.goto("http://local-ui.test/")
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(["Old chat"])
    state["sessions"] = [NEW, OLD]
    page.clock.run_for(61_000)
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(
        ["Chat from the phone", "Old chat"])


def test_a_refresh_keeps_the_rows_of_chats_that_did_not_change(ui):  # noqa: F811
    page, state = _open_page(ui, [OLD, OTHER])
    page.evaluate("document.querySelectorAll('#session-list li').forEach((li) => { li.__kept = true; })")
    renamed = dict(OTHER, title="Other chat, renamed")
    state["sessions"] = [NEW, OLD, renamed]
    page.evaluate("window.__timeShift = 10000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(
        ["Chat from the phone", "Old chat", "Other chat, renamed"])
    kept = page.evaluate("[...document.querySelectorAll('#session-list li')].map((li) => !!li.__kept)")
    assert kept == [False, True, False]


def test_a_refresh_nobody_asked_for_waits_while_focus_is_in_the_list(ui):  # noqa: F811
    page, state = _open_page(ui, [OLD, OTHER])
    page.locator("#session-list a").nth(1).focus()
    state["sessions"] = [NEW, OLD, OTHER]
    page.evaluate("window.__timeShift = 10000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(500)
    assert _titles(page) == ["Old chat", "Other chat"]
    playwright.expect(page.locator("#session-list a").nth(1)).to_be_focused()
    # Once focus has left the list, the next chance brings it up to date.
    page.locator("#prompt").focus()
    page.evaluate("window.__timeShift = 20000")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(
        ["Chat from the phone", "Old chat", "Other chat"])


def test_focus_stays_on_its_chat_when_a_turn_end_moves_the_row(ui):  # noqa: F811
    """Reading the list while a turn in this tab ends: the turn's own chat
    jumps to the top, and focus must not fall off the chat it was on."""
    page, state = ui
    _serve_sessions(page, state, [OLD, OTHER])
    state["session"] = {
        "provider": "claude", "messages": [{"role": "user", "text": "Fix the build"}],
        "live_run": {"run_id": "run-1", "active": True, "between_turns": False, "next_idx": 5},
    }
    page.add_init_script(HARNESS)
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#session-list .session-title")).to_have_count(2)
    page.wait_for_function("window.__streams.length === 1", timeout=5000)
    page.locator('#session-list a[data-session="other"]').focus()
    state["sessions"] = [dict(OTHER, mtime=1_700_000_900), OLD]
    result = {"type": "result", "_idx": 5, "subtype": "success", "result": "Done."}
    page.evaluate("window.__push(0, [EVENT])".replace("EVENT", json.dumps(result)))
    playwright.expect(page.locator("#session-list .session-title")).to_have_text(["Other chat", "Old chat"])
    playwright.expect(page.locator('#session-list a[data-session="other"]')).to_be_focused()
