"""Run the real chat UI against mocked HTTP/SSE, without starting providers."""
from __future__ import annotations

import json
import os
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from jinja2 import Environment, FileSystemLoader

REQUIRE_BROWSER = os.environ.get("CLAUDE_WEB_REQUIRE_BROWSER") == "1"
if REQUIRE_BROWSER:
    import playwright.sync_api as playwright
else:
    playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
CLOUD_CAPS = {"accounts": True, "usage": True, "permission_modes": True}
LOCAL_CAPS = {
    "accounts": False, "usage": False, "permission_modes": ["default"],
    "fork": False, "rewind": False, "plan_mode": False,
}
LOCAL_MODELS = [
    {"key": "qwen3:30b", "label": "Qwen", "efforts": ["off", "on"],
     "default_effort": "on", "effort_labels": {"off": "Thinking off", "on": "Thinking on"},
     "is_default": True},
    {"key": "gpt-oss:20b", "label": "GPT OSS", "efforts": ["low", "medium", "high"],
     "default_effort": "medium"},
    {"key": "coder:30b", "label": "Coder", "efforts": []},
]


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as manager:
        try:
            instance = manager.chromium.launch()
        except playwright.Error as exc:
            if REQUIRE_BROWSER:
                pytest.fail(f"Playwright Chromium required: {exc}")
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def ui(browser):
    context = browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    cloud_model = {"key": "claude-test", "label": "Claude", "efforts": ["low", "high"]}
    state = {
        "providers": {"provider_switch": True, "providers": [
            {"key": "claude", "label": "Claude", "available": True,
             "models": [cloud_model], "capabilities": CLOUD_CAPS},
            {"key": "local", "label": "Local", "available": True,
             "models": LOCAL_MODELS, "capabilities": LOCAL_CAPS},
        ]},
        "session": {"provider": "local", "model": "gpt-oss:20b", "effort": "high",
                    "messages": [{"role": "user", "text": "Saved conversation"}]},
        "posts": [], "initial_local": False, "providers_error": False,
        "providers_pending": False, "pending_routes": [],
    }
    template = Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("index.html")

    def render_html():
        # Rendered per request so a test can swap in a real Claude model list
        # (state["claude_models"]) before it loads the page.
        models = state.get("claude_models") or [cloud_model]
        return template.render(
            site_title="Test chat", asset_version=lambda _: "test", models=models,
            models_json=json.dumps(models), effort_levels=["low", "high"],
            account={"active": "shared", "shared_label": "Shared", "credentials": []},
            personalities_payload={"active": 1, "personalities": [{"id": 1, "name": "Default"}]},
            sessions=[], multi_project=False,
        )

    def route(request_route):
        request = request_route.request
        path = urlparse(request.url).path
        if request.method == "POST":
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                + request.post_data_buffer,
            )
            fields = {part.get_param("name", header="content-disposition"): part.get_content()
                      for part in message.iter_parts()}
            state["posts"].append((path, fields))
            if path.startswith("/api/chat/send/"):
                if state.get("send_ok"):
                    request_route.fulfill(json={"ok": True})
                    return
                if state.get("change_settings_during_send"):
                    page.evaluate("""() => {
                        const model = document.getElementById('model-select');
                        model.value = 'coder:30b';
                        model.dispatchEvent(new Event('change'));
                    }""")
                request_route.fulfill(status=409, json={"error": state.get("conflict", "model_changed")})
            elif path == "/api/chat":
                request_route.fulfill(content_type="text/event-stream",
                                      body=state.get("chat_sse", 'data: {"type":"result"}\n\n'))
            else:
                request_route.fulfill(json={})
        elif path == "/":
            html = render_html()
            body = html.replace('<option value="claude" selected>Claude</option>',
                                '<option value="local" selected>Local</option>') if state["initial_local"] else html
            request_route.fulfill(content_type="text/html", body=body)
        elif path.startswith("/static/"):
            asset = ROOT / path.lstrip("/")
            request_route.fulfill(path=asset) if asset.is_file() else request_route.fulfill(status=404)
        elif path == "/api/providers":
            if state["providers_pending"]:
                state["pending_routes"].append(request_route)
            elif state["providers_error"] == "network":
                request_route.abort()
            else:
                request_route.fulfill(status=503 if state["providers_error"] else 200, json=state["providers"])
        elif path == "/api/sessions/saved":
            request_route.fulfill(json=state["session"])
        elif path == "/api/sessions":
            request_route.fulfill(json={"sessions": []})
        elif path == "/api/claude-cli/status":
            request_route.fulfill(json={"cli_present": True})
        else:
            request_route.fulfill(json={})

    page.route("**/*", route)
    yield page, state
    context.close()
    assert errors == []


def assert_local_controls(page):
    playwright.expect(page.locator("#local-effort-control")).to_be_visible()
    for selector in ("#effort-select-label", "#account-select", "#failover-toggle", "#manage-accounts", "#show-usage"):
        playwright.expect(page.locator(selector)).to_be_hidden()
    playwright.expect(page.locator("#permission-mode-select")).to_have_value("default")


def _codex_question_session(page, state, *, live=False, closed=False):
    state["providers"]["providers"].append({
        "key": "codex", "label": "Codex", "available": True,
        "models": [{"key": "gpt-test", "label": "GPT", "efforts": []}],
        "capabilities": CLOUD_CAPS,
    })
    state["session"] = {"provider": "codex", "messages": [{
        "role": "async_question", "type": "async_question", "id": "codex-async:q1",
        "provider": "codex", "session_id": "saved", "closed": closed,
        "questions": [{"question": "Which account?", "options": [
            {"label": "Alex"}, {"label": "Office"}]}],
    }]}
    if live:
        state["send_ok"] = True
        state["session"]["live_run"] = {
            "run_id": "codex-run", "active": True, "between_turns": False,
        }
        page.add_init_script("""(() => {
          const realFetch = window.fetch;
          window.fetch = (url, opts) => String(url).includes('/api/chat/stream/codex-run')
            ? Promise.resolve(new Response(new ReadableStream({start() {}}),
                {headers: {'Content-Type': 'text/event-stream'}}))
            : realFetch(url, opts);
        })();""")
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator(".question-fieldset legend")).to_have_text("Which account?")


