"""The chat page stays exactly one screen tall on a phone, file edits and all.

On a narrow screen the conversation scrolls inside its own box while the
header and the composer stay put, so the page itself has nothing to scroll.
When something inside the conversation was positioned against the page
instead of that box, the page grew far past the screen. A VoiceOver
three-finger swipe then scrolled the page into the empty space below the app:
the screen went blank and touch found nothing until VoiceOver swiped back to
something. The hidden "Added line:" and "Removed line:" labels in every file
edit did exactly that, about 50,000 pixels' worth in one real chat.
"""
from __future__ import annotations

from tests.test_local_ui import browser, playwright, ui  # noqa: F401  (shared browser harness)

EDIT_COUNT = 15
LINES_PER_SIDE = 10


def _edit(index: int) -> dict:
    # Small enough to render expanded (40 lines or fewer), and every line
    # changes, so each one carries a hidden "Added line:" or "Removed line:".
    old_lines = [f"old line {line} of file {index}" for line in range(LINES_PER_SIDE)]
    new_lines = [f"new line {line} of file {index}" for line in range(LINES_PER_SIDE)]
    return {"role": "tool_use", "name": "Edit", "input": {
        "file_path": f"/repo/module_{index}.py",
        "old_string": "\n".join(old_lines),
        "new_string": "\n".join(new_lines),
    }}


def _chat_with_file_edits() -> dict:
    messages = [{"role": "user", "text": "Tidy up every module"}]
    for index in range(EDIT_COUNT):
        messages.append(_edit(index))
        messages.append({"role": "assistant", "text": f"Edited module {index}."})
    return {"provider": "claude", "messages": messages}


def test_file_edits_do_not_make_the_phone_page_scrollable(ui):  # noqa: F811  (the imported fixture)
    page, state = ui
    state["session"] = _chat_with_file_edits()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("http://local-ui.test/?session=saved")
    last_reply = page.locator(".msg.assistant").last
    playwright.expect(last_reply).to_contain_text(f"Edited module {EDIT_COUNT - 1}.")
    labels = page.locator("#transcript .diff-body .sr-only")
    assert labels.count() == EDIT_COUNT * LINES_PER_SIDE * 2

    # The conversation is long enough to scroll inside its own box...
    conversation = page.evaluate("""() => {
        const box = document.getElementById('transcript');
        return {content: box.scrollHeight, visible: box.clientHeight};
    }""")
    assert conversation["content"] > conversation["visible"] * 3, conversation

    # ...but the page around it is exactly one screen.
    sizes = page.evaluate("""() => ({
        page: document.documentElement.scrollHeight,
        screen: document.documentElement.clientHeight,
    })""")
    assert sizes["page"] <= sizes["screen"], sizes

    # Which is what a VoiceOver page scroll runs into: the page must not move.
    page.evaluate("window.scrollTo(0, window.innerHeight)")
    assert page.evaluate("window.scrollY") == 0
