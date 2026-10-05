"""Fully-automated Claude sign-in for credential slots that have an
``auto_email`` configured and a ``CLAUDE_WEB_MAILBOX_POLL_CMD`` on the
host.

The generic flow this module drives:

  1. ``setup_flow.start_oauth`` spawns ``claude auth login``; the
     subprocess prints an ``https://claude.com/cai/oauth/authorize?...``
     URL and blocks on stdin for the paste-back code.
  2. This module launches Chromium, navigates to that URL,
     enters the configured email, and triggers a magic-link email.
  3. It shells out to ``CLAUDE_WEB_MAILBOX_POLL_CMD`` (argv: email,
     after-epoch, timeout-seconds), which blocks until a fresh
     ``https://claude.ai/magic-link#...`` arrives in the target
     mailbox and prints it on stdout.
  4. Chromium navigates to that magic-link (fragment intact so the
     page's JS can exchange the token for a claude.ai session cookie).
  5. Once the OAuth authorize page redirects to
     ``https://platform.claude.com/oauth/code/callback?code=…&state=…``
     the driver reads ``code#state`` off the URL and hands it to
     ``setup_flow.submit_code``, completing the CLI's PKCE exchange.

Anthropic's OAuth URL uses ``state`` to round-trip the CLI's PKCE
``code_verifier`` — that's why the paste-back string is
``<authorization_code>#<verifier>``. If Anthropic changes that layout,
``_extract_paste_code`` will also scrape the visible paste string off
the callback page as a fallback.
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
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("claude-web.auto_signin")


ENV_MAILBOX_CMD = "CLAUDE_WEB_MAILBOX_POLL_CMD"
ENV_BROWSER = "CLAUDE_WEB_SIGNIN_BROWSER"

# Bounds. Not env-configurable — pushing them higher rarely helps and
# usually just papers over a genuine breakage.
EMAIL_SEND_TIMEOUT_S = 60          # entering email + submitting the form
MAILBOX_POLL_TIMEOUT_S = 120       # from send-magic-link to inbox arrival
MAGIC_LINK_REDIRECT_TIMEOUT_S = 60  # magic-link → session cookie → callback


class AutoSigninError(RuntimeError):
    """Non-fatal auto-signin failure. Message is safe to surface to
    the caller (never contains PKCE secrets, session cookies, etc)."""


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


@dataclass
class _CodeResult:
    paste: str  # the "code#verifier" string ready for setup_flow.submit_code


def _paste_from_callback_url(callback_url: str) -> Optional[str]:
    """Anthropic redirects to
    ``https://platform.claude.com/oauth/code/callback?code=A&state=V`` on
    success; that's the paste-back ``A#V``. Returns None if the URL doesn't
    match (in which case the caller falls back to scraping the page)."""
    try:
        u = urlparse(callback_url)
    except ValueError:
        return None
    if "oauth/code/callback" not in (u.path or ""):
        return None
    q = parse_qs(u.query or "")
    code = (q.get("code") or [None])[0]
    state = (q.get("state") or [None])[0]
    if code and state:
        return f"{code}#{state}"
    return None


_PASTE_RE = re.compile(r"[A-Za-z0-9_\-]{20,}#[A-Za-z0-9_\-]{20,}")


def _paste_from_page_text(text: str) -> Optional[str]:
    m = _PASTE_RE.search(text or "")
    return m.group(0) if m else None


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
                "Claude's browser security verification did not finish automatically. "
                "No sign-in email was confirmed. You can open the sign-in link below "
                "to finish in your browser, then paste the returned code here."
            ) from None
        raise AutoSigninError(
            "Claude did not confirm sending a sign-in email. "
            "Open the sign-in link below to continue in your browser."
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


async def _run_browser_flow(
    oauth_url: str,
    email: str,
    poll_mailbox: Callable[[int, int], Awaitable[str]],
    on_stage: Callable[[str], None],
) -> _CodeResult:
    # Import lazily so a claude-web install without playwright still boots.
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    async with async_playwright() as pw:
        async with _browser_context(pw) as ctx:
            page = await ctx.new_page()

            on_stage("opening sign-in page")
            # Cloudflare's JS challenge can take a few seconds; wait_until
            # 'domcontentloaded' before we start hunting for the form.
            await page.goto(oauth_url, wait_until="domcontentloaded", timeout=45000)
            # Give CF a moment to release the interstitial if there is one.
            try:
                await page.wait_for_selector(
                    'input[type="email"], input[name="email"], input[id="email"]',
                    timeout=EMAIL_SEND_TIMEOUT_S * 1000,
                )
            except PWTimeout:
                raise AutoSigninError(
                    "sign-in email field never appeared (Cloudflare or "
                    "Anthropic UI changed?)"
                ) from None

            on_stage("submitting email")
            # Timestamp captured BEFORE we click send — the mailbox poll
            # discards anything received at-or-before this epoch to avoid
            # picking up a leftover email from an earlier attempt.
            send_epoch = int(time.time())
            email_input = page.locator(
                'input[type="email"], input[name="email"], input[id="email"]'
            ).first
            await email_input.fill(email)
            # Anthropic's sign-in has a "Continue with email" button; both
            # click and Enter work. Enter is more resilient to label
            # rewording.
            await email_input.press("Enter")

            on_stage("confirming email was sent")
            await _wait_for_email_sent(page)

            on_stage("waiting for magic-link email")
            magic_link = await poll_mailbox(send_epoch, MAILBOX_POLL_TIMEOUT_S)

            on_stage("opening magic link")
            # The magic link's session-exchange runs in a page-level JS
            # handler; the browser executes it as it lands on the URL.
            await page.goto(magic_link, wait_until="load", timeout=30000)

            on_stage("waiting for callback redirect")
            try:
                await page.wait_for_url(
                    "https://platform.claude.com/oauth/code/callback*",
                    timeout=MAGIC_LINK_REDIRECT_TIMEOUT_S * 1000,
                )
            except PWTimeout:
                raise AutoSigninError(
                    "magic-link sign-in did not redirect to the OAuth "
                    "callback within the timeout"
                ) from None

            callback_url = page.url
            paste = _paste_from_callback_url(callback_url)
            if not paste:
                # Fall back to scraping any visible "code#verifier" text on
                # the callback page.
                try:
                    body_text = await page.locator("body").inner_text(timeout=5000)
                except Exception:
                    body_text = ""
                paste = _paste_from_page_text(body_text)
            if not paste:
                raise AutoSigninError(
                    "reached the callback page but could not extract the "
                    "paste-back code"
                )
            return _CodeResult(paste=paste)


async def run_auto_signin(
    oauth_url: str,
    email: str,
    flow_key: str,
    on_stage: Callable[[str], None],
) -> None:
    """End-to-end: drive the browser dance, then hand the paste-back code
    to setup_flow.submit_code. Raises AutoSigninError on any failure the
    caller should surface; does not swallow subprocess/mail failures."""
    # setup_flow is imported here (not top-level) so tests can stub the
    # browser flow without importing the real submit path.
    from setup_flow import submit_code  # noqa: WPS433 (deliberate late import)

    async def _poll(after_epoch: int, timeout_s: int) -> str:
        return await _poll_mailbox(email, after_epoch, timeout_s)

    result = await _run_browser_flow(oauth_url, email, _poll, on_stage)
    on_stage("exchanging code")
    state = await submit_code(result.paste, flow_key=flow_key)
    if state.status == "done":
        on_stage("done")
        return
    raise AutoSigninError("Claude could not exchange the sign-in code. Start sign-in again.")