@pytest.mark.parametrize("live", [True, False])
def test_codex_async_question_can_be_answered_during_or_after_a_turn(ui, live):
    page, state = ui
    _codex_question_session(page, state, live=live)
    assert state["posts"] == []  # preselection never submits itself
    playwright.expect(page.get_by_role("radio", name="Alex", exact=True)).to_be_checked()
    page.get_by_role("radio", name="Office", exact=True).check()
    page.get_by_role("button", name="Submit answers", exact=True).click()
    playwright.expect(page.locator(".permission-resolved")).to_contain_text("Which account?: Office")
    assert len(state["posts"]) == 1
    path, payload = state["posts"][0]
    assert path == ("/api/chat/send/codex-run" if live else "/api/chat")
    assert payload["provider"] == "codex"
    assert "Which account?\nOffice" in payload["message"].replace("\r\n", "\n")
    if not live:
        assert payload["session_id"] == "saved"


def test_codex_async_question_accepts_free_text_and_retries_a_failed_send(ui):
    page, state = ui
    _codex_question_session(page, state)
    page.get_by_role("textbox", name="Other answer for: Which account?").fill("The account labelled Alex")
    page.route("**/api/chat", lambda route: route.fulfill(status=503, json={"detail": "Unavailable"}))
    page.get_by_role("button", name="Submit answers", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Submit answers", exact=True)).to_be_enabled()
    playwright.expect(page.get_by_role("textbox", name="Other answer for: Which account?")).to_have_value("The account labelled Alex")
    page.unroute("**/api/chat")
    page.get_by_role("button", name="Submit answers", exact=True).click()
    playwright.expect(page.locator(".permission-resolved")).to_contain_text("The account labelled Alex")
    assert "The account labelled Alex" in state["posts"][0][1]["message"]


def test_codex_async_question_skip_needs_no_pending_rpc_and_old_questions_are_readonly(ui):
    page, state = ui
    _codex_question_session(page, state)
    page.get_by_role("button", name="Skip", exact=True).click()
    playwright.expect(page.locator(".permission-resolved")).to_have_text("Question skipped")
    assert state["posts"] == []
    state["session"]["messages"][0]["closed"] = True
    page.reload()
    playwright.expect(page.get_by_role("radio", name="Alex", exact=True)).to_be_disabled()
    playwright.expect(page.get_by_role("button", name="Submit answers", exact=True)).to_have_count(0)


def test_codex_question_from_live_stream_stays_answerable_after_result(ui):
    page, state = ui
    _codex_question_session(page, state)
    question = dict(state["session"]["messages"][0])
    events = [
        {"type": "system", "subtype": "init", "session_id": "saved", "provider": "codex"},
        question, {"type": "result"},
    ]
    state["session"]["messages"] = []
    state["chat_sse"] = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    page.reload()
    page.locator("#prompt").fill("Help me sign in")
    page.locator("#send").click()
    playwright.expect(page.get_by_role("radio", name="Alex", exact=True)).to_be_visible()
    state["chat_sse"] = 'data: {"type":"result"}\n\n'
    page.get_by_role("radio", name="Office", exact=True).check()
    page.get_by_role("button", name="Submit answers", exact=True).click()
    playwright.expect(page.locator(".permission-resolved")).to_contain_text("Which account?: Office")
    assert len(state["posts"]) == 2
    assert state["posts"][1][0] == "/api/chat"
    assert state["posts"][1][1]["session_id"] == "saved"


def _account_page_with_link_request(page, state, flow):
    """Serve /account for one slot whose link request answers with ``flow``
    on every status poll; a pasted code lands in ``state["code"]``."""
    template = Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("account.html")
    html = template.render(
        asset_version=lambda _: "test", user={"sub": "test"},
        account={"shared_label": "Shared", "credentials": [{
            "id": 2, "label": "Alex", "configured": False,
            "auto_email": "test@example.com", "auto_signin_available": True,
        }]}, codex_account={"credentials": []},
    )

    def route(request_route):
        request = request_route.request
        path = urlparse(request.url).path
        if path == "/account":
            state["page_loads"] = state.get("page_loads", 0) + 1
            request_route.fulfill(content_type="text/html", body=html)
        elif path.startswith("/static/"):
            asset = ROOT / path.lstrip("/")
            request_route.fulfill(path=asset) if asset.is_file() else request_route.fulfill(status=404)
        elif path.endswith("/oauth/auto_signin"):
            request_route.fulfill(json={**flow, "stage": "launching browser", "error": None,
                                        "magic_link": None, "awaiting_verification": False})
        elif path.endswith("/oauth/verification_code"):
            state["verification"] = request.post_data_json["code"]
            flow.update(stage="signing in with your code", awaiting_verification=False, error=None)
            request_route.fulfill(json=flow)
        elif path.endswith("/status"):
            state["polls"] = state.get("polls", 0) + 1
            if state.get("on_poll"):
                state["on_poll"](state["polls"])
            request_route.fulfill(json={"flow": flow, "credential": {"configured": False}})
        elif path.endswith("/oauth/code"):
            state["code"] = request.post_data_json["code"]
            request_route.fulfill(json={"flow": {"status": "done"}, "configured": True})
        else:
            request_route.fulfill(json={})

    page.route("**/*", route)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto("http://account-ui.test/account")
    page.get_by_role("button", name="Get sign-in link", exact=True).click()


def test_automatic_signin_failure_exposes_the_existing_link_and_code_form(browser):
    context = browser.new_context()
    page = context.new_page()
    state = {"code": None}
    flow = {"status": "awaiting_code", "url": "https://claude.ai/oauth?state=test",
            "stage": None, "error": "Browser security verification did not finish automatically.",
            "magic_link": None}
    try:
        _account_page_with_link_request(page, state, flow)
        playwright.expect(page.locator("#oauth-error")).to_contain_text("did not finish automatically")
        playwright.expect(page.get_by_role("link", name="Open the Claude sign-in page")).to_have_attribute("href", flow["url"])
        playwright.expect(page.locator("#oauth-magic-link-block")).to_be_hidden()
        playwright.expect(page.locator("#oauth-code")).to_be_visible()
        page.locator("#oauth-code").fill("test-code#test-state")
        page.get_by_role("button", name="Finish sign-in", exact=True).click()
        page.wait_for_load_state()
        assert state["code"] == "test-code#test-state"
    finally:
        context.close()


def test_the_link_arrives_with_a_code_form_and_the_browser_finishes_after_the_code(browser):
    context = browser.new_context()
    page = context.new_page()
    flow = {"status": "awaiting_code", "url": "https://claude.ai/oauth?state=test",
            "stage": "waiting for the sign-in email", "error": None, "magic_link": None,
            "awaiting_verification": False}
    state = {"code": None, "configured": False}

    def on_poll(n):
        # Poll 1: still reading mail. Poll 2: link in hand, browser parked
        # waiting for the person's code. After the code: stages, then done.
        if n == 2:
            flow.update(stage="waiting for your verification code", awaiting_verification=True,
                        magic_link="https://claude.ai/magic-link#token")
        if state.get("verification") and n >= state["verification_poll"] + 2:
            flow.update(status="done", stage=None)

    state["on_poll"] = on_poll
    try:
        _account_page_with_link_request(page, state, flow)
        playwright.expect(page.locator("#oauth-status")).to_contain_text("waiting for the sign-in email")
        playwright.expect(page.locator("#oauth-magic-link-block")).to_be_hidden()
        link = page.get_by_role("link", name="Open your sign-in link")
        playwright.expect(link).to_have_attribute("href", "https://claude.ai/magic-link#token", timeout=10000)
        playwright.expect(page.locator("#oauth-status")).to_contain_text("short verification code")
        # A screen-reader user lands on the link when it arrives; the short
        # code goes in the form under it, and the long-code form stays out
        # of the way while the server's browser is doing the work.
        assert page.evaluate("document.activeElement.id") == "oauth-magic-link"
        playwright.expect(page.locator("#oauth-code")).to_be_hidden()
        field = page.get_by_role("textbox", name="Verification code from claude.ai")
        field.fill("482913")
        state["verification_poll"] = state["polls"]
        page.get_by_role("button", name="Continue", exact=True).click()
        playwright.expect(page.locator("#oauth-verification-form")).to_be_hidden()
        assert state["verification"] == "482913"
        playwright.expect(page.locator("#oauth-status")).to_contain_text("signing in with your code", timeout=10000)
        # "done" reloads the page so the slot renders as signed in.
        for _ in range(50):
            if state["page_loads"] >= 2:
                break
            page.wait_for_timeout(200)
        assert state["page_loads"] >= 2
        assert state["code"] is None  # the long code never went through the person
    finally:
        context.close()


def test_a_refused_verification_code_reopens_the_form_with_the_reason(browser):
    context = browser.new_context()
    page = context.new_page()
    flow = {"status": "awaiting_code", "url": "https://claude.ai/oauth?state=test",
            "stage": "waiting for your verification code", "error": None,
            "magic_link": "https://claude.ai/magic-link#token", "awaiting_verification": True}
    state = {"code": None}

    def on_poll(n):
        if state.get("verification"):
            flow.update(stage="waiting for your verification code", awaiting_verification=True,
                        error="claude.ai did not accept that code. Check it and try again.")

    state["on_poll"] = on_poll
    try:
        _account_page_with_link_request(page, state, flow)
        field = page.get_by_role("textbox", name="Verification code from claude.ai")
        field.fill("000000")
        page.get_by_role("button", name="Continue", exact=True).click()
        playwright.expect(page.locator("#oauth-error")).to_contain_text("did not accept that code", timeout=10000)
        playwright.expect(field).to_be_visible()
        playwright.expect(field).to_be_enabled()
        playwright.expect(page.get_by_role("button", name="Continue", exact=True)).to_be_enabled()
    finally:
        context.close()


def test_local_boot_and_keyboard_effort_follow_model_capabilities(ui):
    page, state = ui
    state["initial_local"] = True
    page.add_init_script("localStorage.setItem('claude-web.provider', 'local')")
    page.goto("http://local-ui.test/")
    assert_local_controls(page)
    slider = page.get_by_role("slider", name="Effort:")
    playwright.expect(slider).to_have_attribute("max", "1")
    playwright.expect(slider).to_have_attribute("aria-valuetext", "Thinking on")
    slider.focus()
    slider.press("ArrowLeft")
    playwright.expect(slider).to_have_attribute("aria-valuetext", "Thinking off")
    page.locator("#model-select").select_option("gpt-oss:20b")
    playwright.expect(slider).to_have_attribute("max", "2")
    playwright.expect(slider).to_have_attribute("aria-valuetext", "medium")
    slider.focus()
    slider.press("End")
    playwright.expect(slider).to_have_attribute("aria-valuetext", "high")
    assert page.evaluate("localStorage.getItem('claude-web.effort.local')") == "high"
    page.locator("#model-select").select_option("coder:30b")
    playwright.expect(slider).to_be_disabled()
    playwright.expect(slider).to_have_attribute("aria-valuetext", "Not adjustable")
    page.locator("#provider-select").select_option("claude")
    playwright.expect(page.locator("#local-effort-control")).to_be_hidden()
    playwright.expect(page.locator("#show-usage")).to_be_visible()
    playwright.expect(page.locator("#account-select")).to_be_visible()
    assert state["posts"] == []


@pytest.mark.parametrize("availability", ["offline", "unconfigured", "http_error", "network"])
def test_saved_local_unavailable_never_falls_back_to_cloud(ui, availability):
    page, state = ui
    if availability == "offline":
        state["providers"]["providers"][1].update(available=False, models=[])
    elif availability == "unconfigured":
        state["providers"]["providers"] = state["providers"]["providers"][:1]
    else:
        state["providers_error"] = availability
    page.add_init_script("localStorage.setItem('claude-web.provider', 'local')")
    page.goto("http://local-ui.test/")
    playwright.expect(page.locator("#provider-select")).to_have_value("local")
    playwright.expect(page.locator("#provider-select")).to_be_visible()
    playwright.expect(page.locator("#provider-status")).to_contain_text("Local is unavailable")
    playwright.expect(page.locator("#send")).to_be_disabled()
    assert_local_controls(page)
    page.locator("#prompt").fill("Keep this message local")
    page.locator("#prompt").press("Enter")
    playwright.expect(page.locator("#prompt")).to_have_value("Keep this message local")
    assert state["posts"] == []
    assert page.evaluate("localStorage.getItem('claude-web.provider')") == "local"
    page.locator("#new-chat").click()
    playwright.expect(page.locator("#send")).to_be_disabled()
    page.locator("#provider-select").select_option("claude")
    playwright.expect(page.locator("#provider-status")).to_be_hidden()
    playwright.expect(page.locator("#send")).to_be_enabled()
    playwright.expect(page.locator("#model-select")).to_have_value("claude-test")
    page.locator("#prompt").fill("Use Claude explicitly")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    assert state["posts"][0][1]["provider"] == "claude"


@pytest.mark.parametrize("switch_to_cloud", [False, True])
@pytest.mark.parametrize("discovery_fails", [False, True])
def test_pending_discovery_blocks_keyboard_submit_and_preserves_choice(ui, switch_to_cloud, discovery_fails):
    page, state = ui
    state["providers_pending"] = True
    page.add_init_script("localStorage.setItem('claude-web.provider', 'local')")
    page.goto("http://local-ui.test/")
    playwright.expect(page.locator("#provider-select")).to_have_value("local")
    playwright.expect(page.locator("#send")).to_be_disabled()
    playwright.expect(page.locator("#provider-status")).to_contain_text("Checking")
    page.locator("#prompt").fill("Wait for my selected provider")
    page.locator("#prompt").press("Enter")
    playwright.expect(page.locator("#prompt")).to_have_value("Wait for my selected provider")
    assert state["posts"] == []
    if switch_to_cloud:
        page.locator("#provider-select").select_option("claude")
    assert state["pending_routes"]
    for route in state["pending_routes"]:
        route.fulfill(status=503 if discovery_fails else 200, json=state["providers"])
    expected_provider = "claude" if switch_to_cloud else "local"
    playwright.expect(page.locator("#provider-select")).to_have_value(expected_provider)
    if discovery_fails and not switch_to_cloud:
        playwright.expect(page.locator("#send")).to_be_disabled()
        playwright.expect(page.locator("#provider-status")).to_contain_text("Local is unavailable")
        assert state["posts"] == []
    else:
        playwright.expect(page.locator("#send")).to_be_enabled()
        with page.expect_response("**/api/chat"):
            page.locator("#prompt").press("Enter")
        assert state["posts"][0][1]["provider"] == expected_provider
        if not switch_to_cloud:
            assert "account_slot" not in state["posts"][0][1]


@pytest.mark.parametrize("pending", ["providers", "session"])
def test_session_submit_waits_for_provider_and_saved_settings(ui, pending):
    page, state = ui
    session_routes = []
    if pending == "providers":
        state["providers_pending"] = True
    else:
        page.route("**/api/sessions/saved", lambda route: session_routes.append(route))
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#send")).to_be_disabled()
    page.locator("#prompt").fill("Continue the saved local conversation")
    page.locator("#prompt").press("Enter")
    playwright.expect(page.locator("#prompt")).to_have_value("Continue the saved local conversation")
    assert state["posts"] == []
    routes = state["pending_routes"] if pending == "providers" else session_routes
    assert routes
    for route in routes:
        route.fulfill(json=state["providers"] if pending == "providers" else state["session"])
    playwright.expect(page.locator("#send")).to_be_enabled()
    playwright.expect(page.locator("#provider-select")).to_have_value("local")
    playwright.expect(page.locator("#model-select")).to_have_value("gpt-oss:20b")
    playwright.expect(page.locator("#local-effort-range")).to_have_attribute("aria-valuetext", "high")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    fields = state["posts"][0][1]
    assert (fields["provider"], fields["model"], fields["effort"]) == ("local", "gpt-oss:20b", "high")
    assert fields["session_id"] == "saved"
    assert "account_slot" not in fields


@pytest.mark.parametrize("pending", ["session", "providers", "codex_account", "session_error"])
def test_new_local_chat_survives_abandoned_cloud_session_load(ui, pending):
    page, state = ui
    state["session"].update(provider="claude", model="claude-test")
    routes = []
    if pending == "providers":
        state["providers_pending"] = True
        routes = state["pending_routes"]
    elif pending == "codex_account":
        state["session"].update(provider="codex", account_slot="cred:1")
        state["providers"]["providers"].append({
            "key": "codex", "label": "Codex", "available": True,
            "models": [{"key": "codex-test"}], "capabilities": CLOUD_CAPS,
        })
        page.route("**/api/providers?codex_account_slot=*", lambda route: routes.append(route))
    else:
        page.route("**/api/sessions/saved", lambda route: routes.append(route))
    page.add_init_script("localStorage.setItem('claude-web.provider', 'local')")
    pending_url = {
        "providers": "**/api/providers",
        "codex_account": "**/api/providers?codex_account_slot=*",
    }.get(pending, "**/api/sessions/saved")
    with page.expect_request(pending_url):
        page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#send")).to_be_disabled()
    assert routes
    page.locator("#new-chat").click()
    page.locator("#provider-select").select_option("local")
    payload = state["providers"] if pending in ("providers", "codex_account") else state["session"]
    for route in routes:
        route.fulfill(status=503 if pending == "session_error" else 200, json=payload)
    # All mocked boot requests are now complete, including the abandoned load.
    page.wait_for_load_state("networkidle")
    playwright.expect(page.locator("#provider-select")).to_have_value("local")
    playwright.expect(page.locator("#transcript")).to_be_empty()
    playwright.expect(page.locator("#status")).not_to_contain_text("Could not load session")
    playwright.expect(page.locator("#send")).to_be_enabled()
    page.locator("#prompt").fill("Start a new Local conversation")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    fields = state["posts"][0][1]
    assert fields["provider"] == "local"
    assert not fields.get("session_id")
    assert "account_slot" not in fields


@pytest.mark.parametrize("source,target", [("local", "claude"), ("claude", "local")])
def test_session_restoration_and_provider_switch_start_fresh(ui, source, target):
    page, state = ui
    state["session"]["provider"] = source
    page.add_init_script("localStorage.setItem('claude-web.effort.local', 'low')")
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#provider-select")).to_have_value(source)
    playwright.expect(page.locator("#transcript")).to_contain_text("Saved conversation")
    if source == "local":
        playwright.expect(page.locator("#model-select")).to_have_value("gpt-oss:20b")
        playwright.expect(page.locator("#local-effort-range")).to_have_attribute("aria-valuetext", "high")
    page.locator("#provider-select").select_option(target)
    playwright.expect(page).to_have_url("http://local-ui.test/")
    playwright.expect(page.locator("#transcript")).to_be_empty()


@pytest.mark.parametrize("availability", ["offline", "providers_error", "model_removed"])
def test_unavailable_local_session_keeps_model_effort_and_local_controls(ui, availability):
    page, state = ui
    state["providers_error"] = availability == "providers_error"
    if availability == "model_removed":
        state["providers"]["providers"][1]["models"] = [LOCAL_MODELS[0]]
    else:
        state["providers"]["providers"][1].update(available=False, models=[])
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#provider-select")).to_have_value("local")
    playwright.expect(page.locator("#model-select")).to_have_value("gpt-oss:20b")
    playwright.expect(page.locator("#effort-select")).to_have_value("high")
    playwright.expect(page.locator("#local-effort-range")).to_be_disabled()
    playwright.expect(page.locator("#local-effort-help")).to_contain_text("unavailable")
    assert_local_controls(page)
    page.locator("#provider-select").select_option("claude")
    playwright.expect(page).to_have_url("http://local-ui.test/")


@pytest.mark.parametrize("conflict", ["model_changed", "effort_changed"])
@pytest.mark.parametrize("change_settings_during_send", [False, True])
def test_local_settings_reach_live_send_and_fresh_run_after_conflict(ui, conflict, change_settings_during_send):
    page, state = ui
    state["conflict"] = conflict
    state["change_settings_during_send"] = change_settings_during_send
    state["session"]["live_run"] = {"run_id": "local-run", "active": True, "between_turns": True}
    page.add_init_script("""
        const nativeFetch = window.fetch;
        window.fetch = (url, options) => {
          if (String(url).includes('/api/chat/stream/local-run')) {
            return Promise.resolve(new Response(new ReadableStream({
              start(controller) {
                options.signal.addEventListener('abort', () => controller.error(
                  new DOMException('Aborted', 'AbortError')));
              }
            }), {headers: {'Content-Type': 'text/event-stream'}}));
          }
          return nativeFetch(url, options);
        };
    """)
    page.goto("http://local-ui.test/?session=saved")
    playwright.expect(page.locator("#model-select")).to_have_value("gpt-oss:20b")
    page.wait_for_function("sessionStorage.getItem('claude-web.active-run') === 'local-run'")
    if conflict == "model_changed":
        page.locator("#model-select").select_option("qwen3:30b")
        expected_model, expected_effort = "qwen3:30b", "on"
    else:
        page.locator("#local-effort-range").focus()
        page.locator("#local-effort-range").press("Home")
        expected_model, expected_effort = "gpt-oss:20b", "low"
    assert state["posts"] == []
    page.locator("#prompt").fill("Continue the work")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    assert [path for path, _ in state["posts"]] == ["/api/chat/send/local-run", "/api/chat"]
    for _, fields in state["posts"]:
        assert fields["provider"] == "local"
        assert fields["model"] == expected_model
        assert fields["effort"] == expected_effort
        assert fields["message"] == "Continue the work"
        assert "account_slot" not in fields
    assert state["posts"][1][1]["session_id"] == "saved"


def test_model_notice_is_shown_spoken_and_splits_the_reply(ui):
    # A safeguard refusal: the CLI retries on a fallback model and says so. The
    # notice must be in the transcript, spoken, and sit between the refused
    # attempt and the fallback model's reply rather than after both.
    page, state = ui
    summary = "Fable 5.1's safeguards flagged a message. This chat is now on Opus 4.8."
    full = ("Fable 5.1's safeguards flagged this message. Our intentionally broad "
            "safeguards can sometimes flag legitimate coding and cybersecurity "
            "tasks. Switched to Opus 4.8.\n\nDetails: `[cyber]`")
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "First attempt"}]}},
        {"type": "model_notice", "subtype": "model_refusal_fallback", "title": "Model switched",
         "summary": summary, "message": full, "original_model": "claude-fable-5-1",
         "fallback_model": "claude-opus-4-8", "scope": "session"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fallback answer"}]}},
        {"type": "result"},
    ]
    state["chat_sse"] = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    page.add_init_script("""
        window.__announced = [];
        document.addEventListener('DOMContentLoaded', () => {
            const el = document.getElementById('status-announcer');
            new MutationObserver(() => {
                if (el.textContent) window.__announced.push(el.textContent);
            }).observe(el, {childList: true, characterData: true, subtree: true});
        });
    """)
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Audit this binary")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()

    notice = page.locator("#transcript article.msg.info", has_text="Switched to Opus 4.8")
    playwright.expect(notice).to_have_count(1)
    playwright.expect(notice.get_by_role("heading")).to_have_text("Model switched")
    playwright.expect(page.locator("#transcript")).to_contain_text("Fallback answer")
    texts = page.locator("#transcript article").all_text_contents()
    first = next(i for i, t in enumerate(texts) if "First attempt" in t)
    note = next(i for i, t in enumerate(texts) if "Switched to Opus 4.8" in t)
    reply = next(i for i, t in enumerate(texts) if "Fallback answer" in t)
    assert first < note < reply
    assert "Fallback answer" not in texts[first]
    page.wait_for_function(
        "summary => window.__announced.includes(summary)", arg=summary, timeout=15000,
    )


