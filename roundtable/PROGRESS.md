# roundtable-mcp — improvement work: progress & plan

_Last updated: 2026-06-02._

Self-audit of roundtable-mcp + the gemini-mcp/openai-mcp servers, driven through
the roundtable itself (threads 60 = audit, 61 = design pass), then implemented in
phases. This doc lets a future session resume cleanly.

## ⚠️ Canonical location — edit `claude-web/roundtable/`, NOT `roundtable-mcp/`

The live roundtable MCP server is launched with `PYTHONPATH=/home/matt/claude-web`
and `-m roundtable.mcp_server`, so it imports **`/home/matt/claude-web/roundtable/`**
— which is git-tracked inside the `claude-web` repo. `/home/matt/roundtable-mcp/`
is a **stale dev duplicate** that nothing live imports; this whole effort was
first written there by mistake, then ported into `claude-web/roundtable/`. The two
are in sync as of this commit. Treat `claude-web/roundtable/` as canonical and
consider deleting `roundtable-mcp/` to avoid re-confusing a future session.

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
- **Only Anthropic participants consume the binding today** (the existing
  SDK-with-tools path). Gemini/OpenAI is Goal 1.

## Remaining

- **Goal 1 / Phase 3 (NEXT, largest piece):** repo/tool access for Gemini +
  OpenAI. Build one `LocalRepoToolExecutor` (Read/Grep/Glob; path canonicalize +
  symlink-escape reject under cwd; result-size caps + truncation markers), wire
  into `_call_openai` (Responses `function_call`→execute→`function_call_output`,
  **replay reasoning items** across turns) and `_call_gemini` (manual
  `functionCall`/`functionResponse` loop, `mode=AUTO`, NOT auto-execution).
  Read-only first; no Edit/Write to Gemini/OpenAI in v1.
- **Goal 3 / Phase 4:** schema-structured findings. `SchemaSpec`,
  `roundtable_ask_structured` / `ask_parallel_structured`; adapters (OpenAI
  `text.format` json_schema; Gemini response schema + **2-call fallback** where
  tools+schema unsupported on non-Gemini-3; Anthropic forced submit-tool);
  `structured_outputs` table; `roundtable_corroborate_findings` (deterministic,
  support = distinct participants, no self-corroboration).
- **Goal 2 / Phase 5:** `roundtable_converge` over `ask_parallel_structured` +
  corroborate + optional synthesis round. Separate `independent_agreement`
  (round 1) from `post_deliberation_consensus`; synthesizer never the sole truth.
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
