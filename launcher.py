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


def _binary_dir() -> Path:
    """Directory containing the executable (or the launcher script).

    PyInstaller sets ``sys.frozen`` on the frozen binary; ``sys.executable``
    is then the .exe path itself. From source the same logic falls back to
    the launcher.py path, which keeps both paths exercised in tests.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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


def _print_first_run_banner(host: str, port: int, will_open_browser: bool) -> None:
    sample = _binary_dir() / ".env.example"
    setup_url = f"http://{host}:{port}/setup"
    open_line = (
        "Opening that URL in your default browser now…"
        if will_open_browser
        else "Open that URL manually (auto-open was disabled with --no-browser)."
    )
    msg = [
        "─" * 68,
        "claude-web — first-run bootstrap",
        "─" * 68,
        "No .env file or auth env vars were found, so we're starting in",
        "  AUTH_MODE=none  (localhost-only, no authentication)",
        "",
        f"Setup page: {setup_url}",
        open_line,
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


def _open_browser_when_ready(host: str, port: int, path: str,
                             timeout_s: float = 30.0) -> None:
    """Background thread: poll /healthz, then open the browser.

    The launcher hands control to ``uvicorn.run`` (a blocking call), so
    we can't await readiness on the main thread. A daemon thread polls
    ``/healthz`` until it returns 200 (or the timeout elapses) and then
    calls ``webbrowser.open`` once. Daemon so it dies with the process
    if the user Ctrl-Cs before readiness.

    Host coercion: ``0.0.0.0`` isn't a valid URL host on Windows; rewrite
    to ``127.0.0.1`` for the loopback probe and the browser URL. The
    server itself still binds the original host.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "::1") else host
    base = f"http://{probe_host}:{port}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    else:
        print(
            f"\nNote: /healthz did not respond within {timeout_s:.0f}s; "
            "skipping browser auto-open.",
            flush=True,
        )
        return
    try:
        webbrowser.open(base + path, new=2)
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
    # Browser auto-open: defaults to "yes on first-run, no otherwise" so
    # double-clicking the binary on a fresh machine lands the user on
    # /setup, while a configured deployment doesn't get its operator's
    # browser hijacked on every restart. --open forces, --no-browser
    # suppresses, CLAUDE_WEB_OPEN_BROWSER=true/false sets a default.
    open_group = p.add_mutually_exclusive_group()
    open_group.add_argument(
        "--open", dest="open_browser", action="store_true", default=None,
        help="Open /setup in the default browser once the server is ready.",
    )
    open_group.add_argument(
        "--no-browser", dest="open_browser", action="store_false",
        help="Suppress the first-run browser auto-open.",
    )
    return p.parse_args(argv)


def _resolve_open_browser(args: argparse.Namespace, first_run: bool) -> bool:
    """Decide whether to auto-open the browser this launch.

    Precedence: explicit CLI flag (--open / --no-browser) wins; then the
    CLAUDE_WEB_OPEN_BROWSER env var; otherwise default to True only on
    first-run so a configured server doesn't hijack the operator's
    browser on every restart.
    """
    if args.open_browser is not None:
        return args.open_browser
    env_val = os.environ.get("CLAUDE_WEB_OPEN_BROWSER", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    if env_val in ("0", "false", "no", "off"):
        return False
    return first_run


def _run(argv: list[str] | None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    # Load `.env` BEFORE importing app — app.py reads many env vars at
    # import time (CLAUDE_HOME, PROJECT_DIRS, auth config, ...) so a
    # post-import load would be ignored.
    loaded = _load_dotenv_files()

    # If literally nothing is configured, flip to AUTH_MODE=none so the
    # binary boots on first launch. The banner makes it loud — this is
    # not a silent default change for real deployments (any one auth var
    # disables the bootstrap).
    first_run = not loaded and _looks_unconfigured()
    open_browser = _resolve_open_browser(args, first_run)

    if first_run:
        os.environ["AUTH_MODE"] = "none"
        os.environ.setdefault("SESSION_COOKIE_INSECURE", "true")
        os.environ.setdefault("CLAUDE_WEB_CSRF_STRICT", "false")
        _print_first_run_banner(args.host, args.port, open_browser)
        _check_claude_cli()

    if open_browser:
        # Daemon thread; dies with the process. The thread polls
        # /healthz so the browser opens only after uvicorn is actually
        # serving requests, not just after the python import completes.
        threading.Thread(
            target=_open_browser_when_ready,
            args=(args.host, args.port, "/setup"),
            daemon=True,
        ).start()

    # Imports are deferred so --help is fast and importing uvicorn/app
    # at module load doesn't trip the PyInstaller analyzer twice.
    import uvicorn

    import app  # noqa: F401 — registers the FastAPI instance as `app.app`

    uvicorn.run(
        app.app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0


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
