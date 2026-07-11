"""Roundtable core library — multi-AI threaded conversations.

The gemini-mcp and openai-mcp servers are stateless one-shot reviewers:
each call is a fresh prompt with no memory of prior calls and no
visibility into what the *other* reviewer said. That makes multi-AI
audits a middle-manager pattern — Claude summarises Gemini's review
into a prompt for OpenAI, vice versa. Reviewers can't address each
other's verbatim words; they're trusting Claude's translation.

This module gives them a shared chat thread instead. A caller creates a
thread, posts code or context, then calls ``roundtable_ask`` to route
the turn to a named participant — and that participant sees every
prior message (orchestrator notes, the other AI's responses, the user's
clarifications) verbatim. Threads persist in SQLite so a debate can
span sessions, restarts, or even days.

This file is the **library** — plain Python functions, no MCP / HTTP /
UI dependencies. ``roundtable.mcp_server`` wraps these as FastMCP tools
for the stdio MCP entrypoint; a future webapp can import the same
functions directly without going through MCP.

Storage layout (under ``$CLAUDE_ROUNDTABLE_STATE_DIR`` or
``~/.claude-roundtable/``):
    state.db                   SQLite, WAL mode
        threads(id, topic, participants_json, created_at, closed_at,
                house_rules)
        messages(thread_id, idx, speaker, content, ts)
        artifacts(thread_id, name, version, content, ts)

Participants are well-known short names mapping to (provider, model)
pairs. New ones can be added without code changes by extending
``PARTICIPANTS``. The orchestrator is just another speaker — its turns
are posted via ``roundtable_post``, not ``roundtable_ask``. Claude is
also available as a first-class participant (claude-sonnet, claude-opus)
so the AI driving the session can include itself in independent
parallel reviews — see ``roundtable_ask_parallel``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import difflib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

import anthropic
import jsonschema
from google import genai
from google.genai import types as genai_types
from openai import OpenAI


logger = logging.getLogger(__name__)

# ─── API clients & transports ────────────────────────────────────────────

_gemini_key = os.environ.get("GEMINI_API_KEY")
_openai_key = os.environ.get("OPENAI_API_KEY")
_anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

# Discover the Claude Code CLI so Anthropic-provider turns can be routed
# through the user's OAuth subscription (Pro / Team plan) instead of
# paying per token via ANTHROPIC_API_KEY. ``claude-ha`` is preferred — it
# wraps ``claude`` with multi-account failover, so usage-limit errors on
# one account roll over to the next without the orchestrator noticing.
# Falls back to plain ``claude`` if the failover wrapper isn't installed.
# If neither is on PATH this stays None and the SDK path is the only
# option for Anthropic participants.
_CLAUDE_CLI: Optional[str] = shutil.which("claude-ha") or shutil.which("claude")

# Transport selector for Anthropic participants:
#   "auto" (default): use CLI if discovered, else fall back to SDK.
#   "cli":            always subprocess to claude/claude-ha; refuse if absent.
#   "api":            always use the anthropic SDK; refuse without API key.
# Force "api" in environments where the CLI isn't logged-in (CI, smoke
# tests, headless deploys) so an OAuth-less host doesn't silently fail.
_ANTHROPIC_TRANSPORT = os.environ.get(
    "CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT", "auto",
).strip().lower()
if _ANTHROPIC_TRANSPORT not in {"auto", "cli", "api"}:
    raise RuntimeError(
        f"CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT={_ANTHROPIC_TRANSPORT!r}; "
        f"must be one of: auto, cli, api."
    )

# Set true the first time transport=auto falls back from the (subscription)
# CLI to the (per-token) API SDK, so the warning fires once per process
# rather than on every Anthropic turn. See _call_anthropic_router.
_warned_auto_api_fallback = False

# At least one viable Anthropic path OR another provider must be set, or
# there's nothing to route to. This is a runtime/startup concern, not an
# import-time one — importing this module stays side-effect-free so the webapp
# can import it to introspect PARTICIPANTS and run the deterministic repo-tool
# tests on a host with no provider credentials. Callers gate explicitly:
# providers_configured() (webapp degrades the route to 503) or ensure_routable()
# (the MCP stdio server refuses to start).
_anthropic_available = bool(_anthropic_key) or _CLAUDE_CLI is not None


def providers_configured() -> bool:
    """True if at least one provider is routable: a Gemini/OpenAI/Anthropic API
    key, or a ``claude``/``claude-ha`` binary on PATH for the subscription
    transport."""
    return bool(_gemini_key or _openai_key or _anthropic_available)


def ensure_routable() -> None:
    """Raise unless at least one provider is configured. For entrypoints that
    should refuse to start with nothing to route to."""
    if not providers_configured():
        raise RuntimeError(
            "Need at least one of: GEMINI_API_KEY, OPENAI_API_KEY, "
            "ANTHROPIC_API_KEY, or a 'claude'/'claude-ha' binary on PATH "
            "(for the subscription-auth transport). Nothing to route to "
            "without one of these."
        )

# max_retries=0: retry policy lives in _provider_call. The SDKs' internal
# retries (OpenAI and Anthropic both default to 2) stack multiplicatively
# with ours — a failing attempt became 3 SDK tries × 3 _provider_call
# attempts, turning one slow/failed call into a multi-tens-of-minutes hang.
# google-genai already defaults to no retries (tenacity stop_after_attempt(1)).
_gemini = genai.Client(api_key=_gemini_key) if _gemini_key else None
_openai = OpenAI(api_key=_openai_key, max_retries=0) if _openai_key else None
_anthropic = anthropic.Anthropic(api_key=_anthropic_key, max_retries=0) if _anthropic_key else None


# ─── Participant registry ────────────────────────────────────────────────

# Provider-agnostic short names. Adding a new entry here is enough; no
# tool signatures need to change. Pinned aliases (latest, etc.) so a
# provider model bump applies automatically without redeploy. The
# ``label`` shown in transcripts and to participants is intentionally
# distinct from the model id — a roundtable participant identifies
# itself by role ("Gemini Pro"), not by version string. The KEYS are
# likewise stable caller-facing handles, not model-version promises:
# "gpt-5-mini" deliberately stays put across mini bumps (currently
# gpt-5.6-luna, the fast/affordable tier of the 5.6 series), so don't
# rename a key on a model refresh — change only the "model" value.
PARTICIPANTS: dict[str, dict] = {
    "gemini-flash": {
        "provider": "gemini",
        "model": "gemini-flash-latest",
        "label": "Gemini Flash",
    },
    "gemini-pro": {
        "provider": "gemini",
        "model": "gemini-pro-latest",
        "label": "Gemini Pro",
    },
    "gpt-5-mini": {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "label": "GPT-5 Mini",
    },
    "gpt-5-terra": {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "label": "GPT-5 Terra",
    },
    "gpt-5": {
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "label": "GPT-5",
    },
    "claude-sonnet": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "label": "Claude Sonnet",
    },
    "claude-opus": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "label": "Claude Opus",
    },
    "claude-fable": {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "label": "Claude Fable",
    },
}


def _participant_provider_available(name: str) -> bool:
    info = PARTICIPANTS.get(name)
    if info is None:
        return False
    provider = info["provider"]
    if provider == "gemini":
        return _gemini is not None
    if provider == "openai":
        return _openai is not None
    if provider == "anthropic":
        # Availability depends on the configured transport. In "auto" mode
        # either path is acceptable. In "cli" mode we require the binary
        # regardless of API-key presence; in "api" mode we require the
        # SDK regardless of CLI presence.
        if _ANTHROPIC_TRANSPORT == "cli":
            return _CLAUDE_CLI is not None
        if _ANTHROPIC_TRANSPORT == "api":
            return _anthropic is not None
        return _CLAUDE_CLI is not None or _anthropic is not None
    return False


# Startup assertion: each PARTICIPANTS entry must have a unique ``label``.
# Labels are used by _format_transcript to tag turns, by _build_system_prompt
# to introduce other participants, and by roundtable_post as reserved
# speaker names. Two entries sharing a label would make those mechanisms
# silently ambiguous (whose (you) tag is this? which one is reserved?).
_seen_labels: set[str] = set()
for _name, _info in PARTICIPANTS.items():
    _label = _info["label"]
    if _label in _seen_labels:
        raise RuntimeError(
            f"PARTICIPANTS has duplicate label {_label!r}; each participant "
            f"must have a unique transcript label."
        )
    _seen_labels.add(_label)
del _seen_labels, _name, _info, _label


# ─── State store ─────────────────────────────────────────────────────────

STATE_DIR = Path(
    os.environ.get("CLAUDE_ROUNDTABLE_STATE_DIR", str(Path.home() / ".claude-roundtable"))
).resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = STATE_DIR / "state.db"

_db: Optional[sqlite3.Connection] = None

# Global lock for every DB write path. The sqlite3 connection is shared
# across FastMCP worker threads (check_same_thread=False), which only
# disables the threading guard — the Python connection object itself is
# not safe under simultaneous use, and read-modify-write critical sections
# (next-idx allocation, artifact version bump, closed-state re-check)
# need serialization across threads. roundtable_ask_parallel uses a
# worker pool, and Matt can fire concurrent MCP tool calls from a single
# Claude session, so this race IS reachable in practice.
#
# RLock (not Lock) because some call sites need to nest acquisitions —
# e.g. _conn() takes the lock for cold-init, but it's also called from
# inside critical sections that already hold the lock (artifact bump,
# message append, fork). RLock lets the same thread re-enter without
# deadlocking; other threads still block.
_db_lock = threading.RLock()
# Bound on retries when a cross-process writer grabs the (thread, idx) or
# (thread, name, version) slot we just computed. A handful is plenty — the
# loser just reads the new MAX and takes the next free slot.
_IDX_COLLISION_RETRIES = 8


def _conn() -> sqlite3.Connection:
    """Open (lazily) and return the singleton SQLite connection.

    First-call DDL / migrations are serialised under ``_db_lock`` so two
    threads calling concurrently on a cold DB don't run CREATE TABLE
    against each other.
    """
    global _db
    if _db is not None:
        return _db
    with _db_lock:
        if _db is not None:
            return _db
        c = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        # This store is shared with the standalone roundtable-mcp process. WAL
        # allows one writer at a time across processes; without a busy_timeout
        # a concurrent write from the other process returns SQLITE_BUSY
        # immediately (default 0ms) and surfaces as "database is locked". Wait
        # out the other writer's lock instead. _db_lock only serialises threads
        # within THIS process, so it can't help here.
        c.execute("PRAGMA busy_timeout=5000")
        c.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                participants_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                closed_at REAL,
                house_rules TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                thread_id INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL,
                PRIMARY KEY (thread_id, idx)
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id)"
        )
        # Artifacts are versioned blobs (typically code under review). Each
        # set_artifact bumps the version and also appends a synthetic
        # orchestrator message to the transcript so participants see it in
        # context — the table is the canonical store for "give me v2 raw".
        c.execute(
            """CREATE TABLE IF NOT EXISTS artifacts (
                thread_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL,
                PRIMARY KEY (thread_id, name, version)
            )"""
        )
        # A thread may be bound to a working directory so participants can
        # read the real repo (Goal 4) instead of having code pasted in. The
        # permission_policy is a SERIALIZABLE string (not a Callable) so the
        # binding survives the MCP JSON boundary; core resolves it into a
        # ToolUseContext + permission callback at turn time. One binding per
        # thread (PK thread_id); rebinding REPLACEs.
        c.execute(
            """CREATE TABLE IF NOT EXISTS repo_contexts (
                thread_id INTEGER PRIMARY KEY,
                working_directory TEXT NOT NULL,
                allowed_tools_json TEXT,
                permission_policy TEXT NOT NULL DEFAULT 'readonly',
                created_at REAL NOT NULL
            )"""
        )
        # Schema migrations: older DBs may predate columns added after
        # initial release. Symmetric ALTERs so any column added later is
        # backfilled with its CREATE TABLE default rather than crashing
        # _thread_row / roundtable_create on a missing column.
        # Durable per-turn token/cost accounting. One row per provider call
        # that reported usage; the CLI transport reports none and writes
        # nothing. Lets roundtable_usage() total a thread's spend instead of
        # the numbers only existing in a log line.
        c.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                thread_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                participant TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cached_tokens INTEGER,
                finish_reason TEXT
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_thread ON usage(thread_id)"
        )
        # A thread may carry a standing "context pack" — the constraints,
        # conventions, and prior decisions only the orchestrator holds —
        # injected into every participant's system prompt so the panel isn't
        # blind to context Gemini/OpenAI can't otherwise see. One pack per
        # thread (PK thread_id); set_context REPLACEs. Kept out of the
        # transcript on purpose: it rides the cached system-prompt prefix
        # (stable across turns) rather than the volatile, trimmed message log.
        c.execute(
            """CREATE TABLE IF NOT EXISTS thread_context (
                thread_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                created_at REAL NOT NULL
            )"""
        )
        # A thread may carry one active compaction: messages with idx <=
        # upto_idx are replaced, in what PARTICIPANTS see, by the stored
        # summary. Non-destructive — the messages table keeps every original
        # row (roundtable_history(raw=True) still shows them); only the
        # rendered view changes. One row per thread; re-compacting REPLACEs
        # with a larger upto_idx.
        c.execute(
            """CREATE TABLE IF NOT EXISTS compactions (
                thread_id INTEGER PRIMARY KEY,
                upto_idx INTEGER NOT NULL,
                summary TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        cols = {row[1] for row in c.execute("PRAGMA table_info(threads)").fetchall()}
        if "participants_json" not in cols:
            c.execute(
                "ALTER TABLE threads ADD COLUMN participants_json TEXT "
                "NOT NULL DEFAULT '[]'"
            )
        if "house_rules" not in cols:
            c.execute("ALTER TABLE threads ADD COLUMN house_rules TEXT")
        _db = c
        return _db


def _next_idx(thread_id: int) -> int:
    """Read-only helper: return the next message index for a thread.

    Caller MUST hold ``_db_lock`` when pairing this with the subsequent
    INSERT — otherwise two threads can observe the same MAX and race to
    write the same idx, hitting an IntegrityError on the second writer.
    Used directly by ``_append_message`` which holds the lock.
    """
    with _db_lock:
        row = _conn().execute(
            "SELECT COALESCE(MAX(idx), -1) FROM messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return int(row[0]) + 1


def _thread_row(thread_id: int) -> Optional[dict]:
    with _db_lock:
        row = _conn().execute(
            "SELECT id, topic, participants_json, created_at, closed_at, house_rules "
            "FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        # Load the context pack in the same lock acquisition (the pack lives in
        # a separate 1:1 table). Both consumers — _build_system_prompt and the
        # _run_turn trim budget — read it off this dict, so it's fetched once
        # per ask here, not per participant in the parallel fan-out.
        ctx_row = (
            _conn().execute(
                "SELECT content FROM thread_context WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is not None else None
        )
    if row is None:
        return None
    return {
        "id": row[0],
        "topic": row[1],
        "participants": json.loads(row[2] or "[]"),
        "created_at": row[3],
        "closed_at": row[4],
        "house_rules": row[5] or "",
        "context_pack": (ctx_row[0] if ctx_row else "") or "",
    }


def _thread_messages(thread_id: int) -> list[dict]:
    with _db_lock:
        rows = _conn().execute(
            "SELECT idx, speaker, content, ts FROM messages "
            "WHERE thread_id = ? ORDER BY idx",
            (thread_id,),
        ).fetchall()
    return [
        {"idx": r[0], "speaker": r[1], "content": r[2], "ts": r[3]}
        for r in rows
    ]


def _append_message(thread_id: int, speaker: str, content: str) -> int:
    """Allocate the next message index and INSERT it atomically.

    ``_db_lock`` serialises this process's threads, but the standalone
    roundtable-mcp process shares the same DB and can allocate the same idx
    between our MAX read and INSERT. That collides on the (thread_id, idx)
    primary key, so retry on IntegrityError with a freshly-read idx — the
    other writer is committed by then, so MAX advances and the retry takes
    the next slot.
    """
    for _attempt in range(_IDX_COLLISION_RETRIES):
        with _db_lock:
            idx = _next_idx(thread_id)
            try:
                _conn().execute(
                    "INSERT INTO messages(thread_id, idx, speaker, content, ts) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (thread_id, idx, speaker, content, time.time()),
                )
            except sqlite3.IntegrityError:
                continue
            return idx
    raise RuntimeError(
        f"could not allocate a message idx for thread {thread_id} after "
        f"{_IDX_COLLISION_RETRIES} attempts (cross-process contention)"
    )


def _compaction_row(thread_id: int) -> Optional[dict]:
    """Return the thread's active compaction, or None if it has none."""
    with _db_lock:
        row = _conn().execute(
            "SELECT upto_idx, summary, created_at FROM compactions "
            "WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    if row is None:
        return None
    return {"upto_idx": row[0], "summary": row[1], "created_at": row[2]}


def _effective_messages(thread_id: int) -> list[dict]:
    """The message list PARTICIPANTS see: raw messages, unless the thread has
    been compacted — then a single synthetic orchestrator message carrying the
    stored summary stands in for everything at or before the compaction
    cutoff, followed by the uncompacted tail verbatim.

    The synthetic message reuses the cutoff idx so a later re-compaction can
    tell what the summary already covers. Raw history is never modified —
    ``_thread_messages`` / ``roundtable_history(raw=True)`` still return it.
    """
    messages = _thread_messages(thread_id)
    comp = _compaction_row(thread_id)
    if comp is None:
        return messages
    tail = [m for m in messages if m["idx"] > comp["upto_idx"]]
    synthetic = {
        "idx": comp["upto_idx"],
        "speaker": "orchestrator",
        "content": comp["summary"],
        "ts": comp["created_at"],
    }
    return [synthetic] + tail


def _thread_is_closed(thread_id: int) -> bool:
    """Re-read just the closed_at column. Used to recheck closure right
    before committing a participant response, since a ``roundtable_close``
    can race with an in-flight provider call that started before closure.
    """
    with _db_lock:
        row = _conn().execute(
            "SELECT closed_at FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    return row is not None and row[0] is not None


# ─── Conversation rendering ──────────────────────────────────────────────

# Cap the size of a single prompt we send to a provider. Multi-turn
# threads accumulate; without a soft cap a long debate will eventually
# trip the model's context limit with a confusing 400. We prefer a
# truncation warning the participant can see and react to ("you've lost
# the older context, summarise if you need it") over an opaque API error.
PROMPT_CHAR_CAP = int(os.environ.get("CLAUDE_ROUNDTABLE_PROMPT_CHAR_CAP", "400000"))

# Cap the standing context pack (roundtable_set_context). It rides in the
# system prompt — which on the default Anthropic CLI transport is passed as an
# argv arg (--system-prompt), and this host's ARG_MAX is ~130KB. Keep the pack
# well under that so base framing + house_rules fit alongside on argv.
CONTEXT_PACK_CHAR_CAP = int(
    os.environ.get("CLAUDE_ROUNDTABLE_CONTEXT_PACK_CHAR_CAP", "60000")
)
# Floor for the transcript budget once the context pack is subtracted from
# PROMPT_CHAR_CAP, so a large pack can't starve the conversation to nothing.
MIN_TRANSCRIPT_CAP = int(
    os.environ.get("CLAUDE_ROUNDTABLE_MIN_TRANSCRIPT_CAP", "50000")
)


def _format_transcript(
    messages: list[dict], for_participant_label: str,
) -> str:
    """Render the message list as a labelled transcript the AI can read.

    The current participant's own prior turns are tagged ``(you)`` so
    the model can orient itself; everyone else gets just their speaker
    label. Speaker labels are bracketed so it's obvious where one
    turn ends and the next begins, even when content contains
    arbitrary user-supplied text.
    """
    lines: list[str] = []
    for m in messages:
        speaker = m["speaker"]
        tag = f"[{speaker}]"
        if speaker == for_participant_label:
            tag = f"[{speaker} (you)]"
        lines.append(f"{tag}:\n{m['content']}")
    return "\n\n".join(lines)


_OMITTED_MARKER_SPEAKER = "system"


def _truncate_message_body(m: dict, body_cap: int) -> dict:
    """If a single message body exceeds ``body_cap`` chars, replace its
    middle with an inline marker. Returns the message (possibly modified).

    Used by the message-aware trimmer when an individual message (often a
    freshly-bumped artifact) is so large that even keeping just it would
    blow the cap. We refuse to silently drop it — the orchestrator pasted
    it deliberately — but mark the cut so the participant knows.
    """
    body = m["content"]
    if len(body) <= body_cap:
        return m
    half = body_cap // 2 - 100
    if half <= 0:
        # body_cap too tiny to meaningfully truncate; keep the body intact
        # and let the upstream provider reject it rather than send garbage.
        return m
    omitted = len(body) - (half * 2)
    truncated = (
        body[:half]
        + f"\n\n[… {omitted} chars omitted within this message — "
        f"oversized for the context window …]\n\n"
        + body[-half:]
    )
    return {**m, "content": truncated}


def _trim_messages_to_cap(
    messages: list[dict], cap: int, for_participant_label: str,
) -> list[dict]:
    """Trim ``messages`` so the rendered transcript fits within ``cap`` chars.

    Strategy:
        1. Always keep the FIRST message (topic-setting / earliest context)
           and the LAST message (the current ask, freshest artifact, or
           orchestrator instruction the participant must react to).
        2. Walk from newest back toward oldest, including middle messages
           greedily until the budget runs out.
        3. Replace the dropped middle with a single system-speaker marker
           so the participant knows context was omitted and can ask for
           specific older messages if needed.
        4. If any single message body would on its own exceed the cap
           (usually a huge artifact paste), truncate WITHIN it via
           ``_truncate_message_body`` — never silently drop it.

    Trimming at message boundaries (vs the prior char-slice approach)
    means we never bisect a ``[speaker]`` tag, a UTF-8 byte sequence, or
    a code fence — the participant always sees well-formed turns.
    """
    if not messages:
        return messages

    rendered = _format_transcript(messages, for_participant_label=for_participant_label)
    if len(rendered) <= cap:
        return messages

    # Reserve headroom for the omitted-marker we may insert, plus some
    # margin for the speaker-tag formatting overhead per turn.
    marker_budget = 300
    effective_cap = max(cap - marker_budget, 1)
    # Don't let a single message eat the entire budget. A reasonable cap
    # is half the effective cap — leaves room for the other anchored
    # message plus any recent turns.
    per_message_cap = effective_cap // 2

    first = _truncate_message_body(messages[0], per_message_cap)
    last_idx = len(messages) - 1
    last = _truncate_message_body(messages[-1], per_message_cap)

    def _rendered_len(m: dict) -> int:
        return len(_format_transcript([m], for_participant_label=for_participant_label)) + 2

    if last_idx == 0:
        return [first]

    budget = effective_cap - _rendered_len(first) - _rendered_len(last)
    middle = messages[1:last_idx]
    kept: list[dict] = []
    for m in reversed(middle):
        m_trunc = _truncate_message_body(m, per_message_cap)
        m_len = _rendered_len(m_trunc)
        if m_len > budget:
            break
        kept.insert(0, m_trunc)
        budget -= m_len

    result: list[dict] = [first]
    dropped = len(middle) - len(kept)
    if dropped > 0:
        result.append({
            "speaker": _OMITTED_MARKER_SPEAKER,
            "content": (
                f"[… {dropped} older messages omitted to fit the context "
                f"window — ask the orchestrator for specific earlier "
                f"messages by index if you need them …]"
            ),
        })
    result.extend(kept)
    result.append(last)
    return result


def _build_system_prompt(
    thread: dict, participant_label: str, all_participants: list[str],
    web_search: bool = False, tools_enabled: bool = False,
    readonly_tools: bool = False,
) -> str:
    """The system prompt orients the participant: who they are, who the
    other participants are, what the topic is, and what behaviour is
    expected (respond as the participant, not as a narrator).

    A thread-level ``house_rules`` string (set at create time) is folded
    in verbatim at the end so an output-format contract — e.g. 'reply
    with Critical/High/Medium issues, file:line refs, no preamble' —
    propagates to every participant without the orchestrator restating
    it on each ask.

    When ``web_search`` is True we explicitly tell the participant they
    have a web-search tool wired up. Without this clause models reliably
    refuse on "I don't have live web access" grounds even when the tool
    is in fact attached — the system prompt training pulls harder than
    the tool descriptor.

    When ``tools_enabled`` is True the participant has the Read / Grep /
    Glob filesystem tools available for this turn (via the agent-SDK
    path with permission gating). We tell them so explicitly because the
    main framing leans hard on "you are a roundtable participant" — a
    role that, untold, suggests pure text reasoning rather than tool
    use. We also tell them to PUSH BACK on other panellists' claims
    that contradict what the code says; without that nudge the synth
    tends to consolidate hallucinated context rather than correct it.
    """
    other_labels = [
        PARTICIPANTS[p]["label"] for p in all_participants
        if PARTICIPANTS.get(p) and PARTICIPANTS[p]["label"] != participant_label
    ]
    others_clause = (
        f" Other AI participants in this roundtable: {', '.join(other_labels)}."
        if other_labels else ""
    )
    web_clause = (
        " You have a live web-search tool wired up for this turn — use it "
        "whenever the question turns on current information (current docs, "
        "recent releases, what a repo actually contains today). Do NOT "
        "refuse on 'I have no live web access' grounds; that is no longer "
        "true." if web_search else ""
    )
    readonly_tools_clause = (
        " You have read-only tools for this turn — Read, Grep, and Glob — "
        "scoped to the project this thread is bound to. Every call is gated by "
        "a user-approval prompt in the browser, so use them deliberately (one "
        "Grep to find the right file beats ten exploratory Reads). If other "
        "panellists make claims the code contradicts, verify against the "
        "source and correct them with file:line references — that's the point "
        "of having tools. You cannot modify files; if a change is warranted, "
        "propose it as a unified diff in your reply."
        if (tools_enabled and readonly_tools) else ""
    )
    tools_clause = (
        " You have the full Claude Code tool set wired up for this turn — "
        "Read, Grep, Glob, Bash, Edit, Write, plus whatever MCP servers and "
        "skills are configured in the user's settings (github, memory, etc.). "
        "Scoped to the project this thread is bound to. Every tool call is "
        "gated by a user-approval prompt in the browser, so use tools "
        "deliberately rather than reflexively (one Grep to find the right "
        "file is better than ten exploratory Reads; ask for a single edit "
        "rather than a string of speculative ones). If other panellists "
        "have made claims about the project that the code contradicts, "
        "correct them with file:line references — that is the whole point "
        "of having tools here. You are a panellist, not the user's main "
        "assistant: avoid taking destructive actions unprompted; if a "
        "concrete change is warranted, propose it (and the unified diff) in "
        "your reply rather than running Edit / Write / git on your own."
        if (tools_enabled and not readonly_tools) else ""
    )
    base = (
        f"You are {participant_label}, participating in a multi-AI roundtable. "
        f"The 'orchestrator' speaker is the session driver — usually another "
        f"AI agent coordinating the thread, sometimes a human; treat them as "
        f"a peer facilitator who pastes code, posts framing, and asks "
        f"questions, not as a separate authority.{others_clause} The topic "
        f"is: {thread['topic']!r}. You will see the full transcript so far. "
        f"Respond ONLY with your own contribution — do not prefix it with "
        f"your name or repeat the speaker label, and do not write turns for "
        f"any other participant. Address other participants by name when you "
        f"want to agree, disagree, or build on their points. Be concrete and "
        f"substantive; this is a working session, not a status meeting."
        f"{web_clause}{tools_clause}{readonly_tools_clause}"
    )
    house_rules = (thread.get("house_rules") or "").strip()
    if house_rules:
        base += (
            "\n\n=== House rules for this thread (apply to every reply) ===\n"
            + house_rules
        )
    # Standing context pack: stable per thread, so it stays in the cached
    # system-prompt prefix and doesn't re-bill per turn (see roundtable_set_
    # context). Read off the thread dict, same as house_rules.
    context_pack = (thread.get("context_pack") or "").strip()
    if context_pack:
        base += (
            "\n\n=== Standing context for this thread (constraints, "
            "conventions, prior decisions — treat as ground truth) ===\n"
            + context_pack
        )
    return base


# ─── Provider calls ──────────────────────────────────────────────────────

# Per-request HTTP timeout in seconds. Without this a stalled provider
# wedges the FastMCP worker thread forever — and in roundtable_ask_parallel
# one stuck call blocks the as_completed() loop on every participant.
PROVIDER_TIMEOUT_SEC = float(
    os.environ.get("CLAUDE_ROUNDTABLE_PROVIDER_TIMEOUT_SEC", "300")
)
# Total attempts including the initial try. 1 = no retry. We retry only on
# known-transient errors (rate limits, 5xx, connection drops, timeouts) —
# bad-request / auth / content-policy failures bypass retry and surface
# immediately so the orchestrator can see the real cause.
PROVIDER_MAX_ATTEMPTS = int(
    os.environ.get("CLAUDE_ROUNDTABLE_PROVIDER_MAX_ATTEMPTS", "3")
)
PROVIDER_RETRY_BASE_SEC = float(
    os.environ.get("CLAUDE_ROUNDTABLE_PROVIDER_RETRY_BASE_SEC", "1.0")
)
# Hard WALL-CLOCK cap per provider attempt. PROVIDER_TIMEOUT_SEC above is an
# IDLE timeout on the socket — and at least OpenAI's edge sends periodic
# keepalive bytes during long non-streaming reasoning calls, resetting the
# idle window, so a slow generation sails straight past it (observed: a
# gpt-5.5 effort=high call on a large transcript holding the socket open
# 30+ minutes, wedging roundtable_ask_parallel until the MCP client tore
# the connection down). The wall cap converts that into a clean
# per-participant failure. Big high-effort calls legitimately run ~9
# minutes, so the default leaves headroom above that.
PROVIDER_WALL_TIMEOUT_SEC = float(
    os.environ.get("CLAUDE_ROUNDTABLE_PROVIDER_WALL_TIMEOUT_SEC", "900")
)


class ProviderWallTimeout(Exception):
    """A provider call exceeded PROVIDER_WALL_TIMEOUT_SEC of wall clock.

    Deliberately NOT a TimeoutError subclass: idle timeouts are transient
    and worth retrying, but a generation that blew the wall cap would very
    likely blow it again — surface immediately instead of paying for the
    same near-half-hour twice. The abandoned worker thread is a daemon and
    dies with the process; the underlying HTTP call is left to finish or
    fail on its own.
    """


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True if ``exc`` is a known transient provider failure worth retrying.

    Each SDK raises its own typed exception hierarchy; we check membership
    in the provider-specific retryable set rather than parsing strings.
    A few classes (TimeoutError, ConnectionError) are retried regardless
    of which provider raised them, since httpx surfaces those at the
    transport layer.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # subprocess.TimeoutExpired doesn't inherit from TimeoutError — it
    # inherits from subprocess.SubprocessError. Handle it explicitly so
    # a slow Claude Code CLI invocation retries the same way an API
    # timeout would.
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    import openai as _o
    if isinstance(exc, (_o.RateLimitError, _o.APITimeoutError,
                        _o.APIConnectionError, _o.InternalServerError)):
        return True
    import anthropic as _a
    if isinstance(exc, (_a.RateLimitError, _a.APITimeoutError,
                        _a.APIConnectionError, _a.InternalServerError)):
        return True
    from google.genai import errors as _g
    if isinstance(exc, _g.APIError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (408, 429, 500, 502, 503, 504):
            return True
    return False


def _call_with_wall_cap(what: str, fn):
    """Run ``fn()`` in a daemon thread, bounded by PROVIDER_WALL_TIMEOUT_SEC.

    A plain daemon thread (not ThreadPoolExecutor) so an abandoned call
    can't block interpreter shutdown — the executor's atexit hook joins
    its threads, which would reintroduce the hang at exit.
    """
    outcome: list = []          # [("ok", result)] or [("err", exc)]
    done = threading.Event()

    def _runner():
        try:
            outcome.append(("ok", fn()))
        except BaseException as exc:
            outcome.append(("err", exc))
        finally:
            done.set()

    t = threading.Thread(target=_runner, daemon=True, name=f"wall-{what}")
    t.start()
    if not done.wait(PROVIDER_WALL_TIMEOUT_SEC):
        raise ProviderWallTimeout(
            f"{what} exceeded the {PROVIDER_WALL_TIMEOUT_SEC:.0f}s wall-clock "
            f"cap (idle timeouts don't bound long reasoning generations; "
            f"lower effort, shrink the thread, or raise "
            f"CLAUDE_ROUNDTABLE_PROVIDER_WALL_TIMEOUT_SEC)"
        )
    kind, value = outcome[0]
    if kind == "err":
        raise value
    return value


def _provider_call(what: str, fn):
    """Run ``fn()`` with exponential-backoff retry on transient errors.

    ``what`` is a short label like ``gemini/gemini-pro-latest`` used in
    log lines so a flaky run shows up clearly. We log start/end/elapsed
    on every attempt — stderr only, since stdout is the MCP JSON-RPC
    channel and any print there would corrupt the protocol.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, PROVIDER_MAX_ATTEMPTS + 1):
        t0 = time.time()
        try:
            result = _call_with_wall_cap(what, fn)
            elapsed = time.time() - t0
            logger.info(
                "%s ok attempt=%d/%d elapsed=%.2fs",
                what, attempt, PROVIDER_MAX_ATTEMPTS, elapsed,
            )
            return result
        except Exception as exc:
            last_exc = exc
            elapsed = time.time() - t0
            transient = _is_transient_provider_error(exc)
            if attempt >= PROVIDER_MAX_ATTEMPTS or not transient:
                logger.warning(
                    "%s failed attempt=%d/%d elapsed=%.2fs transient=%s err=%s: %s",
                    what, attempt, PROVIDER_MAX_ATTEMPTS, elapsed,
                    transient, type(exc).__name__, exc,
                )
                raise
            backoff = PROVIDER_RETRY_BASE_SEC * (2 ** (attempt - 1))
            backoff *= 0.5 + random.random()  # 0.5x..1.5x jitter
            logger.warning(
                "%s transient attempt=%d/%d elapsed=%.2fs err=%s: %s backoff=%.2fs",
                what, attempt, PROVIDER_MAX_ATTEMPTS, elapsed,
                type(exc).__name__, exc, backoff,
            )
            time.sleep(backoff)
    # Unreachable — loop either returns or raises — but mypy/pyright like
    # an explicit raise here.
    assert last_exc is not None
    raise last_exc


# Map a unified ``effort`` knob to provider-specific reasoning/thinking
# configs. ``low / medium / high`` are intentionally coarse — finer tuning
# (custom thinking budgets, xhigh effort) is a follow-up if a use case
# justifies it. ``None`` means "send no reasoning config" — every provider
# then applies its own default.
#   - OpenAI gpt-5.x: ``reasoning_effort`` accepts low/medium/high (plus
#     minimal/none/xhigh on some models; we stick to the safe trio).
#   - Gemini: family-dependent. Gemini 3+ uses the semantic
#     ``thinking_level`` (low/medium/high); legacy Gemini-2.x uses the
#     integer ``thinking_budget`` below (0–24576 tokens). See
#     ``_gemini_uses_thinking_level`` — the two knobs are mutually
#     exclusive (both → 400).
#   - Anthropic: medium/high enable adaptive thinking (the only thinking
#     mode on 4.6+; ``enabled``+``budget_tokens`` is deprecated). Opus 4.8+
#     additionally takes a first-class ``output_config.effort`` knob — see
#     ``_anthropic_supports_effort``; the CLI transport passes ``--effort``.
_GEMINI_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}


def _gemini_uses_thinking_level(model: str) -> bool:
    """True if ``model`` takes the semantic ``thinking_level`` knob.

    Gemini 3+ and the rolling ``-latest`` aliases (which now resolve to a
    Gemini-3 tier) use ``thinking_level``. Only explicit Gemini-1.x/2.x
    model ids fall back to the legacy integer ``thinking_budget``. We
    default unknown/aliased names to the modern knob because that's what
    the aliases resolve to today; an explicit ``gemini-2.x`` opts out.
    """
    m = model.lower()
    return not ("gemini-1." in m or "gemini-2" in m)


@dataclasses.dataclass
class ProviderResult:
    """Structured return from a single provider call.

    ``_run_turn`` and every ``_call_*`` function return this instead of a
    bare string so the caller can surface token usage, finish reason, and
    (later) structured output / tool traces without changing the public
    return shape — ``roundtable_ask`` still unwraps ``.text``.

    ``usage`` is a provider-neutral dict (``input_tokens`` /
    ``output_tokens`` / ``cached_tokens`` where the provider exposes them)
    or None when the transport can't report it (the CLI ``-p`` path).
    """
    text: str
    usage: Optional[dict] = None
    finish_reason: Optional[str] = None
    structured: Optional[object] = None
    raw: Optional[object] = None


def _extract_usage(provider: str, resp: object) -> Optional[dict]:
    """Pull a provider-neutral usage dict off a raw SDK response.

    Defensive by design: SDK response shapes drift across versions, so a
    missing attribute yields None for that field rather than raising —
    usage telemetry must never be able to fail a turn that otherwise
    succeeded. ``cached_tokens`` surfaces prompt-cache hits once Phase 1
    caching lands; it stays None until the provider reports it.
    """
    try:
        if provider == "gemini":
            u = getattr(resp, "usage_metadata", None)
            if u is None:
                return None
            return {
                "input_tokens": getattr(u, "prompt_token_count", None),
                "output_tokens": getattr(u, "candidates_token_count", None),
                "cached_tokens": getattr(u, "cached_content_token_count", None),
            }
        if provider == "openai":
            u = getattr(resp, "usage", None)
            if u is None:
                return None
            # Responses API uses input/output_tokens; chat.completions uses
            # prompt/completion_tokens. Read both, prefer whichever is set.
            inp = getattr(u, "input_tokens", None)
            if inp is None:
                inp = getattr(u, "prompt_tokens", None)
            out = getattr(u, "output_tokens", None)
            if out is None:
                out = getattr(u, "completion_tokens", None)
            details = getattr(u, "prompt_tokens_details", None) or getattr(
                u, "input_tokens_details", None
            )
            cached = getattr(details, "cached_tokens", None) if details else None
            return {"input_tokens": inp, "output_tokens": out, "cached_tokens": cached}
        if provider == "anthropic":
            u = getattr(resp, "usage", None)
            if u is None:
                return None
            return {
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
                "cached_tokens": getattr(u, "cache_read_input_tokens", None),
            }
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        return None
    return None


def _log_usage(label: str, result: "ProviderResult", thread_id: Optional[int] = None) -> None:
    """Log a per-turn token-usage line and, when ``thread_id`` is given,
    persist it to the ``usage`` table for durable per-thread accounting.

    A turn whose transport can't report usage (the CLI path) records nothing.
    """
    u = result.usage
    if not u:
        return
    logger.info(
        "usage participant=%s in=%s out=%s cached=%s finish=%s",
        label, u.get("input_tokens"), u.get("output_tokens"),
        u.get("cached_tokens"), result.finish_reason,
    )
    if thread_id is None:
        return
    try:
        with _db_lock:
            _conn().execute(
                "INSERT INTO usage(thread_id, ts, participant, input_tokens, "
                "output_tokens, cached_tokens, finish_reason) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id, time.time(), label,
                    u.get("input_tokens"), u.get("output_tokens"),
                    u.get("cached_tokens"), result.finish_reason,
                ),
            )
    except sqlite3.Error as e:  # telemetry must never break a turn
        logger.warning("usage persist failed for thread %s: %s", thread_id, e)


def roundtable_usage(thread_id: int) -> dict:
    """Per-participant and total token usage recorded for a thread."""
    with _db_lock:
        rows = _conn().execute(
            "SELECT participant, COUNT(*), "
            "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cached_tokens),0) "
            "FROM usage WHERE thread_id = ? GROUP BY participant",
            (thread_id,),
        ).fetchall()
    by_participant = [
        {
            "participant": r[0], "turns": r[1],
            "input_tokens": r[2], "output_tokens": r[3], "cached_tokens": r[4],
        }
        for r in rows
    ]
    return {
        "thread_id": thread_id,
        "by_participant": by_participant,
        "total_input_tokens": sum(p["input_tokens"] for p in by_participant),
        "total_output_tokens": sum(p["output_tokens"] for p in by_participant),
    }


# Per-text-chunk callback for streamed turns. Invoked from whatever thread
# the provider call runs on (a roundtable_ask worker thread); the webapp
# bridges it back to its event loop via run_coroutine_threadsafe. None =
# non-streaming (the default for every panel ask).
StreamDelta = Callable[[str], None]


def _gemini_response_text(resp: object) -> str:
    """Visible text off a genai response, tolerant of the ``.text`` property
    *raising* (it does when the final candidate holds only function-call parts,
    e.g. the tool loop hit its round cap) rather than just being absent."""
    if resp is None:
        return ""
    try:
        return resp.text or ""
    except Exception:  # noqa: BLE001 — fall back to manual part extraction
        pass
    try:
        out = []
        for cand in (getattr(resp, "candidates", None) or []):
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                t = getattr(part, "text", None)
                if t:
                    out.append(t)
        return "".join(out)
    except Exception:  # noqa: BLE001
        return ""


def _call_gemini(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Send the rendered transcript + final instruction to Gemini.

    Gemini supports a separate ``system_instruction`` parameter, so we
    use it instead of cramming the system text into the user message.
    The user message is the transcript followed by the orchestrator's
    instruction (if any) — that pattern keeps the model's "what do I
    do next?" prompt right at the end of the input where it has the
    strongest priming effect.

    When ``effort`` is provided we attach a ``ThinkingConfig`` so the
    model spends extra compute reasoning before producing its visible
    reply. The knob is family-dependent: Gemini 3+ (and the rolling
    ``-latest`` aliases, which now resolve to a Gemini-3 tier) take the
    semantic ``thinking_level``; only legacy Gemini-2.x takes the integer
    ``thinking_budget``. Passing the legacy budget to a Gemini-3 model
    degrades its reasoning, and setting both at once is a 400 — see
    ``_gemini_uses_thinking_level``. Thoughts are not surfaced to the
    transcript.

    When ``web_search`` is True we attach Google Search grounding so
    the model can fetch current information instead of refusing on
    "I have no live web access" grounds — billed per grounded call.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    config: dict = {
        "system_instruction": system_prompt,
        # genai HttpOptions.timeout is milliseconds. Set per-request so
        # one provider's hang doesn't take down sibling participants in
        # roundtable_ask_parallel.
        "http_options": genai_types.HttpOptions(
            timeout=int(PROVIDER_TIMEOUT_SEC * 1000),
        ),
    }
    if effort:
        if _gemini_uses_thinking_level(model):
            # Gemini 3+: semantic level (low/medium/high map straight
            # through). Never also set thinking_budget — the two together
            # are a 400, and the budget alone degrades Gemini-3 reasoning.
            config["thinking_config"] = genai_types.ThinkingConfig(
                thinking_level=effort,
            )
        elif effort in _GEMINI_BUDGETS:
            # Legacy Gemini-2.x: integer token budget.
            config["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=_GEMINI_BUDGETS[effort],
            )
    if web_search:
        config["tools"] = [
            genai_types.Tool(google_search=genai_types.GoogleSearch()),
        ]

    if on_delta is not None:
        # Streamed: iterate chunks, emit each chunk's text, accumulate the
        # full reply. No _provider_call retry wrapper — a mid-stream retry
        # would re-emit already-streamed text. The SDK's http_options timeout
        # still bounds the call.
        def _do_stream() -> ProviderResult:
            parts: list[str] = []
            last = None
            for chunk in _gemini.models.generate_content_stream(
                model=model, contents=user_msg, config=config,
            ):
                last = chunk
                t = getattr(chunk, "text", None)
                if t:
                    parts.append(t)
                    on_delta(t)
            return ProviderResult(
                text="".join(parts),
                usage=_extract_usage("gemini", last) if last is not None else None,
                raw=last,
            )

        return _call_with_wall_cap(f"gemini/{model}/stream", _do_stream)

    def _do_call():
        return _gemini.models.generate_content(
            model=model, contents=user_msg, config=config,
        )

    resp = _provider_call(f"gemini/{model}", _do_call)
    return ProviderResult(
        text=resp.text or "", usage=_extract_usage("gemini", resp), raw=resp,
    )


def _call_openai(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Send the conversation to OpenAI via the Responses API.

    All OpenAI turns go through ``responses.create`` — the no-search and
    web-search paths used to diverge (chat.completions vs Responses),
    which meant two usage shapes, two output-extraction paths, and no
    hosted-tool support on the default path. Unifying on Responses is
    also the prerequisite for the Goal-1 OpenAI function-calling loop,
    which only the Responses API exposes.

    The system prompt rides the top-level ``instructions`` field and the
    transcript + orchestrator turn ride ``input`` — mirroring how the
    other providers receive system vs user content.

    When ``effort`` is provided it passes through as
    ``reasoning={"effort": ...}``; every OpenAI participant in the
    registry is a gpt-5.x reasoning model. When ``web_search`` is True we
    attach the hosted ``web_search`` tool so the model can fetch current
    information instead of refusing on no-live-access grounds.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"

    kwargs: dict = {
        "model": model,
        "instructions": system_prompt,
        "input": user_msg,
        "timeout": PROVIDER_TIMEOUT_SEC,
        # OpenAI prompt caching is automatic for >1024-token prefixes; the
        # cache_key is a routing hint that raises hit-rate for requests
        # sharing a prefix. The system prompt is stable per participant per
        # thread (it embeds the label, topic, and house rules), so hashing
        # it groups a participant's repeated turns onto the same cache
        # lineage — which is where the long shared transcript prefix lives.
        "prompt_cache_key": hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest()[:32],
    }
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]

    if on_delta is not None:
        # Streamed via the Responses API. ``response.output_text.delta`` events
        # carry the visible text; ``response.completed`` carries the final
        # response (with usage). No retry wrapper (would re-emit).
        def _do_stream() -> ProviderResult:
            parts: list[str] = []
            final = None
            with _openai.responses.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        d = getattr(event, "delta", "") or ""
                        if d:
                            parts.append(d)
                            on_delta(d)
                    elif etype == "response.completed":
                        final = getattr(event, "response", None)
                if final is None:
                    final = stream.get_final_response()
            text = (
                getattr(final, "output_text", None)
                if final is not None else None
            ) or "".join(parts)
            return ProviderResult(
                text=text,
                usage=_extract_usage("openai", final) if final is not None else None,
                raw=final,
            )

        return _call_with_wall_cap(f"openai/{model}/stream", _do_stream)

    def _do_call():
        return _openai.responses.create(**kwargs)

    resp = _provider_call(f"openai/{model}", _do_call)
    return ProviderResult(
        text=resp.output_text or "", usage=_extract_usage("openai", resp),
        raw=resp,
    )


# Anthropic Messages API requires ``max_tokens``. Adaptive thinking shares
# this budget with the visible reply, so a high-effort turn needs more
# headroom or it gets clipped mid-thought. We scale the cap by effort; the
# env var is the no-effort default and an explicit override.
_ANTHROPIC_MAX_TOKENS = int(
    os.environ.get("CLAUDE_ROUNDTABLE_ANTHROPIC_MAX_TOKENS", "16384")
)
_ANTHROPIC_MAX_TOKENS_BY_EFFORT = {"low": 8192, "medium": 16384, "high": 32768}

# Models that accept the Messages-API ``output_config.effort`` knob. The
# feature shipped with Opus 4.8; Sonnet 4.6 predates it and isn't known to
# accept it, so we gate rather than risk a 400. Extend as models gain it.
# (The CLI transport handles effort itself via ``--effort`` and isn't gated
# here — this gate only guards the SDK Messages path.)
_ANTHROPIC_EFFORT_MODELS = ("opus-4-8",)


def _anthropic_supports_effort(model: str) -> bool:
    """True if ``model`` accepts the Messages-API ``output_config.effort``."""
    m = model.lower()
    return any(tag in m for tag in _ANTHROPIC_EFFORT_MODELS)


def _call_anthropic(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Send the transcript to an Anthropic model via the Messages API.

    Anthropic's API keeps the system prompt as a top-level ``system``
    field (unlike OpenAI's role=system message), and only supports a
    single content stream per message — so we collapse the transcript +
    orchestrator instruction into one user message, mirroring how the
    other two providers receive it.

    When ``effort`` is medium or high we enable adaptive extended
    thinking, which lets the model allocate its own internal reasoning
    budget before producing the visible response. Adaptive is the only
    supported thinking mode on 4.6+ — the older ``type=enabled`` with
    ``budget_tokens`` is deprecated. The visible response is composed of
    ``text`` blocks; ``thinking`` blocks are dropped before returning so
    the transcript stays human-readable.

    On models that support it (Opus 4.8+, see ``_anthropic_supports_effort``)
    we also pass the first-class ``output_config.effort`` knob, and scale
    ``max_tokens`` by effort so a high-effort turn isn't clipped. Sonnet
    4.6 predates the effort knob and is left on adaptive-thinking only.

    When ``web_search`` is True we attach Anthropic's hosted
    ``web_search`` server tool (the 2026-02-09 revision the SDK ships)
    so the model can fetch current information mid-turn.

    The system prompt and transcript are sent as cache-broken block
    arrays so a thread's stable prefix is read from cache on later turns
    — see the inline note. Cache hits surface as ``cached_tokens`` in the
    logged usage.
    """
    # Prompt caching: mark the stable prefix (system prompt + the transcript
    # so far) with ephemeral cache_control breakpoints. Anthropic matches the
    # longest previously-cached prefix, so as the transcript grows each turn
    # the earlier turns are read from cache (~90% cheaper input) and only the
    # delta is processed. The orchestrator instruction, when present, is the
    # one volatile piece and rides its own uncached tail block. Caching only
    # engages above the provider's ~1024-token minimum; shorter threads just
    # skip it. Two breakpoints here, well under Anthropic's limit of four.
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    content_blocks: list = [
        {
            "type": "text",
            "text": transcript,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if instruction:
        content_blocks.append(
            {"type": "text", "text": f"\n\n[orchestrator]:\n{instruction}"}
        )
    max_tokens = (
        _ANTHROPIC_MAX_TOKENS_BY_EFFORT.get(effort, _ANTHROPIC_MAX_TOKENS)
        if effort else _ANTHROPIC_MAX_TOKENS
    )
    kwargs: dict = {
        "model": model,
        "system": system_blocks,
        "messages": [{"role": "user", "content": content_blocks}],
        "max_tokens": max_tokens,
        "timeout": PROVIDER_TIMEOUT_SEC,
    }
    if effort in {"medium", "high"}:
        kwargs["thinking"] = {"type": "adaptive"}
    if effort and _anthropic_supports_effort(model):
        # First-class effort knob (Opus 4.8+). low/medium/high pass straight
        # through; the API also accepts xhigh/max but our enum stops at high.
        kwargs["output_config"] = {"effort": effort}
    if web_search:
        kwargs["tools"] = [
            {"type": "web_search_20260209", "name": "web_search"},
        ]

    if on_delta is not None:
        # Streamed via Messages API. ``text_stream`` yields visible-text
        # deltas only (thinking blocks are excluded), which is exactly what
        # we want to surface. The final message (for usage/stop_reason) comes
        # from get_final_message(). No retry wrapper (would re-emit).
        def _do_stream() -> ProviderResult:
            parts: list[str] = []
            with _anthropic.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if text:
                        parts.append(text)
                        on_delta(text)
                final = stream.get_final_message()
            return ProviderResult(
                text="".join(parts),
                usage=_extract_usage("anthropic", final),
                finish_reason=getattr(final, "stop_reason", None),
                raw=final,
            )

        return _call_with_wall_cap(f"anthropic/{model}/stream", _do_stream)

    def _do_call():
        return _anthropic.messages.create(**kwargs)

    resp = _provider_call(f"anthropic/{model}", _do_call)
    text_parts = [
        block.text for block in resp.content
        if getattr(block, "type", None) == "text"
    ]
    return ProviderResult(
        text="".join(text_parts), usage=_extract_usage("anthropic", resp),
        finish_reason=getattr(resp, "stop_reason", None), raw=resp,
    )


class _StreamFailedBeforeOutput(RuntimeError):
    """The CLI stream-json subprocess failed without emitting any text (e.g.
    the bundled version rejects a streaming flag). Signals the caller it's
    safe to fall back to a plain non-streaming call — nothing was emitted, so
    the fallback won't double up."""


def _cli_stream_json(args: list[str], user_msg: str, on_delta: "StreamDelta") -> str:
    """Run the claude CLI in stream-json mode, emitting visible text deltas.

    Returns the full accumulated text. Raises ``_StreamFailedBeforeOutput`` if
    the subprocess fails before emitting anything (caller falls back to
    non-streaming); a plain RuntimeError if it dies AFTER partial output (the
    caller must NOT fall back then, or it'd re-emit). Parses defensively across
    a few event shapes so CLI output-format drift degrades to fewer deltas.
    """
    # Merge stderr INTO stdout. With --verbose the CLI can write more to stderr
    # than the ~64KB pipe buffer holds; since we drain stdout in a loop and only
    # read stderr after it, a separate stderr PIPE would fill, block the child,
    # stall the stdout loop, and deadlock until the wall-cap (leaking the proc,
    # because the kill() in finally is itself blocked in the loop). Interleaved
    # diagnostic lines aren't JSON, so the parser skips them; we keep the last
    # few for the error tail.
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    parts: list[str] = []
    noise_tail: list[str] = []  # recent non-JSON lines, for the error message

    def _emit_from(obj: dict) -> None:
        # Shape 1: partial-message stream_event wrapping a raw Anthropic event.
        ev = obj.get("event") if obj.get("type") == "stream_event" else None
        if isinstance(ev, dict):
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                parts.append(delta["text"])
                on_delta(delta["text"])
            return
        # Shape 2: a complete assistant message (no partial-message flag).
        if obj.get("type") == "assistant":
            msg = obj.get("message") or {}
            for blk in msg.get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                    parts.append(blk["text"])
                    on_delta(blk["text"])
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(user_msg)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                # Non-JSON diagnostic (stderr merged in / --verbose noise).
                noise_tail.append(line)
                del noise_tail[:-40]
                continue
            if isinstance(obj, dict):
                _emit_from(obj)
        rc = proc.wait(timeout=PROVIDER_TIMEOUT_SEC)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if rc != 0:
        tail = "\n".join(noise_tail)[-2000:]
        msg = f"claude CLI (stream-json) exit={rc}; output tail: {tail!r}"
        if not parts:
            raise _StreamFailedBeforeOutput(msg)
        raise RuntimeError(msg)
    return "".join(parts)


def _call_anthropic_cli(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Subprocess to ``claude`` / ``claude-ha`` and read the response.

    This is the subscription-auth path for Anthropic participants: it
    uses the OAuth session from ``claude /login`` (your Pro / Team plan
    quota) rather than the paid-per-token API. Picked automatically when
    ``CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT`` is ``auto`` (the default)
    and a ``claude`` binary is on PATH; force with ``=cli`` or fall back
    to the SDK path with ``=api``.

    ``claude-ha`` is preferred when present — it wraps the same CLI but
    fails over between accounts on usage-limit errors, so a long
    roundtable session that exhausts one Pro quota silently rolls onto
    the next without dropping the turn.

    The user message (transcript + orchestrator instruction) is piped
    via stdin rather than passed positionally. argv goes through
    ``execve(2)`` whose total size is capped by the kernel's ARG_MAX —
    nominally 2 MiB on mainline Linux but as low as ~130 KiB on some
    vendor ARM kernels (the dockge RK3588 host being the case that bit
    us). The rendered transcript + system prompt + flags routinely runs
    past that ceiling on a non-trivial audit, surfacing as
    ``OSError: Argument list too long`` partway through a long thread.
    Stdin has no such cap; pipes block on the writer instead. claude-ha
    forwards stdin to the backgrounded inner ``claude`` and replays the
    buffer across account rotations, so this path is safe even when a
    Pro plan tips over mid-call. Tools are disabled and session
    persistence is off — every roundtable turn is a one-shot stateless
    invocation that should never write to the local ``~/.claude/``
    projects dir.
    """
    if _CLAUDE_CLI is None:
        raise RuntimeError(
            "Anthropic CLI transport requested but no claude/claude-ha "
            "binary was found on PATH at server start."
        )
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    args = [
        _CLAUDE_CLI,
        "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        # Empty string disables all tools by default — we want a pure
        # text response from the participant, not an agent that tries to
        # read files or spawn its own roundtables. Web search is the
        # one allow-listed exception, opt-in per call.
        "--tools", "web_search" if web_search else "",
        # Don't leave a session row behind. Each ask is independent.
        "--no-session-persistence",
        # Defensive: an arbitrary user transcript could contain text that
        # looks like a slash command. Disable that surface entirely.
        "--disable-slash-commands",
    ]
    if effort:
        # CLI accepts low/medium/high/xhigh/max — we only ever pass the
        # normalised lowercase trio because that's what _normalise_effort
        # validates against.
        args.extend(["--effort", effort])
    # No positional prompt — user_msg goes via stdin (see docstring).

    if on_delta is not None:
        # Streamed path: re-run with stream-json + partial messages and parse
        # text deltas. If the bundled CLI rejects the streaming flags (older
        # version) the subprocess exits before emitting anything — fall back
        # to the plain capture below so synthesis still completes. A failure
        # AFTER emitting partial text re-raises (surfaced as an error event).
        stream_args = args + [
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
        ]
        try:
            out = _call_with_wall_cap(
                f"anthropic-cli/{model}/stream",
                lambda: _cli_stream_json(stream_args, user_msg, on_delta),
            )
            return ProviderResult(text=out)
        except _StreamFailedBeforeOutput as exc:
            # Streaming flags rejected (older CLI) and nothing was emitted —
            # safe to retry without streaming below.
            logger.warning(
                "anthropic-cli stream unsupported (%s); falling back to "
                "non-streaming capture", exc,
            )
            # Fall through to the non-streaming path below.

    def _do_call() -> str:
        proc = subprocess.run(
            args,
            input=user_msg,
            text=True,
            capture_output=True,
            timeout=PROVIDER_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            # Surface BOTH streams. claude-ha writes account-switching
            # diagnostics to stderr, but the inner `claude -p` can exit
            # nonzero with an EMPTY stderr and the real error on stdout —
            # which is exactly the silent `exit=1; stderr ''` that a
            # concurrent roundtable hits. Without stdout the failure is
            # undiagnosable.
            err = (proc.stderr or "")[-2000:]
            out = (proc.stdout or "")[-2000:]
            raise RuntimeError(
                f"claude CLI exit={proc.returncode}; stderr: {err!r}; stdout: {out!r}"
            )
        return proc.stdout

    out = _provider_call(f"anthropic-cli/{model}", _do_call)
    # CLI -p text mode reports no token usage; usage stays None here.
    return ProviderResult(text=out)


# ─── Permission-gated tool use (Layer 1) ─────────────────────────────────
#
# A caller (typically a webapp) can pass a ``ToolUseContext`` into
# ``roundtable_ask`` / ``roundtable_ask_parallel`` to enable real
# filesystem tools for Anthropic participants. Every tool call routes
# through ``permission_callback`` so the user is the gate — no
# pre-sandboxing required. When no context is passed, the existing
# zero-tools subprocess / SDK paths are used (preserving MCP-stdio
# behaviour where there is no UI to surface a prompt).
#
# This is intentionally Anthropic-only for Layer 1: Claude Code's
# bundled CLI already speaks Read/Grep/Glob and the agent SDK already
# exposes ``can_use_tool`` for permission interception. Adding the same
# capability to Gemini and OpenAI participants requires building a
# function-calling loop per provider and is left for Layer 2.

# Decision strings the callback returns. Mirrors claude-web's
# ``/api/permission/{id}`` decision values so the same browser flow
# resolves both main-chat and roundtable permissions.
PermissionDecision = str  # "allow" | "allow_session" | "deny"

# Synchronous callback. Runs in a worker thread spawned by the SDK's
# can_use_tool adapter, so the implementation is free to block on
# whatever cross-thread mechanism it likes (asyncio Future via
# run_coroutine_threadsafe, threading.Event, raw input, etc.).
PermissionCallback = Callable[
    [str, dict, str],  # participant_label, tool_name, tool_input
    PermissionDecision,
]


@dataclasses.dataclass
class ToolUseContext:
    """Caller-provided context that turns on real tool use for Anthropic
    participants. Pass to ``roundtable_ask`` / ``roundtable_ask_parallel``
    as ``tool_use_context=`` — None preserves the no-tools default.

    ``permission_callback`` runs once per tool invocation. Return one of
    ``"allow"`` / ``"allow_session"`` / ``"deny"``; the SDK is told allow
    or deny accordingly (allow_session is treated as allow on the
    roundtable side — any per-session allowlist behaviour is the
    caller's responsibility, since it depends on how the caller
    identifies sessions).

    ``working_directory`` is the cwd for the Claude subprocess. Without
    it, the subprocess inherits the host process's cwd — usually NOT
    what you want for a user-driven webapp, since it'd let the panel
    grep ``/home/matt``. When None (unbound thread), tool use is
    implicitly disabled even if a callback was provided — the safe
    default is to revert to the zero-tools path.

    ``allowed_tools`` is an OPTIONAL clamp. ``None`` (the default)
    means the participant inherits the full claude_code preset toolset
    — Bash, Edit, Write, Read, Grep, Glob, plus any tools the user's
    ``setting_sources=["user","project","local"]`` adds (MCP servers,
    custom skills, etc.). Every call still routes through the
    permission_callback, so "full toolset" doesn't mean "no gate" — it
    means "the gate is the user, not a pre-flight allowlist". Set
    explicitly to clamp the panel to a narrower surface (e.g. read-only
    audits would pass ``["Read", "Grep", "Glob"]``).

    ``max_turns`` caps the agent loop so a runaway exploration can't
    burn unbounded subscription quota / tokens.
    """
    permission_callback: PermissionCallback
    working_directory: Optional[Union[str, Path]] = None
    allowed_tools: Optional[list[str]] = None
    max_turns: int = 8


# ─── Thread-bound repo context (Goal 4) ──────────────────────────────────
#
# A thread can be bound to a working directory via ``roundtable_bind_repo``.
# The binding is stored as plain serializable fields (path + a string
# ``permission_policy``), so it survives the MCP JSON boundary that a
# Callable can't cross. At turn time ``_effective_tool_context`` resolves
# the binding into a ``ToolUseContext`` with a built-in permission callback
# derived from the policy — that's what lets repo-grounded turns work over
# stdio MCP, not just from the webapp (which can still pass its own
# interactive callback explicitly).

_READONLY_TOOLS = ["Read", "Grep", "Glob"]
_VALID_TOOL_POLICIES = {"readonly", "deny", "ask"}

# Optional allowlist of directory roots a thread may be bound to, from
# ``ROUNDTABLE_REPO_ROOTS`` (os.pathsep-separated). Empty = no allowlist
# configured; binding is then permitted anywhere (with a logged warning),
# which suits a single-user host but should be set on a shared deployment.
_REPO_ROOT_ALLOWLIST = [
    str(Path(p).resolve())
    for p in os.environ.get("ROUNDTABLE_REPO_ROOTS", "").split(os.pathsep)
    if p.strip()
]


def _path_under_allowlist(path: str) -> bool:
    """True if ``path`` is within an allowlisted root (or no allowlist set)."""
    if not _REPO_ROOT_ALLOWLIST:
        return True
    rp = Path(path).resolve()
    for root in _REPO_ROOT_ALLOWLIST:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _validate_bindable_dir(working_directory: str) -> Path:
    """Resolve ``working_directory`` and enforce the bindability rules shared
    by ``roundtable_bind_repo`` and ``roundtable_bind_diff``: it must exist,
    be a directory, and sit under the ``ROUNDTABLE_REPO_ROOTS`` allowlist
    (when one is configured). Returns the resolved path."""
    resolved = Path(working_directory).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"working_directory {working_directory!r} is not an existing "
            f"directory (resolved to {resolved})."
        )
    if not _path_under_allowlist(str(resolved)):
        raise ValueError(
            f"working_directory {resolved} is outside ROUNDTABLE_REPO_ROOTS "
            f"({_REPO_ROOT_ALLOWLIST}). Add it to the allowlist to bind here."
        )
    return resolved


def _readonly_permission_callback(
    participant_label: str, tool_name: str, tool_input: dict,
) -> PermissionDecision:
    """Allow only read-only tools; deny anything that could mutate state."""
    return "allow" if tool_name in _READONLY_TOOLS else "deny"


def _deny_all_permission_callback(
    participant_label: str, tool_name: str, tool_input: dict,
) -> PermissionDecision:
    return "deny"


def _policy_callback(policy: str) -> Optional[PermissionCallback]:
    """Map a stored string policy to a built-in permission callback.

    ``readonly`` and ``deny`` resolve to self-contained callbacks. ``ask``
    needs an interactive (e.g. browser) callback supplied by the caller —
    there is no built-in for it, so this returns None and the resolver
    falls back to the no-tools path rather than silently auto-allowing.
    """
    if policy == "readonly":
        return _readonly_permission_callback
    if policy == "deny":
        return _deny_all_permission_callback
    return None  # "ask" — must be supplied externally


def _effective_tool_context(
    thread_id: int, explicit: Optional[ToolUseContext] = None,
) -> Optional[ToolUseContext]:
    """Resolve the ToolUseContext for a turn from the thread's repo binding.

    An ``explicit`` context with a working_directory always wins (the
    webapp passing its own interactive callback). Otherwise we look up the
    thread's stored binding and synthesize a context whose callback comes
    from the string policy. Returns None (no tools) when there is no
    binding, or when the policy is ``ask`` but no interactive callback was
    supplied — never auto-allow a gated policy.
    """
    if explicit is not None and explicit.working_directory is not None:
        return explicit
    binding = roundtable_repo_context(thread_id)
    if binding is None:
        return explicit
    policy = binding["permission_policy"]
    callback = (
        explicit.permission_callback
        if explicit is not None and explicit.permission_callback is not None
        else _policy_callback(policy)
    )
    if callback is None:
        logger.warning(
            "thread %s repo policy=%r needs an interactive permission "
            "callback that wasn't supplied; tool use disabled for this turn.",
            thread_id, policy,
        )
        return None
    allowed = binding["allowed_tools"]
    if allowed is None and policy == "readonly":
        allowed = list(_READONLY_TOOLS)
    return ToolUseContext(
        permission_callback=callback,
        working_directory=binding["working_directory"],
        allowed_tools=allowed,
    )


# Resolved at import time so a missing claude_agent_sdk fails loudly the
# first time a caller actually passes a ToolUseContext, rather than
# poisoning normal no-tools routing.
_AGENT_SDK_IMPORT_ERROR: Optional[Exception] = None


def _import_agent_sdk():
    """Lazy-import the agent SDK. Returns ``(module, options_cls)`` or
    raises a RuntimeError pointing the user at the install path.

    The SDK isn't a hard dependency of roundtable-mcp — the MCP stdio
    server runs fine without it (no UI to surface permission prompts).
    Only callers that want tool-use need it on their PATH (typically
    claude-web's venv, which installs it for the main chat anyway).
    """
    global _AGENT_SDK_IMPORT_ERROR
    if _AGENT_SDK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "claude_agent_sdk import failed: "
            f"{type(_AGENT_SDK_IMPORT_ERROR).__name__}: "
            f"{_AGENT_SDK_IMPORT_ERROR}. Install in the host venv to "
            "enable permission-gated tool use for Anthropic participants."
        )
    try:
        import claude_agent_sdk as sdk
    except Exception as exc:  # noqa: BLE001 — surface install hint
        _AGENT_SDK_IMPORT_ERROR = exc
        raise RuntimeError(
            "claude_agent_sdk is not installed. Install with "
            "'pip install claude-agent-sdk' in the host venv to enable "
            "permission-gated tool use for Anthropic participants."
        ) from exc
    return sdk


def _call_anthropic_sdk_with_tools(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], web_search: bool,
    tool_use_context: ToolUseContext, participant_label: str,
) -> ProviderResult:
    """Run an Anthropic participant turn via the agent SDK with real tools.

    Uses ``claude_agent_sdk.query`` (one-shot, stateless — the right
    shape for a single roundtable turn) plus a ``can_use_tool``
    permission gate so every tool call is approved by the user
    out-of-band before it runs.

    The bundled CLI under the SDK reuses the same OAuth credentials as
    the subprocess CLI path, so this stays subscription-billed on Matt's
    setup. If ``ANTHROPIC_API_KEY`` is set in env the SDK will prefer
    it; callers who want to force subscription auth should strip the
    key from the inherited env (claude-web does this for its per-user
    credential slots).

    Tool results land in the SDK's internal message stream; we only
    expose the final assistant ``TextBlock`` content to the roundtable
    transcript. The fact that Read/Grep/Glob ran is observable via the
    permission_callback firing (claude-web turns those into SSE events
    the browser displays). Tool I/O is intentionally NOT written into
    the roundtable transcript — it'd balloon the transcript with file
    contents that the next participant's snapshot doesn't need.

    Web search is honoured the same way as the non-tools paths: add the
    web_search tool to the allowlist. The model decides whether to use
    it. (We don't intercept web_search with a permission prompt — it's a
    network call to Anthropic's hosted index, not local filesystem
    access; behaves the same as the existing ``--tools web_search`` CLI
    flag.)
    """
    sdk = _import_agent_sdk()

    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"

    cwd = tool_use_context.working_directory
    cwd_str = str(cwd) if cwd is not None else None
    logger.info(
        "anthropic-sdk-tools enter participant=%s model=%s cwd=%s "
        "effort=%s web_search=%s allowed_tools=%s",
        participant_label, model, cwd_str, effort, web_search,
        tool_use_context.allowed_tools,
    )

    perm_cb = tool_use_context.permission_callback

    async def _can_use_tool(tool_name, tool_input, context):
        # Run the user's sync callback on a worker thread so it's free
        # to block on cross-thread machinery (e.g. asyncio.run_coroutine_
        # threadsafe back into a webapp's main loop) without freezing
        # the SDK's event loop.
        logger.info(
            "anthropic-sdk-tools can_use_tool fired participant=%s tool=%s",
            participant_label, tool_name,
        )
        try:
            decision = await asyncio.to_thread(
                perm_cb, participant_label, tool_name, tool_input,
            )
        except Exception as exc:  # noqa: BLE001 — callback faults → deny
            logger.warning(
                "anthropic-sdk-tools/%s permission callback raised "
                "%s: %s (denying)",
                participant_label, type(exc).__name__, exc,
            )
            return sdk.PermissionResultDeny(
                message=f"Permission callback errored: {type(exc).__name__}",
            )
        decision_str = (decision or "deny").strip().lower()
        if decision_str in ("allow", "allow_session"):
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message="User denied permission.")

    options_kwargs: dict = {
        # Mirror the main claude-web chat's options surface so a
        # roundtable Claude has the SAME capability as a normal Claude
        # Code session — full tool set, MCP servers, hooks, skills,
        # CLAUDE.md memory — gated through the same can_use_tool
        # permission UI. The roundtable framing is appended onto the
        # claude_code preset so the model knows it's a panellist while
        # still having all tool docs.
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
        "permission_mode": "default",
        "can_use_tool": _can_use_tool,
        # The SDK prefers its bundled CLI snapshot over PATH, silently
        # pinning runs to the CLI version the SDK shipped with. Prefer
        # the auto-updated system CLI; None (filtered below) falls back
        # to the bundle for installs without one.
        "cli_path": shutil.which("claude"),
        # No allowed_tools cap — the user's settings.json + can_use_tool
        # gate are the source of truth. Capping it here would silently
        # hide tools the user has explicitly allowed in their settings.
        "setting_sources": ["user", "project", "local"],
        "skills": "all",
        "model": model,
        "max_turns": tool_use_context.max_turns,
        # Adaptive thinking on medium/high; same policy as the other
        # Anthropic paths. ThinkingConfigAdaptive is a TypedDict, NOT a
        # dataclass — the bare ``ThinkingConfigAdaptive()`` form yields
        # an empty ``{}`` and the SDK's _build_command then KeyErrors
        # on the missing ``type`` discriminator. Pass the literal dict.
        "thinking": (
            {"type": "adaptive"} if effort in {"medium", "high"} else None
        ),
    }
    # If the caller explicitly opted into a narrower toolset via
    # ``tool_use_context.allowed_tools``, honour it. The default
    # (``allowed_tools=None`` → ``effective_allowed_tools()`` returns
    # the read-only trio) is no longer applied here — that was Layer 1
    # paranoia. The user's settings + the per-call permission card are
    # the trust boundary now. Only callers who explicitly want to clamp
    # the panel get clamped.
    if tool_use_context.allowed_tools is not None:
        options_kwargs["allowed_tools"] = list(tool_use_context.allowed_tools)
        if web_search and "WebSearch" not in options_kwargs["allowed_tools"]:
            options_kwargs["allowed_tools"].append("WebSearch")
    if cwd_str is not None:
        options_kwargs["cwd"] = cwd_str
    if effort:
        options_kwargs["effort"] = effort
    if _ANTHROPIC_TRANSPORT != "api":
        # auto/cli mean subscription intent, but the SDK child prefers an
        # inherited ANTHROPIC_API_KEY over OAuth — silently billing the API
        # per-token (and failing every turn outright when the key's account
        # has no credits: the CLI reports it as is_error=true subtype=success,
        # which the SDK masks into "error result: success"). options.env can
        # override but not remove, so blank the key; the CLI treats an empty
        # value as unset and falls back to OAuth.
        options_kwargs["env"] = {"ANTHROPIC_API_KEY": ""}

    options = sdk.ClaudeAgentOptions(
        **{k: v for k, v in options_kwargs.items() if v is not None},
    )

    async def _prompt_iter():
        # claude_agent_sdk.query() refuses can_use_tool with a plain
        # string prompt — streaming mode (AsyncIterable[dict]) is
        # mandatory when a permission gate is attached. Content MUST be
        # a list of typed blocks (not a bare string) — the SDK iterates
        # over message.content expecting dicts with a 'type' key. Bare
        # string yields a KeyError mid-iteration.
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_msg}],
            },
            "parent_tool_use_id": None,
            "session_id": "roundtable-turn",
        }

    async def _drive() -> str:
        text_parts: list[str] = []
        async for msg in sdk.query(prompt=_prompt_iter(), options=options):
            if isinstance(msg, sdk.AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, sdk.TextBlock):
                        text_parts.append(blk.text)
        return "\n".join(text_parts).strip()

    def _do_call() -> str:
        # One event loop per call. _call_anthropic_sdk_with_tools is
        # invoked from worker threads (roundtable_ask_parallel uses a
        # ThreadPoolExecutor; roundtable_ask itself runs in whichever
        # thread the caller dispatched on). asyncio.run() creates and
        # tears down the loop cleanly, leaving no state behind.
        return asyncio.run(asyncio.wait_for(_drive(), timeout=PROVIDER_TIMEOUT_SEC))

    out = _provider_call(f"anthropic-sdk-tools/{model}", _do_call)
    # TODO(usage): the agent SDK surfaces usage on ResultMessage; capture it
    # when the tool-trace plumbing lands (Goal 1 / Phase 3). None for now.
    return ProviderResult(text=out)


def _call_anthropic_router(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
    participant_label: str = "",
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Pick CLI vs SDK based on the transport setting and what's available.

    ``auto`` prefers CLI when a binary is on PATH (cheaper — uses
    subscription quota instead of per-token API) and falls back to SDK.
    The other modes are explicit overrides for users who want to pin a
    transport regardless of what's installed.

    When ``tool_use_context`` is provided AND it has a working_directory
    (a project-bound thread), the SDK-with-tools path takes precedence
    regardless of transport — that's the path with the can_use_tool
    permission hook, which is the whole point of passing a context. A
    context without a working_directory falls back to the no-tools path
    (the conservative default: no ambient filesystem access without an
    explicit sandbox root, even if the caller has a callback ready).
    """
    if (tool_use_context is not None
            and tool_use_context.working_directory is not None):
        # Tool-use turns aren't streamed (the agent loop's text arrives
        # interleaved with tool calls); on_delta is ignored here by design.
        return _call_anthropic_sdk_with_tools(
            model, system_prompt, transcript, instruction, effort, web_search,
            tool_use_context, participant_label,
        )
    if _ANTHROPIC_TRANSPORT == "cli":
        return _call_anthropic_cli(
            model, system_prompt, transcript, instruction, effort, web_search,
            on_delta=on_delta,
        )
    if _ANTHROPIC_TRANSPORT == "api":
        if _anthropic is None:
            raise RuntimeError(
                "transport=api but ANTHROPIC_API_KEY is not set."
            )
        return _call_anthropic(
            model, system_prompt, transcript, instruction, effort, web_search,
            on_delta=on_delta,
        )
    # auto: prefer CLI (subscription) if available, else SDK (API).
    if _CLAUDE_CLI is not None:
        return _call_anthropic_cli(
            model, system_prompt, transcript, instruction, effort, web_search,
            on_delta=on_delta,
        )
    if _anthropic is not None:
        # Falling back to per-token API billing because no claude/claude-ha
        # CLI was on PATH at startup. Warn once — silently metering the API
        # key when the user expected their Pro/Team subscription is exactly
        # the footgun this branch is here to surface.
        global _warned_auto_api_fallback
        if not _warned_auto_api_fallback:
            _warned_auto_api_fallback = True
            logger.warning(
                "Anthropic transport=auto but no claude/claude-ha CLI on "
                "PATH — falling back to the ANTHROPIC_API_KEY SDK path, "
                "which bills per-token instead of using the Pro/Team "
                "subscription. Put the claude CLI on the server's PATH to "
                "use the subscription, or set "
                "CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT=cli to fail loudly."
            )
        return _call_anthropic(
            model, system_prompt, transcript, instruction, effort, web_search,
            on_delta=on_delta,
        )
    raise RuntimeError(
        "No Anthropic transport available: neither claude CLI on PATH "
        "nor ANTHROPIC_API_KEY in env."
    )


# ─── Public operations ───────────────────────────────────────────────────
#
# These are the functions a webapp or MCP wrapper calls. They are plain
# Python — no FastMCP, no HTTP — so they're importable from any context.
# Each one is fully self-contained: validation, DB writes, provider calls,
# error surfacing. Return shapes are JSON-serialisable dicts / strings so
# the same function can back an MCP tool, a FastAPI endpoint, or a CLI.


def roundtable_create(
    topic: str, participants: list[str] = [], house_rules: str = "",
    context: str = "",
) -> dict:
    """Create a new roundtable thread.

    ``topic`` is a short description shown in each participant's system
    prompt — keep it focused so participants understand the brief. The
    optional ``participants`` list pre-registers expected speakers
    (e.g. ``["gemini-pro", "gpt-5"]``); these names appear in every
    participant's system prompt as "other AI participants in this
    roundtable", which helps them address each other by name. Any
    participant can still be asked at any time even if not listed.

    ``house_rules`` is an optional output-format contract folded into
    every participant's system prompt for this thread — e.g. "Reply with
    Critical / High / Medium issues, file:line refs, no preamble. Cite
    the code, don't quote it back." Saves restating the format every
    ask. Leave empty for a free-form discussion.

    ``context`` is an optional standing context pack (constraints,
    conventions, prior decisions) injected into every participant's system
    prompt — see ``roundtable_set_context``. Convenience for setting it at
    create time; it can also be set or replaced later.

    Returns ``{"thread_id", "topic", "participants", "house_rules"}`` plus
    ``context_bytes``/``context_truncated`` when a context pack was set.
    """
    unknown = [p for p in participants if p not in PARTICIPANTS]
    if unknown:
        raise ValueError(
            f"Unknown participants: {unknown}. "
            f"Known: {sorted(PARTICIPANTS.keys())}"
        )
    unavailable = [
        p for p in participants if not _participant_provider_available(p)
    ]
    if unavailable:
        raise ValueError(
            f"Participants {unavailable} need an API key that wasn't set "
            f"in the server env."
        )
    if house_rules and len(house_rules) > _HOUSE_RULES_CHAR_CAP:
        raise ValueError(
            f"house_rules is {len(house_rules)} chars, over the "
            f"{_HOUSE_RULES_CHAR_CAP} cap. The system prompt rides argv under "
            f"~130k ARG_MAX alongside the context pack — trim it, or move "
            f"standing context into a context pack (roundtable_set_context)."
        )
    with _db_lock:
        cur = _conn().execute(
            "INSERT INTO threads(topic, participants_json, created_at, house_rules) "
            "VALUES(?, ?, ?, ?)",
            (topic, json.dumps(participants), time.time(), house_rules or None),
        )
        thread_id = int(cur.lastrowid)
    created = {
        "thread_id": thread_id,
        "topic": topic,
        "participants": participants,
        "house_rules": house_rules,
    }
    if context.strip():
        ctx = _store_context(thread_id, context, "inline")
        created["context_bytes"] = ctx["bytes"]
        created["context_truncated"] = ctx["truncated"]
    return created


def roundtable_bind_repo(
    thread_id: int, working_directory: str,
    permission_policy: str = "readonly",
    allowed_tools: Optional[list[str]] = None,
) -> dict:
    """Bind a thread to a working directory so participants can read the repo.

    Once bound, ``roundtable_ask`` turns for tool-capable participants run
    with real filesystem tools rooted at ``working_directory``, every call
    gated by the policy. This is what lets the panel audit ground truth
    instead of pasted excerpts — and it works over stdio MCP because the
    binding is plain serializable data, resolved into a permission callback
    server-side (see ``_effective_tool_context``).

    ``permission_policy`` is one of:
      - ``"readonly"`` (default): allow only Read/Grep/Glob under the root.
      - ``"deny"``: register the binding but allow no tools (useful to
        stage a path, or to hard-stop tool use on a thread).
      - ``"ask"``: defer to an interactive callback the *caller* supplies
        (e.g. the webapp's browser prompt). Over plain MCP, where no such
        callback exists, ``ask`` turns run with tools disabled rather than
        auto-allowing — bind ``readonly`` if you want MCP reads.

    ``allowed_tools`` optionally clamps the toolset further; ``None`` uses
    the policy default (the read-only trio for ``readonly``).

    Anthropic participants always consume the binding (the agent-SDK tool
    path); Gemini/OpenAI consume it via their own permission-gated
    function-calling loops when ``CLAUDE_ROUNDTABLE_PANEL_TOOLS`` is set.

    Returns the stored binding dict.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — cannot bind a repo.")
    if permission_policy not in _VALID_TOOL_POLICIES:
        raise ValueError(
            f"permission_policy must be one of {sorted(_VALID_TOOL_POLICIES)}; "
            f"got {permission_policy!r}"
        )
    resolved = _validate_bindable_dir(working_directory)
    if not _REPO_ROOT_ALLOWLIST:
        logger.warning(
            "binding thread %s to %s with no ROUNDTABLE_REPO_ROOTS allowlist "
            "configured — any path is bindable. Set the env var on a shared "
            "deployment.", thread_id, resolved,
        )
    with _db_lock:
        _conn().execute(
            "INSERT OR REPLACE INTO repo_contexts(thread_id, working_directory, "
            "allowed_tools_json, permission_policy, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                thread_id, str(resolved),
                json.dumps(allowed_tools) if allowed_tools is not None else None,
                permission_policy, time.time(),
            ),
        )
    return {
        "thread_id": thread_id,
        "working_directory": str(resolved),
        "permission_policy": permission_policy,
        "allowed_tools": allowed_tools,
    }


def roundtable_repo_context(thread_id: int) -> Optional[dict]:
    """Return the thread's repo binding, or None if it isn't bound.

    Returns ``{"thread_id", "working_directory", "permission_policy",
    "allowed_tools"}`` — ``allowed_tools`` is the stored clamp or None.
    """
    with _db_lock:
        row = _conn().execute(
            "SELECT working_directory, allowed_tools_json, permission_policy "
            "FROM repo_contexts WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "thread_id": thread_id,
        "working_directory": row[0],
        "allowed_tools": json.loads(row[1]) if row[1] else None,
        "permission_policy": row[2],
    }


# ─── Standing context pack ───────────────────────────────────────────────


def _store_context(thread_id: int, content: str, source: str) -> dict:
    """Store (replacing any existing) the standing context pack for a thread.

    The pack is injected verbatim into every participant's system prompt by
    ``_build_system_prompt``, so it must stay within ``CONTEXT_PACK_CHAR_CAP``
    — an oversized pack is middle-truncated (with a marker) rather than
    rejected or sent past the argv/context ceiling. Returns
    ``{"thread_id", "bytes", "truncated", "source"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — cannot set context.")
    stored = _truncate_message_body(
        {"content": content}, CONTEXT_PACK_CHAR_CAP,
    )["content"]
    with _db_lock:
        _conn().execute(
            "INSERT OR REPLACE INTO thread_context(thread_id, content, source, "
            "created_at) VALUES(?, ?, ?, ?)",
            (thread_id, stored, source, time.time()),
        )
    return {
        "thread_id": thread_id,
        "bytes": len(stored),
        "truncated": stored != content,
        "source": source,
    }


def roundtable_set_context(thread_id: int, content: str) -> dict:
    """Store a standing context pack for a thread — the constraints,
    conventions, and prior decisions only the orchestrator holds — injected
    into EVERY participant's system prompt so the panel isn't blind to them.

    Use it so Gemini / GPT propose designs and findings that respect the
    project's real rules (e.g. "auth is Keycloak SSO via oauth2-proxy",
    "never add indexers outside Prowlarr") instead of generic best practice
    that contradicts them. Set it once per thread: the pack is stable across
    turns and rides the cached prompt prefix, so it doesn't re-bill every
    ask. Calling again replaces it.

    Keep it curated — relevant constraints, not a memory dump. An oversized
    pack is middle-truncated to the context char cap. Returns
    ``{"thread_id", "bytes", "truncated", "source"}``.
    """
    return _store_context(thread_id, content, "inline")


def roundtable_bind_context(thread_id: int, paths: list[str]) -> dict:
    """Assemble a context pack by reading the named doc files and store it
    like ``roundtable_set_context``.

    For pointing the panel at standing docs without pasting them — a
    conventions file, a design doc. Each path must resolve under
    ``ROUNDTABLE_REPO_ROOTS`` (same allowlist as ``roundtable_bind_repo``).
    Files are concatenated with ``=== <path> ===`` headers and the result is
    capped / truncated like set_context. Returns
    ``{"thread_id", "files", "bytes", "truncated"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — cannot bind context.")
    if not paths:
        raise ValueError("paths is empty — nothing to bind.")
    sections: list[str] = []
    read_paths: list[str] = []
    for p in paths:
        resolved = Path(p).expanduser().resolve()
        if not _path_under_allowlist(str(resolved)):
            raise ValueError(
                f"context file {resolved} is outside ROUNDTABLE_REPO_ROOTS "
                f"({_REPO_ROOT_ALLOWLIST}). Add it to the allowlist to read it."
            )
        if not resolved.is_file():
            raise ValueError(
                f"context file {p!r} is not an existing file "
                f"(resolved to {resolved})."
            )
        if _is_excluded_repo_path(resolved):
            raise ValueError(
                f"context file {resolved} looks like VCS metadata or a secret "
                f"file; refusing to send it to external panelists."
            )
        # Bound the per-file read: the pack is clamped to CONTEXT_PACK_CHAR_CAP
        # regardless, so pulling more than that from any one file is wasted
        # work — and a guard against a stray oversized file.
        with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(CONTEXT_PACK_CHAR_CAP + 1)
        sections.append(f"=== {resolved} ===\n{text}")
        read_paths.append(str(resolved))
    result = _store_context(thread_id, "\n\n".join(sections), "files")
    return {
        "thread_id": thread_id,
        "files": read_paths,
        "bytes": result["bytes"],
        "truncated": result["truncated"],
    }


def roundtable_context(thread_id: int) -> Optional[dict]:
    """Return the thread's stored context pack, or None if unset.

    Returns ``{"thread_id", "content", "source", "bytes"}``. Mirrors
    ``roundtable_repo_context``.
    """
    with _db_lock:
        row = _conn().execute(
            "SELECT content, source FROM thread_context WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "thread_id": thread_id,
        "content": row[0],
        "source": row[1],
        "bytes": len(row[0] or ""),
    }


# ─── GitHub repo binding ─────────────────────────────────────────────────

_GITHUB_CLONE_ROOT = STATE_DIR / "github"
# Refuse to bind a working tree larger than this. A depth-1 clone of a sane
# repo is small; this stops an accidental monorepo from blowing up reads.
_BIND_GITHUB_MAX_BYTES = int(
    os.environ.get("CLAUDE_ROUNDTABLE_BIND_GITHUB_MAX_BYTES", str(200 * 1024 * 1024))
)
_GIT_CLONE_TIMEOUT_SEC = int(
    os.environ.get("CLAUDE_ROUNDTABLE_GIT_CLONE_TIMEOUT_SEC", "180")
)
_GITHUB_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _git(args: list[str], env: Optional[dict] = None) -> str:
    """Run a git/gh command; return stdout, raise with stderr on failure.

    Output is captured (never written to stdout — that's the MCP JSON-RPC
    channel) and network ops are bounded by ``_GIT_CLONE_TIMEOUT_SEC``.
    ``env`` is merged over the inherited environment — used to pin
    GIT_ALLOW_PROTOCOL / GIT_TERMINAL_PROMPT on clones.
    """
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=_GIT_CLONE_TIMEOUT_SEC,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(args[:3])} …` failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout


def _github_clone_url(repo: str) -> str:
    """Resolve an ``owner/name`` shorthand to a GitHub HTTPS URL, or accept an
    explicit ``https://`` or ``file://`` git URL. Other transports are refused:
    ``ssh``/``git@`` (host-key hang + key/SSRF risk), plain ``http`` (SSRF), and
    git's ``ext::``/``fd::`` helpers (command execution). ``file://`` is allowed
    — it's no broader than ``roundtable_bind_repo`` to a local path."""
    repo = repo.strip()
    if "://" in repo or repo.startswith("git@"):
        if not (repo.startswith("https://") or repo.startswith("file://")):
            raise ValueError(
                f"only https:// or file:// git URLs are allowed; got {repo!r} "
                f"(ssh/git@, http, and ext::/fd:: transports are refused)"
            )
        return repo
    if _GITHUB_SHORTHAND_RE.match(repo):
        owner, name = repo.split("/", 1)
        name = name[:-4] if name.endswith(".git") else name
        return f"https://github.com/{owner}/{name}.git"
    raise ValueError(
        f"repo must be 'owner/name' or an https:// / file:// git URL; got {repo!r}"
    )


def _rmtree_force(path: Path) -> None:
    """Remove a tree, including read-only files. Git marks loose objects and
    pack files read-only; a plain ``shutil.rmtree(..., ignore_errors=True)``
    silently leaves them on Windows (read-only files can't be unlinked there),
    so a stripped ``.git`` reappears in the working tree and clone dirs can't
    be cleaned up. The handler clears the read-only bit and retries, swallowing
    anything it still can't remove so exception-cleanup callers never raise."""
    def _on_error(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    try:
        shutil.rmtree(path, onexc=_on_error)
    except TypeError:  # Python < 3.12 spells it onerror, not onexc
        shutil.rmtree(path, onerror=_on_error)


def roundtable_bind_github(thread_id: int, repo: str, ref: str = "HEAD") -> dict:
    """Shallow-clone a GitHub repo (or any git URL) and bind it read-only.

    Turns "panel, review owner/name" into one call: every participant then
    reads the same ground truth instead of a pasted excerpt. ``repo`` is
    ``"owner/name"`` (cloned via ``gh`` when it's installed, so private repos
    work; otherwise the public HTTPS URL) or any git-cloneable URL, including
    ``file://`` for local repos and tests. ``ref`` is a branch, tag, or
    commit SHA; ``"HEAD"`` (default) takes the remote's default branch.

    Clones depth-1 under the state dir, strips ``.git`` (so the panel can't
    read remote URLs or stored credentials), enforces a working-tree size
    cap, then registers a ``readonly`` binding via ``roundtable_bind_repo``.
    A failed clone raises rather than leaving the panel silently ungrounded.

    Returns ``{"thread_id", "repo", "ref", "commit_sha",
    "working_directory", "file_count"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — cannot bind a repo.")

    slug = re.sub(r"[^A-Za-z0-9._-]", "_", repo.strip())[:80]
    dest = _GITHUB_CLONE_ROOT / f"t{thread_id}-{slug}"
    if dest.exists():
        _rmtree_force(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    use_gh = bool(_GITHUB_SHORTHAND_RE.match(repo.strip())) and shutil.which("gh")
    # Pin the transport allowlist (https/file only) and disable credential
    # prompts so a clone can't reach a transport helper (ext::/fd::), probe
    # internal hosts, or hang on an auth prompt. core.symlinks=false stops a
    # hostile repo writing an outward symlink into the worktree.
    clone_env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:file"}
    if ref and ref.startswith("-"):
        raise ValueError(f"invalid ref {ref!r}")
    try:
        if use_gh:
            _git(["gh", "repo", "clone", repo.strip(), str(dest), "--",
                  "--depth", "1", "--config", "core.symlinks=false"], env=clone_env)
        else:
            _git(
                ["git", "-c", "core.symlinks=false", "clone", "--depth", "1",
                 "--", _github_clone_url(repo), str(dest)],
                env=clone_env,
            )
        if ref and ref != "HEAD":
            # fetch+checkout handles a branch, tag, OR commit SHA uniformly.
            # ref can't start with '-' (guarded above) so it can't pose as an
            # option in operand position.
            _git(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref], env=clone_env)
            _git(["git", "-C", str(dest), "checkout", "FETCH_HEAD"], env=clone_env)
        commit_sha = _git(["git", "-C", str(dest), "rev-parse", "HEAD"]).strip()
    except Exception:
        _rmtree_force(dest)
        raise

    size = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    if size > _BIND_GITHUB_MAX_BYTES:
        _rmtree_force(dest)
        raise ValueError(
            f"cloned working tree is {size} bytes, over the "
            f"{_BIND_GITHUB_MAX_BYTES}-byte cap (raise "
            f"CLAUDE_ROUNDTABLE_BIND_GITHUB_MAX_BYTES if intended)."
        )
    _rmtree_force(dest / ".git")
    file_count = sum(1 for p in dest.rglob("*") if p.is_file())

    roundtable_bind_repo(thread_id, str(dest), permission_policy="readonly")
    return {
        "thread_id": thread_id, "repo": repo, "ref": ref,
        "commit_sha": commit_sha, "working_directory": str(dest),
        "file_count": file_count,
    }


# ─── Working-diff binding ────────────────────────────────────────────────

_DIFF_ARTIFACT_NAME = "working-diff"
# Total cap on the captured diff text; middle-truncated beyond it. Sits
# under PROMPT_CHAR_CAP so the artifact announcement can't starve the
# rest of the transcript.
_BIND_DIFF_MAX_CHARS = int(
    os.environ.get("CLAUDE_ROUNDTABLE_BIND_DIFF_MAX_CHARS", "200000")
)
# Untracked files are each rendered as a /dev/null pseudo-diff (one git
# invocation apiece); cap how many so a node_modules-style spray can't
# stall the call.
_BIND_DIFF_MAX_UNTRACKED = 50


def roundtable_bind_diff(
    thread_id: int, working_directory: str, base: str = "HEAD",
) -> dict:
    """Capture the repo's working diff as a versioned artifact and bind the
    repo read-only — one call to put "review my uncommitted change" in
    front of the panel.

    Runs ``git diff <base>`` in ``working_directory`` (``HEAD`` covers
    staged + unstaged; any commit-ish works, e.g. ``main`` to review a
    whole branch), appends each untracked file as a ``/dev/null``
    pseudo-diff, and stores the result via ``roundtable_set_artifact`` as
    ``'working-diff'`` — so re-capturing after edits announces what changed
    since the last capture. Also registers a ``readonly`` repo binding
    (replacing any existing one, like ``roundtable_bind_github``) so
    participants can open the full files behind the hunks.

    Files matching the secret/VCS exclusion patterns (``.env``, key
    material, ``.git``) are dropped from both the tracked diff and the
    untracked list — same rule as every other panel-facing read.

    Raises ``ValueError`` when the tree is clean against ``base`` — a
    silent empty artifact would read as "reviewed, no changes".

    Returns ``{"thread_id", "base", "commit_sha", "files_changed",
    "files_excluded", "untracked_included", "untracked_skipped",
    "artifact_version", "chars", "truncated"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — cannot bind a diff.")
    if base.startswith("-"):
        raise ValueError(f"invalid base ref {base!r}")
    resolved = _validate_bindable_dir(working_directory)

    def _g(*args: str) -> str:
        return _git(["git", "-C", str(resolved), *args])

    try:
        _g("rev-parse", "--is-inside-work-tree")
        commit_sha = _g("rev-parse", "HEAD").strip()
    except RuntimeError as exc:
        raise ValueError(
            f"{resolved} is not a git work tree with at least one commit: {exc}"
        ) from exc

    changed = [f for f in _g("diff", "--name-only", base, "--").splitlines() if f]
    excluded = [f for f in changed if _is_excluded_repo_path(Path(f))]
    if excluded:
        # Pathspec-exclude the secret files rather than diffing then
        # scrubbing text — the diff never contains their contents at all.
        diff_text = _g(
            "diff", base, "--", ".",
            *[f":(exclude){f}" for f in excluded],
        )
    else:
        diff_text = _g("diff", base)

    untracked = [
        f for f in _g("ls-files", "--others", "--exclude-standard").splitlines()
        if f and not _is_excluded_repo_path(Path(f))
    ]
    untracked_shown = untracked[:_BIND_DIFF_MAX_UNTRACKED]
    untracked_blocks: list[str] = []
    for f in untracked_shown:
        # --no-index exits 1 when the files differ (always true against
        # /dev/null), so this can't go through _git's zero-exit check.
        proc = subprocess.run(
            ["git", "-C", str(resolved), "diff", "--no-index", "--",
             "/dev/null", f],
            capture_output=True, text=True, timeout=_GIT_CLONE_TIMEOUT_SEC,
        )
        if proc.returncode in (0, 1) and proc.stdout:
            untracked_blocks.append(proc.stdout)

    if not diff_text.strip() and not untracked_blocks:
        raise ValueError(
            f"working tree at {resolved} is clean against {base!r} — "
            f"nothing to bind. Use roundtable_bind_repo for a plain binding."
        )

    parts = [
        f"# Working diff of {resolved.name} vs {base} "
        f"(HEAD = {commit_sha[:12]})",
    ]
    if excluded:
        parts.append(
            f"# {len(excluded)} changed file(s) excluded as secret/VCS "
            f"paths: {', '.join(excluded)}"
        )
    if len(untracked) > len(untracked_shown):
        parts.append(
            f"# {len(untracked) - len(untracked_shown)} untracked file(s) "
            f"beyond the {_BIND_DIFF_MAX_UNTRACKED}-file cap omitted"
        )
    if diff_text.strip():
        parts.append(diff_text.rstrip())
    if untracked_blocks:
        parts.append("# Untracked files (shown as additions):")
        parts.extend(b.rstrip() for b in untracked_blocks)
    content = "\n\n".join(parts)
    capped = _truncate_message_body(
        {"content": content}, _BIND_DIFF_MAX_CHARS,
    )["content"]

    roundtable_bind_repo(thread_id, str(resolved), permission_policy="readonly")
    art = roundtable_set_artifact(thread_id, _DIFF_ARTIFACT_NAME, capped)
    return {
        "thread_id": thread_id,
        "base": base,
        "commit_sha": commit_sha,
        "files_changed": len(changed) - len(excluded),
        "files_excluded": len(excluded),
        "untracked_included": len(untracked_shown),
        "untracked_skipped": len(untracked) - len(untracked_shown),
        "artifact_version": art["version"],
        "chars": len(capped),
        "truncated": capped != content,
    }


def roundtable_repo_pack(
    thread_id: int, query: str = "", max_files: int = 40, max_bytes: int = 160_000,
) -> dict:
    """Inject a read-only snapshot of the thread's bound repo into the transcript.

    Cheap grounding that reaches EVERY participant — including providers whose
    tool loop is off, or to skip many tool round-trips: builds a file tree plus
    the contents of up to ``max_files`` files (``max_bytes`` total) and posts it
    as an orchestrator turn, so the next ask sees the repo inline. With
    ``query`` set, files whose path or contents contain it (case-insensitive)
    are included first. Requires a prior ``bind_repo`` / ``bind_github``.

    Returns ``{"thread_id", "files_included", "bytes_included", "truncated"}``.
    """
    binding = roundtable_repo_context(thread_id)
    if binding is None:
        raise RuntimeError(
            f"thread {thread_id} has no bound repo — call roundtable_bind_repo "
            f"or roundtable_bind_github first."
        )
    root = Path(binding["working_directory"]).resolve()

    # Clamp caller-supplied limits so one call can't balloon the transcript.
    max_files = max(1, min(max_files, _REPO_PACK_MAX_FILES))
    max_bytes = max(2000, min(max_bytes, _REPO_PACK_MAX_BYTES))

    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:  # jail: skip symlinks whose target escapes the repo root
            if not p.resolve().is_relative_to(root):
                continue
        except OSError:
            continue
        if _is_excluded_repo_path(p.relative_to(root)):
            continue  # never inline VCS metadata / secret files to the panel
        files.append(p)
    rels = [p.relative_to(root).as_posix() for p in files]

    q = query.strip().lower()

    def _priority(p: Path, rel: str) -> int:
        if q:
            if q in rel.lower():
                return 0
            try:
                if q in p.read_text("utf-8", errors="ignore").lower():
                    return 1
            except OSError:
                pass
        if rel.rsplit("/", 1)[-1].lower().startswith("readme"):
            return 2
        return 3

    order = sorted(range(len(files)), key=lambda i: (_priority(files[i], rels[i]), rels[i]))
    per_file_cap = max(max_bytes // max(max_files, 1), 2000)

    chunks: list[str] = []
    used = included = 0
    truncated = False
    for i in order:
        if included >= max_files or used >= max_bytes:
            truncated = True
            break
        try:
            text = files[i].read_text("utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > per_file_cap:
            text = text[:per_file_cap] + "\n[… file truncated …]"
            truncated = True
        block = f"### {rels[i]}\n```\n{text}\n```\n"
        if used + len(block) > max_bytes and included > 0:
            truncated = True
            break
        chunks.append(block)
        used += len(block)
        included += 1
    if included < len(files):
        truncated = True

    _TREE_CAP = 500
    tree = "\n".join(rels[:_TREE_CAP])
    if len(rels) > _TREE_CAP:
        tree += f"\n[… {len(rels) - _TREE_CAP} more files …]"
    header = (
        f"[repo pack: {root.name} — {included} of {len(files)} files inlined"
        + (f", query={query!r}" if query else "")
        + "]"
    )
    body = (
        f"{header}\n\n## File tree ({len(rels)} files)\n{tree}\n\n"
        f"## Files\n" + "\n".join(chunks)
    )
    roundtable_post(thread_id, body, speaker="orchestrator")
    return {
        "thread_id": thread_id, "files_included": included,
        "bytes_included": used, "truncated": truncated,
    }


def roundtable_post(thread_id: int, content: str, speaker: str = "orchestrator") -> dict:
    """Append a message to the thread without invoking a participant.

    Use this to drop in code, context, the orchestrator's own notes,
    a clarification from the human user, or the result of a verification
    you ran. The next ``roundtable_ask`` call will include this message
    in the transcript the participant reads.

    ``speaker`` defaults to ``"orchestrator"`` (Claude or the human
    driving). For human turns you can pass ``"matt"`` etc. — anything
    that helps the participants tell who said what. Reserved speaker
    labels are the official participant labels (``"Gemini Pro"`` etc.);
    using one of those would confuse the system prompt, so they're
    blocked.

    Returns ``{"thread_id", "idx"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    reserved = {info["label"] for info in PARTICIPANTS.values()}
    if speaker in reserved:
        raise ValueError(
            f"Speaker name {speaker!r} is reserved for an AI participant. "
            f"Use roundtable_ask(participant=...) to have that participant "
            f"speak, or pick a different speaker label (e.g. 'orchestrator', "
            f"'matt', 'human')."
        )
    idx = _append_message(thread_id, speaker, content)
    return {"thread_id": thread_id, "idx": idx}


def _resolve_participant(participant: str) -> dict:
    """Validate a participant name and return its info dict, or raise.

    Lookup is case-insensitive and tolerant of surrounding whitespace —
    the LLM driving the orchestrator sometimes hallucinates camel-case
    (``Gemini-Pro``) or accidentally pastes the display label
    (``Gemini Pro``). Normalising those to the canonical lowercase key
    is much friendlier than crashing the tool.
    """
    key = (participant or "").strip().lower()
    info = PARTICIPANTS.get(key)
    if info is None:
        raise ValueError(
            f"Unknown participant: {participant!r}. "
            f"Known: {sorted(PARTICIPANTS.keys())}"
        )
    if not _participant_provider_available(key):
        raise RuntimeError(
            f"Participant {key!r} uses provider {info['provider']!r} "
            f"which has no API key configured."
        )
    return info


_VALID_EFFORTS = {"low", "medium", "high"}


def _normalise_effort(effort: Optional[str]) -> Optional[str]:
    """Accept a user-supplied effort value and normalise / validate it.

    Empty string is treated as ``None`` (provider default) so callers can
    pass the JSON-friendly default. Anything outside the valid set raises
    so misspellings ('higher', 'max') don't silently degrade to defaults.
    """
    if effort is None or effort == "":
        return None
    if effort not in _VALID_EFFORTS:
        raise ValueError(
            f"effort must be one of {sorted(_VALID_EFFORTS)} or empty; "
            f"got {effort!r}"
        )
    return effort


# ─── Layer 2: read-only repo tools for Gemini / OpenAI participants ───────
#
# Anthropic participants get filesystem tools for free (the bundled Claude
# CLI / agent SDK). Gemini and OpenAI don't, so on a repo-bound thread they
# debate blind. This gives them a permission-gated, read-only,
# working-directory-jailed Read/Grep/Glob toolset via each provider's
# function-calling loop. Every call routes through the same
# ToolUseContext.permission_callback the Anthropic path uses, so the webapp's
# approval card and the readonly/deny policies work identically. Gated by
# CLAUDE_ROUNDTABLE_PANEL_TOOLS (default off) AND requires a bound
# working_directory — never grants ambient filesystem access.

PANEL_TOOLS_ENABLED = os.environ.get(
    "CLAUDE_ROUNDTABLE_PANEL_TOOLS", "",
).strip().lower() in ("1", "true", "yes")

_TOOL_READ_MAX_BYTES = 64 * 1024
_TOOL_GREP_MAX_MATCHES = 200
_TOOL_GLOB_MAX_RESULTS = 200
# Hard cap on tool-call rounds, independent of ToolUseContext.max_turns, so a
# provider stuck in a tool-call loop can't burn unbounded tokens/quota.
# Env-tunable because a deep effort=high audit legitimately spends more
# rounds than a quick question — gpt-5.x tends to issue ONE Read/Grep per
# round, so 12 rounds is ~12 file reads, not 12 batches.
_PANEL_TOOL_MAX_ROUNDS = int(
    os.environ.get("CLAUDE_ROUNDTABLE_PANEL_TOOL_MAX_ROUNDS", "12")
)
# Appended as a final user turn when the round cap is hit while the model is
# still requesting tools: one last no-tools call forces a text answer from
# the evidence gathered so far, instead of committing an empty turn.
_TOOL_BUDGET_FINAL_INSTRUCTION = (
    "[orchestrator]: Your tool-call budget for this turn is exhausted. Do "
    "not request more tool calls. Answer in full now from the evidence you "
    "have already gathered."
)

# Upper clamps so a single caller-driven bulk op can't balloon a transcript
# (and the next ask's cost) or exceed the CLI argv budget.
_REPO_PACK_MAX_FILES = 200
_REPO_PACK_MAX_BYTES = 1_000_000
_CONVERGE_MAX_FINDINGS = 100
# house_rules rides the system prompt on argv (~130k ARG_MAX on this host)
# alongside the ≤60k context pack; cap it so the two together can't E2BIG.
_HOUSE_RULES_CHAR_CAP = int(
    os.getenv("CLAUDE_ROUNDTABLE_HOUSE_RULES_CHAR_CAP", "20000")
)

# ── Paths never surfaced to external panel models ───────────────────────────
# VCS metadata (remote URLs, embedded tokens) and common secret files must
# never reach Gemini/OpenAI — not via the panel tools (model-driven
# Read/Grep/Glob), roundtable_repo_pack (bulk inline), or roundtable_bind_context
# (explicit bind). Intentionally conservative: a false exclude just hides a file
# from the panel; a false include can leak a credential off-box.
_SECRET_NAME_RE = re.compile(
    r"(^\.env($|\.)|^\.envrc$|^\.netrc$|^\.pgpass$|\.pem$|\.key$|\.p12$|\.pfx$|"
    r"\.keystore$|\.jks$|^id_(rsa|dsa|ecdsa|ed25519)$|credentials\.json$|"
    r"\.htpasswd$)",
    re.IGNORECASE,
)


def _is_excluded_repo_path(rel: Path) -> bool:
    """True if a path must never be surfaced to external panelists: any ``.git``
    component (VCS metadata) or a filename matching a secret pattern. ``rel`` may
    be repo-relative or absolute — only its parts and final name are inspected."""
    if ".git" in rel.parts:
        return True
    return bool(_SECRET_NAME_RE.search(rel.name))


# Provider-neutral declarations. Tool names match the Anthropic convention
# ("Read"/"Grep"/"Glob") so _readonly_permission_callback and the webapp
# permission UI treat panel tool calls exactly like Claude's.
_PANEL_TOOL_DECLS = [
    {
        "name": "Read",
        "description": "Read a UTF-8 text file from the bound repository. "
                       "Returns the file contents (truncated if very large).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "Grep",
        "description": "Search the bound repository for a regular expression. "
                       "Returns matching lines prefixed with file:line.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path": {"type": "string", "description": "Optional repo-relative file or dir to limit the search."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "Glob",
        "description": "List repo-relative paths matching a glob (e.g. '**/*.py').",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, repo-relative."},
            },
            "required": ["pattern"],
        },
    },
]


class _RepoTools:
    """Permission-gated, read-only, working-directory-jailed tool executor.

    One instance per turn. ``execute(name, args)`` runs a single tool call:
    it asks the permission callback, resolves+jails the path under the
    working directory (``resolve()`` collapses symlinks, so an escaping
    symlink fails the ``relative_to`` check), and returns a STRING result
    suitable for feeding back to the model. Never raises for a denied/invalid
    call — returns an explanatory string so the model can adapt.
    """

    def __init__(self, root: Path, permission_callback: PermissionCallback, label: str):
        self.root = root.resolve()
        self.permission_callback = permission_callback
        self.label = label

    def _resolve(self, rel: str) -> Optional[Path]:
        if rel is None:
            return None
        try:
            p = (self.root / rel).resolve()
            rp = p.relative_to(self.root)
        except (ValueError, OSError):
            return None
        if _is_excluded_repo_path(rp):
            return None  # never expose VCS metadata or secret files
        return p

    def execute(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            decision = (self.permission_callback(self.label, name, args) or "deny").strip().lower()
        except Exception as exc:  # noqa: BLE001 — callback fault → deny
            logger.warning("panel-tools permission callback raised %s: %s (deny)",
                           type(exc).__name__, exc)
            decision = "deny"
        if decision not in ("allow", "allow_session"):
            return f"[permission denied for {name}]"
        try:
            if name == "Read":
                return self._read(args.get("path"))
            if name == "Grep":
                return self._grep(args.get("pattern"), args.get("path"))
            if name == "Glob":
                return self._glob(args.get("pattern"))
        except Exception as exc:  # noqa: BLE001 — tool fault → message, not crash
            return f"[{name} error: {type(exc).__name__}: {exc}]"
        return f"[unknown tool {name!r}]"

    def _read(self, rel: str) -> str:
        p = self._resolve(rel)
        if p is None or not p.is_file():
            return f"[no such file: {rel}]"
        data = p.read_bytes()[: _TOOL_READ_MAX_BYTES + 1]
        text = data.decode("utf-8", errors="replace")
        if len(text) > _TOOL_READ_MAX_BYTES:
            text = text[:_TOOL_READ_MAX_BYTES] + "\n[… truncated]"
        return text

    def _grep(self, pattern: str, rel: Optional[str]) -> str:
        if not pattern:
            return "[grep: empty pattern]"
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"[grep: bad regex: {exc}]"
        base = self._resolve(rel) if rel else self.root
        if base is None:
            return f"[grep: path outside repo: {rel}]"
        files = [base] if base.is_file() else [
            f for f in base.rglob("*") if f.is_file()
        ]
        out: list[str] = []
        for f in files:
            try:
                # Re-jail by REAL path before opening: rglob yields symlink
                # paths whose .relative_to(root) succeeds even when the link
                # target is outside the repo, so opening f would read through
                # the link and leak external content. resolve() collapses the
                # link; reject anything that lands outside root. (_read is
                # already safe via _resolve; this closes the same hole here.)
                real = f.resolve()
                if not real.is_relative_to(self.root):
                    continue
                if _is_excluded_repo_path(f.relative_to(self.root)):
                    continue  # never expose VCS metadata or secret files
                rp = f.relative_to(self.root).as_posix()  # '/' on every host; see _glob
                with f.open(encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append(f"{rp}:{n}:{line.rstrip()[:300]}")
                            if len(out) >= _TOOL_GREP_MAX_MATCHES:
                                out.append("[… more matches truncated]")
                                return "\n".join(out)
            except (OSError, ValueError):
                continue
        return "\n".join(out) if out else "[no matches]"

    def _glob(self, pattern: str) -> str:
        if not pattern:
            return "[glob: empty pattern]"
        try:
            # Re-jail by REAL path before yielding: self.root.glob follows a
            # symlink under root whose target is outside, while relative_to
            # passes lexically — so an outward link would leak external file
            # names/existence. resolve() collapses the link; reject anything
            # landing outside root. Mirrors _read/_grep. (as_posix() shows the
            # un-resolved relative path with '/' on every host.)
            hits = []
            for p in self.root.glob(pattern):
                try:
                    if not p.is_file() or not p.resolve().is_relative_to(self.root):
                        continue
                except OSError:
                    continue
                rp = p.relative_to(self.root)
                if _is_excluded_repo_path(rp):
                    continue  # never expose VCS metadata or secret files
                hits.append(rp.as_posix())
            hits.sort()
        except (ValueError, OSError, NotImplementedError) as exc:
            return f"[glob error: {exc}]"
        if not hits:
            return "[no matches]"
        if len(hits) > _TOOL_GLOB_MAX_RESULTS:
            hits = hits[:_TOOL_GLOB_MAX_RESULTS] + ["[… more truncated]"]
        return "\n".join(hits)


def _call_gemini_with_tools(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], web_search: bool,
    tools: _RepoTools,
) -> ProviderResult:
    """Gemini participant turn with a manual (permission-gated) function-calling
    loop over the read-only repo tools. Automatic function calling is disabled
    so every call goes through ``tools.execute`` (and thus the permission gate).
    """
    decls = [
        genai_types.FunctionDeclaration(
            name=d["name"], description=d["description"], parameters=d["parameters"],
        )
        for d in _PANEL_TOOL_DECLS
    ]
    tool_list = [genai_types.Tool(function_declarations=decls)]
    if web_search:
        tool_list.append(genai_types.Tool(google_search=genai_types.GoogleSearch()))
    config: dict = {
        "system_instruction": system_prompt,
        "tools": tool_list,
        # Manual loop — never let the SDK auto-execute (it'd bypass the gate).
        "automatic_function_calling": genai_types.AutomaticFunctionCallingConfig(
            disable=True,
        ),
        "http_options": genai_types.HttpOptions(timeout=int(PROVIDER_TIMEOUT_SEC * 1000)),
    }
    if effort and _gemini_uses_thinking_level(model):
        config["thinking_config"] = genai_types.ThinkingConfig(thinking_level=effort)
    elif effort in _GEMINI_BUDGETS:
        config["thinking_config"] = genai_types.ThinkingConfig(
            thinking_budget=_GEMINI_BUDGETS[effort],
        )

    user_text = transcript + (f"\n\n[orchestrator]:\n{instruction}" if instruction else "")
    contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=user_text)])]

    def _do_call() -> ProviderResult:
        last = None
        exhausted = True
        for _round in range(_PANEL_TOOL_MAX_ROUNDS):
            resp = _provider_call(
                f"gemini-tools/{model}",
                lambda: _gemini.models.generate_content(
                    model=model, contents=contents, config=config,
                ),
            )
            last = resp
            cand = (resp.candidates or [None])[0]
            parts = getattr(getattr(cand, "content", None), "parts", None) or []
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            if not calls:
                exhausted = False
                break
            # Echo the model's function-call turn, then answer each call.
            contents.append(genai_types.Content(role="model", parts=parts))
            resp_parts = []
            for fc in calls:
                result = tools.execute(fc.name, dict(fc.args or {}))
                resp_parts.append(genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=fc.name, response={"result": result},
                    ),
                ))
            contents.append(genai_types.Content(role="user", parts=resp_parts))
        if exhausted:
            # Same forced-final-answer shape as the OpenAI loop. Tools stay
            # declared (history already contains function parts, which the
            # API rejects without matching declarations) — mode NONE just
            # forbids new calls this turn.
            logger.warning(
                "gemini-tools/%s: tool budget exhausted after %d rounds; "
                "forcing final answer", model, _PANEL_TOOL_MAX_ROUNDS,
            )
            contents.append(genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=_TOOL_BUDGET_FINAL_INSTRUCTION)],
            ))
            final_config = dict(config)
            final_config["tool_config"] = genai_types.ToolConfig(
                function_calling_config=genai_types.FunctionCallingConfig(
                    mode="NONE",
                ),
            )
            last = _provider_call(
                f"gemini-tools/{model}/final",
                lambda: _gemini.models.generate_content(
                    model=model, contents=contents, config=final_config,
                ),
            )
        text = _gemini_response_text(last)
        if exhausted and not (text or "").strip():
            text = (
                f"[tool budget exhausted after {_PANEL_TOOL_MAX_ROUNDS} "
                f"rounds without a final answer — re-ask with narrower "
                f"scope or raise CLAUDE_ROUNDTABLE_PANEL_TOOL_MAX_ROUNDS]"
            )
        return ProviderResult(
            text=text,
            usage=_extract_usage("gemini", last) if last else None, raw=last,
        )

    return _call_with_wall_cap(f"gemini-tools/{model}", _do_call)


def _call_openai_with_tools(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], web_search: bool,
    tools: _RepoTools,
) -> ProviderResult:
    """OpenAI participant turn with a manual function-calling loop over the
    read-only repo tools, via the Responses API. Every function call routes
    through ``tools.execute`` (permission-gated)."""
    tool_specs = [
        {
            "type": "function", "name": d["name"], "description": d["description"],
            "parameters": d["parameters"],
        }
        for d in _PANEL_TOOL_DECLS
    ]
    if web_search:
        tool_specs.append({"type": "web_search"})
    user_text = transcript + (f"\n\n[orchestrator]:\n{instruction}" if instruction else "")
    input_items: list = [{"role": "user", "content": user_text}]
    base_kwargs: dict = {
        "model": model,
        "instructions": system_prompt,
        "tools": tool_specs,
        "timeout": PROVIDER_TIMEOUT_SEC,
    }
    if effort:
        base_kwargs["reasoning"] = {"effort": effort}

    def _do_call() -> ProviderResult:
        last = None
        exhausted = True
        for _round in range(_PANEL_TOOL_MAX_ROUNDS):
            resp = _provider_call(
                f"openai-tools/{model}",
                lambda: _openai.responses.create(input=input_items, **base_kwargs),
            )
            last = resp
            fn_calls = [
                item for item in (getattr(resp, "output", None) or [])
                if getattr(item, "type", None) == "function_call"
            ]
            if not fn_calls:
                exhausted = False
                break
            # Carry the model's reasoning/function-call items forward, then
            # append each function_call_output keyed by call_id. Mutate in
            # place (not +=) so input_items stays the closure variable.
            input_items.extend(getattr(resp, "output", []) or [])
            for call in fn_calls:
                try:
                    args = json.loads(getattr(call, "arguments", "") or "{}")
                except ValueError:
                    args = {}
                result = tools.execute(getattr(call, "name", ""), args)
                input_items.append({
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id", None),
                    "output": result,
                })
        if exhausted:
            # The model was still mid-investigation when the round cap hit;
            # its last response is reasoning/tool-calls with no text. Force
            # a text answer (tool_choice="none") rather than returning the
            # empty string the caller would commit verbatim.
            logger.warning(
                "openai-tools/%s: tool budget exhausted after %d rounds; "
                "forcing final answer", model, _PANEL_TOOL_MAX_ROUNDS,
            )
            input_items.append(
                {"role": "user", "content": _TOOL_BUDGET_FINAL_INSTRUCTION}
            )
            last = _provider_call(
                f"openai-tools/{model}/final",
                lambda: _openai.responses.create(
                    input=input_items, tool_choice="none", **base_kwargs,
                ),
            )
        text = (getattr(last, "output_text", None) or "") if last else ""
        if exhausted and not text.strip():
            text = (
                f"[tool budget exhausted after {_PANEL_TOOL_MAX_ROUNDS} "
                f"rounds without a final answer — re-ask with narrower "
                f"scope or raise CLAUDE_ROUNDTABLE_PANEL_TOOL_MAX_ROUNDS]"
            )
        return ProviderResult(
            text=text, usage=_extract_usage("openai", last) if last else None, raw=last,
        )

    return _call_with_wall_cap(f"openai-tools/{model}", _do_call)


def _run_turn(
    thread: dict, info: dict, messages: list[dict], instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
    on_delta: "Optional[StreamDelta]" = None,
) -> ProviderResult:
    """Render the transcript for one participant and call its provider.

    Pure function over the inputs — no DB writes. Caller decides when to
    append the response to the thread, which lets parallel asks commit
    several responses atomically (and against the same transcript
    snapshot) without one bleed-through into another's view.

    ``tool_use_context`` opts the participant into real filesystem tool
    use, every call routed through the supplied permission_callback.
    Anthropic uses the agent-SDK tool path; Gemini and OpenAI use their
    function-calling loops when ``PANEL_TOOLS_ENABLED`` is set (otherwise
    they ignore the context and debate from the transcript alone).
    """
    # Reserve room for the context pack: it's part of the system prompt (built
    # below at _build_system_prompt), so the transcript must fit in what's left
    # of the cap once the pack is accounted for — floored so a huge pack can't
    # zero out the conversation.
    context_pack = thread.get("context_pack") or ""
    trimmed = _trim_messages_to_cap(
        messages,
        max(PROMPT_CHAR_CAP - len(context_pack), MIN_TRANSCRIPT_CAP),
        for_participant_label=info["label"],
    )
    transcript = _format_transcript(trimmed, for_participant_label=info["label"])
    provider = info["provider"]
    have_repo = (
        tool_use_context is not None
        and tool_use_context.working_directory is not None
    )
    # Layer 2: read-only repo tools for Gemini/OpenAI on a bound thread, gated
    # by the env flag. Tool-use turns are not streamed (the text arrives
    # interleaved with tool calls), so on_delta is ignored on this path.
    panel_tools = (
        have_repo and PANEL_TOOLS_ENABLED and provider in ("gemini", "openai")
    )
    system_prompt = _build_system_prompt(
        thread, info["label"], thread.get("participants") or [],
        web_search=web_search,
        tools_enabled=have_repo and (provider == "anthropic" or panel_tools),
        readonly_tools=panel_tools,
    )
    if panel_tools:
        repo_tools = _RepoTools(
            Path(tool_use_context.working_directory),
            tool_use_context.permission_callback,
            info["label"],
        )
        if provider == "gemini":
            return _call_gemini_with_tools(
                info["model"], system_prompt, transcript, instruction,
                effort, web_search, repo_tools,
            )
        return _call_openai_with_tools(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search, repo_tools,
        )
    if provider == "gemini":
        return _call_gemini(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search, on_delta=on_delta,
        )
    if provider == "openai":
        return _call_openai(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search, on_delta=on_delta,
        )
    if provider == "anthropic":
        # Routes between CLI (subscription), SDK (API), and SDK-with-tools
        # (permission-gated) based on tool_use_context +
        # CLAUDE_ROUNDTABLE_ANTHROPIC_TRANSPORT and what's installed.
        return _call_anthropic_router(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search,
            tool_use_context=tool_use_context,
            participant_label=info["label"],
            on_delta=on_delta,
        )
    raise RuntimeError(f"Unknown provider {provider!r} for model {info['model']!r}")


def roundtable_ask(
    thread_id: int, participant: str, prompt: str = "", effort: str = "",
    web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
    on_delta: "Optional[StreamDelta]" = None,
) -> str:
    """Route a turn to a named AI participant.

    The participant sees the FULL transcript of the thread so far,
    rendered with speaker labels. If ``prompt`` is supplied it's
    appended as a final orchestrator turn — useful for steering ("argue
    against Gemini's last point", "summarise where you agree", "spot
    bugs in the code I just posted").

    The participant's response is appended to the thread before being
    returned, so a subsequent ``roundtable_ask`` to a different
    participant sees it as part of the history.

    ``participant`` must be one of the keys in ``PARTICIPANTS``
    (currently: ``gemini-flash``, ``gemini-pro``, ``gpt-5-mini``,
    ``gpt-5-terra``, ``gpt-5``, ``claude-sonnet``, ``claude-opus``,
    ``claude-fable``).

    ``effort`` selects the participant's reasoning/thinking spend:
    ``"low" | "medium" | "high"``, or empty (default) to let the
    provider pick. ``high`` is right for hard code review or
    architecture debate; ``medium`` is the balanced default for normal
    discussion; ``low`` for quick reactions. Maps per-provider:
    OpenAI ``reasoning_effort``, Gemini thinking_budget (1024 / 8192 /
    24576), Anthropic adaptive thinking on for medium+ and off for low.

    ``web_search`` (default False) attaches each provider's hosted
    web-search tool for the turn — Google Search grounding for Gemini,
    the Responses-API ``web_search`` tool for OpenAI, the
    ``web_search_20260209`` server tool for Anthropic (or
    ``--tools web_search`` on the CLI transport). Turn this on when the
    answer needs current information; leave off for self-contained
    debates so the model doesn't pay grounding fees it doesn't need.

    Returns the participant's response text.
    """
    info = _resolve_participant(participant)
    effort = _normalise_effort(effort)
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(
            f"Thread {thread_id} is closed — reopen by creating a new "
            f"thread that references it, or accept that the debate is over."
        )

    # Persist the orchestrator prompt as a real transcript turn BEFORE
    # snapshotting (mirroring roundtable_ask_parallel). Without this,
    # roundtable_history would show the participant's answer with no
    # record of what was asked — making retrospective debate auditing
    # impossible. _run_turn is called with empty instruction since the
    # prompt is already in the snapshot.
    if prompt.strip():
        _append_message(thread_id, "orchestrator", prompt)
    messages = _effective_messages(thread_id)
    try:
        result = _run_turn(
            thread, info, messages, "", effort, web_search,
            tool_use_context=tool_use_context, on_delta=on_delta,
        )
    except Exception as exc:
        # Surface the provider error to the caller AND record it in the
        # thread so subsequent participants can see what went wrong (e.g.
        # rate limit, context too long, content-policy refusal) instead
        # of being confused by a missing turn.
        err_msg = f"[provider error: {type(exc).__name__}: {exc}]"
        _append_message(thread_id, info["label"], err_msg)
        raise

    _log_usage(info["label"], result, thread_id=thread_id)
    response = (result.text or "").strip()
    if not response:
        response = "[empty response from provider]"
    if _thread_is_closed(thread_id):
        # A roundtable_close raced with the in-flight provider call.
        # Don't drop the response (we already paid for it) — record it
        # but mark the closure so audit-readers know the timeline.
        response = response + (
            "\n\n[note: thread was closed during this call; response "
            "recorded post-closure]"
        )
    _append_message(thread_id, info["label"], response)
    return response


def roundtable_ask_parallel(
    thread_id: int, participants: list[str], prompt: str = "",
    effort: str = "", web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
) -> dict:
    """Ask multiple participants the SAME question in parallel — each sees
    the transcript only up to the prompt, never each other's answers.

    This is the cure for sequential-bias in multi-AI review: when you
    ``roundtable_ask`` Gemini first then GPT, GPT is anchored by Gemini's
    answer and produces a reaction, not an independent read. Here both
    fire against the same transcript snapshot and neither sees the
    other's response until both are committed.

    Flow:
        1. ``prompt`` (if non-empty) is posted ONCE as an orchestrator
           message — every participant sees identical framing.
        2. The transcript is snapshotted.
        3. All participants are called concurrently in worker threads.
        4. Once every call returns (or errors), responses are appended
           to the thread in the order ``participants`` was given.

    ``effort`` is applied uniformly to every participant on this call —
    see ``roundtable_ask`` for the per-provider mapping. Pass empty for
    provider defaults. ``web_search`` is likewise applied uniformly —
    every participant on the call gets web access or none does.

    Returns ``{"thread_id", "responses": {name: text}, "errors":
    {name: "ExceptionType: message"}}``. A participant either appears
    in ``responses`` or in ``errors``, never both. Errors are also
    written into the transcript as ``[provider error: …]`` turns so the
    next ``roundtable_ask`` can address them.
    """
    if not participants:
        raise ValueError("roundtable_ask_parallel requires at least one participant.")
    if len(set(participants)) != len(participants):
        raise ValueError(
            f"Duplicate participants in {participants!r}; each name must appear "
            f"once per parallel ask."
        )
    infos = {p: _resolve_participant(p) for p in participants}
    effort = _normalise_effort(effort)
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(
            f"Thread {thread_id} is closed — reopen by creating a new "
            f"thread that references it, or accept that the debate is over."
        )

    # The orchestrator prompt becomes a real transcript turn so each
    # participant sees identical framing. After this point we snapshot
    # and DO NOT mutate the message list until every provider call
    # returns — that's what guarantees independence.
    if prompt.strip():
        _append_message(thread_id, "orchestrator", prompt)
    snapshot = _effective_messages(thread_id)

    # Empty instruction in _run_turn — the prompt is already in the
    # snapshot. Passing it again would duplicate it for each participant.
    # Catch only Exception, NOT BaseException — swallowing
    # KeyboardInterrupt/SystemExit would prevent clean shutdown and
    # record signals as fake provider errors in the transcript.
    def _one(
        name: str,
    ) -> tuple[str, Optional[ProviderResult], Optional[Exception]]:
        info = infos[name]
        try:
            resp = _run_turn(
                thread, info, snapshot, instruction="", effort=effort,
                web_search=web_search,
                tool_use_context=tool_use_context,
            )
            return name, resp, None
        except Exception as exc:
            return name, None, exc

    results: dict[str, tuple[Optional[ProviderResult], Optional[Exception]]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(participants)
    ) as pool:
        futures = [pool.submit(_one, p) for p in participants]
        for fut in concurrent.futures.as_completed(futures):
            name, resp, err = fut.result()
            results[name] = (resp, err)

    # Recheck closure once before the commit loop. If the thread was
    # closed mid-flight we still record the responses (already paid for)
    # but tag them so an audit-reader sees the timeline.
    closed_mid_flight = _thread_is_closed(thread_id)

    # Commit in the caller-requested order so the transcript reads
    # deterministically (and matches what the caller passed in), not in
    # whatever order the providers happened to finish.
    responses: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in participants:
        resp, err = results[name]
        label = infos[name]["label"]
        if err is not None:
            msg = f"[provider error: {type(err).__name__}: {err}]"
            _append_message(thread_id, label, msg)
            errors[name] = f"{type(err).__name__}: {err}"
            continue
        _log_usage(label, resp, thread_id=thread_id)
        clean = (resp.text or "").strip() or "[empty response from provider]"
        if closed_mid_flight:
            clean = clean + (
                "\n\n[note: thread was closed during this call; response "
                "recorded post-closure]"
            )
        _append_message(thread_id, label, clean)
        responses[name] = clean

    return {"thread_id": thread_id, "responses": responses, "errors": errors}


# ─── Structured asks ─────────────────────────────────────────────────────
#
# roundtable_ask_parallel returns freeform prose, which the orchestrator
# then regex-parses into findings for roundtable_converge — lossy and
# fragile. A structured ask forces each participant's reply through a
# caller-supplied JSON Schema instead: native enforcement where the
# provider supports it (OpenAI json_schema response format, Gemini
# response_json_schema, Anthropic forced tool use), a prompt contract on
# the CLI transport — and ALWAYS a local jsonschema validation with one
# corrective retry, so the caller gets a validated object or an error,
# never almost-JSON.

# The schema rides system prompts / tool declarations; cap it so a
# pathological schema can't blow the CLI argv budget (~130k ARG_MAX here,
# shared with house_rules and the context pack).
_STRUCTURED_SCHEMA_CHAR_CAP = int(
    os.environ.get("CLAUDE_ROUNDTABLE_SCHEMA_CHAR_CAP", "20000")
)
# One corrective re-ask after a failed validation. More buys little: a
# model that ignores the validator's error message twice will keep
# ignoring it, and each retry re-bills the whole transcript.
_STRUCTURED_MAX_REPAIR_ATTEMPTS = 1
# How much of an invalid reply to quote back in the repair instruction —
# enough to locate the mistake without re-pasting a huge payload.
_STRUCTURED_REPAIR_QUOTE_CAP = 4000

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?|\n?\s*```\s*$")


def _strip_json_fences(text: str) -> str:
    """Remove a leading/trailing markdown code fence from ``text``.

    Even schema-prompted models wrap JSON in ``` fences often enough that
    rejecting on it would burn repair attempts on pure formatting.
    """
    return _JSON_FENCE_RE.sub("", text or "").strip()


def _validate_structured(candidate: object, schema: dict) -> object:
    """Parse (if text) and schema-validate a structured reply.

    Returns the validated object. Raises ``ValueError`` whose message is
    written to be fed straight back to the model as repair guidance.
    """
    if isinstance(candidate, str):
        raw = _strip_json_fences(candidate)
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            raise ValueError(
                f"reply is not valid JSON ({exc}). Reply with ONLY one JSON "
                f"object — no prose, no markdown fences."
            ) from exc
    else:
        obj = candidate
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as exc:
        path = "$" + "".join(f"[{p!r}]" for p in exc.absolute_path)
        raise ValueError(
            f"reply violates the schema at {path}: {exc.message}"
        ) from exc
    return obj


_STRUCTURED_PROMPT_CONTRACT = (
    "\n\n=== Structured output contract (this turn) ===\n"
    "Respond with ONLY a single JSON object — no prose before or after, no "
    "markdown fences — that validates against this JSON Schema:\n"
)


def _call_openai_structured(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], schema: dict,
) -> ProviderResult:
    """OpenAI structured turn via the Responses API ``json_schema`` format.

    ``strict=True`` gives constrained decoding but rejects schemas that
    don't meet OpenAI's strict-subset rules (every object needs
    ``additionalProperties: false`` and all properties required); on that
    400 we retry non-strict — the local validation in the caller is the
    real contract either way.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    kwargs: dict = {
        "model": model,
        "instructions": system_prompt,
        "input": user_msg,
        "timeout": PROVIDER_TIMEOUT_SEC,
    }
    if effort:
        kwargs["reasoning"] = {"effort": effort}

    def _fmt(strict: bool) -> dict:
        return {
            "format": {
                "type": "json_schema",
                "name": "structured_reply",
                "schema": schema,
                "strict": strict,
            }
        }

    import openai as _o
    try:
        resp = _provider_call(
            f"openai-structured/{model}",
            lambda: _openai.responses.create(text=_fmt(True), **kwargs),
        )
    except _o.BadRequestError as exc:
        # Schema outside the strict subset — retry without constrained
        # decoding rather than bouncing a legitimate schema to the caller.
        logger.warning(
            "openai-structured/%s: strict json_schema rejected (%s); "
            "retrying non-strict", model, exc,
        )
        resp = _provider_call(
            f"openai-structured/{model}/lax",
            lambda: _openai.responses.create(text=_fmt(False), **kwargs),
        )
    return ProviderResult(
        text=resp.output_text or "", usage=_extract_usage("openai", resp),
        raw=resp,
    )


def _call_gemini_structured(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], schema: dict,
) -> ProviderResult:
    """Gemini structured turn via ``response_json_schema`` + JSON mime type.

    Falls back to mime-type-only plus the prompt contract when the
    installed google-genai predates raw-JSON-Schema support (pydantic
    rejects the config key) or the API rejects the schema — local
    validation in the caller covers both paths.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    base_config: dict = {
        "system_instruction": system_prompt,
        "response_mime_type": "application/json",
        "http_options": genai_types.HttpOptions(
            timeout=int(PROVIDER_TIMEOUT_SEC * 1000),
        ),
    }
    if effort:
        if _gemini_uses_thinking_level(model):
            base_config["thinking_config"] = genai_types.ThinkingConfig(
                thinking_level=effort,
            )
        elif effort in _GEMINI_BUDGETS:
            base_config["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=_GEMINI_BUDGETS[effort],
            )

    from google.genai import errors as _g_errors
    try:
        resp = _provider_call(
            f"gemini-structured/{model}",
            lambda: _gemini.models.generate_content(
                model=model, contents=user_msg,
                config={**base_config, "response_json_schema": schema},
            ),
        )
    except (TypeError, ValueError, _g_errors.APIError) as exc:
        # TypeError/ValueError: SDK too old for response_json_schema
        # (pydantic ValidationError is a ValueError). APIError: the API
        # rejected this particular schema. Either way the mime type plus
        # the prompt contract still gets JSON out; validation is local.
        logger.warning(
            "gemini-structured/%s: native json-schema path failed "
            "(%s: %s); falling back to prompt contract",
            model, type(exc).__name__, exc,
        )
        fallback_sys = (
            system_prompt + _STRUCTURED_PROMPT_CONTRACT
            + json.dumps(schema, indent=2)
        )
        resp = _provider_call(
            f"gemini-structured/{model}/lax",
            lambda: _gemini.models.generate_content(
                model=model, contents=user_msg,
                config={**base_config, "system_instruction": fallback_sys},
            ),
        )
    return ProviderResult(
        text=_gemini_response_text(resp),
        usage=_extract_usage("gemini", resp), raw=resp,
    )


def _call_anthropic_structured_api(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str], schema: dict,
) -> ProviderResult:
    """Anthropic structured turn via forced tool use on the Messages API.

    A single tool whose ``input_schema`` is the caller's schema, with
    ``tool_choice`` pinned to it — the assistant's only legal move is to
    emit conforming input. Extended thinking is NOT enabled here: the API
    rejects a forced tool_choice combined with thinking, so a structured
    turn trades thinking for schema enforcement (``effort`` still scales
    ``max_tokens``).
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    max_tokens = (
        _ANTHROPIC_MAX_TOKENS_BY_EFFORT.get(effort, _ANTHROPIC_MAX_TOKENS)
        if effort else _ANTHROPIC_MAX_TOKENS
    )
    kwargs: dict = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
        "max_tokens": max_tokens,
        "timeout": PROVIDER_TIMEOUT_SEC,
        "tools": [{
            "name": "structured_reply",
            "description": "Emit your final answer as structured data "
                           "conforming to this schema.",
            "input_schema": schema,
        }],
        "tool_choice": {"type": "tool", "name": "structured_reply"},
    }
    resp = _provider_call(
        f"anthropic-structured/{model}",
        lambda: _anthropic.messages.create(**kwargs),
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            obj = block.input
            return ProviderResult(
                text=json.dumps(obj, indent=2), structured=obj,
                usage=_extract_usage("anthropic", resp),
                finish_reason=getattr(resp, "stop_reason", None), raw=resp,
            )
    # Forced tool_choice should make this unreachable; fall through to the
    # text blocks so the caller's parse/validate sees whatever came back.
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    )
    return ProviderResult(
        text=text, usage=_extract_usage("anthropic", resp), raw=resp,
    )


def _structured_turn(
    thread: dict, info: dict, messages: list[dict], instruction: str,
    effort: Optional[str], schema: dict,
) -> ProviderResult:
    """One participant's schema-enforced turn, validated, with one repair.

    Renders the transcript exactly like ``_run_turn`` (same trim budget,
    same system prompt), dispatches to the provider's structured path —
    or the prompt contract on the Anthropic CLI transport, which has no
    enforcement hook — then locally validates. A failed validation gets
    ONE corrective re-ask carrying the validator's error and the invalid
    output; a second failure raises.
    """
    context_pack = thread.get("context_pack") or ""
    trimmed = _trim_messages_to_cap(
        messages,
        max(PROMPT_CHAR_CAP - len(context_pack), MIN_TRANSCRIPT_CAP),
        for_participant_label=info["label"],
    )
    transcript = _format_transcript(trimmed, for_participant_label=info["label"])
    system_prompt = _build_system_prompt(
        thread, info["label"], thread.get("participants") or [],
    )
    provider = info["provider"]
    schema_text = json.dumps(schema, indent=2)

    def _dispatch(instr: str) -> ProviderResult:
        if provider == "openai":
            return _call_openai_structured(
                info["model"], system_prompt, transcript, instr, effort, schema,
            )
        if provider == "gemini":
            return _call_gemini_structured(
                info["model"], system_prompt, transcript, instr, effort, schema,
            )
        if provider == "anthropic":
            # SDK Messages path gets forced tool use; the CLI transport has
            # no enforcement hook, so it gets the schema as a prompt
            # contract — the local validate+repair below is its net.
            use_api = _ANTHROPIC_TRANSPORT == "api" or (
                _ANTHROPIC_TRANSPORT == "auto" and _CLAUDE_CLI is None
            )
            if use_api:
                if _anthropic is None:
                    raise RuntimeError(
                        "Anthropic structured ask needs ANTHROPIC_API_KEY "
                        "on this transport."
                    )
                return _call_anthropic_structured_api(
                    info["model"], system_prompt, transcript, instr,
                    effort, schema,
                )
            contract_sys = (
                system_prompt + _STRUCTURED_PROMPT_CONTRACT + schema_text
            )
            return _call_anthropic_cli(
                info["model"], contract_sys, transcript, instr, effort,
            )
        raise RuntimeError(f"Unknown provider {provider!r}")

    instr = instruction
    last_err: Optional[ValueError] = None
    for attempt in range(1 + _STRUCTURED_MAX_REPAIR_ATTEMPTS):
        result = _dispatch(instr)
        candidate = (
            result.structured if result.structured is not None else result.text
        )
        try:
            obj = _validate_structured(candidate, schema)
        except ValueError as exc:
            last_err = exc
            bad = (result.text or "")[:_STRUCTURED_REPAIR_QUOTE_CAP]
            instr = (
                (instruction + "\n\n" if instruction else "")
                + f"Your previous reply failed validation: {exc}\n"
                + f"Previous reply (possibly truncated):\n{bad}\n\n"
                + "Reply again with ONLY a single JSON object that "
                + "validates against the schema."
            )
            logger.warning(
                "structured turn for %s failed validation "
                "(attempt %d/%d): %s",
                info["label"], attempt + 1,
                1 + _STRUCTURED_MAX_REPAIR_ATTEMPTS, exc,
            )
            continue
        result.structured = obj
        result.text = json.dumps(obj, indent=2)
        return result
    raise ValueError(
        f"{info['label']} could not produce a schema-valid reply after "
        f"{1 + _STRUCTURED_MAX_REPAIR_ATTEMPTS} attempts: {last_err}"
    )


def roundtable_ask_structured(
    thread_id: int, participants: list[str], schema: dict,
    prompt: str = "", effort: str = "",
) -> dict:
    """Ask participants for replies conforming to a JSON Schema — the
    machine-readable sibling of ``roundtable_ask_parallel``.

    Use this when the panel's output feeds a pipeline instead of a human:
    verdict collection, findings lists for ``roundtable_converge``, scored
    votes. Each participant is forced through ``schema`` — natively where
    the provider supports it (OpenAI ``json_schema`` response format,
    Gemini ``response_json_schema``, Anthropic forced tool use on the API
    transport; the CLI transport gets the schema as a prompt contract) —
    and every reply is locally validated against ``schema``, with one
    corrective retry, so ``results`` contains parsed, validated objects,
    never almost-JSON.

    ``schema`` must be a JSON Schema whose root is ``{"type": "object"}``
    (both OpenAI strict mode and Anthropic tool input require an object
    root — wrap a bare list as ``{"items": [...]}``).

    Semantics mirror ``roundtable_ask_parallel``: ``prompt`` is posted once
    as an orchestrator turn, the transcript is snapshotted, participants
    run concurrently against the same snapshot (no anchoring), and the
    pretty-printed JSON replies are committed to the thread in call order
    so later turns can reference them. ``web_search`` is intentionally not
    offered — grounding plus constrained decoding is where providers
    disagree most; run a searching ask first, then collect verdicts.

    Returns ``{"thread_id", "results": {name: object}, "errors":
    {name: "ExceptionType: message"}}`` — a participant appears in exactly
    one of the two.
    """
    if not participants:
        raise ValueError("roundtable_ask_structured requires at least one participant.")
    if len(set(participants)) != len(participants):
        raise ValueError(
            f"Duplicate participants in {participants!r}; each name must "
            f"appear once per structured ask."
        )
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(
            'schema must be a JSON Schema dict with root {"type": "object"} '
            "— OpenAI strict mode and Anthropic tool input both require an "
            "object root."
        )
    schema_len = len(json.dumps(schema))
    if schema_len > _STRUCTURED_SCHEMA_CHAR_CAP:
        raise ValueError(
            f"schema serialises to {schema_len} chars, over the "
            f"{_STRUCTURED_SCHEMA_CHAR_CAP} cap — it rides system prompts "
            f"and tool declarations; trim it."
        )
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(f"schema is not a valid JSON Schema: {exc.message}") from exc

    infos = {p: _resolve_participant(p) for p in participants}
    effort = _normalise_effort(effort)
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(
            f"Thread {thread_id} is closed — reopen by creating a new "
            f"thread that references it, or accept that the debate is over."
        )

    if prompt.strip():
        _append_message(thread_id, "orchestrator", prompt)
    snapshot = _effective_messages(thread_id)

    def _one(
        name: str,
    ) -> tuple[str, Optional[ProviderResult], Optional[Exception]]:
        try:
            resp = _structured_turn(
                thread, infos[name], snapshot, "", effort, schema,
            )
            return name, resp, None
        except Exception as exc:
            return name, None, exc

    collected: dict[str, tuple[Optional[ProviderResult], Optional[Exception]]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(participants)
    ) as pool:
        futures = [pool.submit(_one, p) for p in participants]
        for fut in concurrent.futures.as_completed(futures):
            name, resp, err = fut.result()
            collected[name] = (resp, err)

    closed_mid_flight = _thread_is_closed(thread_id)
    results: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name in participants:
        resp, err = collected[name]
        label = infos[name]["label"]
        if err is not None:
            msg = f"[provider error: {type(err).__name__}: {err}]"
            _append_message(thread_id, label, msg)
            errors[name] = f"{type(err).__name__}: {err}"
            continue
        _log_usage(label, resp, thread_id=thread_id)
        committed = resp.text
        if closed_mid_flight:
            committed += (
                "\n\n[note: thread was closed during this call; response "
                "recorded post-closure]"
            )
        _append_message(thread_id, label, committed)
        results[name] = resp.structured
    return {"thread_id": thread_id, "results": results, "errors": errors}


# ─── Artifacts ───────────────────────────────────────────────────────────

# Cap on the unified diff we include in the synthetic transcript message
# when an artifact is bumped. The full new content is always included;
# the diff is just a reading aid. If the diff is enormous (rewrite), we
# drop it rather than balloon the transcript.
ARTIFACT_DIFF_CHAR_CAP = int(
    os.environ.get("CLAUDE_ROUNDTABLE_ARTIFACT_DIFF_CHAR_CAP", "20000")
)


def _latest_artifact_version(thread_id: int, name: str) -> int:
    """Return the highest version stored for (thread, name), or 0 if none."""
    with _db_lock:
        row = _conn().execute(
            "SELECT COALESCE(MAX(version), 0) FROM artifacts "
            "WHERE thread_id = ? AND name = ?",
            (thread_id, name),
        ).fetchone()
    return int(row[0])


def roundtable_has_artifacts(thread_id: int) -> bool:
    """True if the thread has any artifact rows. Public + lock-held so callers
    in other processes/threads (e.g. the claude-web assistant producer) don't
    touch the shared connection unsynchronised."""
    with _db_lock:
        row = _conn().execute(
            "SELECT 1 FROM artifacts WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row is not None


def _get_artifact_content(thread_id: int, name: str, version: int) -> Optional[str]:
    with _db_lock:
        row = _conn().execute(
            "SELECT content FROM artifacts WHERE thread_id = ? AND name = ? AND version = ?",
            (thread_id, name, version),
        ).fetchone()
    return row[0] if row else None


def _render_artifact_diff(old: str, new: str, name: str, old_v: int, new_v: int) -> str:
    """Unified diff between two versions. Caller is responsible for the
    size cap — we just generate the standard ``difflib`` output here."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{name}@v{old_v}",
        tofile=f"{name}@v{new_v}",
        n=3,
    )
    return "".join(diff)


# ─── Grounded confirm/refute over review findings ────────────────────────

_CONVERGE_WINDOW_LINES = 40
_VALID_VERDICTS = ("confirmed", "refuted", "unresolved")
_VALID_CONVERGE_TRANSPORTS = {"cli", "api", "auto"}

_CONVERGE_SYSTEM_PROMPT = (
    "You are a code-review verifier. You are given a CLAIM about code, the "
    "reviewer's stated PROOF, and the ACTUAL code at the cited location (with "
    "line numbers). Decide whether the claim holds against the real code. "
    "Judge ONLY from the code shown — do not speculate about code you can't "
    "see; if the cited location lacks enough to decide, say so.\n\n"
    "Reply with exactly two lines:\n"
    "VERDICT: confirmed | refuted | unresolved\n"
    "EVIDENCE: <one line, cite the line number(s) you relied on>"
)


def _excerpt_around(text: str, line: int) -> str:
    """Return a line-numbered window of ``text`` centred on ``line`` (1-based).

    A non-positive ``line`` means "not line-specific" — return the head of the
    content (already byte-capped by the reader) instead of a centred window.
    """
    lines = text.splitlines()
    if line and line > 0:
        lo = max(0, line - 1 - _CONVERGE_WINDOW_LINES)
        hi = min(len(lines), line - 1 + _CONVERGE_WINDOW_LINES + 1)
    else:
        lo, hi = 0, min(len(lines), 2 * _CONVERGE_WINDOW_LINES + 1)
    return "\n".join(f"{lo + i + 1}: {s}" for i, s in enumerate(lines[lo:hi]))


def _parse_verdict(text: str) -> str:
    """Map a verifier reply to one of _VALID_VERDICTS; default unresolved.

    An explicit ``VERDICT: x`` line wins. Otherwise a single unambiguous
    verdict keyword is taken; anything ambiguous or absent is unresolved.
    """
    t = (text or "").lower()
    m = re.search(r"verdict\s*[:\-]\s*(confirmed|refuted|unresolved)", t)
    if m:
        return m.group(1)
    present = {v for v in _VALID_VERDICTS if re.search(rf"\b{v}\b", t)}
    return present.pop() if len(present) == 1 else "unresolved"


def _oneshot_text(
    info: dict, transport: str, system_prompt: str, user_msg: str,
) -> str:
    """One stateless text turn to a participant's provider — no tools, no
    thread. Returns the stripped reply text.

    The bulky ``user_msg`` is passed as the transcript (stdin on the CLI
    transport), keeping large payloads off argv; only the small fixed
    system prompt rides argv. ``transport`` (cli/api/auto) only affects
    Anthropic participants. Shared by ``roundtable_converge`` judgments
    and ``roundtable_compact`` summaries.
    """
    model = info["model"]
    provider = info["provider"]
    if provider == "anthropic":
        if transport == "cli":
            res = _call_anthropic_cli(model, system_prompt, user_msg, "")
        elif transport == "api":
            if _anthropic is None:
                raise RuntimeError("transport='api' but ANTHROPIC_API_KEY is not set.")
            res = _call_anthropic(model, system_prompt, user_msg, "")
        else:  # auto — let the router pick CLI (subscription) or SDK
            res = _call_anthropic_router(
                model, system_prompt, user_msg, "",
                tool_use_context=None, participant_label=info["label"],
            )
    elif provider == "gemini":
        res = _call_gemini(model, system_prompt, user_msg, "")
    elif provider == "openai":
        res = _call_openai(model, system_prompt, user_msg, "")
    else:
        raise RuntimeError(f"one-shot turn: unsupported provider {provider!r}")
    return (res.text or "").strip()


def _judge_finding(info: dict, transport: str, user_msg: str) -> str:
    """One verifier turn — pure text judgment, no tools. Returns reply text."""
    return _oneshot_text(info, transport, _CONVERGE_SYSTEM_PROMPT, user_msg)


def roundtable_converge(
    thread_id: int, findings: list[dict],
    verifier: str = "claude-opus", transport: str = "cli",
) -> dict:
    """Grounded confirm/refute pass over structured review findings.

    For each finding ``{claim, file, line, proof, severity}`` this fetches the
    real code at the cited ``file:line`` from the thread's bound repo — read
    via the same jailed, read-only tools the panel uses — and asks ``verifier``
    to rule it ``confirmed`` / ``refuted`` / ``unresolved`` against the actual
    code. It never re-debates, only checks. Retrieval is deterministic and
    free; only the judgment calls a model, routed by default to the free
    Anthropic CLI (subscription) transport.

    Needs a repo binding (``roundtable_bind_repo``) — verification has no
    ground truth without one. A finding whose ``file:line`` can't be read is
    pre-marked ``unresolved`` and never reaches the model.

    ``transport`` is ``"cli"`` (default, free) | ``"api"`` | ``"auto"`` and
    only affects Anthropic verifiers.

    Returns ``{"thread_id", "ledger", "summary"}`` where each ledger row is
    the finding plus ``{"verdict", "evidence", "verifier"}`` and ``summary``
    counts each verdict.
    """
    if transport not in _VALID_CONVERGE_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {sorted(_VALID_CONVERGE_TRANSPORTS)}; "
            f"got {transport!r}"
        )
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    info = _resolve_participant(verifier)
    ctx = _effective_tool_context(thread_id)
    if ctx is None or ctx.working_directory is None:
        raise RuntimeError(
            "roundtable_converge needs a repo binding (roundtable_bind_repo) "
            "to fetch ground truth for verification."
        )
    repo_tools = _RepoTools(
        Path(ctx.working_directory), ctx.permission_callback, info["label"],
    )

    if len(findings) > _CONVERGE_MAX_FINDINGS:
        raise ValueError(
            f"converge got {len(findings)} findings, over the "
            f"{_CONVERGE_MAX_FINDINGS} cap — each runs a serial verifier call. "
            f"Split into batches."
        )
    ledger: list[dict] = []
    for f in findings:
        path = (f.get("file") or "").strip()
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        row = {
            "claim": (f.get("claim") or "").strip(),
            "file": path,
            "line": line,
            "proof": (f.get("proof") or "").strip(),
            "severity": (f.get("severity") or "").strip(),
            "verifier": info["label"],
        }
        raw = repo_tools.execute("Read", {"path": path}) if path else "[no file cited]"
        if any(raw.startswith(p) for p in (
            "[no such file", "[permission denied", "[Read error", "[no file cited",
        )):
            # Cited location can't be read — can't confirm or refute it.
            ledger.append({**row, "verdict": "unresolved", "evidence": raw})
            continue
        user_msg = (
            f"CLAIM: {row['claim']}\n"
            f"LOCATION: {path}:{line}\n"
            f"SEVERITY: {row['severity'] or 'unspecified'}\n"
            f"REVIEWER'S PROOF: {row['proof'] or '(none given)'}\n\n"
            f"ACTUAL CODE at {path} (line-numbered):\n{_excerpt_around(raw, line)}"
        )
        reply = _judge_finding(info, transport, user_msg)
        ledger.append({**row, "verdict": _parse_verdict(reply), "evidence": reply})

    summary = {
        v: sum(1 for r in ledger if r["verdict"] == v) for v in _VALID_VERDICTS
    }
    return {"thread_id": thread_id, "ledger": ledger, "summary": summary}


# ─── Thread compaction ───────────────────────────────────────────────────

# Fewer messages than this in the would-be-compacted prefix isn't worth a
# summariser turn — the summary would be as long as the originals.
_COMPACT_MIN_MESSAGES = 2
# Per-artifact cap when re-showing latest artifact versions after the
# summary; middle-truncated beyond this so one huge artifact can't undo
# the compaction it rode in on.
_COMPACT_ARTIFACT_CHAR_CAP = int(
    os.environ.get("CLAUDE_ROUNDTABLE_COMPACT_ARTIFACT_CHAR_CAP", "30000")
)

_COMPACT_SYSTEM_PROMPT = (
    "You are compressing the transcript of a multi-AI roundtable so the "
    "discussion can continue in less context. Write a dense summary that "
    "preserves, in this order: (1) decisions reached and why; (2) each "
    "participant's current position, BY NAME, including unresolved "
    "disagreements; (3) open questions and pending work; (4) hard facts "
    "established — file:line references, measurements, verdicts, exact "
    "identifiers — verbatim, never paraphrased away. Do not editorialise "
    "or add recommendations of your own. Write it as briefing prose "
    "addressed to the panel, not commentary about summarising."
)


def roundtable_compact(
    thread_id: int, keep_last: int = 10,
    summarizer: str = "claude-opus", transport: str = "cli",
) -> dict:
    """Compact a long thread: replace older turns, in what participants
    see, with a model-written summary — keeping the last ``keep_last``
    messages verbatim.

    Every ask re-sends the whole transcript, so a long debate's cost grows
    with its history and eventually hits the trim cap (older turns silently
    dropped). Compaction spends one summariser turn to convert that history
    into a dense briefing: ``summarizer`` (default ``claude-opus`` on the
    free CLI ``transport``, like ``roundtable_converge``) preserves
    decisions, per-participant positions, open questions, and hard facts.
    The latest version of any artifact whose announcement falls inside the
    compacted range is re-shown after the summary, so the panel never loses
    the code under review.

    Non-destructive: raw messages stay in the DB. ``roundtable_history``
    shows the compacted view by default (what a participant would see) and
    the originals with ``raw=True``; ``roundtable_fork`` always copies raw
    history. Re-compacting later summarises the previous summary plus the
    turns since — it never re-reads already-compacted originals.

    Returns ``{"thread_id", "compacted_upto_idx", "messages_compacted",
    "kept_verbatim", "summary_chars", "artifacts_reshown"}``.
    """
    if keep_last < 0:
        raise ValueError(f"keep_last must be >= 0; got {keep_last}")
    if transport not in _VALID_CONVERGE_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {sorted(_VALID_CONVERGE_TRANSPORTS)}; "
            f"got {transport!r}"
        )
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(f"Thread {thread_id} is closed — nothing to compact for.")
    info = _resolve_participant(summarizer)

    effective = _effective_messages(thread_id)
    cut = effective[: len(effective) - keep_last] if keep_last else list(effective)
    prev = _compaction_row(thread_id)
    if prev is not None:
        # Drop nothing that's already covered: the synthetic summary message
        # carries prev's upto_idx, so "new since last compaction" is
        # everything strictly after it.
        new_in_cut = [m for m in cut if m["idx"] > prev["upto_idx"]]
        if not new_in_cut:
            raise ValueError(
                f"nothing new to compact on thread {thread_id} — the "
                f"existing compaction already covers idx <= {prev['upto_idx']} "
                f"and keep_last={keep_last} retains the rest."
            )
    if len(cut) < _COMPACT_MIN_MESSAGES:
        raise ValueError(
            f"only {len(cut)} message(s) would be compacted on thread "
            f"{thread_id} (keep_last={keep_last}) — not worth a summariser "
            f"turn; lower keep_last or let the thread grow."
        )
    upto_idx = cut[-1]["idx"]
    cutoff_ts = cut[-1]["ts"]

    trimmed = _trim_messages_to_cap(cut, PROMPT_CHAR_CAP, for_participant_label="")
    prefix_text = _format_transcript(trimmed, for_participant_label="")
    summary = _oneshot_text(info, transport, _COMPACT_SYSTEM_PROMPT, prefix_text)
    if not summary:
        raise RuntimeError(
            f"summarizer {summarizer!r} returned an empty summary — thread "
            f"left uncompacted."
        )

    # Re-show the latest version of every artifact announced inside the
    # compacted range (announcement ts <= cutoff). Artifacts announced in
    # the kept tail are still visible there and are skipped.
    with _db_lock:
        latest_rows = _conn().execute(
            "SELECT a.name, a.version, a.content, a.ts FROM artifacts a "
            "JOIN (SELECT name, MAX(version) AS v FROM artifacts "
            "      WHERE thread_id = ? GROUP BY name) m "
            "ON a.name = m.name AND a.version = m.v "
            "WHERE a.thread_id = ? ORDER BY a.name",
            (thread_id, thread_id),
        ).fetchall()
    artifact_sections: list[str] = []
    for name, version, content, ts in latest_rows:
        if ts > cutoff_ts:
            continue
        shown = _truncate_message_body(
            {"content": content}, _COMPACT_ARTIFACT_CHAR_CAP,
        )["content"]
        artifact_sections.append(
            f"=== Artifact {name!r} (v{version}, latest) ===\n{shown}\n"
            f"=== End artifact ==="
        )

    body = (
        f"[compacted transcript: messages up to idx {upto_idx} are "
        f"summarised below; the full originals remain available to the "
        f"orchestrator via roundtable_history(raw=True)]\n\n{summary}"
    )
    if artifact_sections:
        body += (
            "\n\n=== Artifacts under discussion (latest versions, re-shown "
            "after compaction) ===\n" + "\n\n".join(artifact_sections)
        )

    now = time.time()
    with _db_lock:
        _conn().execute(
            "INSERT OR REPLACE INTO compactions(thread_id, upto_idx, summary, "
            "created_at) VALUES(?, ?, ?, ?)",
            (thread_id, upto_idx, body, now),
        )
    raw_compacted = sum(
        1 for m in _thread_messages(thread_id) if m["idx"] <= upto_idx
    )
    return {
        "thread_id": thread_id,
        "compacted_upto_idx": upto_idx,
        "messages_compacted": raw_compacted,
        "kept_verbatim": len(effective) - len(cut),
        "summary_chars": len(body),
        "artifacts_reshown": len(artifact_sections),
    }


def roundtable_set_artifact(
    thread_id: int, name: str, content: str,
) -> dict:
    """Store a named artifact on the thread and announce it in the transcript.

    Use this for the code (or doc, spec, prompt) the roundtable is
    iterating on. Each call bumps the version automatically — v1 on
    first set, v2 on next, etc. A synthetic ``orchestrator`` message is
    appended to the thread containing the full new content AND a
    unified diff against the previous version, so participants see
    what changed without you re-pasting the whole file.

    Returns ``{"thread_id", "name", "version", "diff_omitted": bool}``.
    ``diff_omitted`` is true when the diff exceeded the size cap and
    only the full new content was included in the transcript (still
    queryable via ``roundtable_get_artifact`` for the prior version).
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        raise RuntimeError(
            f"Thread {thread_id} is closed — cannot update artifacts. "
            f"Create a new thread if the work continues."
        )
    name = name.strip()
    if not name:
        raise ValueError("Artifact name must be a non-empty string.")

    # Take the lock for the version-bump + INSERT + transcript append so two
    # concurrent set_artifact calls on the same name don't both compute
    # new_v = prev_v + 1 and collide on the artifacts PK. The standalone
    # roundtable-mcp process shares this DB and isn't covered by _db_lock, so
    # retry on IntegrityError with a freshly-read version — the body embeds
    # new_v, so it's recomputed each attempt.
    for _attempt in range(_IDX_COLLISION_RETRIES):
        with _db_lock:
            prev_v = _latest_artifact_version(thread_id, name)
            new_v = prev_v + 1
            old = (
                _get_artifact_content(thread_id, name, prev_v) or ""
                if prev_v > 0 else ""
            )

            diff_omitted = False
            if prev_v == 0:
                body = (
                    f"=== Artifact {name!r} (v{new_v}) ===\n"
                    f"{content}\n"
                    f"=== End artifact ==="
                )
            else:
                diff_text = _render_artifact_diff(old, content, name, prev_v, new_v)
                if len(diff_text) > ARTIFACT_DIFF_CHAR_CAP:
                    diff_omitted = True
                    diff_block = (
                        f"[diff vs v{prev_v} omitted — exceeded "
                        f"{ARTIFACT_DIFF_CHAR_CAP}-char cap, treat as a full "
                        f"rewrite]"
                    )
                else:
                    diff_block = f"--- Diff vs v{prev_v} ---\n{diff_text}".rstrip()
                body = (
                    f"=== Artifact {name!r} updated to v{new_v} ===\n"
                    f"{diff_block}\n\n"
                    f"--- Full v{new_v} content ---\n"
                    f"{content}\n"
                    f"=== End artifact ==="
                )

            try:
                _conn().execute(
                    "INSERT INTO artifacts(thread_id, name, version, content, ts) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (thread_id, name, new_v, content, time.time()),
                )
            except sqlite3.IntegrityError:
                continue
            # We're already inside _db_lock so the nested acquire inside
            # _append_message is harmless (RLock allows re-entry). Calling
            # _append_message keeps the next-idx logic in one place.
            _append_message(thread_id, "orchestrator", body)
            return {
                "thread_id": thread_id,
                "name": name,
                "version": new_v,
                "diff_omitted": diff_omitted,
            }
    raise RuntimeError(
        f"could not allocate an artifact version for {name!r} on thread "
        f"{thread_id} after {_IDX_COLLISION_RETRIES} attempts (cross-process "
        f"contention)"
    )


def roundtable_get_artifact(
    thread_id: int, name: str, version: int = 0,
) -> dict:
    """Return the stored content of an artifact.

    ``version=0`` (default) returns the latest version. A positive
    integer returns that specific version (useful for "show me what v1
    looked like before I changed it"). Raises if the artifact name
    has never been set on this thread, or if the requested version
    does not exist.

    Returns ``{"thread_id", "name", "version", "content"}``.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    latest = _latest_artifact_version(thread_id, name)
    if latest == 0:
        raise ValueError(
            f"No artifact named {name!r} on thread {thread_id}."
        )
    want = latest if version == 0 else version
    content = _get_artifact_content(thread_id, name, want)
    if content is None:
        raise ValueError(
            f"Artifact {name!r} on thread {thread_id} has no v{want} "
            f"(latest is v{latest})."
        )
    return {"thread_id": thread_id, "name": name, "version": want, "content": content}


def roundtable_fork(
    thread_id: int, upto_idx: int = -1, new_topic: str = "",
    copy_artifacts: bool = True,
) -> dict:
    """Branch a thread at a specific message index into a new thread.

    Useful when a debate has gone off the rails late but earlier turns
    were productive — fork the productive prefix and steer differently
    from there, without losing or polluting the original thread.

    ``upto_idx`` is the highest message index to copy, inclusive. ``-1``
    (default) copies the whole transcript. Messages keep their content
    and speaker but receive fresh idx values starting at 0 in the new
    thread, so the chronology is preserved without holes.

    ``new_topic`` becomes the topic of the forked thread. If empty,
    defaults to ``"Fork of <original topic>"``. House rules and the
    registered participants list are inherited from the source thread —
    a fork is a continuation of the same conversation, just on a new
    timeline.

    ``copy_artifacts`` (default True): also copy artifact versions whose
    insertion timestamp is at or before the timestamp of the cutoff
    message — i.e. artifacts as they existed at the fork point. Set
    False if you're forking specifically to start with a clean slate of
    artifacts (rare).

    Returns ``{"thread_id", "topic", "messages_copied", "artifacts_copied"}``.
    The source thread is NOT modified — fork is read-only on the original.
    """
    source = _thread_row(thread_id)
    if source is None:
        raise ValueError(f"No such thread: {thread_id}")
    src_messages = _thread_messages(thread_id)
    if not src_messages:
        raise ValueError(
            f"Thread {thread_id} has no messages to fork from."
        )

    # Resolve cutoff. -1 = include everything; otherwise must be a
    # valid index that actually exists in the source thread.
    if upto_idx < 0:
        cutoff_idx = src_messages[-1]["idx"]
    else:
        if not any(m["idx"] == upto_idx for m in src_messages):
            raise ValueError(
                f"upto_idx={upto_idx} does not exist on thread {thread_id} "
                f"(valid range: 0..{src_messages[-1]['idx']})."
            )
        cutoff_idx = upto_idx
    keep = [m for m in src_messages if m["idx"] <= cutoff_idx]
    cutoff_ts = keep[-1]["ts"]

    topic = new_topic.strip() or f"Fork of {source['topic']}"

    with _db_lock:
        cur = _conn().execute(
            "INSERT INTO threads(topic, participants_json, created_at, house_rules) "
            "VALUES(?, ?, ?, ?)",
            (
                topic,
                json.dumps(source.get("participants") or []),
                time.time(),
                source.get("house_rules") or None,
            ),
        )
        new_id = int(cur.lastrowid)
        # Copy messages with fresh idx values starting at 0. We keep the
        # original speaker labels so participant attribution survives the
        # fork, but reset ts to now so list-ordering reflects fork time.
        now = time.time()
        for new_idx, m in enumerate(keep):
            _conn().execute(
                "INSERT INTO messages(thread_id, idx, speaker, content, ts) "
                "VALUES(?, ?, ?, ?, ?)",
                (new_id, new_idx, m["speaker"], m["content"], now),
            )
        artifacts_copied = 0
        if copy_artifacts:
            rows = _conn().execute(
                "SELECT name, version, content, ts FROM artifacts "
                "WHERE thread_id = ? AND ts <= ? "
                "ORDER BY name, version",
                (thread_id, cutoff_ts),
            ).fetchall()
            for name, version, content, ts in rows:
                _conn().execute(
                    "INSERT INTO artifacts(thread_id, name, version, content, ts) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (new_id, name, version, content, ts),
                )
                artifacts_copied += 1
    return {
        "thread_id": new_id,
        "topic": topic,
        "messages_copied": len(keep),
        "artifacts_copied": artifacts_copied,
    }


def roundtable_history(thread_id: int, last_n: int = 0, raw: bool = False) -> str:
    """Return the formatted transcript of a thread.

    ``last_n=0`` (default) returns everything. A positive value returns
    only the most recent N messages — useful for getting a quick read on
    where a long debate stands without pulling the entire history into
    context.

    Format matches what participants see (``[speaker]:\\ncontent``),
    so this also doubles as a way to debug what a participant would have
    seen if asked right now — on a compacted thread (see
    ``roundtable_compact``) that means the summary plus the kept tail.
    ``raw=True`` bypasses compaction and returns every original message.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    all_messages = (
        _thread_messages(thread_id) if raw else _effective_messages(thread_id)
    )
    total = len(all_messages)
    messages = all_messages[-last_n:] if last_n > 0 else all_messages
    compact_note = ""
    if not raw and _compaction_row(thread_id) is not None:
        compact_note = " (compacted view — raw=True for full history)"
    header = (
        f"# Thread {thread['id']}: {thread['topic']}\n"
        f"# Participants: {', '.join(thread.get('participants') or []) or '(none registered)'}\n"
        f"# Messages: {total}{compact_note}"
        + (" (showing last %d)" % last_n if last_n > 0 else "")
        + "\n\n"
    )
    return header + _format_transcript(messages, for_participant_label="")


def roundtable_list(open_only: bool = True, limit: int = 50) -> list[dict]:
    """List threads ordered by most-recent-message.

    ``open_only`` filters out threads that have been explicitly closed
    via ``roundtable_close``. ``limit`` caps the result set so a
    long-lived deployment doesn't return thousands of dead threads.
    """
    sql = (
        "SELECT t.id, t.topic, t.participants_json, t.created_at, t.closed_at, "
        "       COALESCE(MAX(m.ts), t.created_at) AS last_activity, "
        "       COUNT(m.idx) AS msg_count "
        "FROM threads t LEFT JOIN messages m ON m.thread_id = t.id "
    )
    if open_only:
        sql += "WHERE t.closed_at IS NULL "
    sql += "GROUP BY t.id ORDER BY last_activity DESC LIMIT ?"
    with _db_lock:
        rows = _conn().execute(sql, (limit,)).fetchall()
    return [
        {
            "thread_id": r[0],
            "topic": r[1],
            "participants": json.loads(r[2] or "[]"),
            "created_at": r[3],
            "closed_at": r[4],
            "last_activity": r[5],
            "messages": r[6],
        }
        for r in rows
    ]


def roundtable_close(thread_id: int) -> dict:
    """Mark a thread closed.

    Closed threads remain queryable via ``roundtable_history`` /
    ``roundtable_list(open_only=False)`` but ``roundtable_ask`` refuses
    them. This is a soft signal — nothing prevents creating a new
    thread that references the old one if you want to continue the
    conversation after a closure.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    if thread.get("closed_at"):
        return {"thread_id": thread_id, "already_closed_at": thread["closed_at"]}
    now = time.time()
    with _db_lock:
        _conn().execute(
            "UPDATE threads SET closed_at = ? WHERE id = ?", (now, thread_id),
        )
    return {"thread_id": thread_id, "closed_at": now}


def roundtable_participants() -> dict:
    """List the participants the server knows how to route to.

    Each entry includes the provider, model id, display label, and a
    flag for whether the matching API key was configured at startup.
    Useful when the caller needs to pick a participant from a known
    list (instead of memorising the keys above).
    """
    return {
        name: {
            **info,
            "available": _participant_provider_available(name),
        }
        for name, info in PARTICIPANTS.items()
    }


