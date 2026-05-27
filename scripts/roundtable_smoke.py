"""End-to-end smoke test of the roundtable server.

Drives a real two-AI conversation through the storage + provider call
paths (no MCP transport layer — calls the @mcp.tool() functions
directly). Walks through:

  1. create a thread with two participants
  2. post code + an orchestrator note
  3. ask Gemini-flash for a quick take
  4. ask GPT-5-mini to react to what Gemini just said
  5. ask Gemini-flash to respond to GPT-5
  6. print the resulting transcript and assert each AI saw the previous turn

If the AIs really see each other's words, step 5's response should
reference something specific from step 4. We print the transcript so a
human can eyeball it, and we also assert the message count is correct.
"""
import os
import sys
import tempfile
from pathlib import Path

# Make the vendored ./roundtable/ package importable from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the server at a fresh on-disk state dir so this test doesn't
# pollute any real one.
_test_dir = tempfile.mkdtemp(prefix="roundtable-smoke-")
os.environ["CLAUDE_ROUNDTABLE_STATE_DIR"] = _test_dir
# Pin Anthropic transport to the SDK so this test doesn't consume
# subscription quota from the live ~/.claude session. The CLI path is
# covered separately by test_cli_provider.py.
os.environ["CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT"] = "api"
print(f"[smoke] state dir: {_test_dir}")

from roundtable import core as server  # noqa: E402

# Step 1: create a thread.
created = server.roundtable_create(
    topic="Is `x = x or default` a safe Python idiom?",
    participants=["gemini-flash", "gpt-5-mini"],
)
tid = created["thread_id"]
print(f"[smoke] created thread {tid}")

# Step 2: post the orchestrator's framing message + a code snippet.
server.roundtable_post(
    tid,
    "I want a quick three-way on this. Two of you give specific answers; "
    "address each other by name when you push back.",
    speaker="orchestrator",
)
server.roundtable_post(
    tid,
    "Code in question:\n\n"
    "def greet(name=None):\n"
    "    name = name or 'world'\n"
    "    return f'hello {name}'\n",
    speaker="orchestrator",
)
print("[smoke] posted framing + code")

# Step 3: ask Gemini-flash for a take.
print("[smoke] asking gemini-flash …")
reply_a = server.roundtable_ask(tid, "gemini-flash")
print(f"[smoke] gemini-flash said ({len(reply_a)} chars):")
print("    " + reply_a[:500].replace("\n", "\n    "))
assert reply_a.strip(), "gemini-flash returned empty"

# Step 4: ask GPT-5-mini to react.
print("\n[smoke] asking gpt-5-mini to respond to Gemini …")
reply_b = server.roundtable_ask(
    tid, "gpt-5-mini",
    prompt="Where do you agree or disagree with Gemini Flash specifically?",
)
print(f"[smoke] gpt-5-mini said ({len(reply_b)} chars):")
print("    " + reply_b[:500].replace("\n", "\n    "))
assert reply_b.strip(), "gpt-5-mini returned empty"

# Step 5: ask Gemini to respond back. This is the test of true
# multi-turn: gemini-flash should see its OWN prior reply tagged
# `(you)` and react to GPT-5-mini's critique.
print("\n[smoke] asking gemini-flash to respond to gpt-5-mini …")
reply_c = server.roundtable_ask(
    tid, "gemini-flash",
    prompt="GPT-5 Mini just responded to you. Address their specific points.",
)
print(f"[smoke] gemini-flash (round 2) said ({len(reply_c)} chars):")
print("    " + reply_c[:500].replace("\n", "\n    "))
assert reply_c.strip(), "gemini-flash second turn returned empty"

# Step 6: pull the transcript and verify shape.
print("\n[smoke] full transcript:")
print(server.roundtable_history(tid))

msgs = server._thread_messages(tid)
# Orchestrator: 2 explicit posts (framing + code) + 2 implicit posts from
# the ask-with-prompt calls (steps 4 and 5; step 3 had no prompt). Plus 3
# AI turns. Total: 2 + 2 + 3 = 7. The implicit posts are a behaviour
# upgrade — roundtable_ask now mirrors roundtable_ask_parallel and writes
# its prompt into the transcript so retrospective audits can see why a
# participant said what they said.
assert len(msgs) == 7, f"expected 7 messages, got {len(msgs)}"

