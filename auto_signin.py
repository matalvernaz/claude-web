"""Fully-automated Claude sign-in for credential slots that have an
``auto_email`` configured and a ``CLAUDE_WEB_MAILBOX_POLL_CMD`` on the
host.

The generic flow this module drives:

  1. ``setup_flow.start_oauth`` spawns ``claude auth login``; the
     subprocess prints an ``https://claude.com/cai/oauth/authorize?...``
     URL and blocks on stdin for the paste-back code.
  2. This module launches headless chromium, navigates to that URL,
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
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("claude-web.auto_signin")


ENV_MAILBOX_CMD = "CLAUDE_WEB_MAILBOX_POLL_CMD"

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
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 15)
    except asyncio.TimeoutError:
        proc.kill()
        raise AutoSigninError("mailbox poll wrapper hung past its own timeout") from None
    if proc.returncode == 0:
        url = (out or b"").decode("utf-8", errors="replace").strip()
        if url.startswith("https://claude.ai/magic-link#"):
            return url
        raise AutoSigninError(
            "mailbox poll wrapper exited 0 but printed an unexpected line"
        )
    if proc.returncode == 1:
        raise AutoSigninError("no magic-link email arrived within the timeout")
    tail = (err or b"").decode("utf-8", errors="replace").strip()[:200]
    raise AutoSigninError(
        f"mailbox poll wrapper exited {proc.returncode}: {tail or '<no stderr>'}"
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


async def _run_browser_flow(
    oauth_url: str,
    email: str,
    poll_mailbox: Callable[[int, int], Awaitable[str]],
    on_stage: Callable[[str], None],
) -> _CodeResult:
    # Import lazily so a claude-web install without playwright still boots.
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
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
        finally:
            await browser.close()


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
    raise AutoSigninError(
        f"CLI rejected the paste-back code (state={state.status!r}): "
        f"{(state.error or '')[:200]}"
    )
