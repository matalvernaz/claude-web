"""Fetch a claude.ai sign-in link for a credential slot that has an
``auto_email`` configured and a ``CLAUDE_WEB_MAILBOX_POLL_CMD`` on the
host.

The flow this module drives:

  1. ``setup_flow.start_oauth`` spawns ``claude auth login``; the
     subprocess prints an ``https://claude.com/cai/oauth/authorize?...``
     URL and blocks on stdin for the paste-back code.
  2. This module launches Chromium, navigates to that URL, enters the
     configured email, and asks claude.ai to send its magic-link email.
     The browser closes once the page confirms the email went out.
  3. It shells out to ``CLAUDE_WEB_MAILBOX_POLL_CMD`` (argv: email,
     after-epoch, timeout-seconds), which blocks until a fresh
     ``https://claude.ai/magic-link#...`` arrives in the target
     mailbox and prints it on stdout.
  4. The link goes back to the caller, who shows it on the person's
     accounts page. They open it in their own browser, click Authorize,
     and paste the code claude.com shows into the same form the manual
     flow uses; ``setup_flow.submit_code`` finishes the CLI's PKCE
     exchange.

The browser never opens the magic link itself. On 2026-10-05 that page
answered the automated browser with an Arkose puzzle and, two minutes
later, "Couldn't verify your browser", while the login page's own check
passed. Nothing here tries to beat that check; a person gets the link.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger("claude-web.auto_signin")


ENV_MAILBOX_CMD = "CLAUDE_WEB_MAILBOX_POLL_CMD"
ENV_BROWSER = "CLAUDE_WEB_SIGNIN_BROWSER"

# Bounds. Not env-configurable — pushing them higher rarely helps and
# usually just papers over a genuine breakage.
EMAIL_SEND_TIMEOUT_S = 60          # entering email + submitting the form
MAILBOX_POLL_TIMEOUT_S = 120       # from send-magic-link to inbox arrival

_EMAIL_INPUT = 'input[type="email"], input[name="email"], input[id="email"]'


class AutoSigninError(RuntimeError):
    """Non-fatal failure. Message is safe to surface to the caller (never
    contains PKCE secrets, session cookies, or the sign-in link)."""


def mailbox_cmd_configured() -> bool:
    return bool(os.environ.get(ENV_MAILBOX_CMD, "").strip())


async def _poll_mailbox(email: str, after_epoch: int, timeout_s: int) -> str:
    cmd = os.environ.get(ENV_MAILBOX_CMD, "").strip()
    if not cmd:
        raise AutoSigninError(f"{ENV_MAILBOX_CMD} not set on this server")
    argv = shlex.split(cmd) + [email, str(after_epoch), str(timeout_s)]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 15)
    except asyncio.TimeoutError:
        raise AutoSigninError("mailbox poll wrapper hung past its own timeout") from None
    finally:
        # Cancelling an attempt must also stop a shell wrapper's ssh/pwsh
        # children; otherwise retries leave mailbox readers running.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            proc.kill()
        await proc.wait()
    if proc.returncode == 0:
        url = (out or b"").decode("utf-8", errors="replace").strip()
        if url.startswith("https://claude.ai/magic-link#"):
            return url
        raise AutoSigninError(
            "mailbox poll wrapper exited 0 but printed an unexpected line"
        )
    if proc.returncode == 1:
        raise AutoSigninError("no magic-link email arrived within the timeout")
    raise AutoSigninError(
        f"mailbox poll wrapper failed (exit {proc.returncode})"
    )


async def _wait_for_email_sent(page) -> None:
    """Don't start reading mail until the website confirms it sent mail.

    Submitting the email can redirect to Cloudflare instead. Give automatic
    browser verification time to finish before offering manual recovery.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    try:
        await page.wait_for_function(r"""() => {
          const text = document.body?.innerText || '';
          return /check your (?:email|inbox)|click the link sent to|we.ve sent (?:you )?(?:an email|a (?:sign.in|magic|login) link)|enter (?:the |your )?(?:verification|sign.in|login) code/i.test(text);
        }""", timeout=EMAIL_SEND_TIMEOUT_S * 1000)
    except PWTimeout:
        body = await page.locator("body").inner_text(timeout=5000)
        if ("challenge_redirect" in urlparse(page.url).path or re.search(
            r"security verification|verify you are (?:a )?human|verify you are not a bot",
            body, re.IGNORECASE,
        )):
            raise AutoSigninError(
                "Claude's browser security verification did not finish automatically, "
                "so no sign-in email was confirmed. Open the Claude sign-in page below "
                "in your own browser instead, then paste the code here."
            ) from None
        raise AutoSigninError(
            "Claude did not confirm sending a sign-in email. Open the Claude "
            "sign-in page below in your own browser instead, then paste the code here."
        ) from None


