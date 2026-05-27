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

Run ``claude-web --help`` from the frozen binary for the CLI surface.
"""
from __future__ import annotations

import argparse
import os
import sys


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
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

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


if __name__ == "__main__":
    raise SystemExit(main())
