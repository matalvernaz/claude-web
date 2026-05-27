"""Concurrent-writes regression test for the storage layer.

The original `_next_idx` / `_append_message` pair raced under simultaneous
calls from multiple FastMCP worker threads: two threads could SELECT the
same MAX(idx) and then both INSERT with that idx, hitting a PRIMARY KEY
violation on the second writer. Artifact version bumps had the same
shape (read MAX(version), INSERT MAX+1).

This test pounds both write paths from a worker pool and asserts:
  - no IntegrityError surfaces
  - all messages get unique, contiguous idx values
  - all artifact versions form an unbroken 1..N sequence
"""
import concurrent.futures
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the vendored ./roundtable/ package importable when this script is
# invoked directly (e.g. python scripts/foo.py) from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the server at a fresh on-disk state dir so we never touch the
# real DB.
_test_dir = tempfile.mkdtemp(prefix="roundtable-concurrency-")
os.environ["CLAUDE_ROUNDTABLE_STATE_DIR"] = _test_dir
print(f"[concurrency] state dir: {_test_dir}")

from roundtable import core as server  # noqa: E402

# Direct DB writes only — no provider calls. We exercise the lock-protected
# paths: roundtable_post (via _append_message) and roundtable_set_artifact.

NUM_THREADS = 16
POSTS_PER_THREAD = 25
ARTIFACT_BUMPS_PER_THREAD = 10

created = server.roundtable_create(
    topic="concurrency stress test", participants=[],
)
tid = created["thread_id"]
print(f"[concurrency] created thread {tid}")


def _spam_posts(worker_id: int) -> int:
    """Fire N posts from one worker. Returns count of successful posts."""
    count = 0
    for i in range(POSTS_PER_THREAD):
        server.roundtable_post(
            tid, f"worker={worker_id} i={i}", speaker=f"worker-{worker_id}",
        )
        count += 1
    return count


def _spam_artifacts(worker_id: int) -> int:
    """Fire N artifact bumps from one worker against the same name. If the
    version-bump path is unsafe, two workers will collide on the PK."""
    count = 0
    for i in range(ARTIFACT_BUMPS_PER_THREAD):
        server.roundtable_set_artifact(
            tid, name="shared", content=f"worker={worker_id} i={i}",
        )
        count += 1
    return count


print(f"[concurrency] firing {NUM_THREADS} workers × {POSTS_PER_THREAD} posts each …")
post_errors: list[BaseException] = []
with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
    futures = [pool.submit(_spam_posts, i) for i in range(NUM_THREADS)]
    for fut in concurrent.futures.as_completed(futures):
        try:
            fut.result()
        except BaseException as exc:
            post_errors.append(exc)

if post_errors:
    print(f"[concurrency] FAIL: {len(post_errors)} post workers raised:")
    for e in post_errors[:5]:
        print(f"  {type(e).__name__}: {e}")
    raise SystemExit(1)

# Read back: idx values must be 0..(NUM_THREADS*POSTS_PER_THREAD - 1)
db = sqlite3.connect(os.path.join(_test_dir, "state.db"))
rows = db.execute(
    "SELECT idx FROM messages WHERE thread_id = ? ORDER BY idx", (tid,),
).fetchall()
idxs = [r[0] for r in rows]
expected = NUM_THREADS * POSTS_PER_THREAD
assert len(idxs) == expected, f"expected {expected} messages, got {len(idxs)}"
assert idxs == list(range(expected)), (
    f"idx sequence has holes or duplicates: first 10 = {idxs[:10]}, "
    f"last 10 = {idxs[-10:]}"
)
print(f"[concurrency] PASS: {expected} posts, idx values contiguous 0..{expected-1}")

print(f"\n[concurrency] firing {NUM_THREADS} workers × "
      f"{ARTIFACT_BUMPS_PER_THREAD} artifact bumps each …")
art_errors: list[BaseException] = []
with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
    futures = [pool.submit(_spam_artifacts, i) for i in range(NUM_THREADS)]
    for fut in concurrent.futures.as_completed(futures):
        try:
            fut.result()
        except BaseException as exc:
            art_errors.append(exc)

if art_errors:
    print(f"[concurrency] FAIL: {len(art_errors)} artifact workers raised:")
    for e in art_errors[:5]:
        print(f"  {type(e).__name__}: {e}")
    raise SystemExit(1)

versions = [
    r[0] for r in db.execute(
        "SELECT version FROM artifacts WHERE thread_id = ? AND name = 'shared' "
        "ORDER BY version", (tid,),
    ).fetchall()
]
expected_v = NUM_THREADS * ARTIFACT_BUMPS_PER_THREAD
assert len(versions) == expected_v, (
    f"expected {expected_v} artifact versions, got {len(versions)}"
)
assert versions == list(range(1, expected_v + 1)), (
    f"version sequence has holes or duplicates: first 10 = {versions[:10]}, "
    f"last 10 = {versions[-10:]}"
)
print(f"[concurrency] PASS: {expected_v} artifact bumps, "
      f"versions contiguous 1..{expected_v}")