async def _stop_browser_process(proc) -> None:
    if proc is None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        elif proc.returncode is None:
            proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


@asynccontextmanager
async def _browser_context(pw):
    """Drive an ordinary Chromium process through its loopback CDP port.

    The default automation launch stalled at Claude's security check, even
    headed; a normal installed Chromium launch completed that same check.
    Keep the browser's real user agent and default flags. On a Linux server,
    Xvfb supplies a display when installed; otherwise use headless Chromium.
    Each attempt gets a private, temporary profile, never an operator's one.
    """
    browser = proc = display_proc = None
    with tempfile.TemporaryDirectory(prefix="claude-web-signin-") as profile:
        try:
            env = dict(os.environ)
            headless = sys.platform.startswith("linux") and not env.get("DISPLAY")
            xvfb = shutil.which("Xvfb") if headless else None
            if xvfb:
                display_proc = await asyncio.create_subprocess_exec(
                    xvfb, "-displayfd", "1", "-screen", "0", "1280x900x24", "-nolisten", "tcp",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                display = (await asyncio.wait_for(display_proc.stdout.readline(), 10)).strip()
                if not display.isdigit():
                    raise AutoSigninError("Could not start the sign-in browser's display")
                env["DISPLAY"] = ":" + display.decode("ascii")
                headless = False
            binary = (os.environ.get(ENV_BROWSER) or shutil.which("chromium")
                      or shutil.which("chromium-browser") or shutil.which("google-chrome")
                      or pw.chromium.executable_path)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            args = [binary, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check"]
            if headless:
                args.append("--headless=new")
            proc = await asyncio.create_subprocess_exec(
                *args, "about:blank", env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and proc.returncode is None:
                try:
                    browser = await pw.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=1000,
                    )
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            if browser is None:
                raise AutoSigninError("Could not start the sign-in browser")
            yield browser.contexts[0]
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.close(), 5)
                except Exception:
                    pass
            await _stop_browser_process(proc)
            await _stop_browser_process(display_proc)


async def _request_email(
    oauth_url: str,
    email: str,
    on_stage: Callable[[str], None],
) -> int:
    """Drive the login page until it confirms a sign-in email was sent.

    Returns the epoch taken just before the send, so the mailbox poll can
    skip anything left over from an earlier attempt.
    """
    # Import lazily so a claude-web install without playwright still boots.
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    async with async_playwright() as pw:
        async with _browser_context(pw) as ctx:
            page = await ctx.new_page()

            on_stage("opening sign-in page")
            # Cloudflare's JS challenge can take a few seconds; wait_until
            # 'domcontentloaded' before we start hunting for the form.
            await page.goto(oauth_url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(
                    _EMAIL_INPUT, timeout=EMAIL_SEND_TIMEOUT_S * 1000,
                )
            except PWTimeout:
                raise AutoSigninError(
                    "sign-in email field never appeared (Cloudflare or "
                    "Anthropic UI changed?)"
                ) from None

            on_stage("submitting email")
            send_epoch = int(time.time())
            email_input = page.locator(_EMAIL_INPUT).first
            await email_input.fill(email)
            # Anthropic's sign-in has a "Continue with email" button; both
            # click and Enter work. Enter is more resilient to label
            # rewording.
            await email_input.press("Enter")

            on_stage("confirming email was sent")
            await _wait_for_email_sent(page)
            return send_epoch


async def request_magic_link(
    oauth_url: str,
    email: str,
    on_stage: Callable[[str], None],
) -> str:
    """Ask claude.ai for a sign-in email and return the link it contains.

    Raises AutoSigninError on any failure the caller should surface; does
    not swallow subprocess/mail failures.
    """
    send_epoch = await _request_email(oauth_url, email, on_stage)
    on_stage("waiting for the sign-in email")
    return await _poll_mailbox(email, send_epoch, MAILBOX_POLL_TIMEOUT_S)