def test_reopened_chat_shows_past_model_notices(ui):
    page, state = ui
    state["session"] = {"provider": "claude", "model": "claude-test", "messages": [
        {"role": "user", "text": "Audit this binary"},
        {"role": "notice", "title": "Model switched",
         "text": "Fable 5.1's safeguards flagged this message. Switched to Opus 4.8."},
        {"role": "assistant", "text": "Here is the audit."},
    ]}
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/?session=saved")

    notice = page.locator("#transcript article.msg.info", has_text="Switched to Opus 4.8")
    playwright.expect(notice).to_have_count(1)
    playwright.expect(notice.get_by_role("heading")).to_have_text("Model switched")
    texts = page.locator("#transcript article").all_text_contents()
    assert [i for i, t in enumerate(texts) if "Audit this binary" in t][0] \
        < [i for i, t in enumerate(texts) if "Switched to Opus 4.8" in t][0] \
        < [i for i, t in enumerate(texts) if "Here is the audit." in t][0]


def _use_real_claude_models(state):
    import app as app_module

    models = app_module._models_payload()
    state["claude_models"] = models
    state["providers"]["providers"][0]["models"] = models
    return models


@pytest.mark.parametrize("saved, expected, advisor", [
    # An explicit id the opus alias row now covers.
    ("claude-opus-5-5", "opus", None),
    # A retired advisor combo: its executor, then the alias, advisor on.
    ("opus55-fable51-advisor", "opus", "1"),
    ("claude-opus-4-7-1m", "claude-opus-4-7", None),
    # The CLI resolves Haiku to a dated id; the saved bare id still matches.
    ("claude-haiku-4-5", "haiku", None),
    ("claude-sonnet-4-6", "claude-sonnet-4-6", None),
])
def test_saved_claude_pick_follows_the_cli_list(ui, saved, expected, advisor):
    page, state = ui
    _use_real_claude_models(state)
    page.add_init_script(
        "localStorage.setItem('claude-web.provider', 'claude');"
        f"localStorage.setItem('claude-web.model', {json.dumps(saved)});"
    )
    page.goto("http://local-ui.test/")
    playwright.expect(page.locator("#model-select")).to_have_value(expected)
    assert page.evaluate("localStorage.getItem('claude-web.model')") == expected
    if advisor:
        assert page.evaluate("localStorage.getItem('claude-web.advisor')") == advisor