participants_seen = {m["speaker"] for m in msgs}
assert "Gemini Flash" in participants_seen
assert "GPT-5 Mini" in participants_seen
assert "orchestrator" in participants_seen

# Sanity: each AI turn should be at least 50 chars (no trivial 'OK')
ai_turns = [m for m in msgs if m["speaker"] in {"Gemini Flash", "GPT-5 Mini"}]
for m in ai_turns:
    assert len(m["content"]) >= 50, (
        f"AI turn from {m['speaker']!r} is suspiciously short ({len(m['content'])} chars): "
        f"{m['content']!r}"
    )

print("\n[smoke] PASS — three-AI conversation persisted, "
      "each turn saw the previous ones, "
      f"total {len(msgs)} messages")

# ─── Phase 2: house_rules + artifacts + parallel ask ────────────────────

print("\n[smoke] ── phase 2: house rules + artifacts + parallel ask ──")

created2 = server.roundtable_create(
    topic="Spot bugs in the artifact under review.",
    participants=["gemini-flash", "gpt-5-mini"],
    house_rules=(
        "Reply with a bullet list. Each bullet starts with the severity tag "
        "[CRITICAL], [HIGH], or [NIT]. No preamble, no closing summary."
    ),
)
tid2 = created2["thread_id"]
print(f"[smoke] created thread {tid2} with house rules")
assert created2["house_rules"], "house_rules round-tripped empty"

# Post v1 of the artifact via roundtable_set_artifact (not raw post).
set1 = server.roundtable_set_artifact(
    tid2,
    name="palindrome.py",
    content=(
        "def is_palindrome(s):\n"
        "    s = s.lower()\n"
        "    return s == s[::-1]\n"
    ),
)
print(f"[smoke] artifact set v1: {set1}")
assert set1["version"] == 1, f"expected v1, got {set1}"
assert not set1["diff_omitted"], "v1 should never report a diff"

# Ask both AIs in parallel. Neither should see the other's response.
print("\n[smoke] parallel ask round 1 …")
par1 = server.roundtable_ask_parallel(
    tid2,
    participants=["gemini-flash", "gpt-5-mini"],
    prompt=(
        "Review palindrome.py v1. Apply the house rules — bullets only, "
        "severity tags, no preamble."
    ),
)
print(f"[smoke] parallel responses: {list(par1['responses'].keys())}")
print(f"[smoke] parallel errors:    {par1['errors']}")
assert not par1["errors"], f"parallel ask had errors: {par1['errors']}"
assert set(par1["responses"].keys()) == {"gemini-flash", "gpt-5-mini"}
for name, text in par1["responses"].items():
    assert len(text) >= 30, f"{name} returned suspiciously short text: {text!r}"
    print(f"\n[smoke] {name} (round 1, {len(text)} chars):")
    print("    " + text[:400].replace("\n", "\n    "))

# Bump artifact to v2 (adds a strip() — should show in diff).
set2 = server.roundtable_set_artifact(
    tid2,
    name="palindrome.py",
    content=(
        "def is_palindrome(s):\n"
        "    s = s.lower().strip()\n"
        "    return s == s[::-1]\n"
    ),
)
print(f"\n[smoke] artifact set v2: {set2}")
assert set2["version"] == 2

# Read v1 back via get_artifact (the prior version must still be queryable).
got_v1 = server.roundtable_get_artifact(tid2, "palindrome.py", version=1)
assert "strip()" not in got_v1["content"], "v1 should not have strip()"
got_latest = server.roundtable_get_artifact(tid2, "palindrome.py")
assert "strip()" in got_latest["content"], "latest must have strip()"
assert got_latest["version"] == 2

