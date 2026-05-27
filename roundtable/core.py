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
import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

import anthropic
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

# At least one viable Anthropic path OR another provider must be set, or
# the server has nothing to route to.
_anthropic_available = bool(_anthropic_key) or _CLAUDE_CLI is not None
if not (_gemini_key or _openai_key or _anthropic_available):
    raise RuntimeError(
        "Need at least one of: GEMINI_API_KEY, OPENAI_API_KEY, "
        "ANTHROPIC_API_KEY, or a 'claude'/'claude-ha' binary on PATH "
        "(for the subscription-auth transport). Server has nothing to "
        "route to without one of these."
    )

_gemini = genai.Client(api_key=_gemini_key) if _gemini_key else None
_openai = OpenAI(api_key=_openai_key) if _openai_key else None
_anthropic = anthropic.Anthropic(api_key=_anthropic_key) if _anthropic_key else None


# ─── Participant registry ────────────────────────────────────────────────

# Provider-agnostic short names. Adding a new entry here is enough; no
# tool signatures need to change. Pinned aliases (latest, etc.) so a
# provider model bump applies automatically without redeploy. The
# ``label`` shown in transcripts and to participants is intentionally
# distinct from the model id — a roundtable participant identifies
# itself by role ("Gemini Pro"), not by version string.
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
        "model": "gpt-5.4-mini",
        "label": "GPT-5 Mini",
    },
    "gpt-5": {
        "provider": "openai",
        "model": "gpt-5.5",
        "label": "GPT-5",
    },
    "claude-sonnet": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "label": "Claude Sonnet",
    },
    "claude-opus": {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "label": "Claude Opus",
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
        # Schema migrations: older DBs may predate columns added after
        # initial release. Symmetric ALTERs so any column added later is
        # backfilled with its CREATE TABLE default rather than crashing
        # _thread_row / roundtable_create on a missing column.
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
    if row is None:
        return None
    return {
        "id": row[0],
        "topic": row[1],
        "participants": json.loads(row[2] or "[]"),
        "created_at": row[3],
        "closed_at": row[4],
        "house_rules": row[5] or "",
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

    The read-then-insert pair is held under ``_db_lock`` so concurrent
    callers can't both observe the same MAX(idx) and race the INSERT.
    """
    with _db_lock:
        idx = _next_idx(thread_id)
        _conn().execute(
            "INSERT INTO messages(thread_id, idx, speaker, content, ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (thread_id, idx, speaker, content, time.time()),
        )
    return idx


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
        if tools_enabled else ""
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
        f"{web_clause}{tools_clause}"
    )
    house_rules = (thread.get("house_rules") or "").strip()
    if house_rules:
        base += (
            "\n\n=== House rules for this thread (apply to every reply) ===\n"
            + house_rules
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
            result = fn()
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
#   - Gemini 2.5 Flash thinking budgets are 0–24576 tokens; 2.5 Pro has
#     dynamic budget when set to -1. We pick conservative integer budgets
#     that work on both tiers.
#   - Anthropic Opus 4.7 / Sonnet 4.6 only support ``thinking={"type":
#     "adaptive"}``; the deprecated ``enabled``+``budget_tokens`` form is
#     scheduled for removal. So effort here is just an on/off toggle —
#     anything ≥ medium turns thinking on, low disables it.
_GEMINI_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}


def _call_gemini(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
) -> str:
    """Send the rendered transcript + final instruction to Gemini.

    Gemini supports a separate ``system_instruction`` parameter, so we
    use it instead of cramming the system text into the user message.
    The user message is the transcript followed by the orchestrator's
    instruction (if any) — that pattern keeps the model's "what do I
    do next?" prompt right at the end of the input where it has the
    strongest priming effect.

    When ``effort`` is provided we attach a ``ThinkingConfig`` so the
    model spends extra compute reasoning before producing its visible
    reply. Thoughts themselves are not surfaced to the transcript.

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
    if effort and effort in _GEMINI_BUDGETS:
        config["thinking_config"] = genai_types.ThinkingConfig(
            thinking_budget=_GEMINI_BUDGETS[effort],
        )
    if web_search:
        config["tools"] = [
            genai_types.Tool(google_search=genai_types.GoogleSearch()),
        ]

    def _do_call():
        return _gemini.models.generate_content(
            model=model, contents=user_msg, config=config,
        )

    resp = _provider_call(f"gemini/{model}", _do_call)
    return resp.text or ""


def _call_openai(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
) -> str:
    """Send the same conversation shape to OpenAI. gpt-5.x rejects
    ``max_tokens`` and requires ``max_completion_tokens`` — we omit
    both here and let the model use its default, matching the existing
    openai-mcp server's pattern for review/brainstorm calls.

    When ``effort`` is provided we pass it through as ``reasoning_effort``;
    gpt-5.x will then take more time on the turn before emitting its
    response. Non-reasoning models would reject the parameter, but every
    OpenAI participant in this registry is a gpt-5.x variant.

    When ``web_search`` is True we switch to the Responses API and
    attach the hosted ``web_search`` tool — chat.completions does not
    surface OpenAI's hosted tools for gpt-5.x, only the Responses API
    does. The response shape differs (``output_text`` instead of
    ``choices[0].message.content``) so we branch the call sites; the
    no-search path keeps using chat.completions to avoid disturbing the
    well-trodden code path that drives every existing roundtable.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"

    if web_search:
        kwargs: dict = {
            "model": model,
            "instructions": system_prompt,
            "input": user_msg,
            "tools": [{"type": "web_search"}],
            "timeout": PROVIDER_TIMEOUT_SEC,
        }
        if effort:
            kwargs["reasoning"] = {"effort": effort}

        def _do_call_resp():
            return _openai.responses.create(**kwargs)

        resp = _provider_call(f"openai/{model}", _do_call_resp)
        return resp.output_text or ""

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "timeout": PROVIDER_TIMEOUT_SEC,
    }
    if effort:
        kwargs["reasoning_effort"] = effort

    def _do_call():
        return _openai.chat.completions.create(**kwargs)

    resp = _provider_call(f"openai/{model}", _do_call)
    return resp.choices[0].message.content or ""


# Anthropic Messages API requires ``max_tokens`` even when nothing is being
# truncated. We pick a generous cap so a thorough review (or a long
# adaptive-thinking turn that has to fit thoughts + reply inside the same
# budget) doesn't get clipped. Adaptive thinking shares this budget with
# the final response, so it needs to be larger than the visible reply
# would otherwise require.
_ANTHROPIC_MAX_TOKENS = int(
    os.environ.get("CLAUDE_ROUNDTABLE_ANTHROPIC_MAX_TOKENS", "16384")
)


def _call_anthropic(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
) -> str:
    """Send the transcript to an Anthropic model via the Messages API.

    Anthropic's API keeps the system prompt as a top-level ``system``
    field (unlike OpenAI's role=system message), and only supports a
    single content stream per message — so we collapse the transcript +
    orchestrator instruction into one user message, mirroring how the
    other two providers receive it.

    When ``effort`` is medium or high we enable adaptive extended
    thinking, which lets Opus / Sonnet allocate their own internal
    reasoning budget before producing the visible response. Adaptive is
    the only supported thinking mode on 4.6 / 4.7 — the older
    ``type=enabled`` with ``budget_tokens`` is deprecated. The visible
    response is composed of ``text`` blocks; ``thinking`` blocks are
    dropped before returning so the transcript stays human-readable.

    When ``web_search`` is True we attach Anthropic's hosted
    ``web_search`` server tool (the 2026-02-09 revision the SDK ships)
    so the model can fetch current information mid-turn.
    """
    user_msg = transcript
    if instruction:
        user_msg += f"\n\n[orchestrator]:\n{instruction}"
    kwargs: dict = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
        "max_tokens": _ANTHROPIC_MAX_TOKENS,
        "timeout": PROVIDER_TIMEOUT_SEC,
    }
    if effort in {"medium", "high"}:
        kwargs["thinking"] = {"type": "adaptive"}
    if web_search:
        kwargs["tools"] = [
            {"type": "web_search_20260209", "name": "web_search"},
        ]

    def _do_call():
        return _anthropic.messages.create(**kwargs)

    resp = _provider_call(f"anthropic/{model}", _do_call)
    text_parts = [
        block.text for block in resp.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(text_parts)


def _call_anthropic_cli(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
) -> str:
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

    def _do_call() -> str:
        proc = subprocess.run(
            args,
            input=user_msg,
            text=True,
            capture_output=True,
            timeout=PROVIDER_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            # Surface stderr — claude-ha prints account-switching diagnostics
            # there, and any auth/quota failure will be readable.
            tail = (proc.stderr or "")[-2000:]
            raise RuntimeError(
                f"claude CLI exit={proc.returncode}; stderr tail: {tail!r}"
            )
        return proc.stdout

    return _provider_call(f"anthropic-cli/{model}", _do_call)


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
) -> str:
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

    return _provider_call(f"anthropic-sdk-tools/{model}", _do_call)


def _call_anthropic_router(
    model: str, system_prompt: str, transcript: str, instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
    participant_label: str = "",
) -> str:
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
        return _call_anthropic_sdk_with_tools(
            model, system_prompt, transcript, instruction, effort, web_search,
            tool_use_context, participant_label,
        )
    if _ANTHROPIC_TRANSPORT == "cli":
        return _call_anthropic_cli(
            model, system_prompt, transcript, instruction, effort, web_search,
        )
    if _ANTHROPIC_TRANSPORT == "api":
        if _anthropic is None:
            raise RuntimeError(
                "transport=api but ANTHROPIC_API_KEY is not set."
            )
        return _call_anthropic(
            model, system_prompt, transcript, instruction, effort, web_search,
        )
    # auto: prefer CLI (subscription) if available, else SDK (API).
    if _CLAUDE_CLI is not None:
        return _call_anthropic_cli(
            model, system_prompt, transcript, instruction, effort, web_search,
        )
    if _anthropic is not None:
        return _call_anthropic(
            model, system_prompt, transcript, instruction, effort, web_search,
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

    Returns ``{"thread_id", "topic", "participants", "house_rules"}``.
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
    with _db_lock:
        cur = _conn().execute(
            "INSERT INTO threads(topic, participants_json, created_at, house_rules) "
            "VALUES(?, ?, ?, ?)",
            (topic, json.dumps(participants), time.time(), house_rules or None),
        )
        thread_id = int(cur.lastrowid)
    return {
        "thread_id": thread_id,
        "topic": topic,
        "participants": participants,
        "house_rules": house_rules,
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


def _run_turn(
    thread: dict, info: dict, messages: list[dict], instruction: str,
    effort: Optional[str] = None, web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
) -> str:
    """Render the transcript for one participant and call its provider.

    Pure function over the inputs — no DB writes. Caller decides when to
    append the response to the thread, which lets parallel asks commit
    several responses atomically (and against the same transcript
    snapshot) without one bleed-through into another's view.

    ``tool_use_context`` (Anthropic-only for now) opts the participant
    into real filesystem tool use, with every tool call routed through
    the supplied permission_callback. Non-Anthropic providers ignore it.
    """
    trimmed = _trim_messages_to_cap(
        messages, PROMPT_CHAR_CAP, for_participant_label=info["label"],
    )
    transcript = _format_transcript(trimmed, for_participant_label=info["label"])
    system_prompt = _build_system_prompt(
        thread, info["label"], thread.get("participants") or [],
        web_search=web_search,
        tools_enabled=(
            tool_use_context is not None
            and tool_use_context.working_directory is not None
            and info["provider"] == "anthropic"
        ),
    )
    provider = info["provider"]
    if provider == "gemini":
        return _call_gemini(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search,
        )
    if provider == "openai":
        return _call_openai(
            info["model"], system_prompt, transcript, instruction,
            effort, web_search,
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
        )
    raise RuntimeError(f"Unknown provider {provider!r} for model {info['model']!r}")


def roundtable_ask(
    thread_id: int, participant: str, prompt: str = "", effort: str = "",
    web_search: bool = False,
    tool_use_context: Optional[ToolUseContext] = None,
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
    ``gpt-5``, ``claude-sonnet``, ``claude-opus``).

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
    messages = _thread_messages(thread_id)
    try:
        response = _run_turn(
            thread, info, messages, "", effort, web_search,
            tool_use_context=tool_use_context,
        )
    except Exception as exc:
        # Surface the provider error to the caller AND record it in the
        # thread so subsequent participants can see what went wrong (e.g.
        # rate limit, context too long, content-policy refusal) instead
        # of being confused by a missing turn.
        err_msg = f"[provider error: {type(exc).__name__}: {exc}]"
        _append_message(thread_id, info["label"], err_msg)
        raise

    response = response.strip()
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
    snapshot = _thread_messages(thread_id)

    # Empty instruction in _run_turn — the prompt is already in the
    # snapshot. Passing it again would duplicate it for each participant.
    # Catch only Exception, NOT BaseException — swallowing
    # KeyboardInterrupt/SystemExit would prevent clean shutdown and
    # record signals as fake provider errors in the transcript.
    def _one(name: str) -> tuple[str, str, Optional[Exception]]:
        info = infos[name]
        try:
            resp = _run_turn(
                thread, info, snapshot, instruction="", effort=effort,
                web_search=web_search,
                tool_use_context=tool_use_context,
            )
            return name, resp, None
        except Exception as exc:
            return name, "", exc

    results: dict[str, tuple[str, Optional[Exception]]] = {}
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
        clean = resp.strip() or "[empty response from provider]"
        if closed_mid_flight:
            clean = clean + (
                "\n\n[note: thread was closed during this call; response "
                "recorded post-closure]"
            )
        _append_message(thread_id, label, clean)
        responses[name] = clean

    return {"thread_id": thread_id, "responses": responses, "errors": errors}


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

    # Compute diff/body OUTSIDE the lock — it's pure work over inputs +
    # the already-stored prior version (which is immutable once written).
    # Then take the lock just for the version-bump + INSERT + transcript
    # append so two concurrent set_artifact calls on the same name don't
    # both compute new_v = prev_v + 1 and collide on the artifacts PK.
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

        _conn().execute(
            "INSERT INTO artifacts(thread_id, name, version, content, ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (thread_id, name, new_v, content, time.time()),
        )
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


def roundtable_history(thread_id: int, last_n: int = 0) -> str:
    """Return the formatted transcript of a thread.

    ``last_n=0`` (default) returns everything. A positive value returns
    only the most recent N messages — useful for getting a quick read on
    where a long debate stands without pulling the entire history into
    context.

    Format matches what participants see (``[speaker]:\\ncontent``),
    so this also doubles as a way to debug what a participant would have
    seen if asked right now.
    """
    thread = _thread_row(thread_id)
    if thread is None:
        raise ValueError(f"No such thread: {thread_id}")
    all_messages = _thread_messages(thread_id)
    total = len(all_messages)
    messages = all_messages[-last_n:] if last_n > 0 else all_messages
    header = (
        f"# Thread {thread['id']}: {thread['topic']}\n"
        f"# Participants: {', '.join(thread.get('participants') or []) or '(none registered)'}\n"
        f"# Messages: {total}"
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


