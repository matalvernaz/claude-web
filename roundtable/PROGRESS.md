# roundtable-mcp — improvement work: progress & plan

_Last updated: 2026-07-03._

## 2026-07-03 — panel tool-loop exhaustion fix (the "[empty response from provider]" bug)

Root cause of GPT-5 "failing a lot" on repo-bound threads (94/98/100): at
effort=high, gpt-5.5 investigates one Read/Grep per round, exhausts
`_PANEL_TOOL_MAX_ROUNDS` (12) mid-investigation, and the loop returned the
last response's `output_text` — empty, because that response is
reasoning + function calls only. Committed verbatim as
"[empty response from provider]". Reproduced by replaying thread 100's
exact snapshot; confirmed 12 rounds of legitimate single-call turns.
Gemini never hit it (answers after a couple of chunky reads) but shared the
same cliff.

Fix (both `_call_openai_with_tools` and `_call_gemini_with_tools`):
- On exhaust, append `_TOOL_BUDGET_FINAL_INSTRUCTION` and make ONE final
  no-tools call (OpenAI `tool_choice="none"`; Gemini `tool_config` mode
  `NONE` — tools stay declared because history holds function parts).
- If even that yields no text, surface an explicit
  "[tool budget exhausted after N rounds …]" marker instead of the generic
  empty marker.
- `_PANEL_TOOL_MAX_ROUNDS` now env-tunable:
  `CLAUDE_ROUNDTABLE_PANEL_TOOL_MAX_ROUNDS` (default 12).
- Per-round provider calls now go through `_provider_call` (retry/backoff +
  logging) — the tools path previously had NO retries, which is why raw
  `APITimeoutError: Request timed out.` surfaced as participant errors.

Verified: replayed thread 100 ask-1 through the patched loop — 12 rounds,
forced final call, 8.7k-char audit text returned. Known-remaining (not
fixed): usage rows for tool-loop turns record only the final round's
tokens; claude-opus CLI-path error "returned an error result: success"
(thread 100 msgs 4/7) is a separate bug.

Self-audit of roundtable-mcp + the gemini-mcp/openai-mcp servers, driven through
the roundtable itself (threads 60 = audit, 61 = design pass), then implemented in
phases. This doc lets a future session resume cleanly.

## Canonical location — `claude-web/roundtable/` (sole copy)

The live roundtable MCP server is launched with `PYTHONPATH=/home/matt/claude-web`
and `-m roundtable.mcp_server`, so it imports **`/home/matt/claude-web/roundtable/`**,
git-tracked inside the `claude-web` repo — the one and only copy. The old
`/home/matt/roundtable-mcp/` dev duplicate (where this effort was first written by
mistake, then ported here) was **deleted 2026-06-30**; there's no second copy to
keep in sync anymore.

The standalone `gemini-mcp` / `openai-mcp` servers run from their own dirs
(not git repos); their edits are live in place. None of this takes effect until
the processes are restarted (claude-web.service + the MCP server children).

## Done — verified (import + live-API for the risky call shapes)

### Phase 0 — foundation
- `claude-opus-4-7` → `claude-opus-4-8` (`core.py` `PARTICIPANTS`).
- `ProviderResult{text, usage, finish_reason, structured, raw}` — all six
  `_call_*` and `_run_turn` return it; `roundtable_ask`/`ask_parallel` unwrap
  `.text` (return shapes unchanged) and call `_log_usage` (log-only; persistence
  deferred to Phase 6).
- `_extract_usage` (gemini/openai/anthropic, defensive — never fails a turn).
- Registry comment: keys are stable handles, not model-version promises.
- Removed leftover `google-generativeai` from the gemini venv.
- Startup model + SDK-version logging on all three servers.

### Phase 1 — provider correctness / cost
1. **Gemini quality fix:** `_gemini_uses_thinking_level()` routes Gemini-3 +
   `-latest` aliases to `thinking_level`, legacy 2.x to integer `thinking_budget`;
   never both (the 400 trap).
2. **OpenAI unified on Responses API** (`_call_openai`) — one path, one usage
   shape; `web_search` as a tool. Prereq for the Goal-1 OpenAI tool loop.
3. **Anthropic effort:** `output_config.effort` gated to Opus 4.8+
   (`_anthropic_supports_effort`; Sonnet 4.6 left on adaptive-thinking only) +
   effort-scaled `max_tokens` (8k/16k/32k).
4. **`auto` transport warning:** one-time warning when it falls back from the
   subscription CLI to per-token API (`_warned_auto_api_fallback`).
5. **Standalone routers:** classifier input capped to 2k chars + 15s timeout
   (both servers) — fixes feeding the whole payload to a one-word classifier.
6. **Caching:** Anthropic `cache_control` ephemeral blocks on system + transcript
   prefix; OpenAI `prompt_cache_key` = hash of the (per-participant-stable)
   system prompt. Cache hits surface as `cached_tokens` in logged usage.

### Phase 2 — Goal 4: thread-bound repo context
- `repo_contexts` table (one binding per thread; rebind replaces).
- `roundtable_bind_repo` + `roundtable_repo_context` — new public ops, registered
  as MCP tools (13 total).
- **Keystone:** `_effective_tool_context()` resolves a thread's stored *string*
  policy into a `ToolUseContext` + permission callback server-side — so repo
  binding crosses the MCP boundary a `Callable` can't.
- Policies: `readonly` (Read/Grep/Glob under root, mutations denied), `deny`,
  `ask` (interactive callback; over MCP with none supplied → tools disabled,
  never auto-allowed).
