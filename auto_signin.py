"""Sign a credential slot in through a server-side browser, with the person
supplying the one thing it cannot get itself. Needs ``auto_email`` on the
slot and ``CLAUDE_WEB_MAILBOX_POLL_CMD`` on the host.

The flow:

  1. ``setup_flow.start_oauth`` spawns ``claude auth login``; the
     subprocess prints an ``https://claude.com/cai/oauth/authorize?...``
     URL and blocks on stdin for the paste-back code.
  2. This module launches Chromium, navigates to that URL, enters the
     configured email, and asks claude.ai to send its magic-link email.
     The browser stays open on the login page.
  3. It shells out to ``CLAUDE_WEB_MAILBOX_POLL_CMD`` (argv: email,
     after-epoch, timeout-seconds), which blocks until a fresh
     ``https://claude.ai/magic-link#...`` arrives in the target
     mailbox and prints it on stdout.
  4. The link is shown to the person on their accounts page. Opened in
     any browser other than the one that asked for it, claude.ai shows a
     short verification code instead of signing in. The person types
     that code into claude-web.
  5. The server browser enters the code on its login page, lands on the
     OAuth consent page, presses Authorize, and reads ``code#state`` off
     the ``platform.claude.com/oauth/code/callback`` URL for
     ``setup_flow.submit_code``.

The browser never opens the magic link itself. On 2026-10-05 that page
answered the automated browser with an Arkose puzzle and, two minutes
later, "Couldn't verify your browser", while the login page's own check
passed. Nothing here tries to beat that check.

With ``CLAUDE_WEB_SIGNIN_DEBUG_DIR`` set, each stage writes the page's
text (URLs stripped of query and fragment) and a screenshot under it.
The pages after the person's code can only be reached with the person,
so that is how they get fixed when claude.ai changes them.
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
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("claude-web.auto_signin")


ENV_MAILBOX_CMD = "CLAUDE_WEB_MAILBOX_POLL_CMD"
ENV_BROWSER = "CLAUDE_WEB_SIGNIN_BROWSER"
ENV_DEBUG_DIR = "CLAUDE_WEB_SIGNIN_DEBUG_DIR"

# Bounds. Not env-configurable — pushing them higher rarely helps and
# usually just papers over a genuine breakage. The CLI gives the whole
# dance 600 s from its sign-in URL (setup_flow.CODE_TIMEOUT_SECONDS).
EMAIL_SEND_TIMEOUT_S = 60          # entering email + submitting the form
MAILBOX_POLL_TIMEOUT_S = 120       # from send-magic-link to inbox arrival
VERIFICATION_WAIT_S = 420          # the person opens the link and types the code
VERIFICATION_ACCEPT_S = 20         # login page leaves /login after a good code
VERIFICATION_TRIES = 3             # typos are read off a screen
AUTHORIZE_TIMEOUT_S = 90           # consent page → callback URL
CAPTCHA_GRACE_S = 8                # a silent check may still clear; a puzzle won't

_EMAIL_INPUT = 'input[type="email"], input[name="email"], input[id="email"]'
_CODE_INPUT = (
    'input:visible:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])'
    ':not([type="submit"]):not([type="button"]):not([type="email"])'
)
_ENTER_CODE_RE = re.compile(
    r"enter (?:the |your |a )?(?:verification |sign.in |login |one.time )?code", re.IGNORECASE,
)
_SUBMIT_CODE_RE = re.compile(
    r"^\s*(?:continue|verify(?: [a-z]+)*|submit|sign in|log in|confirm)\s*$", re.IGNORECASE,
)
_CONSENT_BUTTON_RE = re.compile(r"^\s*(?:authorize|allow|approve|confirm|continue)\s*$", re.IGNORECASE)
_VERIFY_TEXT_RE = re.compile(
    r"verify your browser|verify you are (?:a )?human|verify you are not a bot", re.IGNORECASE,
)
_CAPTCHA_FRAME_RE = re.compile(
    r"arkoselabs|funcaptcha|hcaptcha|recaptcha|challenges\.cloudflare\.com", re.IGNORECASE,
)
# The puzzle itself lives in a frame with no telling URL (seen 2026-10-05
# after the verification code: "Find all sports and exercise equipment").
_PUZZLE_TEXT_RE = re.compile(
    r"find all |select all |click the .{0,40} that |verify you are|verify your browser",
    re.IGNORECASE,
)
_HANDOFF = (
    "claude.ai asked the server's browser to prove it is human. Open the "
    "Claude sign-in page below in your own browser instead, and paste the "
    "code it gives you here."
)


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


class _Debug:
    """Per-stage page text and screenshots, only under CLAUDE_WEB_SIGNIN_DEBUG_DIR."""

    def __init__(self) -> None:
        root = os.environ.get(ENV_DEBUG_DIR, "").strip()
        self.dir: Optional[Path] = None
        self.n = 0
        if root:
            base = Path(root)
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.dir = base / time.strftime("%Y%m%d-%H%M%S")
            self.dir.mkdir(mode=0o700, exist_ok=True)

    async def snap(self, page, tag: str) -> None:
        if self.dir is None:
            return
        self.n += 1
        stem = self.dir / f"{self.n:02d}-{re.sub(r'[^a-z0-9]+', '-', tag.lower()).strip('-')}"
        try:
            u = urlparse(page.url)
            frames = sorted({urlparse(f.url).netloc for f in page.frames if f.url})
            try:
                text = await page.locator("body").inner_text(timeout=2000)
            except Exception:  # noqa: BLE001 - mid-navigation
                text = ""
            stem.with_suffix(".txt").write_text(
                f"{u.scheme}://{u.netloc}{u.path}\nframes: {frames}\n\n{text[:4000]}\n",
                encoding="utf-8",
            )
            await page.screenshot(path=str(stem.with_suffix(".png")))
        except Exception as e:  # noqa: BLE001 - diagnostics never fail the flow
            log.debug("signin debug snapshot failed: %s", type(e).__name__)


def _callback_prefix(oauth_url: str) -> str:
    """Where the consent page sends the browser once the person authorizes."""
    q = parse_qs(urlparse(oauth_url).query or "")
    return (q.get("redirect_uri") or ["https://platform.claude.com/oauth/code/callback"])[0]


def _paste_from_callback_url(callback_url: str) -> Optional[str]:
    """``…/oauth/code/callback?code=A&state=V`` is the paste-back ``A#V``."""
    try:
        u = urlparse(callback_url)
    except ValueError:
        return None
    if "oauth/code/callback" not in (u.path or ""):
        return None
    q = parse_qs(u.query or "")
    code = (q.get("code") or [None])[0]
    state = (q.get("state") or [None])[0]
    return f"{code}#{state}" if code and state else None