def test_context_meter_takes_the_window_from_the_turn(ui):
    # Nothing in the picker knows a window until a turn reports one; the
    # result carries it by model id and the meter must use it straight away.
    page, state = ui
    _use_real_claude_models(state)
    state["chat_sse"] = "data: " + json.dumps({
        "type": "result", "input_tokens": 500000,
        "context_windows": {"claude-opus-5-5": 1000000, "claude-haiku-4-5": 200000},
    }) + "\n\n"
    page.add_init_script(
        "localStorage.setItem('claude-web.provider', 'claude');"
        "localStorage.setItem('claude-web.model', 'opus');"
    )
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("How full is the context?")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    playwright.expect(page.locator("#context-text")).to_have_text("500.0k / 1000.0k (50%)")


def test_usage_dialog_shows_plan_windows_from_the_last_message_on_a_token_slot(ui):
    """An account on a year-long token can't query Anthropic's usage service,
    but every message brings back the window headers; the dialog renders
    those (as of the last message) instead of claiming the token expired."""
    page, state = ui
    usage = {
        "today": {"turns": 3, "cost_usd": 0, "cache_hit_pct": None, "has_billed_usage": False},
        "currency": "USD", "usd_rate": 1,
        "rate_limit": {
            "captured_at": 1791218280,
            "info": {
                "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1791259200,
                "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.4, "resetsAt": 1791259200},
                    "seven_day": {"utilization": 0.36, "resetsAt": 1791612000},
                    "seven_day_overage_included": {"utilization": 0.38, "resetsAt": 1791612000},
                },
            },
        },
    }
    page.route("**/api/usage?*", lambda r: r.fulfill(json=usage))
    page.route("**/api/usage/history*", lambda r: r.fulfill(json={}))
    page.route("**/api/usage/live?*", lambda r: r.fulfill(
        json={"slot": "shared", "mode": "oauth_token", "error": "usage_needs_sign_in"}))
    page.goto("http://local-ui.test/")
    page.locator("#show-usage").click()
    body = page.locator("#usage-body")
    playwright.expect(body).to_contain_text("Plan windows")
    playwright.expect(body).to_contain_text("as of this account's last message")
    rows = body.locator("table tbody tr")
    playwright.expect(rows).to_have_count(3)
    playwright.expect(rows.nth(0)).to_contain_text("5-hour")
    playwright.expect(rows.nth(0)).to_contain_text("40%")
    playwright.expect(rows.nth(1)).to_contain_text("7-day")
    playwright.expect(rows.nth(1)).to_contain_text("36%")
    playwright.expect(rows.nth(2)).to_contain_text("7-day incl. extra usage")
    playwright.expect(body).to_contain_text("Extra usage: rejected (out of credits)")
    live = page.locator("#usage-live")
    playwright.expect(live).to_contain_text("plan windows below come from the account's most recent message")
    playwright.expect(body).not_to_contain_text("session token is expired")



