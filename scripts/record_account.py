"""Tell Claude Code which account a long-lived token belongs to.

``claude setup-token`` drops the account details its sign-in returns, and
the token can't read the account profile, so a CLI running on one doesn't
know which account it is. The server then treats it as an unknown account:
Claude Code offers only its five-alias model list, and Default runs Sonnet.

Run once per account home (the shared CLAUDE_HOME, or a credential home
under CLAUDE_WEB_PERSONAL_HOMES_DIR) after claude-web has given it its own
.claude.json. It starts the CLI once, with no prompt, with Claude Code's
host identity variables set; with all three set and no account in the
file, the CLI records them there itself, and every later run on that home
knows its account. Then ask claude-web for the lists again
(POST /api/admin/claude-models). A sign-out clears the record.

The ids come from that account's ``oauthAccount`` in a .claude.json written
by a regular ``claude auth login`` (``accountUuid``, ``organizationUuid``).

    python scripts/record_account.py <config dir> <account uuid> <organization uuid> <email>

The token is read from ``<config dir>/.claude_oauth_token`` unless
``--token-file`` names another. The CLI never replaces an account already
recorded in the file, so a different one there is refused unless
``--replace`` removes it first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path


async def _record(home: Path, token: str, account: str, org: str, email: str) -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    with tempfile.TemporaryDirectory() as cwd:
        options = ClaudeAgentOptions(
            cli_path=shutil.which("claude"), cwd=cwd, setting_sources=[],
            env={
                "CLAUDE_CONFIG_DIR": str(home), "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "CLAUDE_CODE_ACCOUNT_UUID": account,
                "CLAUDE_CODE_ORGANIZATION_UUID": org,
                "CLAUDE_CODE_USER_EMAIL": email,
            },
            extra_args={"no-session-persistence": None},
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.get_server_info()
    await asyncio.sleep(1)  # the CLI writes the file as it exits
    try:
        recorded = json.loads((home / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
    except (OSError, ValueError):
        recorded = {}
    if recorded.get("accountUuid") != account:
        print(f"{home}: the CLI did not record the account "
              f"(it has {recorded.get('accountUuid') or 'none'})", file=sys.stderr)
        return 1
    print(f"{home}: recorded {recorded.get('emailAddress') or account}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("home", type=Path, help="the account's CLAUDE_CONFIG_DIR")
    parser.add_argument("account_uuid")
    parser.add_argument("organization_uuid")
    parser.add_argument("email")
    parser.add_argument("--token-file", type=Path,
                        help="defaults to <home>/.claude_oauth_token")
    parser.add_argument("--replace", action="store_true",
                        help="drop a different account already recorded")
    args = parser.parse_args()
    config = args.home / ".claude.json"
    if config.is_symlink() or not config.is_file():
        print(f"{config} is not this account's own file yet; start claude-web on a "
              "build that keeps account state per home first", file=sys.stderr)
        return 1
    data = json.loads(config.read_text(encoding="utf-8"))
    current = (data.get("oauthAccount") or {}).get("accountUuid")
    if current and current != args.account_uuid:
        if not args.replace:
            print(f"{config} already names account {current}; --replace to drop it",
                  file=sys.stderr)
            return 1
        data.pop("oauthAccount")
        config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    token = (args.token_file or args.home / ".claude_oauth_token").read_text(encoding="utf-8").strip()
    return asyncio.run(_record(args.home, token, args.account_uuid,
                               args.organization_uuid, args.email))


if __name__ == "__main__":
    sys.exit(main())
