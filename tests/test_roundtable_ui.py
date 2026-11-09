"""Exercise panel reconnects in the real browser without making provider calls."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from tests.test_local_ui import browser, playwright  # noqa: F401 (shared browser fixture)


@pytest.fixture
def roundtable_ui(browser, client, monkeypatch):  # noqa: F811
    import app
    monkeypatch.setattr(app.setup_flow, "is_configured", lambda *args: True)
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("sessionStorage.setItem('claude-web.rt-stream', 'saved-run')")
    state = {"rejoins": [], "posts": []}

    def route(request_route):
        request = request_route.request
        path = urlparse(request.url).path
        if path.startswith("/api/roundtable/assistant/stream/"):
            state["rejoins"].append(request_route)
        elif request.method == "POST":
            state["posts"].append(path)
            if state.get("post_sse"):
                request_route.fulfill(content_type="text/event-stream", body=state["post_sse"])
            else:
                request_route.fulfill(status=409, json={"detail": "No provider calls in this test"})
        else:
            response = client.get(path)
            request_route.fulfill(status=response.status_code, body=response.content,
                                  headers={"content-type": response.headers.get("content-type", "text/plain")})

    page.route("**/*", route)
    with page.expect_request("**/api/roundtable/assistant/stream/saved-run"):
        page.goto("http://roundtable.test/roundtable")
    yield page, state
    for request in state["rejoins"]:
        try:
            request.abort()
        except playwright.Error:
            pass  # Tests that delivered a response already handled this route.
    context.close()
    assert errors == []


def test_rejoin_blocks_duplicate_review(roundtable_ui):
    page, state = roundtable_ui
    playwright.expect(page.locator("#assistant-submit")).to_be_disabled()
    page.locator("#assistant-input").fill("Do not start another paid review")
    page.locator("#assistant-form").evaluate("form => form.requestSubmit()")
    assert state["posts"] == []


@pytest.mark.parametrize("failure", ["missing", "network", "dropped"])
def test_rejoin_failure_is_announced(roundtable_ui, failure):
    page, state = roundtable_ui
    request = state["rejoins"][0]
    if failure == "missing":
        request.fulfill(status=404, json={"detail": "No such stream"})
    elif failure == "network":
        request.abort()
    else:
        request.fulfill(content_type="text/event-stream", body='event: created\ndata: {"thread_id": 7}\n\n')
    playwright.expect(page.locator("#assistant-error")).to_be_visible()
    playwright.expect(page.locator("#announcer")).to_contain_text("rejoin")
    playwright.expect(page.locator("#assistant-submit")).to_be_enabled()
    saved = page.evaluate("sessionStorage.getItem('claude-web.rt-stream')")
    assert saved == (None if failure == "missing" else "saved-run")


def test_terminal_provider_error_does_not_rejoin_a_failed_run(roundtable_ui):
    page, state = roundtable_ui
    done = {"thread_id": 7, "synthesizer": {"label": "Reviewer"}, "synthesis": "Review finished"}
    state["rejoins"][0].fulfill(content_type="text/event-stream",
                                body="event: done\ndata: " + json.dumps(done) + "\n\n")
    playwright.expect(page.get_by_text("Review finished", exact=True)).to_be_visible()
    playwright.expect(page.locator("#assistant-submit")).to_be_enabled()
    state["post_sse"] = (
        'event: stream\ndata: {"stream_id": "failed-run"}\n\n'
        'event: error\ndata: {"message": "Provider unavailable"}\n\n'
    )
    page.locator("#assistant-input").fill("Start a review")
    page.locator("#assistant-submit").click()
    playwright.expect(page.get_by_text("Error: Provider unavailable", exact=True)).to_be_visible()
    playwright.expect(page.locator("#assistant-submit")).to_be_enabled()
    assert len(state["rejoins"]) == 1
    assert page.evaluate("sessionStorage.getItem('claude-web.rt-stream')") is None