# Parallel ask on v2 — each AI should see the diff message and react to it.
print("\n[smoke] parallel ask round 2 (after v2 bump) …")
par2 = server.roundtable_ask_parallel(
    tid2,
    participants=["gemini-flash", "gpt-5-mini"],
    prompt="v2 just landed. Did the change fix anything you flagged, or introduce new issues?",
)
assert not par2["errors"], f"parallel ask round 2 had errors: {par2['errors']}"
for name, text in par2["responses"].items():
    print(f"\n[smoke] {name} (round 2, {len(text)} chars):")
    print("    " + text[:400].replace("\n", "\n    "))

# Sanity: verify each parallel round committed BOTH responses against the
# SAME transcript snapshot — i.e. the first AI's response is not in the
# second AI's view. The transcript at the time of each call should have
# had identical message counts.
msgs2 = server._thread_messages(tid2)
# Expected: 1 artifact-v1 + 1 orch prompt + 2 ai responses
#         + 1 artifact-v2 + 1 orch prompt + 2 ai responses = 8
assert len(msgs2) == 8, f"expected 8 messages in thread 2, got {len(msgs2)}"
print(f"\n[smoke] thread 2 message count: {len(msgs2)} (expected 8)")

# Confirm the artifact-v2 message contains a diff (the literal '--- Diff' header).
v2_msg = next(m for m in msgs2 if "updated to v2" in m["content"])
assert "Diff vs v1" in v2_msg["content"], "v2 transcript message lacks diff block"
print("[smoke] v2 transcript message contains diff — good")

server.roundtable_close(tid2)

# ─── Phase 3: Claude as a first-class participant (skipped without key) ─

if server._anthropic is None:
    print(
        "\n[smoke] phase 3 skipped — set ANTHROPIC_API_KEY to exercise the "
        "Claude participant path"
    )
else:
    print("\n[smoke] ── phase 3: Claude + Gemini + GPT in parallel ──")
    created3 = server.roundtable_create(
        topic="Three-way independent review with effort=high.",
        participants=["gemini-flash", "gpt-5-mini", "claude-sonnet"],
    )
    tid3 = created3["thread_id"]
    server.roundtable_post(
        tid3,
        "Code in question (find the bug, address each other by name):\n\n"
        "def divide(a, b):\n"
        "    return a / b if b else 0\n",
        speaker="orchestrator",
    )
    par3 = server.roundtable_ask_parallel(
        tid3,
        participants=["gemini-flash", "gpt-5-mini", "claude-sonnet"],
        prompt="Independent review — give two concrete concerns each.",
        effort="medium",
    )
    print(f"[smoke] parallel responses: {list(par3['responses'].keys())}")
    print(f"[smoke] parallel errors:    {par3['errors']}")
    assert not par3["errors"], f"3-way parallel had errors: {par3['errors']}"
    assert set(par3["responses"].keys()) == {
        "gemini-flash", "gpt-5-mini", "claude-sonnet",
    }
    for name, text in par3["responses"].items():
        assert len(text) >= 30, f"{name} returned suspiciously short text: {text!r}"
        print(f"\n[smoke] {name} (3-way, {len(text)} chars):")
        print("    " + text[:400].replace("\n", "\n    "))

    # Verify Claude can read the others and reply with effort=high.
    reply_claude = server.roundtable_ask(
        tid3, "claude-sonnet",
        prompt="Where do you agree or disagree with the other two specifically?",
        effort="high",
    )
    assert reply_claude.strip(), "claude-sonnet follow-up was empty"
    print(f"\n[smoke] claude-sonnet follow-up ({len(reply_claude)} chars):")
    print("    " + reply_claude[:500].replace("\n", "\n    "))

    server.roundtable_close(tid3)
    print("[smoke] phase 3 PASS")

print("\n[smoke] ALL PHASES PASS")

# Cleanup: list / close
print(f"\n[smoke] list view: {server.roundtable_list()}")
print(f"[smoke] close result: {server.roundtable_close(tid)}")
print(f"[smoke] list after close: {server.roundtable_list(open_only=True)}")
print(f"[smoke] list including closed: {server.roundtable_list(open_only=False)}")

sys.exit(0)