def test_usage_dialog_says_when_a_token_accounts_model_limits_stop(ui):
    page, state = ui
    usage = {"today": {"turns": 0, "has_billed_usage": False}, "currency": "USD", "usd_rate": 1,
             "rate_limit": None}
    page.route("**/api/usage?*", lambda r: r.fulfill(json=usage))
    page.route("**/api/usage/history*", lambda r: r.fulfill(json={}))
    page.route("**/api/usage/live?*", lambda r: r.fulfill(json={
        "slot": "shared", "mode": "oauth_token", "error": None,
        "account": {"email": "x"}, "organization": {},
        "limits": [{"label": "Week — Fable", "percent": 57, "resets_at": "2026-10-10T05:59:59+00:00"}],
        "extra_usage": None, "sign_in_expires_at": 1793706573,
    }))
    page.goto("http://local-ui.test/")
    page.locator("#show-usage").click()
    live = page.locator("#usage-live")
    playwright.expect(live).to_contain_text("Week — Fable")
    playwright.expect(live).to_contain_text("57%")
    playwright.expect(live).to_contain_text("four-week sign-in, which ends")
    playwright.expect(live).to_contain_text("Nov")


RT_THREADS = [
    {"thread_id": 344, "topic": "Started by Claude over MCP", "project_key": None,
     "participants": ["gpt-5"], "messages": 3, "open": True,
     "created_at": "2026-10-09T17:09:29+00:00", "last_activity": "2026-10-09T17:20:00+00:00",
     "closed_at": None},
    {"thread_id": 31, "topic": "Asked from the panel page", "project_key": "-home-test",
     "participants": ["gemini-pro"], "messages": 2, "open": True,
     "created_at": "2026-05-22T15:38:52+00:00", "last_activity": "2026-05-22T15:40:00+00:00",
     "closed_at": None},
]