async def _request_email(page, oauth_url: str, email: str, on_stage, debug: _Debug) -> int:
    """Drive the login page until it confirms a sign-in email was sent.

    Returns the epoch taken just before the send, so the mailbox poll can
    skip anything left over from an earlier attempt.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    on_stage("opening sign-in page")
    # Cloudflare's JS challenge can take a few seconds; wait_until
    # 'domcontentloaded' before we start hunting for the form.
    await page.goto(oauth_url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector(_EMAIL_INPUT, timeout=EMAIL_SEND_TIMEOUT_S * 1000)
    except PWTimeout:
        await debug.snap(page, "no email field")
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
    try:
        await _wait_for_email_sent(page)
    finally:
        await debug.snap(page, "after email")
    return send_epoch


async def _click_first_visible(locator) -> bool:
    try:
        if await locator.count() and await locator.first.is_visible():
            await locator.first.click(timeout=5000)
            return True
    except Exception:  # noqa: BLE001 - gone or covered mid-click; the caller looks again
        pass
    return False


async def _enter_verification_code(page, code: str, debug: _Debug) -> bool:
    """Type the person's code into the login page.

    True once the page has left /login (claude.ai accepted it); False if it
    is still there after VERIFICATION_ACCEPT_S, which is what a wrong code
    looks like. Typing through the keyboard works for one field and for a
    row of one-character boxes alike.
    """
    if not await _click_first_visible(page.get_by_role("button", name=_ENTER_CODE_RE)):
        await _click_first_visible(page.get_by_text(_ENTER_CODE_RE))
    field = page.locator(_CODE_INPUT).first
    try:
        await field.wait_for(state="visible", timeout=10000)
    except Exception:  # noqa: BLE001
        await debug.snap(page, "no code field")
        raise AutoSigninError(
            "The login page offered nowhere to type the verification code. "
            "Open the Claude sign-in page below in your own browser instead."
        ) from None
    await field.click()
    await page.keyboard.type(code, delay=40)
    await debug.snap(page, "code typed")
    await page.keyboard.press("Enter")
    await _click_first_visible(page.get_by_role("button", name=_SUBMIT_CODE_RE))
    deadline = time.monotonic() + VERIFICATION_ACCEPT_S
    while time.monotonic() < deadline:
        if "/login" not in (urlparse(page.url).path or ""):
            await debug.snap(page, "after code")
            return True
        if await _challenge_showing(page):
            # Not a wrong code: the right code gets a puzzle here too.
            await debug.snap(page, "bot check after code")
            raise AutoSigninError(_HANDOFF)
        await asyncio.sleep(0.5)
    await debug.snap(page, "code not accepted")
    return False


def _captcha_frames(page) -> bool:
    # The hCaptcha loader frame sits on the login page from the start, so a
    # known URL alone is not a challenge; the words in a frame are.
    return any(_CAPTCHA_FRAME_RE.search(f.url or "") for f in page.frames
               if "newassets.hcaptcha.com" not in (f.url or ""))


async def _challenge_showing(page) -> bool:
    if _captcha_frames(page):
        return True
    for frame in page.frames:
        try:
            text = await frame.locator("body").inner_text(timeout=1000)
        except Exception:  # noqa: BLE001 - detached or still loading
            continue
        if _PUZZLE_TEXT_RE.search(text) or (frame is page.main_frame and _VERIFY_TEXT_RE.search(text)):
            return True
    return False


async def _wait_for_authorization(page, callback_prefix: str, debug: _Debug) -> None:
    """From the signed-in login page to the OAuth callback URL.

    claude.ai returns to the authorize page, where a consent button sends
    the browser on to the callback. A bot check here is handed to the
    person, never attempted.
    """
    deadline = time.monotonic() + AUTHORIZE_TIMEOUT_S
    challenged_at: Optional[float] = None
    seen: Optional[str] = None
    while True:
        if page.url.startswith(callback_prefix):
            await debug.snap(page, "callback")
            return
        if page.url != seen:
            seen = page.url
            await debug.snap(page, "authorize")
        if await _challenge_showing(page):
            challenged_at = challenged_at or time.monotonic()
            if time.monotonic() - challenged_at >= CAPTCHA_GRACE_S:
                await debug.snap(page, "bot check")
                raise AutoSigninError(_HANDOFF)
        else:
            challenged_at = None
        if time.monotonic() >= deadline:
            await debug.snap(page, "authorize timeout")
            raise AutoSigninError(
                "The sign-in did not reach the authorization code in time. "
                "Open the Claude sign-in page below in your own browser instead."
            )
        await _click_first_visible(page.get_by_role("button", name=_CONSENT_BUTTON_RE))
        await asyncio.sleep(1)


async def _await_code(wait: Callable[[], Awaitable[str]]) -> str:
    try:
        code = await asyncio.wait_for(wait(), timeout=VERIFICATION_WAIT_S)
    except asyncio.TimeoutError:
        raise AutoSigninError(
            "No verification code was entered in time and the sign-in link has "
            "expired. Click Get sign-in link to start again."
        ) from None
    code = re.sub(r"\s+", "", code or "")
    if not code:
        raise AutoSigninError("The verification code was empty. Click Get sign-in link to start again.")
    return code


async def run_signin(
    oauth_url: str,
    email: str,
    on_stage: Callable[[str], None],
    on_magic_link: Callable[[str], None],
    wait_for_verification_code: Callable[[], Awaitable[str]],
    on_code_rejected: Callable[[str], None],
) -> str:
    """Sign the slot in and return the CLI's paste-back ``code#state``.

    ``on_magic_link`` receives the emailed link for the person to open;
    ``wait_for_verification_code`` resolves with the short code they read
    there; ``on_code_rejected`` reports a code claude.ai refused, after
    which the wait runs again. Raises AutoSigninError with a message safe
    to show the caller; does not swallow subprocess/mail failures.
    """
    # Import lazily so a claude-web install without playwright still boots.
    from playwright.async_api import async_playwright

    debug = _Debug()
    async with async_playwright() as pw:
        async with _browser_context(pw) as ctx:
            page = await ctx.new_page()
            send_epoch = await _request_email(page, oauth_url, email, on_stage, debug)
            on_stage("waiting for the sign-in email")
            on_magic_link(await _poll_mailbox(email, send_epoch, MAILBOX_POLL_TIMEOUT_S))
            for _ in range(VERIFICATION_TRIES):
                on_stage("waiting for your verification code")
                code = await _await_code(wait_for_verification_code)
                on_stage("signing in with your code")
                if await _enter_verification_code(page, code, debug):
                    break
                on_code_rejected("claude.ai did not accept that code. Check it and try again.")
            else:
                raise AutoSigninError(
                    "claude.ai did not accept the verification code three times. "
                    "Click Get sign-in link to start again."
                )
            on_stage("authorizing claude-web")
            await _wait_for_authorization(page, _callback_prefix(oauth_url), debug)
            paste = _paste_from_callback_url(page.url)
            if not paste:
                raise AutoSigninError(
                    "Reached claude.com's callback page but could not read the code "
                    "from it. Open the Claude sign-in page below in your own browser instead."
                )
            return paste
