"""The server-browser sign-in: it asks for the email, hands the link to the
person, types their verification code, and every failure leaves the manual
form usable."""
import asyncio
import os
import shlex
import sys

import pytest

import auto_signin


@pytest.mark.parametrize("text, expected", [
    ("Check your email. We sent you a secure link.", None),
    ("Performing security verification. Verify you are not a bot.", "browser security verification"),
])
async def test_email_confirmation_distinguishes_a_real_email_from_a_challenge(text, expected, monkeypatch):
    monkeypatch.setattr(auto_signin, "EMAIL_SEND_TIMEOUT_S", 0.2)
    pw = pytest.importorskip("playwright.async_api")
    async with pw.async_playwright() as manager:
        try:
            browser = await manager.chromium.launch()
        except pw.Error as exc:
            if os.environ.get("CLAUDE_WEB_REQUIRE_BROWSER") == "1":
                pytest.fail(str(exc))
            pytest.skip("Chromium unavailable")
        try:
            page = await browser.new_page()
            await page.set_content(f"<body>{text}</body>")
            if expected:
                with pytest.raises(auto_signin.AutoSigninError, match=expected):
                    await auto_signin._wait_for_email_sent(page)
            else:
                await auto_signin._wait_for_email_sent(page)
        finally:
            await browser.close()


async def test_transient_security_check_can_clear_automatically(monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "EMAIL_SEND_TIMEOUT_S", 5)
    async with pw.async_playwright() as manager:
        try:
            browser = await manager.chromium.launch()
        except pw.Error as exc:
            if os.environ.get("CLAUDE_WEB_REQUIRE_BROWSER") == "1":
                pytest.fail(str(exc))
            pytest.skip("Chromium unavailable")
        try:
            page = await browser.new_page()
            await page.set_content("""<body>Performing security verification.
                <script>setTimeout(() => document.body.textContent = 'Check your inbox', 200)</script>
                </body>""")
            await auto_signin._wait_for_email_sent(page)
        finally:
            await browser.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