def _roundtable_page(browser, path="/roundtable"):
    """Serve the roundtable page with a mocked thread API. Each list request's
    query string lands in ``state["list_queries"]``."""
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {"list_queries": [], "errors": errors}
    html = Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("roundtable.html").render(
        site_title="Test", asset_version=lambda _: "test", user={"sub": "anonymous"},
        available=True, default_project="-home-test",
        projects=[{"key": "-home-test", "path": "/home/test", "name": "test"},
                  {"key": "-home-test-app", "path": "/home/test/app", "name": "app"}],
        participants=[], participants_json="[]", coding_profiles=[],
        manifest_bg_color="#000000", manifest_short_name="Test",
    )

    def route(request_route):
        url = urlparse(request_route.request.url)
        if url.path == "/roundtable":
            request_route.fulfill(content_type="text/html", body=html)
        elif url.path.startswith("/static/"):
            asset = ROOT / url.path.lstrip("/")
            request_route.fulfill(path=asset) if asset.is_file() else request_route.fulfill(status=404)
        elif url.path == "/api/roundtable/threads":
            query = parse_qs(url.query)
            state["list_queries"].append(query)
            project = query.get("project", [""])[0]
            request_route.fulfill(json={"threads": [
                t for t in RT_THREADS
                if not project or t["project_key"] == (None if project == "__unbound__" else project)
            ]})
        elif url.path.startswith("/api/roundtable/threads/"):
            thread = next(t for t in RT_THREADS if t["thread_id"] == int(url.path.rsplit("/", 1)[1]))
            request_route.fulfill(json={"thread": thread, "usage": {}, "messages": [
                {"idx": 0, "speaker": "GPT Sol", "content": "Panel reply", "ts": thread["last_activity"]},
            ]})
        else:
            request_route.fulfill(json={})

    page.route("**/*", route)
    page.goto("http://roundtable-ui.test" + path)
    return context, page, state


