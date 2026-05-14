"""Register a personal Claude account for one OIDC user.

Runs *inside* the claude-web container — `scripts/add-personal` is the
host-side wrapper that `docker exec`s into here. The flow:

  1. Build (or refresh) the user's personal CLAUDE_CONFIG_DIR, which is
     mostly symlinks back to CLAUDE_HOME with .credentials.json reserved
     for the real per-user credential file.
  2. Run `claude /login` against that directory so the OAuth token lands
     in the personal home and not in the shared one.
  3. On success, flip ``has_personal=1`` in the user_account table so the
     web UI lets the user switch to their personal slot.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, "/app")

from app import (  # noqa: E402  (path setup above is intentional)
    _ensure_personal_home,
    _mark_personal_registered,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Register a personal Claude account for a logged-in user.",
    )
    ap.add_argument(
        "--sub",
        required=True,
        help=(
            "OIDC subject of the target user. The user can find theirs by "
            "logging in once and reading the response of GET /api/account "
            "(or their session cookie's user.sub)."
        ),
    )
    ap.add_argument(
        "--label",
        default=None,
        help="Optional display name for this account in the toggle (e.g. 'My Pro').",
    )
    args = ap.parse_args()

    home = _ensure_personal_home(args.sub)
    print(f"Personal home: {home}", file=sys.stderr)

    cred = home / ".credentials.json"
    # Any prior creds (real or accidental symlink) get cleared before login
    # so `claude /login` writes a fresh real file into this home, not into
    # CLAUDE_HOME via a symlink.
    if cred.is_symlink() or cred.exists():
        cred.unlink()

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)

    print(
        "Starting `claude /login` — follow the prompts to authenticate "
        "your Claude account.",
        file=sys.stderr,
    )
    rc = subprocess.call(["claude", "/login"], env=env)
    if rc != 0:
        print(f"claude /login exited with status {rc}", file=sys.stderr)
        return rc

    if not cred.exists():
        print(
            f"Login finished but {cred} was not created. Not marking as "
            "registered.",
            file=sys.stderr,
        )
        return 1

    _mark_personal_registered(args.sub, args.label)
    print(
        f"OK: {args.sub} registered. The personal slot is now available "
        "in the web UI's account toggle.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
