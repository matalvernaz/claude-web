"""Run the real chat UI against mocked HTTP/SSE, without starting providers."""
from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import urlparse

import pytest
from jinja2 import Environment, FileSystemLoader

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
    }
    html = Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("index.html").render(
        site_title="Test chat", asset_version=lambda _: "test", models=[cloud_model],
        models_json=json.dumps([cloud_model]), effort_levels=["low", "high"],
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
                if state.get("change_settings_during_send"):
                    page.evaluate("""() => {
                        const model = document.getElementById('model-select');
                        model.value = 'coder:30b';
                        model.dispatchEvent(new Event('change'));
                    }""")
                request_route.fulfill(status=409, json={"error": state.get("conflict", "model_changed")})
            elif path == "/api/chat":
                request_route.fulfill(content_type="text/event-stream", body='data: {"type":"result"}\n\n')
            else:
                request_route.fulfill(json={})
        elif path == "/":
            body = html.replace('<option value="claude" selected>Claude</option>',
                                '<option value="local" selected>Local</option>') if state["initial_local"] else html
            request_route.fulfill(content_type="text/html", body=body)
        elif path.startswith("/static/"):
            asset = ROOT / path.lstrip("/")
            request_route.fulfill(path=asset) if asset.is_file() else request_route.fulfill(status=404)
        elif path == "/api/providers":
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