def test_roundtable_thread_browser_lists_threads_the_ai_started(browser):
    context, page, state = _roundtable_page(browser)
    try:
        page.get_by_role("button", name="Advanced view").click()
        threads = page.locator("#thread-list")
        playwright.expect(threads).to_contain_text("Started by Claude over MCP")
        playwright.expect(threads).to_contain_text("Asked from the panel page")
        assert "project" not in state["list_queries"][-1]
        assert state["errors"] == []
    finally:
        context.close()


def test_roundtable_thread_filter_is_separate_from_the_project_context(browser):
    context, page, state = _roundtable_page(browser)
    try:
        page.get_by_role("button", name="Advanced view").click()
        threads = page.locator("#thread-list")
        playwright.expect(threads).to_contain_text("Started by Claude over MCP")
        page.get_by_label("Show threads").select_option(label="Not tied to a project")
        playwright.expect(threads).not_to_contain_text("Asked from the panel page")
        playwright.expect(threads).to_contain_text("Started by Claude over MCP")
        assert state["list_queries"][-1]["project"] == ["__unbound__"]
        page.get_by_label("Show threads").select_option(label="test")
        playwright.expect(threads).not_to_contain_text("Started by Claude over MCP")
        assert state["list_queries"][-1]["project"] == ["-home-test"]
        # The assistant's project context is its own control: changing it
        # neither refetches nor refilters the browser.
        queries = len(state["list_queries"])
        page.locator("#project-filter").select_option("")
        page.wait_for_timeout(300)
        assert len(state["list_queries"]) == queries
        assert page.locator("#thread-filter").input_value() == "-home-test"
        assert state["errors"] == []
    finally:
        context.close()


def test_roundtable_link_to_a_thread_opens_it(browser):
    context, page, state = _roundtable_page(browser, "/roundtable?thread=344")
    try:
        playwright.expect(page.locator("#advanced-pane")).to_be_visible()
        playwright.expect(page.locator("#thread-detail-topic")).to_have_text("Started by Claude over MCP")
        playwright.expect(page.locator("#thread-messages")).to_contain_text("Panel reply")
        playwright.expect(page.get_by_role("button", name="Advanced view")).to_have_attribute("aria-pressed", "true")
        assert state["errors"] == []
    finally:
        context.close()


PANEL_TURN = [
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-panel", "name": "mcp__panel__review",
         "input": {"stage": "plan", "repo": "/r", "message": "Change **hello** to howdy in greet.py."}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu-panel", "content": "Sent to the panel (round 1 of 3)"},
    ]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "It's with the panel."}]}},
    {"type": "result"},
    {"type": "panel_round", "stage": "plan", "round": 1, "max_rounds": 3, "repo": "/r",
     "thread_id": 77, "participants": ["GPT Sol", "Gemini Pro"]},
    {"type": "panel_reply", "stage": "plan", "round": 1, "participant": "GPT Sol",
     "verdict": "approve", "text": "Fine.\nVERDICT: APPROVE"},
    {"type": "panel_reply", "stage": "plan", "round": 1, "participant": "Gemini Pro",
     "verdict": "changes", "text": "STYLE.md says howdy.\nVERDICT: CHANGES NEEDED"},
    {"type": "panel_verdict", "stage": "plan", "round": 1, "max_rounds": 3,
     "outcome": "changes", "thread_id": 77, "errors": []},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Gemini has a point."}]}},
    {"type": "result"},
]


