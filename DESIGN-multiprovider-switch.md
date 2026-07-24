# Mid-chat provider switching (Claude ⇄ Codex)

Status: design locked, verified against code + live spikes 2026-07-23. Slice 0 in build.

## Goal

Let a user switch AI provider inside one conversation, with the destination
provider carrying over the prior turns. Today provider is fixed per
conversation: `ActiveRun.provider` is set once (`app.py:6021`) and `/api/chat`
hard-rejects a provider that doesn't match the session's registry entry
(`app.py:9227-9230`).

## The decomposition (why the obvious framing is wrong)

The naive framing is a binary: (A) shared UI shell, each provider blind to the
other's turns, vs (B) a lossy text "handoff". Both are wrong because they
collapse three distinct lifecycles into one `session_id`-bound-to-a-provider
object. Split them:

- **Conversation** — provider-neutral identity + ordered, user-visible history.
  The thing that persists and that switching operates on.
- **Provider binding** — 0..1 live Claude session and 0..1 Codex thread attached
  to a conversation, each with a `synced_through_seq` watermark.
- **Run** — one immutable execution attempt against exactly one binding.

Once split, A and B stop being architectures and become policy on top of the
same model.

## Source-of-truth split (the load-bearing rule)

A canonical append-only `conversation_event` log is authoritative for
**ordering, UI, ownership, switching, export** — and nothing else. Native
transcripts stay authoritative for **resumability, compaction, tool
continuation, and file checkpoints**. The canonical log is *not* executable
state; replaying a historical `tool_use` as a live call would risk
re-execution.

## Capture point — VERIFIED CORRECTION

Canonical events MUST be captured at raw provider ingress, **not** at
`ActiveRun.emit`. `emit` is downstream of truncation:

- Claude: full result is `blk.content` at `app.py:11517`; it is sliced to
  `text[:TOOL_RESULT_PREVIEW*4]` (800 chars) at `app.py:11528` *before* the
  event dict reaches `emit` (`app.py:6035`).
- Codex: `_tool_result_event` slices at `codex_provider.py:580`
  (`preview_cap = TOOL_RESULT_PREVIEW*4`, passed at `app.py:9072`).
- The live cap is **800** (200×4), not 200. The bare `TOOL_RESULT_PREVIEW=200`
  at `app.py:1153` is the history-reader (`session_transcript`) path, a
  different code path from the live stream.

So: use `emit` only for events that arrive uncut (`run_started`,
`user_prompt`, errors, `result`). Capture tool content at:
- Claude: `blk.content` at `app.py:11517`, before the slice.
- Codex: the raw `item` (~`app.py:9058-9060`), before `codex_provider.item_events()`.

Capturing at `emit` would bake 800-char-truncated tool results into the source
of truth permanently — an unrecoverable defect.

## Spike evidence (both directions proven)

- **Codex → Claude — PROVEN.** Forged a `<uuid>.jsonl` in the CLI's on-disk
  transcript format (cloned a real user+assistant envelope, rewrote identity +
  content to a synthetic exchange with secret `HELIOTROPE-42`), wrote it under
  `~/.claude/projects/<sanitized-cwd>/`, ran
  `claude -p "…passphrase?" --resume <id> --model haiku`. It replied
  `HELIOTROPE-42`. The CLI ingests fabricated, non-append-produced history as
  real context. Claude Code 2.1.198.
- **Claude → Codex — PROVEN by shipping code.** The existing driver already
  sends input items via `turn/start` (`app.py:8906`); fresh thread + injected
  first message needs no new surface.

## Codex `externalAgentConfig/import` — real but not a live bridge

The 0.144.6 app-server exposes `externalAgentConfig/{detect,import,readHistories}`.
`import` takes `migrationItems[].itemType ∈ {AGENTS_MD, CONFIG, SKILLS, PLUGINS,
MCP_SERVER_CONFIG, SUBAGENTS, HOOKS, COMMANDS, SESSIONS}`; a `SESSIONS` item is
`{cwd, path, title}` pointing at an external session file, imported into codex
rollout history at **session granularity**. It's a migration/onboarding tool
(mature since ~0.128), conservative-and-lossy by design. Usable as an optional
fidelity upgrade for the Claude→Codex *fork* (we mint a new thread anyway);
**not** usable to inject a delta into a live thread. Never a dependency — the
fresh-thread + injected-message path is the baseline.

## Robustness / version policy

The Claude on-disk JSONL format is internal and drifts between releases (direct
parsers "can break on any release"). Therefore the Claude adapter
(canonical → forged JSONL) is a **capability-probed fast path** with a
**text-preamble fallback**. Cache the probe by `(provider, binary_version,
adapter_version)`; fail closed to the text adapter when a fixture probe fails.
Validate tool-use/tool-result pairing before marking a conversation eligible for
forged-JSONL projection (2.1.218 hardened resume against unpaired blocks).
Keep the codex schema dump as a build artifact keyed by `codex --version`.