- `ROUNDTABLE_REPO_ROOTS` allowlist (enforced if set; warns if unset).
- MCP `ask`/`ask_parallel` wrappers resolve and pass the context.
- Anthropic consumes the binding via the SDK-with-tools path; Gemini/OpenAI
  via their function-calling loops, gated by `CLAUDE_ROUNDTABLE_PANEL_TOOLS`
  (Goal 1 below — implemented; flag enabled 2026-06-24).

### Phase 7 — context layer (2026-06-30)
- **Context seeding (Goal 6):** `thread_context` table (one pack per thread;
  set_context REPLACEs). `roundtable_set_context` / `roundtable_bind_context`
  (allowlisted file read) / `roundtable_context`, plus a `context=` arg on
  `roundtable_create`. The pack is injected into the cached system-prompt prefix
  by `_build_system_prompt` (adjacent to house_rules — stable across turns, so no
  cache bust), capped at `CONTEXT_PACK_CHAR_CAP` (60k; conservative against the
  CLI `--system-prompt` argv / ~130k ARG_MAX), and the `_run_turn` transcript
  budget reserves room for it (floored at `MIN_TRANSCRIPT_CAP`). Loaded once per
  ask in `_thread_row`. Lets the panel respect the user's standing constraints —
  serves planning and review alike. **20 MCP tools total now.**
- **Grounded converge (Goal 2, v1):** `roundtable_converge` — deterministic
  retrieval of the cited `file:line` via `_RepoTools` (free, jailed, read-only),
  then a per-finding confirm/refute judgment routed to the free Anthropic CLI by
  default; an unreadable cite is pre-marked `unresolved` with no model call.
  Returns a `{claim,file,line,proof,severity,verdict,evidence}` ledger + tallies.
  Verdict parsed from text (explicit `VERDICT:` line, else a single keyword) —
  swap for native structured output when Goal 3 lands.
- Covered by `tests/test_context_layer.py` (18 tests).

## Remaining

- **Goal 1 / Phase 3 — DONE (impl. ~2026-06-12; flag enabled 2026-06-24):**
  repo/tool access for Gemini + OpenAI. `_RepoTools` (Read/Grep/Glob; path
  canonicalize + symlink-escape reject under cwd — incl. the rglob/grep hole;
  result-size caps + truncation markers); `_call_openai_with_tools` (Responses
  `function_call`→execute→`function_call_output`, replays reasoning items) and
  `_call_gemini_with_tools` (manual loop, `automatic_function_calling.disable=True`).
  Read-only only; no Edit/Write for Gemini/OpenAI. Gated by
  `CLAUDE_ROUNDTABLE_PANEL_TOOLS`; covered by `tests/test_panel_tools.py`.
- **Goal 3 / Phase 4:** schema-structured findings. `SchemaSpec`,
  `roundtable_ask_structured` / `ask_parallel_structured`; adapters (OpenAI
  `text.format` json_schema; Gemini response schema + **2-call fallback** where
  tools+schema unsupported on non-Gemini-3; Anthropic forced submit-tool);
  `structured_outputs` table; `roundtable_corroborate_findings` (deterministic,
  support = distinct participants, no self-corroboration).
- **Goal 2 / Phase 5 — v1 DONE (2026-06-30, see Phase 7):** `roundtable_converge`
  ships as a grounded confirm/refute pass. Still pending: build it over
  `ask_parallel_structured` + `corroborate` + an optional synthesis round, and
  separate `independent_agreement` (round 1) from `post_deliberation_consensus`
  (synthesizer never the sole truth).
- **Goal 5 / Phase 6:** streaming. `runs` + `run_events` tables, `ProgressEvent`,
  `on_event` callback + poll API (Callables can't cross MCP). Partial text stays
  in `run_events`, never `messages` (preserves ask_parallel independence).

## Deferred (lower value / its own task)

- **Token-aware trimming** (replace the 400k-char cap with per-provider token
  counting). Char cap is adequate; caching works below it regardless.
- **Gemini explicit `caches.create`** (lifecycle/TTL/invalidation). Implicit
  caching already benefits from our stable-prefix shape.
- **git/snapshot fingerprinting** for `repo_contexts` (cache-keying + parallel
  read consistency once Goal 1 tool loops exist).
- **Standalone venv SDK bumps** (google-genai 2.0→2.6, openai 2.36→2.38) —
  behavior risk, do deliberately, not silently.
- **Effort enum expansion** to none/minimal/xhigh/max with per-provider clamping
  (Gemini: minimal/low/med/high; OpenAI: none..xhigh; Anthropic: low..max).
- **Plaintext API keys** in the MCP config (out of scope; flagged in the audit).

## Verified facts (2026-06-02 — don't re-verify)

- **Models:** Opus 4.8 = `claude-opus-4-8` (effort low/med/high/xhigh/max, default
  high; `speed:"fast"` research preview). Sonnet 4.6 current. GPT-5.5 current
  flagship + `gpt-5.4-mini`. Gemini 3.1 Pro / 3.5 Flash (use `thinking_level`,
  not `thinking_budget`; sending both → 400).
- **SDK surfaces confirmed against the installed versions:** google-genai 2.6
  `ThinkingConfig.thinking_level` (accepts `'low'/'medium'/'high'`); anthropic
  0.104 `messages` `output_config.effort` (`low|medium|high|xhigh|max`) +
  `cache_control` blocks; openai 2.38 Responses `prompt_cache_key`
  (+ `prompt_cache_retention`).

## Audit/design threads
- Thread **60** — the audit (model currency, unused SDK features, cost).
- Thread **61** — the design pass for Goals 1–5 (Gemini Pro + GPT-5, parallel).