def _watch_announcements(page):
    page.add_init_script("""
        window.__announced = [];
        document.addEventListener('DOMContentLoaded', () => {
            const el = document.getElementById('status-announcer');
            new MutationObserver(() => {
                if (el.textContent) window.__announced.push(el.textContent);
            }).observe(el, {childList: true, characterData: true, subtree: true});
        });
    """)


def test_panel_review_rides_a_new_chat_and_the_argument_plays_out_in_it(ui):
    page, state = ui
    state["providers"]["providers"][0]["capabilities"] = {**CLOUD_CAPS, "panel": True}
    state["chat_sse"] = "".join(f"data: {json.dumps(e)}\n\n" for e in PANEL_TURN)
    _watch_announcements(page)
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    toggle = page.get_by_role("checkbox", name="Panel review")
    playwright.expect(toggle).to_be_visible()
    toggle.check()
    page.locator("#prompt").fill("Change the greeting")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()

    transcript = page.locator("#transcript")
    playwright.expect(transcript).to_contain_text("Gemini has a point.")
    chat_post = next(fields for path, fields in state["posts"] if path == "/api/chat")
    assert chat_post["panel"] == "1"
    headings = transcript.get_by_role("heading").all_text_contents()
    order = ["Claude to the panel: plan", "Panel review: plan, round 1 of 3",
             "GPT Sol: approves", "Gemini Pro: wants changes", "Panel verdict"]
    positions = [headings.index(h) for h in order]
    assert positions == sorted(positions)
    submission = page.locator("article.panel-submission")
    playwright.expect(submission.locator("strong")).to_have_text("hello")
    playwright.expect(transcript).not_to_contain_text("Sent to the panel")
    verdict = page.locator("article.panel-verdict")
    playwright.expect(verdict).to_contain_text("They want changes")
    playwright.expect(verdict.get_by_role("link")).to_have_attribute("href", "/roundtable?thread=77")
    page.wait_for_function(
        "() => window.__announced.some(t => t.includes('Gemini Pro wants changes'))", timeout=15000)
    page.wait_for_function(
        "() => window.__announced.some(t => t.startsWith('Panel verdict: They want changes'))",
        timeout=15000)


def test_panel_review_is_hidden_where_the_provider_cannot_do_it(ui):
    page, state = ui
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    playwright.expect(page.locator("#panel-toggle-label")).to_be_hidden()


def test_a_reopened_panel_chat_reads_as_the_argument_it_was(ui):
    page, state = ui
    state["providers"]["providers"][0]["capabilities"] = {**CLOUD_CAPS, "panel": True}
    state["session"] = {"provider": "claude", "model": "claude-test",
                        "panel": {"enabled": True, "thread_id": 77}, "messages": [
        {"role": "user", "text": "Change the greeting"},
        {"role": "tool_use", "name": "mcp__panel__review", "summary": "",
         "input": {"stage": "changes", "repo": "/r", "message": "Changed greet.py."}},
        {"role": "tool_result", "text": "Sent to the panel (round 1 of 3)", "is_error": False},
        {"role": "panel_result", "text": "[Review panel] This message is from claude-web's review panel, "
                                         "not typed by Matt.\nRound 1 of 3 on your changes: approved.\n\n"
                                         "### GPT Sol: approves\nGood."},
        {"role": "assistant", "text": "Committing."},
    ]}
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/?session=saved")
    transcript = page.locator("#transcript")
    playwright.expect(transcript).to_contain_text("Committing.")
    playwright.expect(page.locator("article.panel-submission")).to_contain_text("Changed greet.py.")
    result = page.locator("article.panel-result")
    playwright.expect(result.get_by_role("heading", name="Review panel")).to_be_visible()
    playwright.expect(result).to_contain_text("Round 1 of 3 on your changes: approved.")
    playwright.expect(result).not_to_contain_text("[Review panel]")
    playwright.expect(transcript).not_to_contain_text("Sent to the panel")
    playwright.expect(page.get_by_role("checkbox", name="Panel review")).to_be_checked()


def test_the_panels_deadlock_card_says_the_panel_is_asking(ui):
    page, state = ui
    state["providers"]["providers"][0]["capabilities"] = {**CLOUD_CAPS, "panel": True}
    card = {"type": "question_request", "id": "panel-q", "asker": "The review panel",
            "timeout_seconds": 900, "questions": [{
                "question": "The panel still has objections to Claude's plan after 3 rounds. What now?",
                "header": "Panel", "multiSelect": False, "options": [
                    {"label": "Go ahead anyway", "description": "Treat it as approved: Claude makes the changes."},
                    {"label": "Keep discussing", "description": "Give them up to 3 more rounds."},
                    {"label": "Stop", "description": "Claude stops here and waits for you."},
                ]}]}
    state["chat_sse"] = f"data: {json.dumps(card)}\n\n"
    _watch_announcements(page)
    page.add_init_script("localStorage.setItem('claude-web.provider', 'claude')")
    page.goto("http://local-ui.test/")
    page.locator("#prompt").fill("Change the greeting")
    with page.expect_response("**/api/chat"):
        page.locator("#send").click()
    dialog = page.locator("dialog[open]")
    playwright.expect(dialog.get_by_role("heading", name="The review panel is asking")).to_be_visible()
    playwright.expect(dialog).not_to_contain_text("Claude is asking")
    dialog.get_by_label("Go ahead anyway", exact=False).check()
    with page.expect_request("**/api/permission/panel-q") as req:
        dialog.get_by_role("button", name="Submit answers").click()
    sent = req.value.post_data
    assert "Go ahead anyway" in sent
    page.wait_for_function(
        "() => window.__announced.some(t => t.startsWith('The review panel is asking'))", timeout=15000)
