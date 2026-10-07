"""Exercise a frozen portable install with fake accounts, never real secrets."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


def main() -> None:
    exe = Path(sys.argv[1]).resolve()
    root = exe.parent / "portable-data"
    root.mkdir()  # Refuse to run against an existing user's portable data.
    state = root / "state"
    state.mkdir()
    sub = base64.urlsafe_b64encode(hashlib.sha256(b"anonymous").digest()).rstrip(b"=").decode()
    with sqlite3.connect(state / "state.db") as conn:
        conn.execute("CREATE TABLE user_credential (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "user_sub TEXT NOT NULL, label TEXT NOT NULL, created_at REAL NOT NULL, "
                     "UNIQUE(user_sub, label))")
        conn.execute("CREATE TABLE user_account (user_sub TEXT PRIMARY KEY, "
                     "active TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO user_account VALUES ('anonymous', 'cred:12', ?)", (time.time(),))
        for cred_id, label in ((11, "Portable One"), (12, "Portable Two")):
            conn.execute("INSERT INTO user_credential VALUES (?, 'anonymous', ?, ?)",
                         (cred_id, label, time.time()))
            home = root / "claude-accounts" / sub / str(cred_id)
            home.mkdir(parents=True)
            (home / ".claude_oauth_token").write_text("sk-ant-oat01-" + "x" * 96, encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_", "CODEX_", "OIDC_", "SESSION_"))
           and k not in ("AUTH_MODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY")}
    env.update(CLAUDE_WEB_CLI_AUTOUPDATE="false", CLAUDE_WEB_CODEX_AUTOUPDATE="false",
               CLAUDE_WEB_CLI_MODELS_FETCH="false")
    base = "http://127.0.0.1:38472"
    log = exe.parent.parent / "portable-smoke.log"
    with tempfile.TemporaryDirectory() as cwd, log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([str(exe), "--headless", "--port", "38472"],
                                   cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            for _ in range(90):
                try:
                    with urlopen(base + "/healthz", timeout=1):
                        break
                except (URLError, OSError):
                    if process.poll() is not None:
                        raise RuntimeError("Portable executable exited before becoming ready") from None
                    time.sleep(1)
            with urlopen(base + "/api/account", timeout=10) as response:
                account = json.load(response)
            assert account["active"] == "cred:12", account
            assert [(c["label"], c["configured"]) for c in account["credentials"]] == [
                ("Portable One", True), ("Portable Two", True),
            ], account
            with urlopen(base + "/", timeout=10) as response:
                assert response.status == 200
            print("Portable binary boots from another directory with both accounts signed in.")
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    print(log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
