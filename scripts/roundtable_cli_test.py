"""End-to-end test of the Anthropic CLI (subscription) transport.

The smoke test pins ``CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT=api`` so it
doesn't burn Pro/Team subscription quota. This test does the opposite:
pins ``=cli`` and verifies one Claude participant ask actually round-trips
through ``claude`` / ``claude-ha`` and returns a usable text response.

Run on a host where ``claude /login`` has been completed. Skips with a
clear message if no CLI binary is on PATH or if the OAuth session is
absent / expired.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the vendored ./roundtable/ package importable from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not (shutil.which("claude-ha") or shutil.which("claude")):
    print("[cli] SKIP — no claude/claude-ha on PATH; nothing to test.")
    sys.exit(0)

_test_dir = tempfile.mkdtemp(prefix="roundtable-cli-")
os.environ["CLAUDE_ROUNDTABLE_STATE_DIR"] = _test_dir
os.environ["CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT"] = "cli"
# Snappier failure if the CLI is wedged. Keep generous enough that a
# real cold start (model spin-up, OAuth refresh) still has room.
os.environ.setdefault("CLAUDE_ROUNDTABLE_PROVIDER_TIMEOUT_SEC", "180")
print(f"[cli] state dir: {_test_dir}")
print(f"[cli] transport pinned to CLI; binary: "
      f"{shutil.which('claude-ha') or shutil.which('claude')}")

from roundtable import core as server  # noqa: E402

# Sanity check the discovery happened.
assert server._CLAUDE_CLI, "_CLAUDE_CLI was not populated at import"
assert server._ANTHROPIC_TRANSPORT == "cli"
print(f"[cli] server._CLAUDE_CLI = {server._CLAUDE_CLI}")

# Confirm participant availability reflects the CLI transport — should
# be True even if ANTHROPIC_API_KEY is unset, since the CLI is present.
participants = server.roundtable_participants()
assert participants["claude-sonnet"]["available"], (
    f"claude-sonnet should be available via CLI transport; "
    f"got {participants['claude-sonnet']}"
)
print("[cli] claude-sonnet reported available (CLI transport)")

# Drive one ask through the subscription path.
tid = server.roundtable_create(
    topic="CLI transport sanity",
    participants=["claude-sonnet"],
)["thread_id"]
server.roundtable_post(
    tid,
    "Please reply with the literal phrase 'roundtable cli ok' on its own "
    "line, then add one sentence about why deterministic test phrases are "
    "useful.",
    speaker="orchestrator",
)
print("[cli] asking claude-sonnet via CLI …")
reply = server.roundtable_ask(tid, "claude-sonnet", effort="low")
print(f"[cli] reply ({len(reply)} chars):")
print("    " + reply[:600].replace("\n", "\n    "))

# Don't be too strict on phrase match — model can wrap punctuation, case
# may vary. Just confirm the test sentinel substring is present.
assert "roundtable cli ok" in reply.lower(), (
    f"expected sentinel phrase in response, got: {reply!r}"
)
print("[cli] sentinel phrase present — subscription transport works")

# Verify the transcript was written sanely.
msgs = server._thread_messages(tid)
assert len(msgs) == 2, f"expected 2 messages (orch + claude), got {len(msgs)}"
assert msgs[1]["speaker"] == "Claude Sonnet", msgs[1]
assert msgs[1]["content"].strip(), "Claude Sonnet turn is empty"
print(f"[cli] transcript intact: {len(msgs)} messages, "
      f"final speaker = {msgs[1]['speaker']!r}")

print("\n[cli] PASS")
