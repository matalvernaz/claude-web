"""A question card's "Other" answer box is there only while "Other" is picked.

Always there, it was one more edit field on every question for a screen
reader, picked or not (Matt, 2026-11-04).
"""
from __future__ import annotations

import json

from tests.test_local_ui import browser, playwright, ui  # noqa: F401  (shared browser harness)

QUESTION = "Which colour should the button be?"


def _card(multi: bool) -> dict:
    return {"type": "question_request", "id": "q-other", "timeout_seconds": 900, "questions": [{
        "question": QUESTION, "header": "Colour", "multiSelect": multi,
        "options": [{"label": "Red", "description": "Warm"}, {"label": "Blue", "description": "Calm"}],
    }]}


def _open(ui, multi: bool):  # noqa: F811
    page, state = ui
    state["chat_sse"] = f"data: {json.dumps(_card(multi))}\n\n"
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Make me a button")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    dialog = page.locator("dialog[open]")
    playwright.expect(dialog.get_by_role("heading", name="Claude is asking")).to_be_visible()
    box = dialog.get_by_label(f"Other answer for: {QUESTION}")
    return page, dialog, box


def test_the_other_box_shows_only_while_other_is_picked(ui):  # noqa: F811
    page, dialog, box = _open(ui, multi=False)
    playwright.expect(box).to_be_hidden()
    dialog.get_by_label("Red", exact=False).check()
    playwright.expect(box).to_be_hidden()
    dialog.get_by_label("Other:", exact=True).check()
    playwright.expect(box).to_be_visible()
    box.fill("Green")
    # Picking another option takes the box away again, and picking Other
    # brings it back with what was typed.
    dialog.get_by_label("Blue", exact=False).check()
    playwright.expect(box).to_be_hidden()
    dialog.get_by_label("Other:", exact=True).check()
    playwright.expect(box).to_have_value("Green")
    with page.expect_request("**/api/permission/q-other") as request:
        dialog.get_by_role("button", name="Submit answers").click()
    assert "Green" in request.value.post_data


def test_with_several_answers_the_box_follows_its_own_checkbox(ui):  # noqa: F811
    page, dialog, box = _open(ui, multi=True)
    playwright.expect(box).to_be_hidden()
    dialog.get_by_label("Other:", exact=True).check()
    playwright.expect(box).to_be_visible()
    box.fill("Green")
    dialog.get_by_label("Red", exact=False).check()
    playwright.expect(box).to_be_visible()
    dialog.get_by_label("Other:", exact=True).uncheck()
    playwright.expect(box).to_be_hidden()
    with page.expect_request("**/api/permission/q-other") as request:
        dialog.get_by_role("button", name="Submit answers").click()
    sent = request.value.post_data
    assert "Red" in sent and "Green" not in sent