## Schema (additive; migrate via the `PRAGMA table_info` pattern at `app.py:1823`)

`conversation(conversation_id PK, owner_sub, project_key, title, last_seq,
capture_state[live_complete|legacy_partial|incomplete], created_at, updated_at)`

`conversation_binding(binding_id PK, conversation_id, provider CHECK(claude|codex),
native_session_id, status[provisional|active|superseded|deleted],
synced_through_seq, provider_version, created_at, updated_at)`
— partial unique index on `(conversation_id, provider) WHERE status IN
('provisional','active')` so exactly one binding is live but superseded rows
survive as aliases (old URLs keep resolving).

`conversation_event(conversation_id, seq, source_key, provider, binding_id,
run_id, run_event_idx, event_type, visibility[transcript|control|internal],
replayable, normalized_json, raw_json, original_bytes, stored_bytes,
payload_sha256, truncated, created_at)`
— PK `(conversation_id, seq)`; unique `(conversation_id, source_key)` for
idempotent dedup. Allocate `seq` atomically in one transaction
(`INSERT … SELECT COALESCE(MAX(seq),0)+1 … WHERE`), never read-then-write in
Python — a switch/reconnect race can briefly have two runs writing the same
`conversation_id`.

Extend `runs` with `provider, conversation_id, binding_id`; write in
`_persist_run_meta` (`app.py:5056`), restore in `_restore_persisted_runs`
(`app.py:5255`) instead of defaulting recovered runs to Claude.

Canonical write failure: log loudly, mark the conversation `incomplete`, **do
not kill the run**, make it Slice-1-ineligible.

## Slices

- **Slice 0 — live shadow-write + schema + lazy 1:1 wrap.** Strictly additive,
  zero behavior change. Capture at ingress (above); lazy-wrap existing sessions
  on `list_sessions` (`app.py:932`) and `api_session` (`app.py:7295`) via an
  idempotent ensure-helper keyed `(provider, project_key, native_id)`; keep
  returning native transcript unchanged; mark wrapped history `legacy_partial`;
  do not claim ownerless sessions. Extend `runs`, link conversation+binding in
  `api_chat` (`app.py:9182`), activate the binding when the native id first
  appears (`app.py:6052-6057` for Claude, `_record_codex_session` `app.py:8876`
  for Codex).
- **Slice 1 — cross-provider fork as the switch.** On a provider-changed
  `/api/chat`, project canonical events through a destination adapter, mint a
  new `session_id`, clean break. Disable rewind across a foreign-provider turn.
- **Slice 2 — optional guarded in-place switch.** Only between turns, empty
  queue, no pending permission cards, no live background tasks.

## Slice 1 landmines (bank now, handle then)

- **Codex authority inversion / prompt injection.** A fresh Codex thread + one
  injected `turn/start` text item flattens roles: Claude assistant text,
  historical tool output, and any attacker-influenced file content all arrive as
  the *current user instruction*. Mitigate: label the handoff an untrusted
  historical record; length-prefix sections (don't trust XML/MD delimiters);
  exclude raw tool output by default (project name + bounded args + outcome +
  digest); append the real new prompt in a separate delimited block.
- **Projection budget.** Codex app-server flags injected items >1K tokens and
  caps individual items ~10K. The handoff must be bounded + compacted, recording
  which canonical `seq` range was omitted/summarized. Never serialize an
  unbounded conversation into the single item at `app.py:8903`.
- **Background subagents are default in 2.1.198.** "Between turns" is not a
  sufficient switch barrier — a main turn can complete while a background
  subagent still mutates the workspace. Gate on no live background tasks
  (`app.py:11145-11189`).
- **Rewind = silent workspace corruption.** Claude rewind is a checkpoint
  file-restore, Codex-unaware (`app.py:10744`). Claude → Codex edits → Claude →
  rewind wipes the Codex edits. Disable across a foreign turn or hard-warn.
- **SSE cursor.** Per-run `_idx` (`app.py:1650`) gives no conversation-global
  cursor; use canonical `seq` for reconnect dedup across a merged view.

## Tests (Slice 0 gate)

Idempotent migration, no data loss; repeated list/open ⇒ one conversation + one
binding; concurrent ensure ⇒ no duplicate; ownerless sessions stay ownerless;
Claude/Codex provisional binding activates when native id appears; restart
restores provider/conversation/binding linkage; a 2,000-char Claude tool result
and a 2,000-char Codex command result each store **>800** chars; duplicate Codex
`item/completed` allocates no second `seq`; `seq` strictly increasing across two
runs in one conversation; canonical-DB failure doesn't break SSE but bars
switching; `/api/sessions` and `/api/sessions/{sid}` payloads byte-identical to
today.