async def test_cancelling_mailbox_poll_reaps_the_running_wrapper(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    script = tmp_path / "poll.py"
    script.write_text(
        "import os,time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n"
    )
    monkeypatch.setenv(auto_signin.ENV_MAILBOX_CMD, shlex.join([sys.executable, str(script)]))
    task = asyncio.create_task(auto_signin._poll_mailbox("test@example.com", 1, 120))
    try:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
        pid = int(pid_file.read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_challenge_keeps_the_same_cli_flow_available_for_manual_login(client, monkeypatch, tmp_path):
    import app
    import setup_flow

    state = setup_flow.OAuthFlowState(
        variant="claudeai", flow_key="test-auto", home=tmp_path,
        status="awaiting_code", url="https://claude.ai/oauth/authorize?secret=test",
    )
    monkeypatch.setattr(app, "_require_owned_credential", lambda *a: {"auto_email": "test@example.com"})
    monkeypatch.setattr(app, "_ensure_credential_home", lambda *a: tmp_path)
    monkeypatch.setattr(app, "_credential_flow_key", lambda *a: "test-auto")
    monkeypatch.setenv(auto_signin.ENV_MAILBOX_CMD, "unused-test-wrapper")
    monkeypatch.setattr(setup_flow, "_flows", {"test-auto": state})

    async def start(*a, **kw):
        return state

    async def challenge(**kw):
        raise auto_signin.AutoSigninError("Browser verification required. Open the sign-in link.")

    monkeypatch.setattr(setup_flow, "start_oauth", start)
    monkeypatch.setattr(auto_signin, "run_signin", challenge)
    initial = await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    assert initial["stage"] == "launching browser"
    await state.auto_task
    assert state.status == "awaiting_code"
    assert state.stage is None
    assert "Browser verification" in state.error
    assert state.url == initial["url"]
    assert state.code_event.is_set() is False


async def test_cancel_stops_browser_driver_and_cli_wait(monkeypatch, tmp_path):
    import setup_flow

    cleaned = asyncio.Event()

    async def browser_driver():
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    state = setup_flow.OAuthFlowState(variant="claudeai", home=tmp_path, status="awaiting_code")
    state.auto_task = asyncio.create_task(browser_driver())
    monkeypatch.setattr(setup_flow, "_flows", {"cancel-test": state})
    await asyncio.sleep(0)
    await setup_flow.cancel_flow("cancel-test")
    assert cleaned.is_set()
    assert state.auto_task.cancelled()
    assert state.status == "cancelled"
    assert state.code_event.is_set()


async def test_browser_exception_does_not_expose_oauth_url(client, monkeypatch, tmp_path, caplog):
    import app
    import setup_flow

    state = setup_flow.OAuthFlowState(variant="claudeai", home=tmp_path, status="awaiting_code", url="https://claude.ai/oauth")
    monkeypatch.setattr(app, "_require_owned_credential", lambda *a: {"auto_email": "test@example.com"})
    monkeypatch.setattr(app, "_ensure_credential_home", lambda *a: tmp_path)
    monkeypatch.setattr(app, "_credential_flow_key", lambda *a: "error-test")
    monkeypatch.setenv(auto_signin.ENV_MAILBOX_CMD, "unused-test-wrapper")
    monkeypatch.setattr(setup_flow, "_flows", {"error-test": state})

    async def start(*a, **kw):
        return state

    async def crash(**kw):
        raise RuntimeError("goto https://claude.ai/oauth?code=SECRET_VERIFIER")

    monkeypatch.setattr(setup_flow, "start_oauth", start)
    monkeypatch.setattr(auto_signin, "run_signin", crash)
    await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    await state.auto_task
    assert state.status == "awaiting_code"
    assert "SECRET_VERIFIER" not in state.error + caplog.text


def _slot_flow(app, setup_flow, monkeypatch, tmp_path, key):
    state = setup_flow.OAuthFlowState(
        variant="claudeai", flow_key=key, home=tmp_path,
        status="awaiting_code", url="https://claude.ai/oauth?state=test",
    )
    monkeypatch.setattr(app, "_require_owned_credential", lambda *a: {"auto_email": "test@example.com"})
    monkeypatch.setattr(app, "_ensure_credential_home", lambda *a: tmp_path)
    monkeypatch.setattr(app, "_credential_flow_key", lambda *a: key)
    monkeypatch.setenv(auto_signin.ENV_MAILBOX_CMD, "unused-test-wrapper")
    monkeypatch.setattr(setup_flow, "_flows", {key: state})

    async def start(variant, *a, **kw):
        # The dance costs the person the same either way, so it mints the
        # year-long token rather than a four-week sign-in.
        assert variant == "token"
        return state

    monkeypatch.setattr(setup_flow, "start_oauth", start)
    return state


class _Body:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


async def test_the_person_opens_the_link_and_types_the_code_and_the_browser_finishes(
    client, monkeypatch, tmp_path,
):
    import app
    import setup_flow

    state = _slot_flow(app, setup_flow, monkeypatch, tmp_path, "handshake")
    seen = {}

    async def fake_drive():
        await state.code_event.wait()
        seen["paste"] = state.code
        state.status = "done"

    state.driver_task = asyncio.create_task(fake_drive())

    async def browser(*, oauth_url, email, on_stage, on_magic_link,
                      wait_for_verification_code, on_code_rejected):
        on_stage("waiting for the sign-in email")
        on_magic_link("https://claude.ai/magic-link#token")
        on_stage("waiting for your verification code")
        seen["code"] = await wait_for_verification_code()
        on_stage("authorizing claude-web")
        return "authcode#verifier"

    monkeypatch.setattr(auto_signin, "run_signin", browser)
    initial = await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    assert initial["magic_link"] is None and initial["awaiting_verification"] is False

    # Too early: the browser has not asked yet.
    for _ in range(50):
        if state.awaiting_verification:
            break
        await asyncio.sleep(0.01)
    public = state.to_public()
    assert public["magic_link"] == "https://claude.ai/magic-link#token"
    assert public["awaiting_verification"] is True
    assert public["stage"] == "waiting for your verification code"

    with pytest.raises(app.HTTPException) as too_long:
        await app.api_credentials_oauth_verification_code(2, _Body({"code": "x" * 33}), user={"sub": "test"})
    assert too_long.value.status_code == 400
    accepted = await app.api_credentials_oauth_verification_code(
        2, _Body({"code": " 123 456 "}), user={"sub": "test"})
    assert accepted["awaiting_verification"] is False
    await state.auto_task
    assert seen == {"code": "123456", "paste": "authcode#verifier"}
    assert state.status == "done"
    assert state.stage is None and state.error is None

    with pytest.raises(app.HTTPException) as nothing_waiting:
        await app.api_credentials_oauth_verification_code(2, _Body({"code": "123456"}), user={"sub": "test"})
    assert nothing_waiting.value.status_code == 409


async def test_a_refused_code_reopens_the_form_with_the_reason(client, monkeypatch, tmp_path):
    import app
    import setup_flow

    state = _slot_flow(app, setup_flow, monkeypatch, tmp_path, "refused")
    codes = []

    async def browser(*, oauth_url, email, on_stage, on_magic_link,
                      wait_for_verification_code, on_code_rejected):
        on_magic_link("https://claude.ai/magic-link#token")
        on_stage("waiting for your verification code")
        codes.append(await wait_for_verification_code())
        on_code_rejected("claude.ai did not accept that code. Check it and try again.")
        on_stage("waiting for your verification code")
        codes.append(await wait_for_verification_code())
        raise auto_signin.AutoSigninError("gave up")

    monkeypatch.setattr(auto_signin, "run_signin", browser)
    await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    for _ in range(50):
        if state.awaiting_verification:
            break
        await asyncio.sleep(0.01)
    await app.api_credentials_oauth_verification_code(2, _Body({"code": "111111"}), user={"sub": "test"})
    for _ in range(50):
        if state.awaiting_verification and state.error:
            break
        await asyncio.sleep(0.01)
    public = state.to_public()
    assert public["awaiting_verification"] is True
    assert "did not accept" in public["error"]
    assert public["status"] == "awaiting_code"
    await app.api_credentials_oauth_verification_code(2, _Body({"code": "222222"}), user={"sub": "test"})
    await state.auto_task
    assert codes == ["111111", "222222"]
    assert state.error == "gave up" and state.status == "awaiting_code"


async def test_an_unanswered_code_wait_ends_with_a_start_again_message(monkeypatch):
    monkeypatch.setattr(auto_signin, "VERIFICATION_WAIT_S", 0.05)

    async def never():
        await asyncio.Future()

    with pytest.raises(auto_signin.AutoSigninError, match="start again"):
        await auto_signin._await_code(never)


async def _page(pw_module, manager):
    try:
        browser = await manager.chromium.launch()
    except pw_module.Error as exc:
        if os.environ.get("CLAUDE_WEB_REQUIRE_BROWSER") == "1":
            pytest.fail(str(exc))
        pytest.skip("Chromium unavailable")
    return browser, await browser.new_page()


_CALLBACK = "https://platform.claude.com/oauth/code/callback"


async def _route_claude(page, login_html, authorize_html):
    async def login(route):
        await route.fulfill(content_type="text/html", body=login_html)

    async def authorize(route):
        await route.fulfill(content_type="text/html", body=authorize_html)

    async def callback(route):
        await route.fulfill(content_type="text/html", body="<body>Paste code: shown</body>")

    await page.route("https://claude.ai/login**", login)
    await page.route("https://claude.com/cai/oauth/authorize**", authorize)
    await page.route(_CALLBACK + "**", callback)


@pytest.mark.parametrize("field_html", [
    '<input id="c" autocomplete="one-time-code">',
    "".join(f'<input class="box" maxlength="1" id="b{i}">' for i in range(6)),
])
async def test_the_browser_types_the_code_into_one_field_or_six_boxes(field_html, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "VERIFICATION_ACCEPT_S", 5)
    login_html = f"""<body><p>To continue, click the link sent to you</p>
      <button id="open">Enter verification code</button>
      <div id="codebox" hidden>{field_html}<button id="go">Continue</button></div>
      <script>
        document.getElementById('open').onclick = () => {{ codebox.hidden = false; }};
        const boxes = [...document.querySelectorAll('.box')];
        boxes.forEach((b, i) => b.addEventListener('input', () => {{
          if (b.value && boxes[i + 1]) boxes[i + 1].focus();
          if (boxes.every(x => x.value)) location.href = 'https://claude.com/cai/oauth/authorize?ok=1';
        }}));
        document.getElementById('go').onclick = () => {{
          if (document.getElementById('c') && document.getElementById('c').value === '482913')
            location.href = 'https://claude.com/cai/oauth/authorize?ok=1';
        }};
      </script></body>"""
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await _route_claude(page, login_html, "<body>Authorize?</body>")
            await page.goto("https://claude.ai/login?returnTo=x")
            assert await auto_signin._enter_verification_code(page, "482913", auto_signin._Debug()) is True
            assert page.url.startswith("https://claude.com/cai/oauth/authorize")
        finally:
            await browser.close()


async def test_a_puzzle_after_the_code_is_handed_to_the_person_not_called_a_wrong_code(monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "VERIFICATION_ACCEPT_S", 5)
    # What claude.ai did on 2026-10-05: the hCaptcha loader frame is there
    # from the start; "Verify email address" then opens a puzzle in a frame
    # with no telling URL, and the page stays on /login.
    login_html = """<body><input id="c"><button id="go">Verify email address</button>
      <iframe src="https://newassets.hcaptcha.com/captcha/v1/loader.js"></iframe>
      <script>document.getElementById('go').onclick = () => {
        const f = document.createElement('iframe');
        f.srcdoc = '<p>Find all sports and exercise equipment</p><button>Skip</button>';
        document.body.appendChild(f);
      };</script></body>"""
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await _route_claude(page, login_html, "<body>unused</body>")
            await page.route("https://newassets.hcaptcha.com/**",
                             lambda route: route.fulfill(content_type="text/html", body="<body></body>"))
            await page.goto("https://claude.ai/login?returnTo=x")
            with pytest.raises(auto_signin.AutoSigninError, match="your own browser"):
                await auto_signin._enter_verification_code(page, "100760", auto_signin._Debug())
        finally:
            await browser.close()


async def test_a_code_claude_keeps_on_the_login_page_counts_as_refused(monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "VERIFICATION_ACCEPT_S", 1)
    login_html = '<body><input id="c"><button>Continue</button></body>'
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await _route_claude(page, login_html, "<body>unused</body>")
            await page.goto("https://claude.ai/login?returnTo=x")
            assert await auto_signin._enter_verification_code(page, "000000", auto_signin._Debug()) is False
        finally:
            await browser.close()


async def test_the_browser_presses_authorize_and_reads_the_code_off_the_callback(monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "AUTHORIZE_TIMEOUT_S", 10)
    authorize_html = f"""<body><h1>Claude Code wants access</h1>
      <button onclick="location.href='{_CALLBACK}?code=abc123&state=ver456'">Authorize</button>
      <button>Accept all cookies</button></body>"""
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await _route_claude(page, "<body>unused</body>", authorize_html)
            await page.goto("https://claude.com/cai/oauth/authorize?x=1")
            await auto_signin._wait_for_authorization(page, _CALLBACK, auto_signin._Debug())
            assert auto_signin._paste_from_callback_url(page.url) == "abc123#ver456"
        finally:
            await browser.close()


async def test_a_bot_check_after_the_code_is_handed_to_the_person(monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(auto_signin, "AUTHORIZE_TIMEOUT_S", 10)
    monkeypatch.setattr(auto_signin, "CAPTCHA_GRACE_S", 0.2)
    authorize_html = '<body>Loading...<iframe src="https://client-api.arkoselabs.com/fc/gc/"></iframe></body>'
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await _route_claude(page, "<body>unused</body>", authorize_html)
            await page.route("https://client-api.arkoselabs.com/**",
                             lambda route: route.fulfill(content_type="text/html", body="<body>puzzle</body>"))
            await page.goto("https://claude.com/cai/oauth/authorize?x=1")
            with pytest.raises(auto_signin.AutoSigninError, match="your own browser"):
                await auto_signin._wait_for_authorization(page, _CALLBACK, auto_signin._Debug())
        finally:
            await browser.close()


async def test_debug_snapshots_strip_urls_and_land_in_a_private_dir(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    monkeypatch.setenv(auto_signin.ENV_DEBUG_DIR, str(tmp_path / "debug"))
    async with pw.async_playwright() as manager:
        browser, page = await _page(pw, manager)
        try:
            await page.route("https://claude.ai/magic-link**",
                             lambda route: route.fulfill(content_type="text/html", body="<body>Loading...</body>"))
            await page.goto("https://claude.ai/magic-link?secret=1#token")
            debug = auto_signin._Debug()
            await debug.snap(page, "after link")
        finally:
            await browser.close()
    run_dir, = (tmp_path / "debug").iterdir()
    assert oct(run_dir.stat().st_mode & 0o777) == "0o700"
    text = (run_dir / "01-after-link.txt").read_text()
    assert text.startswith("https://claude.ai/magic-link\n")
    assert "secret" not in text and "token" not in text
    assert (run_dir / "01-after-link.png").exists()


async def test_pasting_the_code_stops_a_mailbox_read_still_in_progress(monkeypatch, tmp_path):
    import setup_flow

    state = setup_flow.OAuthFlowState(
        variant="claudeai", flow_key="paste-test", home=tmp_path, status="awaiting_code")
    stopped = asyncio.Event()

    async def reader():
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def fake_drive():
        await state.code_event.wait()
        state.status = "done"

    state.auto_task = asyncio.create_task(reader())
    state.driver_task = asyncio.create_task(fake_drive())
    monkeypatch.setattr(setup_flow, "_flows", {"paste-test": state})
    await asyncio.sleep(0)
    result = await setup_flow.submit_code("code#verifier", flow_key="paste-test")
    assert result.status == "done"
    assert stopped.is_set()
    assert state.auto_task.cancelled()
