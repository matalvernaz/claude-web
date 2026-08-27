"""Drive a real browser at phone and desktop sizes to check the mobile shell.

The mobile header split can only fail in a browser: the moved-node sheets
depend on computed styles and on <dialog>.showModal(), neither of which a
template assertion can see. This script starts a throwaway app instance,
points headless Chromium at it over CDP, and checks the behaviour end to end.

Needs `chromium` on PATH and `websockets` in the venv (both present on the
homelab host; neither is a runtime dependency of the app). Run it as:

    .venv/bin/python scripts/mobile_shell_check.py

Everything is hermetic: AUTH_MODE=none, a temp state dir, a dummy API key, and
port 3099. It never touches ~/.claude-web/state.db, which a second app
instance would otherwise hydrate and mutate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

APP_PORT = 3099
CDP_PORT = 9222
BASE = f"http://127.0.0.1:{APP_PORT}"
REPO = Path(__file__).resolve().parent.parent

PHONE = {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True}
DESKTOP = {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False}

# iOS zooms a focused control under this size and never zooms back out.
MIN_CONTROL_FONT_PX = 16.0
# iOS Human Interface Guidelines minimum touch target.
MIN_TAP_PX = 44.0


class Browser:
    """Minimal CDP client: navigate, resize, evaluate."""

    def __init__(self, ws_url: str) -> None:
        self._ws = connect(ws_url, max_size=None)
        self._next_id = 0
        self.events: list[dict] = []
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Log.enable")

    def send(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            frame = json.loads(self._ws.recv())
            if frame.get("id") == msg_id:
                if "error" in frame:
                    raise RuntimeError(f"{method}: {frame['error']}")
                return frame.get("result", {})
            if "method" in frame:
                self.events.append(frame)

    def resize(self, metrics: dict) -> None:
        self.send("Emulation.setDeviceMetricsOverride", metrics)

    def navigate(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        # No load event to await reliably over a bare CDP socket; the app's JS
        # is deferred, so poll for the marker it sets on <html> instead.
        for _ in range(100):
            time.sleep(0.1)
            try:
                if self.evaluate("!!document.getElementById('header-menu-toggle')"):
                    return
            except RuntimeError:
                continue
        raise RuntimeError(f"page never became ready: {url}")

    def evaluate(self, expression: str):
        result = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text", "eval failed"))
        return result["result"].get("value")

    def script_errors(self) -> list[str]:
        """Uncaught exceptions and console errors seen since the last drain."""
        found = []
        for frame in self.events:
            if frame["method"] == "Runtime.exceptionThrown":
                details = frame["params"]["exceptionDetails"]
                exc = details.get("exception") or {}
                # The bare text is always "Uncaught (in promise)"; the useful
                # part is the rejection value plus its stack.
                found.append(" ".join(filter(None, [
                    details.get("text"),
                    exc.get("description") or exc.get("value")
                    or details.get("url"),
                ])))
            elif frame["method"] == "Log.entryAdded":
                entry = frame["params"]["entry"]
                if entry.get("level") == "error":
                    found.append(entry.get("text", ""))
        self.events.clear()
        return found

    def close(self) -> None:
        self._ws.close()


PROBE = """
(() => {
  const q = (s) => document.querySelector(s);
  const controls = q("#header-controls");
  const panel = q("#sessions-panel");
  const menuBtn = q("#header-menu-toggle");
  const sessBtn = q("#toggle-sessions");
  const css = (el, prop) => el ? getComputedStyle(el)[prop] : null;
  return {
    menuToggleDisplay: css(menuBtn, "display"),
    controlsDisplay: css(controls, "display"),
    controlsParent: controls ? controls.parentElement.id || controls.parentElement.className : null,
    panelParent: panel ? panel.parentElement.id || panel.parentElement.className : null,
    panelDisplay: css(panel, "display"),
    menuSheetOpen: q("#menu-sheet").open,
    sessionsSheetOpen: q("#sessions-sheet").open,
    sessHasPopup: sessBtn.getAttribute("aria-haspopup"),
    sessExpanded: sessBtn.getAttribute("aria-expanded"),
    newChatInBar: !!q(".header-bar #new-chat"),
    modelFontPx: parseFloat(css(q("#model-select"), "fontSize") || "0"),
    sessBtnHeight: sessBtn.getBoundingClientRect().height,
    sendBtnHeight: q("#send").getBoundingClientRect().height,
    bodyScrollHeight: document.body.scrollHeight,
    innerHeight: window.innerHeight,
    activeInSheet: !!(document.activeElement && document.activeElement.closest(".sheet")),
  };
})()
"""

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' :: {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mobile-shell-check-")
    env = dict(os.environ)
    env.update({
        "AUTH_MODE": "none",
        "ANTHROPIC_API_KEY": "sk-verify-only",
        "CLAUDE_WEB_STATE_DIR": f"{tmp}/state",
        "CLAUDE_HOME": f"{tmp}/claude-home",
        "CLAUDE_PROJECT_DIR": f"{tmp}/project",
        "CLAUDE_WEB_CLI_AUTOUPDATE": "false",
        "CLAUDE_WEB_CSRF_STRICT": "false",
        "CLAUDE_ROUNDTABLE_DB": f"{tmp}/roundtable.db",
    })
    for var in ("OIDC_REDIRECT_URI", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET", "SESSION_SECRET"):
        env.pop(var, None)
    Path(f"{tmp}/project").mkdir(parents=True, exist_ok=True)

    app_proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "app:app", "--host", "127.0.0.1",
         "--port", str(APP_PORT), "--log-level", "warning"],
        cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    chrome_proc = subprocess.Popen(
        ["chromium", "--headless=new", f"--remote-debugging-port={CDP_PORT}",
         "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         f"--user-data-dir={tmp}/chrome", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    browser = None
    try:
        _wait_for(f"{BASE}/healthz", "app")
        ws_url = _wait_for_cdp()
        browser = Browser(ws_url)

        print("phone (390x844):")
        browser.resize(PHONE)
        browser.navigate(BASE + "/")
        s = browser.evaluate(PROBE)
        check("menu toggle is visible", s["menuToggleDisplay"] != "none", s["menuToggleDisplay"])
        check("inline controls are display:none", s["controlsDisplay"] == "none", s["controlsDisplay"])
        check("new chat stayed on the bar", s["newChatInBar"])
        check("sessions button announces a dialog", s["sessHasPopup"] == "dialog")
        check("sessions button dropped aria-expanded", s["sessExpanded"] is None, str(s["sessExpanded"]))
        check("sessions button is a real tap target",
              s["sessBtnHeight"] >= MIN_TAP_PX, f"{s['sessBtnHeight']}px")
        check("send button is a real tap target",
              s["sendBtnHeight"] >= MIN_TAP_PX, f"{s['sendBtnHeight']}px")
        check("shell does not scroll the body",
              s["bodyScrollHeight"] <= s["innerHeight"] + 1,
              f"{s['bodyScrollHeight']} vs {s['innerHeight']}")

        print("phone, settings sheet open:")
        browser.evaluate("document.querySelector('#header-menu-toggle').click()")
        time.sleep(0.2)
        s = browser.evaluate(PROBE)
        check("sheet is modally open", s["menuSheetOpen"])
        check("controls moved into the sheet body", s["controlsParent"] == "menu-sheet-body",
              str(s["controlsParent"]))
        check("controls are visible in the sheet", s["controlsDisplay"] != "none", s["controlsDisplay"])
        check("focus landed inside the sheet", s["activeInSheet"])
        check("model select is 16px or larger",
              s["modelFontPx"] >= MIN_CONTROL_FONT_PX, f"{s['modelFontPx']}px")

        print("phone, usage dialog opened from inside the sheet:")
        browser.evaluate("document.querySelector('#show-usage').click()")
        time.sleep(1.0)
        nested = browser.evaluate("""({
          usageOpen: document.querySelector('#usage-dialog').open,
          menuStillOpen: document.querySelector('#menu-sheet').open,
          focusInUsage: !!(document.activeElement
            && document.activeElement.closest('#usage-dialog')),
        })""")
        check("usage dialog opens as a second modal", nested["usageOpen"])
        check("focus moved into the usage dialog", nested["focusInUsage"])
        check("settings sheet stayed open underneath", nested["menuStillOpen"])
        browser.evaluate("document.querySelector('#usage-dialog').close()")
        time.sleep(0.2)
        check("settings sheet still usable after the nested modal closed",
              browser.evaluate("document.querySelector('#menu-sheet').open"))

        print("phone, settings sheet closed:")
        browser.evaluate("document.querySelector('#menu-sheet').close()")
        time.sleep(0.2)
        s = browser.evaluate(PROBE)
        check("sheet closed", not s["menuSheetOpen"])
        check("controls went home", s["controlsParent"] == "header-actions",
              str(s["controlsParent"]))

        print("phone, chats sheet:")
        browser.evaluate("document.querySelector('#toggle-sessions').click()")
        time.sleep(0.2)
        s = browser.evaluate(PROBE)
        check("chats sheet is modally open", s["sessionsSheetOpen"])
        check("panel moved into the sheet body", s["panelParent"] == "sessions-sheet-body",
              str(s["panelParent"]))
        check("panel is visible in the sheet", s["panelDisplay"] != "none", s["panelDisplay"])
        browser.evaluate("document.querySelector('#sessions-sheet').close()")
        time.sleep(0.2)
        s = browser.evaluate(PROBE)
        check("panel went home", s["panelParent"] == "layout", str(s["panelParent"]))

        print("desktop (1280x900):")
        browser.resize(DESKTOP)
        browser.navigate(BASE + "/")
        s = browser.evaluate(PROBE)
        check("menu toggle is hidden", s["menuToggleDisplay"] == "none", s["menuToggleDisplay"])
        check("controls render inline", s["controlsDisplay"] == "flex", s["controlsDisplay"])
        check("controls are in the header", s["controlsParent"] == "header-actions",
              str(s["controlsParent"]))
        check("sessions button reports region state", s["sessExpanded"] in ("true", "false"),
              str(s["sessExpanded"]))
        check("sessions button has no popup role", s["sessHasPopup"] is None,
              str(s["sessHasPopup"]))
        check("sidebar panel is in the layout", s["panelParent"] == "layout", str(s["panelParent"]))

        print("script health:")
        errors = browser.script_errors()
        for err in errors:
            print(f"    error: {err[:300]}")
        check("no uncaught script errors", not errors, f"{len(errors)} error(s)")
    finally:
        if browser is not None:
            browser.close()
        chrome_proc.terminate()
        app_proc.terminate()
        try:
            app_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app_proc.kill()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


def _wait_for(url: str, what: str, tries: int = 100) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"{what} never came up at {url}")


def _wait_for_cdp(tries: int = 100) -> str:
    for _ in range(tries):
        try:
            targets = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=1).read()
            )
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError("chromium never exposed a page target")


if __name__ == "__main__":
    sys.exit(main())