# Each artifact bump also appends a synthetic message — verify total
# message count matches posts + artifact-bumps.
total_msgs = db.execute(
    "SELECT COUNT(*) FROM messages WHERE thread_id = ?", (tid,),
).fetchone()[0]
expected_total = expected + expected_v
assert total_msgs == expected_total, (
    f"expected {expected_total} total messages (posts + artifact synths), "
    f"got {total_msgs}"
)
print(f"[concurrency] PASS: {total_msgs} total messages "
      f"({expected} posts + {expected_v} artifact synths)")

# Verify message idx sequence is still contiguous after the artifact spam
idxs2 = [r[0] for r in db.execute(
    "SELECT idx FROM messages WHERE thread_id = ? ORDER BY idx", (tid,),
).fetchall()]
assert idxs2 == list(range(expected_total)), (
    f"final idx sequence has holes or duplicates: first 10 = {idxs2[:10]}, "
    f"last 10 = {idxs2[-10:]}"
)
print(f"[concurrency] PASS: final idx sequence contiguous 0..{expected_total-1}")

print("\n[concurrency] ALL PASS")

# ─── Fork sanity test ──────────────────────────────────────────────────

print("\n[fork] ── roundtable_fork unit test ──")

fork_tid_src = server.roundtable_create(
    topic="source thread for fork test", participants=[],
    house_rules="be terse",
)["thread_id"]
server.roundtable_post(fork_tid_src, "msg 0", speaker="orchestrator")
server.roundtable_post(fork_tid_src, "msg 1", speaker="orchestrator")
server.roundtable_set_artifact(fork_tid_src, "a.py", "v1 content")
server.roundtable_post(fork_tid_src, "msg 3 (post-artifact)", speaker="orchestrator")
server.roundtable_set_artifact(fork_tid_src, "a.py", "v2 content")
server.roundtable_post(fork_tid_src, "msg 5 (after v2)", speaker="orchestrator")

src_msgs = server._thread_messages(fork_tid_src)
print(f"[fork] source has {len(src_msgs)} messages")
assert len(src_msgs) == 6, f"setup wrong: {len(src_msgs)}"

# Fork at msg idx=3 (post-artifact v1, before v2)
forked = server.roundtable_fork(fork_tid_src, upto_idx=3, new_topic="forked at 3")
print(f"[fork] forked: {forked}")
assert forked["messages_copied"] == 4, f"expected 4 messages, got {forked['messages_copied']}"
assert forked["artifacts_copied"] == 1, (
    f"expected only v1 artifact copied, got {forked['artifacts_copied']}"
)

# Verify fork inherited house_rules
fork_row = server._thread_row(forked["thread_id"])
assert fork_row["house_rules"] == "be terse", f"house_rules not inherited: {fork_row}"
assert fork_row["topic"] == "forked at 3", fork_row

# Verify the v2 artifact did NOT come over
try:
    server.roundtable_get_artifact(forked["thread_id"], "a.py", version=2)
    raise AssertionError("v2 should not exist in fork")
except ValueError:
    pass  # expected

v1_in_fork = server.roundtable_get_artifact(forked["thread_id"], "a.py", version=1)
assert v1_in_fork["content"] == "v1 content", v1_in_fork

# Fork the whole thread (upto_idx=-1)
full_fork = server.roundtable_fork(fork_tid_src, new_topic="full clone")
assert full_fork["messages_copied"] == 6, full_fork
assert full_fork["artifacts_copied"] == 2, full_fork

# Default new_topic ("Fork of <topic>")
defaulted = server.roundtable_fork(fork_tid_src)
defaulted_row = server._thread_row(defaulted["thread_id"])
assert defaulted_row["topic"].startswith("Fork of "), defaulted_row

# upto_idx out of range
try:
    server.roundtable_fork(fork_tid_src, upto_idx=99)
    raise AssertionError("should have rejected out-of-range upto_idx")
except ValueError:
    pass

print("[fork] ALL PASS")

# ─── Polish: case-insensitive participant lookup ────────────────────

print("\n[polish] ── participant case normalization ──")
# These should resolve to the same participant info (using internal helper)
info_lc = server._resolve_participant("gemini-pro")
info_uc = server._resolve_participant("Gemini-Pro")
info_ws = server._resolve_participant("  GEMINI-PRO  ")
assert info_lc is info_uc is info_ws or (
    info_lc == info_uc == info_ws
), "case/whitespace normalization broken"
print("[polish] participant lookup case-insensitive & whitespace-tolerant")
print("[polish] ALL PASS")

