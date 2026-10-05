"""The sign-in link request: the browser only asks for the email, the link
is handed to the person, and every failure leaves the manual form usable."""
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
    monkeypatch.setattr(auto_signin, "request_magic_link", challenge)
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
    monkeypatch.setattr(auto_signin, "request_magic_link", crash)
    await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    await state.auto_task
    assert state.status == "awaiting_code"
    assert "SECRET_VERIFIER" not in state.error + caplog.text


async def test_a_received_link_is_shown_while_the_cli_keeps_waiting_for_the_code(
    client, monkeypatch, tmp_path,
):
    import app
    import setup_flow

    state = setup_flow.OAuthFlowState(
        variant="claudeai", flow_key="link-test", home=tmp_path,
        status="awaiting_code", url="https://claude.ai/oauth?state=test",
    )
    monkeypatch.setattr(app, "_require_owned_credential", lambda *a: {"auto_email": "test@example.com"})
    monkeypatch.setattr(app, "_ensure_credential_home", lambda *a: tmp_path)
    monkeypatch.setattr(app, "_credential_flow_key", lambda *a: "link-test")
    monkeypatch.setenv(auto_signin.ENV_MAILBOX_CMD, "unused-test-wrapper")
    monkeypatch.setattr(setup_flow, "_flows", {"link-test": state})

    async def start(*a, **kw):
        return state

    stages = []

    async def deliver(*, oauth_url, email, on_stage):
        on_stage("waiting for the sign-in email")
        return "https://claude.ai/magic-link#token"

    monkeypatch.setattr(setup_flow, "start_oauth", start)
    monkeypatch.setattr(auto_signin, "request_magic_link", deliver)
    initial = await app.api_credentials_oauth_auto_signin(2, user={"sub": "test"})
    assert initial["stage"] == "launching browser"
    assert initial["magic_link"] is None
    await state.auto_task
    # The browser is done; the link is for the person, and the CLI is still
    # the one waiting for the code it leads to.
    public = state.to_public()
    assert public["magic_link"] == "https://claude.ai/magic-link#token"
    assert public["status"] == "awaiting_code"
    assert public["stage"] is None
    assert public["error"] is None
    assert state.code_event.is_set() is False


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
