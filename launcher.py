"""Entry point used by the PyInstaller release build.

The production deploy on Linux just runs ``uvicorn app:app …`` directly,
which PyInstaller can't easily freeze because uvicorn discovers the app
via import-string lookup at runtime. Instead, the frozen binary imports
the app object up front and hands it to ``uvicorn.run()`` programmatically.
That keeps the freeze deterministic and lets us read host/port/env from
either CLI args or environment variables — same precedence the systemd
unit uses.

Environment variables honoured (all optional, with the same defaults as
the upstream ``uvicorn app:app`` invocation in README.md):

* ``CLAUDE_WEB_HOST`` — bind host, default ``127.0.0.1``
* ``CLAUDE_WEB_PORT`` — bind port, default ``3001``
* ``CLAUDE_WEB_FORWARDED_ALLOW_IPS`` — proxy-trust list, default ``*``

When run as a frozen binary (PyInstaller), the launcher also:

* loads ``.env`` from the directory of the executable (or the current
  working directory) before importing the app, so users don't have to
  set env vars in their shell to configure OIDC / Claude paths;
* bootstraps a localhost-only ``AUTH_MODE=none`` first-run if **no**
  config is present anywhere, so a freshly-extracted zip actually
  boots on double-click instead of crashing on missing OIDC creds;
* opens a **native desktop window** (pywebview → Edge WebView2 on
  Windows / WebKit on macOS / GTK WebKit on Linux) embedding the app
  by default. The window is the primary UI; the console window stays
  as the off-switch. ``--no-window`` falls back to opening the system
  browser, and ``--headless`` runs the server with no UI at all;
* traps any startup exception, writes the traceback to
  ``claude-web-startup.log`` next to the executable, prints it to the
  console, and pauses so the console window stays open. Without this
  trap, PyInstaller windows close on uncaught exception and users see
  nothing.

Run ``claude-web --help`` from the frozen binary for the CLI surface.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


# The three UI modes are mutually exclusive. The launcher resolves which
# one to use from CLI flags + env vars + a sane default that depends on
# whether we're a frozen binary (default to native window) or a from-source
# launch (default to headless, since the developer probably wants to drive
# the server with their own browser).
UI_WINDOW = "window"
UI_BROWSER = "browser"
UI_HEADLESS = "headless"


def _binary_dir() -> Path:
    """Directory containing the executable (or the launcher script).

    PyInstaller sets ``sys.frozen`` on the frozen binary; ``sys.executable``
    is then the .exe path itself. From source the same logic falls back to
    the launcher.py path, which keeps both paths exercised in tests.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _load_dotenv_files() -> list[Path]:
    """Load `.env` from the binary's directory and the current working
    directory before importing the app, populating ``os.environ`` with
    any KEY=VALUE pairs that aren't already set.

    The shell environment wins over the file (so an operator who
    exports values in their session can override the bundled file).
    Returns the paths that were actually read, for the first-run banner.
    """
    candidates = [_binary_dir() / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    loaded: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip a single layer of surrounding quotes — bash-style
            # `FOO="bar baz"` is the common idiom, no shell parsing.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if not key or key in os.environ:
                continue
            os.environ[key] = value
        loaded.append(resolved)
    return loaded


def _looks_unconfigured() -> bool:
    """True when no `.env` was loaded AND no auth-related env vars are
    present. Drives the first-run banner that flips ``AUTH_MODE=none``
    so a double-clicked binary actually boots into a usable state."""
    if os.environ.get("AUTH_MODE"):
        return False
    # Any OIDC variable means the user has at least started configuring
    # the auth path — respect it and let auth.configure fail loudly if
    # incomplete, rather than silently re-mode them to `none`.
    for var in ("SESSION_SECRET", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID",
                "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI"):
        if os.environ.get(var):
            return False
    return True


def _print_first_run_banner(host: str, port: int, ui_mode: str) -> None:
    sample = _binary_dir() / ".env.example"
    setup_url = f"http://{host}:{port}/setup"
    if ui_mode == UI_WINDOW:
        ui_line = "Opening a native window onto that page now…"
    elif ui_mode == UI_BROWSER:
        ui_line = "Opening that URL in your default browser now…"
    else:
        ui_line = "Open that URL manually (UI auto-launch was disabled)."
    msg = [
        "─" * 68,
        "claude-web — first-run bootstrap",
        "─" * 68,
        "No .env file or auth env vars were found, so we're starting in",
        "  AUTH_MODE=none  (localhost-only, no authentication)",
        "",
        f"Setup page: {setup_url}",
        ui_line,
        "",
        "To enable proper auth (OIDC) and persistent config:",
        f"  1. Copy {sample}",
        f"     to    {sample.with_suffix('')}    (drop the .example)",
        "  2. Edit it (set SESSION_SECRET + OIDC_* values).",
        "  3. Restart this binary.",
        "─" * 68,
    ]
    print("\n".join(msg), flush=True)


def _check_claude_cli() -> None:
    """Warn loudly if the `claude` CLI isn't on PATH.

    The Anthropic Agent SDK shells out to the bundled Node CLI for every
    model interaction; without it the user can sign into Claude on the
    /setup page but every subsequent chat turn will fail with an opaque
    "claude not found" error from the SDK. Better to surface this once,
    visibly, at startup. Doesn't block — the user may install it
    afterwards or use the binary purely as a viewer.
    """
    if shutil.which("claude") is not None or shutil.which("claude.cmd") is not None:
        return
    print(
        "\nWarning: the `claude` CLI was not found on PATH.\n"
        "  claude-web relies on @anthropic-ai/claude-code (the Node CLI)\n"
        "  for every model interaction. Install Node.js, then:\n"
        "    npm install -g @anthropic-ai/claude-code\n"
        "  Chat turns will fail until that's done.\n",
        flush=True,
    )


def _probe_host(host: str) -> str:
    """Coerce a wildcard bind address to a loopback host for URL building."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "::1") else host


def _wait_for_healthz(host: str, port: int, timeout_s: float = 30.0) -> bool:
    """Block until /healthz answers 200, or timeout. Returns True on ready."""
    base = f"http://{_probe_host(host)}:{port}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


def _open_browser_when_ready(host: str, port: int, path: str,
                             timeout_s: float = 30.0) -> None:
    """Background thread: poll /healthz, then open the system browser.

    The launcher hands control to ``uvicorn.run`` (a blocking call), so
    we can't await readiness on the main thread. A daemon thread polls
    ``/healthz`` until it returns 200 (or the timeout elapses) and then
    calls ``webbrowser.open`` once. Daemon so it dies with the process
    if the user Ctrl-Cs before readiness.
    """
    if not _wait_for_healthz(host, port, timeout_s):
        print(
            f"\nNote: /healthz did not respond within {timeout_s:.0f}s; "
            "skipping browser auto-open.",
            flush=True,
        )
        return
    try:
        webbrowser.open(f"http://{_probe_host(host)}:{port}{path}", new=2)
    except webbrowser.Error as e:
        print(f"\nNote: could not open the browser automatically ({e}).", flush=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="claude-web",
        description="claude-web frozen launcher (uvicorn + app:app).",
    )
    p.add_argument(
        "--host",
        default=os.getenv("CLAUDE_WEB_HOST", "127.0.0.1"),
        help="Bind host (default: 127.0.0.1).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("CLAUDE_WEB_PORT", "3001")),
        help="Bind port (default: 3001).",
    )
    p.add_argument(
        "--forwarded-allow-ips",
        default=os.getenv("CLAUDE_WEB_FORWARDED_ALLOW_IPS", "*"),
        help=(
            "Trusted upstream IPs for X-Forwarded-* headers. Default '*' "
            "matches the systemd unit behaviour; tighten if exposing "
            "directly."
        ),
    )
    # UI mode is a single-choice across the three behaviours: native
    # window (pywebview-driven), system browser, or no UI at all. The
    # flags are mutually exclusive and override CLAUDE_WEB_UI_MODE /
    # the frozen-vs-source default chosen in _resolve_ui_mode.
    ui_group = p.add_mutually_exclusive_group()
    ui_group.add_argument(
        "--window", dest="ui_mode", action="store_const", const=UI_WINDOW,
        help="Embed the app in a native desktop window (pywebview). Default "
             "for the frozen binary.",
    )
    ui_group.add_argument(
        "--open", dest="ui_mode", action="store_const", const=UI_BROWSER,
        help="Open /setup in the user's default system browser instead of "
             "opening a native window.",
    )
    ui_group.add_argument(
        "--no-window", dest="ui_mode", action="store_const", const=UI_BROWSER,
        help="Alias for --open. Suppresses the native window; falls back to "
             "the system browser.",
    )
    ui_group.add_argument(
        "--no-browser", dest="ui_mode", action="store_const", const=UI_HEADLESS,
        help="Run headless: serve the app but do not open any UI.",
    )
    ui_group.add_argument(
        "--headless", dest="ui_mode", action="store_const", const=UI_HEADLESS,
        help="Alias for --no-browser.",
    )
    p.set_defaults(ui_mode=None)
    return p.parse_args(argv)


def _try_import_webview():
    """Return the ``webview`` module on success, ``None`` on any failure.

    pywebview can fail to import on Linux when no GTK/QT backend is
    installed, and at first call when the platform webview runtime is
    missing (e.g. WebView2 evergreen on Windows 10 pre-1809). Both cases
    must degrade gracefully to the system browser. Catching BaseException
    is deliberate — some backends raise SystemExit on missing runtime.
    """
    try:
        import webview  # type: ignore[import-not-found]
        return webview
    except BaseException:
        return None


def _resolve_ui_mode(args: argparse.Namespace, first_run: bool) -> str:
    """Pick the UI mode for this launch.

    Precedence (highest → lowest):
      1. Explicit CLI flag (--window / --open / --headless).
      2. ``CLAUDE_WEB_UI_MODE`` env var.
      3. Frozen-binary default: window mode (falls back to browser if
         pywebview can't import) when this is a first-run AUTH_MODE=none
         bootstrap, otherwise headless (a configured deployment is
         likely server-style — don't hijack the operator's desktop).
      4. From-source default: headless. A developer running
         ``python launcher.py`` typically wants the server only.
    """
    if args.ui_mode is not None:
        return _coerce_mode(args.ui_mode)
    env_val = os.environ.get("CLAUDE_WEB_UI_MODE", "").strip().lower()
    if env_val:
        return _coerce_mode(env_val)
    if _is_frozen() and first_run:
        # The desktop-app target: double-clicked exe on a fresh machine.
        return UI_WINDOW if _try_import_webview() is not None else UI_BROWSER
    return UI_HEADLESS


_UI_MODE_ALIASES = {
    "window": UI_WINDOW, "webview": UI_WINDOW, "desktop": UI_WINDOW,
    "browser": UI_BROWSER, "open": UI_BROWSER,
    "headless": UI_HEADLESS, "none": UI_HEADLESS, "no": UI_HEADLESS,
    # Permissive booleans for CLAUDE_WEB_OPEN_BROWSER back-compat below.
    "true": UI_BROWSER, "1": UI_BROWSER, "yes": UI_BROWSER, "on": UI_BROWSER,
    "false": UI_HEADLESS, "0": UI_HEADLESS, "off": UI_HEADLESS,
}


def _coerce_mode(value: str) -> str:
    return _UI_MODE_ALIASES.get((value or "").strip().lower(), UI_HEADLESS)


def _start_uvicorn_in_thread(host: str, port: int,
                             forwarded_allow_ips: str):
    """Spawn uvicorn on a background thread and return (server, thread).

    Using ``uvicorn.Server`` rather than ``uvicorn.run`` so we can flip
    ``server.should_exit = True`` from the main thread when the desktop
    window closes — the graceful-shutdown signal documented by uvicorn.
    Not a daemon thread so an in-flight request to /api/chat gets a
    chance to drain on window-close rather than being abruptly killed.
    """
    import uvicorn

    import app  # noqa: F401 — needed for app.app

    config = uvicorn.Config(
        app.app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        # Suppress uvicorn's own signal handlers — they only work from
        # the main thread, which webview owns when we're in window mode.
        # The launcher drives shutdown via ``server.should_exit``.
        log_config=None,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=False)
    thread.start()
    return server, thread


def _run_window_mode(host: str, port: int, forwarded_allow_ips: str,
                     setup_path: str) -> int:
    """Background-thread uvicorn + main-thread native window.

    pywebview's ``webview.start()`` MUST run on the main thread on macOS
    (Cocoa requirement) and is recommended-main on Windows/Linux. So we
    invert the normal launcher shape: uvicorn moves to a background
    thread, and the window owns the main thread until the user closes it.

    On clean window close we flip ``server.should_exit`` to ask uvicorn
    to drain and stop; if it doesn't exit within a short grace period
    we abandon and rely on process exit to reclaim the port.
    """
    webview = _try_import_webview()
    if webview is None:
        # Resolver should have caught this and routed to browser mode;
        # belt and braces in case an operator forces --window without
        # the backend installed.
        print(
            "Native window backend is unavailable (pywebview did not import). "
            "Falling back to system browser.",
            flush=True,
        )
        return _run_browser_mode(host, port, forwarded_allow_ips, setup_path)

    server, thread = _start_uvicorn_in_thread(host, port, forwarded_allow_ips)

    # The window must point at the same URL the readiness probe just
    # confirmed; otherwise the webview shows a connection-refused page
    # while uvicorn is still warming up.
    if not _wait_for_healthz(host, port):
        print(
            "\nServer did not become ready within 30s; opening the window "
            "anyway. If it shows an error, the startup log next to the "
            "binary has the details.",
            flush=True,
        )

    url = f"http://{_probe_host(host)}:{port}{setup_path}"
    try:
        webview.create_window(
            "claude-web",
            url,
            width=1100,
            height=780,
            min_size=(640, 480),
            # Confirmation prompt is annoying for a daily-driver desktop
            # app; rely on uvicorn's graceful shutdown to surface
            # anything that actually warrants holding the user back.
            confirm_close=False,
        )
        webview.start()
    except BaseException as e:
        # If the platform backend errors at start() (e.g. WebView2 not
        # installed on Windows), bail out to browser mode cleanly so
        # the user isn't stranded.
        print(
            f"\nNative window failed to launch ({type(e).__name__}: {e}). "
            "Falling back to system browser.",
            flush=True,
        )
        try:
            webbrowser.open(url, new=2)
        except webbrowser.Error:
            pass
        # Drop into the server's lifetime so the user can still use the
        # app from the browser; Ctrl-C in the console window will stop it.
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    # Window closed → drain and stop uvicorn.
    server.should_exit = True
    thread.join(timeout=10)
    return 0


def _run_browser_mode(host: str, port: int, forwarded_allow_ips: str,
                      setup_path: str) -> int:
    """Open the system browser when ready, run uvicorn on the main thread."""
    threading.Thread(
        target=_open_browser_when_ready,
        args=(host, port, setup_path),
        daemon=True,
    ).start()
    return _run_headless_mode(host, port, forwarded_allow_ips)


def _run_headless_mode(host: str, port: int,
                       forwarded_allow_ips: str) -> int:
    """Plain uvicorn.run on the main thread, no UI auto-launch."""
    import uvicorn

    import app  # noqa: F401

    uvicorn.run(
        app.app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )
    return 0


def _run(argv: list[str] | None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    # Load `.env` BEFORE importing app — app.py reads many env vars at
    # import time (CLAUDE_HOME, PROJECT_DIRS, auth config, ...) so a
    # post-import load would be ignored.
    loaded = _load_dotenv_files()

    first_run = not loaded and _looks_unconfigured()
    ui_mode = _resolve_ui_mode(args, first_run)

    if first_run:
        os.environ["AUTH_MODE"] = "none"
        os.environ.setdefault("SESSION_COOKIE_INSECURE", "true")
        os.environ.setdefault("CLAUDE_WEB_CSRF_STRICT", "false")
        _print_first_run_banner(args.host, args.port, ui_mode)
        _check_claude_cli()

    setup_path = "/setup" if first_run else "/"

    if ui_mode == UI_WINDOW:
        return _run_window_mode(
            args.host, args.port, args.forwarded_allow_ips, setup_path,
        )
    if ui_mode == UI_BROWSER:
        return _run_browser_mode(
            args.host, args.port, args.forwarded_allow_ips, setup_path,
        )
    return _run_headless_mode(
        args.host, args.port, args.forwarded_allow_ips,
    )


def main(argv: list[str] | None = None) -> int:
    """Wrap `_run` so a startup crash is *visible* instead of vanishing.

    On Windows the bundled exe opens its own console window when
    double-clicked; that console dies with the process, so an uncaught
    exception during import leaves the user with a one-second flash and
    no information. Trap, log to a file next to the exe, print to the
    still-attached console, and pause on input() so the user can read
    the traceback before the window closes.
    """
    try:
        return _run(argv)
    except SystemExit:
        # argparse exits cleanly on --help / bad args; let it through
        # without the post-mortem prompt or the user gets a confusing
        # "Press Enter" after --help.
        raise
    except BaseException:  # noqa: BLE001 — diagnostic catch-all
        tb = traceback.format_exc()
        log_path = _binary_dir() / "claude-web-startup.log"
        try:
            log_path.write_text(tb, encoding="utf-8")
        except OSError:
            pass
        # Newline before so the trace stands clear of any prior output.
        sys.stderr.write("\n" + tb)
        sys.stderr.write(
            f"\nclaude-web failed during startup. Full traceback above and\n"
            f"in: {log_path}\n"
        )
        # input() only meaningfully holds the console open when a TTY is
        # attached. On a launched-from-explorer .exe that's true; from a
        # detached daemon / service it's a no-op. The try wraps EOFError
        # for non-interactive runs (CI smoke tests, docker logs).
        try:
            input("\nPress Enter to close this window… ")
        except (EOFError, OSError):
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
