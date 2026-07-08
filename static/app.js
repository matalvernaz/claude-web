(() => {
  "use strict";

  const transcript = document.getElementById("transcript");
  const form = document.getElementById("chat-form");
  const promptEl = document.getElementById("prompt");
  const sendBtn = document.getElementById("send");
  const stopBtn = document.getElementById("stop");
  const statusEl = document.getElementById("status");
  const announcer = document.getElementById("status-announcer");
  const newChatBtn = document.getElementById("new-chat");
  const sessionList = document.getElementById("session-list");
  const headerCostEl = document.getElementById("header-cost");
  const modelSelect = document.getElementById("model-select");
  const permModeSelect = document.getElementById("permission-mode-select");
  const effortSelect = document.getElementById("effort-select");
  const effortSelectLabel = document.getElementById("effort-select-label");
  const projectSelect = document.getElementById("project-select");
  const attachInput = document.getElementById("attach-input");
  const attachmentsEl = document.getElementById("attachments");
  const fileAttachmentsEl = document.getElementById("file-attachments");
  const slashMenu = document.getElementById("slash-menu");
  const contextMeter = document.getElementById("context-meter");
  const contextText = document.getElementById("context-text");
  const contextFill = document.getElementById("context-fill");
  const searchModeEl = document.getElementById("search-mode");
  const searchModeLabel = document.getElementById("search-mode-label");
  const searchClearBtn = document.getElementById("search-clear");

  // AbortController for the in-flight chat stream — used by Stop and to
  // abandon a resume attempt. The SDK task lives server-side independently
  // of the fetch; stopping for real goes through POST /api/chat/stop.
  let currentAbort = null;
  // Run-id of the in-flight turn. Persisted to sessionStorage so a reload
  // can rejoin via /api/chat/stream/{run_id}.
  let currentRunId = null;
  // Monotonic stream generation token. Bumped every time a new stream is
  // opened (sendOne / sendInExistingRun / tryResume / loadSession). Late
  // events from an aborted stream check this before mutating UI state, so
  // a stall-recovery sequence (abort old → start new) can't have the old
  // stream's trailing events bleed into the new run's transcript.
  let streamGeneration = 0;
  // Highest server-assigned `_idx` rendered per run. Events arrive in idx
  // order within any one SSE stream, so a monotonic guard is enough to
  // collapse duplicates that come from the same event reaching the DOM
  // twice (overlapping subscribers, replay-then-tail races, etc.).
  const renderedIdxByRun = new Map();
  const RUN_KEY = "claude-web.active-run";
  const MODEL_KEY = "claude-web.model";
  const EFFORT_KEY = "claude-web.effort";
  const PERM_MODE_KEY = "claude-web.permission-mode";
  const PROJECT_KEY = "claude-web.project";

  // One-shot flag armed by /fork: the next send branches the conversation
  // into a new session (server passes fork_session=True) instead of
  // continuing the current one.
  let forkNextSend = false;

  // Retry schedule for rejoining a run after the SSE connection drops
  // (phone lock, app switch, server restart, proxy idle-drop). First
  // attempt is immediate — on mobile the network is usually back the
  // moment the page resumes — with backoff to ride out a server restart.
  const STREAM_RECOVERY_DELAYS_MS = [0, 2000, 5000, 10000];

  // Pending image attachments for the next send. Each entry is {file, dataUrl}.
  // Storage helpers that swallow the SecurityError some browsers throw on
  // localStorage access in private mode / cookie-blocked origins / embedded
  // webviews. Persistence is best-effort — if it fails we just don't
  // persist, never crash the whole app at boot.
  function safeGet(storage, key) {
    try { return storage.getItem(key); } catch { return null; }
  }
  function safeSet(storage, key, value) {
    try { storage.setItem(key, value); } catch { /* ignore */ }
  }
  function safeRemove(storage, key) {
    try { storage.removeItem(key); } catch { /* ignore */ }
  }

  let pendingImages = [];
  // Pending non-image file attachments. Each entry is just {file}; we don't
  // pre-read these on the client because the server saves them to disk and
  // we don't want to hold the bytes in memory twice.
  let pendingFiles = [];

  // Outgoing message queue. While a turn is streaming, pressing Send pushes
  // here instead of starting a new request; drainStream flushes the queue
  // when the current turn finishes. Entries: {text, images, files}. Bounded
  // so a long-idle tab + repeated submits with attachments can't grow the
  // page heap unbounded.
  const messageQueue = [];
  const MAX_QUEUE_LENGTH = 10;
  let isStreaming = false;
  // Guards drainQueueIfPossible against re-entry while its fallback awaits a
  // full sendOne (see drainQueueIfPossible).
  let queueDraining = false;
  const queueArea = document.getElementById("queue-area");

  // Stream stall watchdog. Two clocks:
  //   lastNetworkActivityAt — every byte read from the SSE socket, including
  //     `: ping` heartbeat comments. Detects dead TCP connections.
  //   lastVisibleActivityAt — only updated for renderable events (assistant
  //     text, tool use/result, task progress, etc.). Detects a hung CLI that
  //     keeps emitting heartbeats but isn't actually working.
  // The watchdog gates on lastVisibleActivityAt so a backend that stops
  // producing real events trips the timeout even while pings keep flowing.
  //
  // 35 minutes (was 4) so a legitimate long foreground tool (compile, big
  // test run, `npm install` on a fat monorepo) doesn't get aborted by the
  // client while it's still working. Deliberately set GREATER than the
  // server's MIDTURN_SILENCE_TIMEOUT_MS (30 min) — that's the authoritative
  // "the CLI is wedged" signal and will emit an error event before this
  // client watchdog would have tripped, so we don't tear down a stream the
  // server still considers healthy. This 5-minute buffer absorbs SSE
  // delivery + UI render lag.
  const STREAM_STALL_MS = 35 * 60 * 1000;
  const STREAM_STALL_CHECK_MS = 30 * 1000;
  let streamStartedAt = 0;
  let lastNetworkActivityAt = 0;
  let stallWatchdogHandle = null;

  // Per-model context windows for the meter; null = unknown / hide bar.
  // Keyed on the picker value (which is the variant key, not the raw SDK
  // model id) so "claude-opus-4-7-1m" resolves to the 1M window even though
  // the underlying model id is plain "claude-opus-4-7". Sourced from the
  // server's KNOWN_MODELS so this list can't drift from what's offered in
  // the dropdown. Read from a <script type="application/json"> tag instead
  // of a global so a strict CSP without 'unsafe-inline' for scripts works.
  const MODEL_CONTEXT = {};
  // Effort levels each picker value accepts, keyed like MODEL_CONTEXT
  // (including the "" default entry). Empty/missing = model has no effort
  // knob and the effort picker hides itself.
  const MODEL_EFFORTS = {};
  // Request betas each model needs (e.g. the 1M-context variant). set_model
  // can't change betas on a live CLI, so a switch across differing betas has to
  // go through a fresh spawn — see the model picker's change handler.
  const MODEL_BETAS = {};
  // Advisor model attached at spawn (--advisor). Like betas, it can't change
  // on a live CLI, so switches across differing advisors need a fresh chat.
  const MODEL_ADVISOR = {};
  (() => {
    let data = [];
    const dataEl = document.getElementById("models-data");
    if (dataEl) {
      try { data = JSON.parse(dataEl.textContent); } catch (_) { data = []; }
    }
    for (const m of data) {
      if (m.key && m.context) MODEL_CONTEXT[m.key] = m.context;
      MODEL_EFFORTS[m.key || ""] = m.efforts || [];
      MODEL_BETAS[m.key || ""] = m.betas || [];
      MODEL_ADVISOR[m.key || ""] = m.advisor || "";
    }
  })();
  // Stable comparison key for the spawn-only parts of a model entry (betas +
  // advisor, order-insensitive). Differing keys = mid-chat switch refused.
  function switchKey(modelKey) {
    const betas = (MODEL_BETAS[modelKey || ""] || []).slice().sort().join(",");
    return betas + "|" + (MODEL_ADVISOR[modelKey || ""] || "");
  }
  let lastSeenModel = null;
  let lastInputTokens = null;
  // Highest context-fill threshold already announced this session, so a
  // single turn that nudges from 79% → 81% doesn't keep re-announcing while
  // a turn that drops back below (after compaction or a new chat) re-arms.
  let lastContextThresholdAnnounced = 0;

  // Quietly tell NVDA / VoiceOver something interesting happened. The
  // visible "Thinking…" text in #status is also live, but this region is
  // sr-only and used for milestone announcements (response complete,
  // permission needed) rather than every chunk.
  // Serialized so two calls in the same tick both get spoken. The old design
  // cleared any pending announcement and kept only the latest, which silently
  // dropped messages whenever two landed together (e.g. an error right after a
  // queue-drain notice, or model + permission-mode changes) — for a
  // screen-reader user that's a lost turn outcome, not a cosmetic glitch.
  // Each message clears the region, waits the NVDA coalescing gap (120ms —
  // 10ms wasn't enough), then holds long enough to be spoken before the next.
  const announceQueue = [];
  let announceTimer = null;
  function announce(text) {
    if (!announcer || !text) return;
    announceQueue.push(String(text));
    if (announceQueue.length > 8) announceQueue.shift();  // backpressure guard
    if (!announceTimer) pumpAnnounce();
  }
  function pumpAnnounce() {
    if (!announceQueue.length) { announceTimer = null; return; }
    const text = announceQueue.shift();
    announcer.textContent = "";
    announceTimer = setTimeout(() => {
      announcer.textContent = text;
      // Dwell before the next clear so this message is spoken first. Rough
      // word-count budget (speech rate is unknowable) — capped so a burst
      // can't stall the queue for long.
      const dwell = Math.min(5000, 800 + text.split(/\s+/).length * 160);
      announceTimer = setTimeout(pumpAnnounce, dwell);
    }, 120);
  }

  // --- Event sounds ----------------------------------------------------
  // Distinct earcons so the run state is audible without reading the
  // transcript: a turn finished, a tool needs approval, an error landed, a
  // background task settled, the model auto-fired, auto-followups paused, a
  // permission prompt expired. Each event has its own tone shape so it's
  // identifiable without looking. Tones are synthesised with the Web Audio
  // API (no asset files → nothing to ship in the frozen bundle, and the
  // `default-src 'self'` CSP needs no exception).
  //
  // By default cues fire whenever sounds are enabled, focused or not — a
  // screen-reader user sits on this tab with the window focused, so the old
  // focus-gated behaviour silenced every cue. The "background only" toggle
  // restores the away-only mode (suppress while focused) as an opt-in for
  // anyone who finds a chime over speech redundant.
  const SOUND_KEY = "claude-web.sounds";
  const SOUND_AWAY_KEY = "claude-web.sounds.awayonly";
  const soundToggle = document.getElementById("sound-toggle");
  const soundAwayToggle = document.getElementById("sound-away-toggle");
  let soundsEnabled = safeGet(localStorage, SOUND_KEY) !== "0"; // default on
  let soundsAwayOnly = safeGet(localStorage, SOUND_AWAY_KEY) === "1"; // default off
  let audioCtx = null;

  function ensureAudioCtx() {
    if (audioCtx) return audioCtx;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    try { audioCtx = new Ctor(); } catch { return null; }
    return audioCtx;
  }

  // Browser autoplay policy suspends a context created outside a user
  // gesture. Resume on the first real interaction so the first away-cue
  // actually sounds.
  function unlockAudio() {
    const ctx = ensureAudioCtx();
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  }
  document.addEventListener("pointerdown", unlockAudio, { once: true });
  document.addEventListener("keydown", unlockAudio, { once: true });

  // One master gain so overall cue loudness lives in a single place and
  // individual notes can sit near full scale without clipping when the tail
  // of one note overlaps the attack of the next.
  let soundGain = null;
  function masterGain(ctx) {
    if (soundGain) return soundGain;
    soundGain = ctx.createGain();
    soundGain.gain.value = 0.9;
    soundGain.connect(ctx.destination);
    return soundGain;
  }

  // Single knob to scale every cue's loudness. The per-cue `peak` values stay
  // as relative balance between cues; this lifts them all. Capped at 0.9 so a
  // boosted peak can't drive the oscillator into clipping.
  const CUE_LOUDNESS = 2.6;

  // Schedule one note with an attack-HOLD-release envelope: ramp up fast, sit
  // at full level for the body of the note, then a short release. The old
  // envelope decayed toward zero immediately after the attack, so each beep
  // was a quiet fading ping — most of its duration was near-silent tail.
  function tone(ctx, freq, startAt, dur, type, peak) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    const level = Math.min(0.9, peak * CUE_LOUDNESS);
    const attack = 0.008;
    const release = Math.min(0.06, dur * 0.4);
    const holdUntil = Math.max(startAt + attack, startAt + dur - release);
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(level, startAt + attack);
    gain.gain.setValueAtTime(level, holdUntil);
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + dur);
    osc.connect(gain).connect(masterGain(ctx));
    osc.start(startAt);
    osc.stop(startAt + dur + 0.02);
  }

  // Each cue is separated on THREE axes at once — timbre (waveform), rhythm
  // (note count + spacing), and pitch contour (up / down / flat / alternating)
  // — not just register, so the category is audible before you've memorised
  // the exact tone. The grammar:
  //   sine     = turn-level events (the thing you're waiting on)
  //   single   = background events  (high ping = ok, low thud = failed)
  //   triangle climbing = a prompt wants you to approve
  //   sawtooth alternating = harsh alarm, you must act before anything continues
  //   square staccato = the machine is acting on its own
  function playCue(name, force) {
    if (!soundsEnabled) return;
    // `force` lets the explicit enable-test cue sound even in background-only
    // mode while the window is focused.
    if (soundsAwayOnly && !force && document.hasFocus()) return;
    const ctx = ensureAudioCtx();
    if (!ctx) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const t = ctx.currentTime + 0.01;
    if (name === "done") {
      // Turn finished OK — two rising sine notes.
      tone(ctx, 660, t, 0.14, "sine", 0.18);
      tone(ctx, 880, t + 0.13, 0.2, "sine", 0.18);
    } else if (name === "error") {
      // Turn errored — three slow descending sine notes.
      tone(ctx, 440, t, 0.18, "sine", 0.2);
      tone(ctx, 330, t + 0.16, 0.18, "sine", 0.2);
      tone(ctx, 220, t + 0.32, 0.3, "sine", 0.2);
    } else if (name === "permission") {
      // A tool wants approval — three-note triangle climb, the only climbing
      // cue, so "asking to go up/forward".
      tone(ctx, 740, t, 0.12, "triangle", 0.22);
      tone(ctx, 880, t + 0.16, 0.12, "triangle", 0.22);
      tone(ctx, 1175, t + 0.32, 0.24, "triangle", 0.22);
    } else if (name === "attention") {
      // Auto-followups paused / you must act — harsh sawtooth alternating
      // high-low-high. Different timbre AND contour from the permission climb
      // so "blocked, act now" never sounds like "approve this".
      tone(ctx, 880, t, 0.12, "sawtooth", 0.17);
      tone(ctx, 587, t + 0.17, 0.12, "sawtooth", 0.17);
      tone(ctx, 880, t + 0.34, 0.18, "sawtooth", 0.17);
    } else if (name === "autofire") {
      // Model is auto-firing a follow-up on its own — three fast equal-pitch
      // square staccato blips, a deliberately "robotic" pulse.
      tone(ctx, 600, t, 0.06, "square", 0.14);
      tone(ctx, 600, t + 0.1, 0.06, "square", 0.14);
      tone(ctx, 600, t + 0.2, 0.08, "square", 0.14);
    } else if (name === "task_done") {
      // Background task settled OK — single high sine ping. Single-note rhythm
      // marks it as a minor/background event, not a turn.
      tone(ctx, 1320, t, 0.2, "sine", 0.2);
    } else if (name === "task_error") {
      // Background task failed — single low sine thud. Mirror of task_done.
      // Low pitch reads quieter, so it carries a touch more level.
      tone(ctx, 300, t, 0.24, "sine", 0.24);
    } else if (name === "timeout") {
      // A permission prompt expired or was discarded — soft, slow two-note
      // triangle descent. Triangle + quiet + only two notes keeps it clear of
      // the louder three-note sine error.
      tone(ctx, 660, t, 0.16, "triangle", 0.13);
      tone(ctx, 440, t + 0.2, 0.28, "triangle", 0.13);
    } else if (name === "tool") {
      // A tool started — a soft, very short tick. clarus beeps on every tool
      // (Boing on auto-approve); this is the web equivalent so a turn full of
      // Bash/Read/Edit calls gives steady activity instead of silence until
      // the end. Deliberately quiet and brief so many in a row read as gentle
      // ticking, not an alarm. Long/loud enough to actually register over
      // speech — a 50ms blip was inaudible — but a single note, so it stays
      // clearly subordinate to the multi-note turn cues.
      tone(ctx, 784, t, 0.12, "triangle", 0.24);
    }
  }

  // Per-tool ticks can arrive in bursts — parallel tool calls fire several
  // within a few ms, and the overflow-recovery path can replay a run's whole
  // history through this stream. Throttle so a burst collapses to a single
  // audible tick rather than a machine-gun.
  const TOOL_CUE_MIN_GAP_MS = 200;
  let lastToolCueAt = 0;
  function playToolCue() {
    const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
    if (now - lastToolCueAt < TOOL_CUE_MIN_GAP_MS) return;
    lastToolCueAt = now;
    playCue("tool");
  }

  if (soundToggle) {
    soundToggle.checked = soundsEnabled;
    soundToggle.addEventListener("change", () => {
      soundsEnabled = soundToggle.checked;
      safeSet(localStorage, SOUND_KEY, soundsEnabled ? "1" : "0");
      if (soundsEnabled) {
        unlockAudio(); // the toggle click is the gesture that resumes audio
        // Immediate confirmation so sound can be verified on demand without
        // waiting for a real event — toggle off then on to retest.
        playCue("done", true);
      }
    });
  }

  if (soundAwayToggle) {
    soundAwayToggle.checked = soundsAwayOnly;
    soundAwayToggle.addEventListener("change", () => {
      soundsAwayOnly = soundAwayToggle.checked;
      safeSet(localStorage, SOUND_AWAY_KEY, soundsAwayOnly ? "1" : "0");
    });
  }

  // Format unix timestamp as a short human-friendly relative/absolute string.
  function formatTime(unixSec) {
    const n = Number(unixSec);
    if (!Number.isFinite(n) || n <= 0) return "";
    const ms = n * 1000;
    // Clamp small clock-skew futures so the label doesn't read "in -3m";
    // larger futures (a session somehow stamped tomorrow) fall back to
    // the absolute date format below.
    const diff = Math.max(0, (Date.now() - ms) / 1000);
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + "d ago";
    const d = new Date(ms);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function renderSessionTimes() {
    document.querySelectorAll("#session-list .session-time").forEach((el) => {
      const t = parseInt(el.getAttribute("datetime"), 10);
      if (!isNaN(t)) el.textContent = formatTime(t);
    });
  }
  renderSessionTimes();
  // Re-render every 60s so a long-lived tab doesn't show "2h ago" on a
  // session that was active 7 hours back. Cheap (≤100 nodes typically).
  setInterval(renderSessionTimes, 60_000);

  // Only auto-scroll if the user is already at (or near) the bottom of the
  // transcript. If they've scrolled up to re-read earlier turns we don't
  // want every incoming chunk to yank them back. 64px is enough headroom
  // that a click-to-bottom is forgiving without re-pinning when they're
  // actively scrolling away.
  const SCROLL_PIN_PX = 64;
  function isPinnedToBottom() {
    const el = transcript;
    if (!el) return true;
    return (el.scrollHeight - el.scrollTop - el.clientHeight) < SCROLL_PIN_PX;
  }
  function maybeAutoScroll(force) {
    if (!transcript) return;
    if (force || isPinnedToBottom()) {
      transcript.scrollTop = transcript.scrollHeight;
    }
  }

  // Capture the pinned state BEFORE a DOM mutation, then pass that to
  // maybeAutoScroll afterwards. Without this pattern, appending a tall
  // message can push the user past SCROLL_PIN_PX from the bottom in the
  // very same frame, causing isPinnedToBottom() to return false and the
  // viewport to stay frozen even though the user was at-bottom moments ago.
  function withScrollPin(mutate) {
    const wasPinned = isPinnedToBottom();
    const result = mutate();
    maybeAutoScroll(wasPinned);
    return result;
  }

  const params = new URLSearchParams(location.search);
  let sessionId = params.get("session") || "";
  let sessionProject = params.get("project") || "";

  // Hide the effort picker when the selected model has no effort knob and
  // return whether it's usable right now. The stored pick survives a hide
  // (localStorage), so Opus 4.8 → Sonnet 4.6 → Opus 4.8 round-trips keep the
  // level; the send path re-checks support so a hidden pick is never sent.
  function effortSupported() {
    const efforts = MODEL_EFFORTS[(modelSelect && modelSelect.value) || ""] || [];
    return efforts.length > 0;
  }
  function syncEffortVisibility() {
    if (!effortSelect || !effortSelectLabel) return;
    effortSelectLabel.hidden = !effortSupported();
  }

  // Restore model + effort + project from localStorage so the picks persist across reloads.
  if (modelSelect) {
    const savedModel = safeGet(localStorage, MODEL_KEY);
    if (savedModel !== null && [...modelSelect.options].some((o) => o.value === savedModel)) {
      modelSelect.value = savedModel;
    }
    modelSelect.addEventListener("change", () => {
      const newModel = modelSelect.value;
      // set_model can't change request betas or the advisor on a live CLI. A
      // switch across differing betas/advisors would relabel the model while
      // the CLI kept running with the old attachment — the context meter
      // would over-report, or an advisor would silently stay attached (or
      // never attach) contrary to the label. Only the spawn path applies
      // them, so require a new chat for those switches.
      if (sessionId && switchKey(newModel) !== switchKey(lastSeenModel)) {
        const label = [...modelSelect.options].find((o) => o.value === newModel);
        announce(
          `${label ? label.textContent : newModel} can't be switched to mid-chat `
          + "— its settings only apply to a new chat. Start a new chat to use it.",
        );
        modelSelect.value = lastSeenModel || "";
        return;
      }
      safeSet(localStorage, MODEL_KEY, newModel);
      lastSeenModel = newModel || lastSeenModel;
      renderContextMeter();
      syncEffortVisibility();
      // With a live conversation, switch the running CLI's model in place via
      // set_model so it takes effect from the next turn. Without one, the pick
      // rides the next /api/chat spawn (which reads the model field).
      pushModelChange(newModel);
    });
  }

  // POST a live model switch to the running session. No-op when there's no
  // live conversation yet (the spawn path reads the picker instead). Success
  // is announced via the model_changed SSE event, not here, so a programmatic
  // realignment of the picker can't double-announce. Caveat: set_model changes
  // only the model string, not request betas, so switching to the 1M-context
  // variant still needs a fresh chat to pick up the beta.
  async function pushModelChange(modelKey) {
    if (!sessionId) return;
    try {
      const fd = new FormData();
      fd.append("session_id", sessionId);
      fd.append("model", modelKey || "");
      const r = await fetch("/api/chat/model", { method: "POST", body: fd });
      if (!r.ok) console.warn("live model switch failed", r.status, await r.text());
    } catch (e) {
      console.warn("live model switch error", e);
    }
  }

  // Permission-mode picker: drives set_permission_mode on a live run, or seeds
  // the next spawn (the value also rides the /api/chat form field). Kept in
  // sync with model-driven changes via the permission_mode_changed / plan_mode
  // SSE events below.
  if (permModeSelect) {
    const savedMode = safeGet(localStorage, PERM_MODE_KEY);
    if (savedMode !== null && [...permModeSelect.options].some((o) => o.value === savedMode)) {
      permModeSelect.value = savedMode;
    }
    // A non-default mode restored from a prior session is a standing state a
    // screen-reader user can't see — bypassPermissions in particular disables
    // the approval prompts entirely. Announce it on load so a persisted mode
    // isn't a silent safety surprise. (The picker itself is the queryable
    // status; this is the proactive heads-up.)
    if (permModeSelect.value && permModeSelect.value !== "default") {
      const opt = [...permModeSelect.options].find((o) => o.value === permModeSelect.value);
      const label = opt ? opt.textContent : permModeSelect.value;
      announce(
        `Permission mode is ${label}.`
        + (permModeSelect.value === "bypassPermissions" ? " Approval prompts are off." : ""),
      );
    }
    permModeSelect.addEventListener("change", async () => {
      const mode = permModeSelect.value;
      safeSet(localStorage, PERM_MODE_KEY, mode);
      if (!sessionId) {
        announce(`Permission mode set to ${permModeLabel(mode)} for your next chat.`);
        return;
      }
      try {
        const fd = new FormData();
        fd.append("session_id", sessionId);
        fd.append("mode", mode);
        const r = await fetch("/api/chat/permission-mode", { method: "POST", body: fd });
        if (!r.ok) {
          announce(`Couldn't change permission mode (${r.status}).`);
          console.warn("permission-mode change failed", r.status, await r.text());
        }
        // Success announcement comes from the permission_mode_changed SSE.
      } catch (e) {
        announce("Couldn't change permission mode: network error.");
        console.warn("permission-mode change error", e);
      }
    });
  }

  function permModeLabel(mode) {
    const opt = permModeSelect && [...permModeSelect.options].find((o) => o.value === mode);
    return (opt && opt.textContent) || mode || "default";
  }
  if (effortSelect) {
    const savedEffort = safeGet(localStorage, EFFORT_KEY);
    if (savedEffort !== null && [...effortSelect.options].some((o) => o.value === savedEffort)) {
      effortSelect.value = savedEffort;
    }
    effortSelect.addEventListener("change", () => {
      safeSet(localStorage, EFFORT_KEY, effortSelect.value);
    });
    syncEffortVisibility();
  }
  if (projectSelect) {
    const savedProject = safeGet(localStorage, PROJECT_KEY);
    if (savedProject !== null && [...projectSelect.options].some((o) => o.value === savedProject)) {
      projectSelect.value = savedProject;
    }
    if (sessionProject && [...projectSelect.options].some((o) => o.value === sessionProject)) {
      projectSelect.value = sessionProject;
    }
    projectSelect.addEventListener("change", () => {
      safeSet(localStorage, PROJECT_KEY, projectSelect.value);
    });
  }

  // Account slot toggle (shared vs one of the user's personal credentials).
  // The select value is "shared" or "cred:<id>". Mirrors the personality
  // picker's per-session model: the slot is bound to the session via the
  // ``account_slot`` form field on every /api/chat send, so two tabs on two
  // sessions can run under two different Claude accounts at once. The POST
  // to /api/account/active here only sets the *default* for new chats — it
  // no longer drives resolution for live sessions, which is what used to
  // make a switch in one tab silently respawn every other tab onto the same
  // slot. Mid-conversation the switch takes effect on the next message: the
  // server sees account_slot differ from the run's slot, 409s
  // account_changed, and the browser respawns the CLI under the new
  // CLAUDE_CONFIG_DIR (the in-flight turn keeps the old slot — its
  // subprocess loaded credentials at startup). Respawn-in-place, not fork:
  // the conversation continues on the new account.
  const accountSelect = document.getElementById("account-select");
  if (accountSelect) {
    let lastAccount = accountSelect.value;
    accountSelect.addEventListener("change", async () => {
      const target = accountSelect.value;
      if (target === lastAccount) return;
      try {
        const fd = new FormData();
        fd.append("active", target);
        const r = await fetch("/api/account/active", { method: "POST", body: fd });
        if (!r.ok) {
          let detail = `HTTP ${r.status}`;
          try {
            const body = await r.json();
            if (body && body.detail) detail = body.detail;
          } catch (_) {}
          throw new Error(detail);
        }
        lastAccount = target;
        const label = accountSelect.options[accountSelect.selectedIndex]?.text || target;
        announce(`Account switched to ${label}. Takes effect on your next message.`);
      } catch (err) {
        accountSelect.value = lastAccount;
        announce(`Could not switch account: ${err.message}`);
      }
    });
  }

  // Personality picker. The picker value is bound to the session via the
  // ``personality_id`` form field on /api/chat — each session holds its
  // own voice, so two tabs on two sessions can run two personalities
  // simultaneously. POST to /api/personalities/active sets the *default*
  // for new chats only; it does not reach back into other live sessions.
  //
  // Mid-conversation behaviour depends on the "Apply to current chat"
  // checkbox. Unchecked (default): a personality switch starts a fresh
  // chat, since a resumed JSONL transcript full of the prior voice will
  // leak that voice through even with the history-reset directive in the
  // append. Checked: keep the current session and best-effort apply the
  // new voice — the server respawns the CLI under the new persona on
  // the next message.
  const personalitySelect = document.getElementById("personality-select");
  const applyToCurrent = document.getElementById("personality-apply-current");
  const APPLY_KEY = "personality-apply-current";
  if (applyToCurrent) {
    const saved = safeGet(localStorage, APPLY_KEY);
    if (saved === "true") applyToCurrent.checked = true;
    applyToCurrent.addEventListener("change", () => {
      safeSet(localStorage, APPLY_KEY, applyToCurrent.checked ? "true" : "false");
    });
  }

  if (personalitySelect) {
    let lastPersonality = personalitySelect.value;
    personalitySelect.addEventListener("change", async () => {
      const target = personalitySelect.value;
      if (target === lastPersonality) return;
      const label = personalitySelect.options[personalitySelect.selectedIndex]?.text || target;
      const hasLiveChat = !!(sessionId || currentRunId);
      const applyMode = applyToCurrent && applyToCurrent.checked;
      try {
        // Set user-default first so new chats opened later default to
        // this pick. Existing sessions are session-bound and not affected
        // by this POST.
        const fd = new FormData();
        fd.append("personality_id", target);
        const r = await fetch("/api/personalities/active", {
          method: "POST",
          body: fd,
        });
        if (!r.ok) {
          let detail = `HTTP ${r.status}`;
          try {
            const body = await r.json();
            if (body && body.detail) detail = body.detail;
          } catch (_) {}
          throw new Error(detail);
        }
        lastPersonality = target;
        if (hasLiveChat && !applyMode) {
          // Default mid-conversation behaviour: fork. The current
          // transcript is preserved in the sidebar; the next message
          // starts a fresh chat in the new voice with no resumed
          // history fighting the persona.
          newChatBtn.click();
          announce(`Personality switched to ${label}. Started a fresh chat in the new voice.`);
        } else if (hasLiveChat && applyMode) {
          // Opt-in mid-conversation apply: the existing session keeps
          // its session_id, the next /api/chat send carries the new
          // personality_id which the server compares to the run's and
          // respawns the CLI under the new persona.
          announce(`Personality switched to ${label}. Applied to current chat — voice may carry over from prior turns.`);
        } else {
          announce(`Personality switched to ${label}. Takes effect on your next message.`);
        }
      } catch (err) {
        personalitySelect.value = lastPersonality;
        announce(`Could not switch personality: ${err.message}`);
      }
    });
  }

  function currentProject() {
    if (sessionProject) return sessionProject;
    return projectSelect ? projectSelect.value : "";
  }

  // Always refresh the sidebar from /api/sessions on load — the server-rendered
  // list can be stale if the HTML was cached or another tab created sessions.
  refreshSessions();
  refreshHeaderCost();

  // Boot order: if there's an in-flight run to resume, that wins; otherwise
  // load the session history from the URL. Doing both would race on
  // transcript.innerHTML and produce flicker/duplicates.
  (async () => {
    let resumed = false;
    // tryResume throws when the active-run probe can't reach the server;
    // on boot we just loaded the page from it, so treat that rare race as
    // "nothing to resume" and fall through to the URL's session history.
    try { resumed = await tryResume(); } catch (_) { /* fall through */ }
    if (resumed) return;
    if (sessionId) {
      try {
        await loadSession(sessionId, sessionProject);
      } catch (err) {
        setStatus("Could not load session: " + err.message);
        announce("Could not load the session: " + err.message);
      }
      markActive(sessionId);
    }
    renderContextMeter();
  })();

  // Currency + rate state shared by the header, per-turn meta, and usage
  // dialog. Populated by /api/usage on first refresh. Stays at USD/1.0 until
  // the server tells us otherwise so a fetch failure can't blank the UI.
  let costCurrency = "USD";
  let costUsdRate = 1.0;
  let costFormatter = null;

  function buildCostFormatter() {
    try {
      costFormatter = new Intl.NumberFormat(navigator.language || "en-US", {
        style: "currency",
        currency: costCurrency,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    } catch {
      // Unknown currency code on this browser: fall back to USD format.
      costFormatter = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    }
  }

  buildCostFormatter();

  function formatCost(usdAmount) {
    const usd = Number(usdAmount);
    if (!Number.isFinite(usd)) return "";
    if (!costFormatter) buildCostFormatter();
    const local = usd * costUsdRate;
    // Sub-cent charges can't be shown honestly as dollars-and-cents: $0.00
    // reads as free, a rounded-up $0.01 overstates. Mark them "<$0.01".
    if (local > 0 && local < 0.01) {
      try {
        return "<" + costFormatter.format(0.01);
      } catch {
        return "<$0.01";
      }
    }
    try {
      return costFormatter.format(local);
    } catch {
      return "$" + usd.toFixed(2);
    }
  }

  async function refreshHeaderCost() {
    try {
      const r = await fetch("/api/usage");
      if (!r.ok) return;
      const data = await r.json();
      const cur = typeof data.currency === "string" ? data.currency : "USD";
      const rate = Number(data.usd_rate);
      if (cur !== costCurrency || (Number.isFinite(rate) && rate !== costUsdRate)) {
        costCurrency = cur;
        if (Number.isFinite(rate) && rate > 0) costUsdRate = rate;
        buildCostFormatter();
      }
      if (!headerCostEl) return;
      const today = data.today || {};
      // Hide the header price entirely on subscription-only days — the
      // synthetic SDK cost doesn't match the actual bill, so showing it
      // would mislead. The element is reclaimed for screen readers via
      // aria-hidden + empty text rather than display:none, so a later
      // billed turn can flip it back on without a layout shift.
      if (!today.has_billed_usage) {
        headerCostEl.textContent = "";
        headerCostEl.setAttribute("aria-hidden", "true");
        return;
      }
      const cost = Number(today.cost_usd);
      if (Number.isFinite(cost)) {
        headerCostEl.textContent = formatCost(cost);
        headerCostEl.removeAttribute("aria-hidden");
      }
    } catch { /* ignore */ }
  }

  // Sidebar collapse state — persist across reloads. Default: open on wide
  // screens, closed on narrow ones.
  const toggleBtn = document.getElementById("toggle-sessions");
  const SIDEBAR_KEY = "claude-web.sidebar";
  function applySidebar(collapsed) {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    toggleBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  const saved = safeGet(localStorage, SIDEBAR_KEY);
  applySidebar(saved === "1" || (saved === null && window.matchMedia("(max-width: 720px)").matches));
  toggleBtn.addEventListener("click", () => {
    const willCollapse = !document.body.classList.contains("sidebar-collapsed");
    applySidebar(willCollapse);
    safeSet(localStorage, SIDEBAR_KEY, willCollapse ? "1" : "0");
  });

  // Enter to send, Shift+Enter for newline. A submit while a turn is still
  // streaming is absorbed by the queue path in the form handler (it doesn't
  // open a second /api/chat), so Enter doesn't gate on Send being disabled.
  // When the slash menu is open, Up/Down/Enter/Tab navigate it.
  promptEl.addEventListener("keydown", (e) => {
    if (slashMenu && !slashMenu.hidden && slashItems.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        slashActive = (slashActive + 1) % slashItems.length;
        updateSlashHighlight();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        slashActive = (slashActive - 1 + slashItems.length) % slashItems.length;
        updateSlashHighlight();
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        acceptSlash(slashActive);
        return;
      }
      // Tab closes the menu and lets the browser move focus normally.
      // Trapping Tab here used to break the keyboard escape route for NVDA
      // users — once the menu opened, Tab/Shift+Tab couldn't reach the
      // surrounding controls. Esc still dismisses without focus change.
      if (e.key === "Tab") {
        hideSlashMenu();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        hideSlashMenu();
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  function markActive(id) {
    sessionList.querySelectorAll("li").forEach((li) => li.classList.remove("active"));
    if (!id) return;
    const link = sessionList.querySelector(`a[data-session="${id}"]`);
    if (link) link.parentElement.classList.add("active");
  }

  // Mirrors the CLI's terminal-title behavior: tab title reflects the active
  // chat so multiple open tabs are distinguishable. The session title comes
  // from the sidebar — if the active chat hasn't been listed yet (newly
  // created mid-turn, before the next refreshSessions), fall back to the
  // default. A later refresh will pick the title up automatically because
  // renderSessionList calls back into here.
  const DEFAULT_PAGE_TITLE = "Claude — homelab";
  function updatePageTitle() {
    if (!sessionId) {
      document.title = DEFAULT_PAGE_TITLE;
      return;
    }
    const link = sessionList.querySelector(`a[data-session="${sessionId}"] .session-title`);
    const title = link && link.textContent.trim();
    document.title = title ? `${title} — Claude` : DEFAULT_PAGE_TITLE;
  }

  async function loadSession(id, project) {
    const url = new URL(`/api/sessions/${encodeURIComponent(id)}`, location.origin);
    if (project) url.searchParams.set("project", project);
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    // Cancel any in-flight stream from the previous session — without this,
    // late SSE events can land in the newly-loaded transcript.
    if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
    currentAbort = null;
    currentRunId = null;
    safeRemove(sessionStorage, RUN_KEY);
    setStreaming(false);
    streamGeneration++;
    sessionId = id || "";
    // Use ?? not || so the loaded session's own (possibly empty) project
    // wins over the URL-pinned project — otherwise switching to an
    // unprojected session keeps applying the previous project to sends.
    sessionProject = data.project ?? sessionProject ?? "";
    if (projectSelect && sessionProject &&
        [...projectSelect.options].some((o) => o.value === sessionProject)) {
      projectSelect.value = sessionProject;
    }
    // Per-session token state belongs to the previous chat — clearing
    // these prevents the context meter from reflecting the wrong session.
    lastInputTokens = null;
    lastContextThresholdAnnounced = 0;
    renderContextMeter();
    transcript.innerHTML = "";
    clearPermQueue();
    // Switching sessions: any prior run's dedup state belongs to a different
    // conversation now and shouldn't influence rendering of the new one.
    renderedIdxByRun.clear();
    for (const m of data.messages) {
      if (m.role === "user") {
        const body = appendMessage("user", m.text || "");
        if (m.image_count) appendImagePlaceholder(body, m.image_count);
        if (m.file_count) appendFilePlaceholder(body, m.file_count);
      } else if (m.role === "assistant") {
        appendMessage("assistant", m.text);
      } else if (m.role === "tool_use") {
        if ((m.name === "Edit" || m.name === "Write") && m.input) {
          insertDiffMessage(m.name, m.input);
        } else {
          insertToolMessage("→ " + m.name + (m.summary ? " " + m.summary : ""), m.name);
        }
      } else if (m.role === "tool_result") {
        insertToolMessage((m.is_error ? "✗ " : "← ") + m.text);
      }
    }
    // Force-scroll on session load — we just replaced the entire transcript.
    maybeAutoScroll(true);
    markActive(sessionId);
    updatePageTitle();
    // If a live run owns this session, attach so currentRunId/RUN_KEY/
    // isStreaming reflect reality and later sends route correctly. Guard on
    // sessionId still matching what we loaded so a newer navigation wins
    // (preserves "sidebar nav honored over resume"). Fire-and-forget: the
    // attach holds the SSE open for the life of the turn.
    if (data.live_run && data.live_run.run_id && sessionId === id) {
      attachLiveRun(data.live_run);
    }
  }

  function appendImagePlaceholder(bodyEl, count) {
    const note = document.createElement("div");
    note.className = "image-placeholder";
    note.textContent = `📎 ${count} image${count === 1 ? "" : "s"} attached`;
    bodyEl.parentElement.appendChild(note);
  }

  function appendFilePlaceholder(bodyEl, count) {
    const note = document.createElement("div");
    note.className = "image-placeholder";
    note.textContent = `📄 ${count} file${count === 1 ? "" : "s"} attached`;
    bodyEl.parentElement.appendChild(note);
  }

  // marked is loaded globally from /static/marked.min.js
  if (window.marked && typeof window.marked.setOptions === "function") {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  // Force every assistant-rendered <a href> to open in a new tab with a
  // hardened rel attribute. Without this an assistant link click replaces
  // the chat view (losing in-flight state) and the new tab can read
  // window.opener via the legacy rel-less default.
  if (window.DOMPurify && typeof window.DOMPurify.addHook === "function") {
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A" && node.getAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }

  function renderMarkdown(text) {
    // Assistant output is untrusted — it routinely echoes web pages, file
    // contents, and tool output. marked passes raw HTML through, so an
    // unsanitized .innerHTML is an XSS vector. Fail closed if either lib
    // is missing rather than render unescaped HTML.
    if (
      window.marked &&
      typeof window.marked.parse === "function" &&
      window.DOMPurify &&
      typeof window.DOMPurify.sanitize === "function"
    ) {
      // Strip interactive form controls: assistant output echoes web pages,
      // so without this a prompt-injected page could render a counterfeit
      // "Allow/Deny" button or login field indistinguishable from real UI —
      // especially convincing to a screen-reader user. No legitimate
      // assistant markdown needs form elements.
      return window.DOMPurify.sanitize(window.marked.parse(text || ""), {
        FORBID_TAGS: ["form", "input", "button", "select", "textarea", "option"],
      });
    }
    const div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
  }

  // Per-body raw-markdown cache. Stored off-DOM so the unrendered text
  // doesn't double the DOM weight in long conversations (the previous
  // dataset.raw approach copied every assistant message into an attribute).
  // WeakMap also clears entries automatically when the body element is
  // detached, which `transcript.innerHTML = ""` does on session switch.
  const rawByBody = new WeakMap();

  // Drop the provisional partial-text bubble. Called when the durable
  // assistant event (which carries the same text) arrives, and on
  // result/error/stop so an interrupted partial can't linger.
  function discardPartial(ctx) {
    if (ctx && ctx.partialBody) {
      rawByBody.delete(ctx.partialBody);
      (ctx.partialBody.closest("article") || ctx.partialBody).remove();
      ctx.partialBody = null;
    }
  }

  function appendMessage(role, text) {
    const wasPinned = isPinnedToBottom();
    const el = document.createElement("article");
    el.className = "msg " + role;
    // Heading + actions row. The H3 keeps NVDA's H-key cycling working.
    const header = document.createElement("div");
    header.className = "msg-header";
    const r = document.createElement("h3");
    r.className = "role";
    r.textContent = role === "user" ? "You" : role === "assistant" ? "Claude" : role;
    header.appendChild(r);
    const b = document.createElement("div");
    b.className = "body";
    if (role === "assistant") {
      b.innerHTML = renderMarkdown(text);
      rawByBody.set(b, text || "");
      header.appendChild(makeCopyButton(b));
    } else {
      b.textContent = text;
    }
    el.appendChild(header);
    el.appendChild(b);
    transcript.appendChild(el);
    // appendMessage gets called both on initial render (where we want to
    // scroll) and on streaming text chunks (where the streaming-text
    // handler already calls maybeAutoScroll). Pin-respecting scroll here
    // is the right default for both — but capture the pinned state
    // BEFORE the mutation so a tall message doesn't push the user past
    // SCROLL_PIN_PX in the same frame.
    maybeAutoScroll(wasPinned);
    return b;
  }

  function makeCopyButton(bodyEl) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "msg-copy";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy reply to clipboard");
    btn.addEventListener("click", async () => {
      const raw = rawByBody.get(bodyEl) || bodyEl.textContent || "";
      try {
        await navigator.clipboard.writeText(raw);
        btn.textContent = "Copied";
        announce("Copied reply.");
        setTimeout(() => { btn.textContent = "Copy"; }, 1500);
      } catch {
        announce("Could not copy.");
      }
    });
    return btn;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  // Cosmetic gerund cycler — same vibe as the CLI's "✻ Pondering…" animation.
  // Visual only; the sr-only announcer is what actually talks to screen readers.
  // Base list mined from the Claude Code binary; verified unchanged through
  // 2.1.158 (all 187 single-word entries present, no upstream additions or
  // removals since 2.1.126); locally appended entries are tagged "(local)".
  const GERUNDS = [
    'Accomplishing', 'Actioning', 'Actualizing', 'Architecting', 'Baking',
    'Bamboozling' /*(local)*/, 'Beaming', "Beboppin'", 'Befuddling',
    'Bewitching' /*(local)*/, 'Billowing', 'Blanching', 'Bloviating',
    'Boogieing', 'Boondoggling', 'Booping', 'Bootstrapping', 'Brewing',
    'Bunning', 'Burrowing', 'Calculating', 'Canoodling', 'Capering' /*(local)*/,
    'Caramelizing', 'Cascading', 'Catapulting', 'Cavorting' /*(local)*/,
    'Cerebrating', 'Channeling', 'Channelling', 'Choreographing',
    'Chortling' /*(local)*/, 'Churning', 'Clauding', 'Coalescing', 'Cogitating',
    'Combobulating', 'Composing', 'Computing', 'Concocting',
    'Confounding' /*(local)*/, 'Conjuring' /*(local)*/,
    'Conniving' /*(local)*/, 'Considering', 'Contemplating', 'Cooking',
    'Crafting', 'Creating', 'Crunching', 'Crystallizing', 'Cultivating',
    'Deciphering', 'Deliberating', 'Determining', 'Dilly-dallying',
    'Discombobulating', 'Doing', 'Doodling', 'Drizzling', 'Ebbing', 'Effecting',
    'Elucidating', 'Embellishing', 'Embezzling' /*(local)*/, 'Enchanting',
    'Envisioning', 'Evaporating', 'Fermenting', 'Fiddle-faddling',
    'Filibustering' /*(local)*/, 'Finagling', 'Flambéing', 'Flibbertigibbeting',
    'Flowing', 'Flummoxing', 'Fluttering', 'Forging', 'Forming', 'Frolicking',
    'Frosting', 'Gallivanting', 'Galloping', 'Garnishing', 'Generating',
    'Gesticulating', 'Germinating', 'Gitifying', 'Grooving', 'Gusting',
    'Harmonizing', 'Hashing', 'Hatching', 'Herding', 'Hexing' /*(local)*/,
    'Honking', 'Hoodwinking' /*(local)*/, 'Hullaballooing', 'Hyperspacing',
    'Ideating', 'Imagining', 'Improvising', 'Incubating', 'Inferring',
    'Infusing', 'Ionizing', 'Jitterbugging', 'Julienning', 'Kibitzing' /*(local)*/,
    'Kneading', 'Kvetching' /*(local)*/, 'Leavening', 'Levitating',
    'Lollygagging', 'Manifesting', 'Marinating', 'Meandering', 'Metamorphosing',
    'Misting', 'Moonwalking', 'Moseying', 'Mulling', 'Musing', 'Mustering',
    'Nebulizing', 'Nesting', 'Newspapering', 'Noodling', 'Nucleating',
    'Orbiting', 'Orchestrating', 'Osmosing', 'Perambulating', 'Percolating',
    'Perusing', 'Philosophising', 'Photosynthesizing', 'Plotting' /*(local)*/,
    'Pollinating', 'Pondering', 'Pontificating', 'Pouncing', 'Precipitating',
    'Prestidigitating', 'Processing', 'Proofing', 'Propagating', 'Puttering',
    'Puzzling', 'Quantumizing', 'Razzle-dazzling', 'Razzmatazzing',
    'Recombobulating', 'Reticulating', 'Roosting', 'Ruminating', 'Sautéing',
    'Scampering', 'Scheming' /*(local)*/, 'Schlepping',
    'Schmoozing' /*(local)*/, 'Scurrying', 'Seasoning', 'Shenaniganing',
    'Shimmying', 'Simmering', 'Skedaddling', 'Sketching',
    'Skullduggering' /*(local)*/, 'Slithering', 'Smooshing',
    'Snickering' /*(local)*/, 'Sock-hopping', 'Spelunking', 'Spinning',
    'Sprouting', 'Stewing', 'Sublimating', 'Swindling' /*(local)*/, 'Swirling',
    'Swooping', 'Symbioting', 'Synthesizing', 'Tempering', 'Thinking',
    'Thundering', 'Tinkering', 'Tomfoolering', 'Topsy-turvying',
    'Transfiguring', 'Transmuting', 'Twisting', 'Undulating', 'Unfurling',
    'Unravelling', 'Vibing', 'Waddling', 'Wandering', 'Warping',
    'Wassailing' /*(local)*/, 'Whatchamacalliting', 'Whirlpooling', 'Whirring',
    'Whisking', 'Wibbling', 'Working', 'Wrangling',
    'Yammering' /*(local)*/, 'Yodeling' /*(local)*/, 'Zesting', 'Zigzagging',
    // Multi-word phrases (local). The bundled CLI binary only ships the
    // 187 single-word gerunds above; these phrases are inspired by rubber-duck
    // and yak-shaving sightings in the wild and look fine in "✻ {x}…" form.
    'Consulting the rubber duck' /*(local phrase)*/,
    'Asking the rubber duck' /*(local phrase)*/,
    'Bribing the rubber duck' /*(local phrase)*/,
    "Pestering Schrödinger's cat" /*(local phrase)*/,
    'Shaving the yak' /*(local phrase)*/,
    'Reticulating splines' /*(local phrase)*/,
    'Untangling the spaghetti' /*(local phrase)*/,
    'Dusting off the docs' /*(local phrase)*/,
    'Negotiating with the linter' /*(local phrase)*/,
    'Counting the rubber ducks' /*(local phrase)*/,
    'Stalking the bug' /*(local phrase)*/,
    'Reading the source, Luke' /*(local phrase)*/,
    'Bargaining with TypeScript' /*(local phrase)*/,
    'Auditing the cargo cult' /*(local phrase)*/,
  ];
  let gerundTimer = null;
  let gerundSpeakTimer = null;
  let currentGerund = "Working";
  let currentActiveTodo = null;  // activeForm of the in_progress todo, or null
  let lastVisibleActivityAt = 0;
  // Tools currently mid-call, keyed by tool_use_id so a parallel-tool
  // turn doesn't lose track when one finishes before another. The
  // first-inserted entry wins for the spinner label — flipping between
  // names every 3.5s would be more noise than signal. Cleared on result /
  // stop / error so a stale tool name can't survive into the next turn.
  const inFlightTools = new Map();
  // Two clocks:
  //   GERUND_PICK_MS — how often the random gerund word changes (3.5s, so
  //     the verb actually feels alive when it shows up).
  //   GERUND_TICK_MS — how often we repaint the status text (1s, so the
  //     elapsed-time suffix updates "2m 13s → 2m 14s" smoothly without
  //     flipping the gerund word more often than is readable).
  // Speech is paced wider (~12s) so NVDA isn't cut off mid-word.
  const GERUND_PICK_MS = 3500;
  const GERUND_TICK_MS = 1000;
  const GERUND_SPEAK_MS = 12000;
  const GERUND_IDLE_MS = 3000;
  // Don't show "0s" — the first second of a turn is fast enough that the
  // elapsed-time suffix just adds visual noise. Above this threshold the
  // turn is long enough that knowing the elapsed time is useful.
  const ELAPSED_SUFFIX_THRESHOLD_MS = 5000;

  function formatElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r === 0 ? m + "m" : `${m}m ${r < 10 ? "0" : ""}${r}s`;
  }

  function currentToolLabel() {
    if (inFlightTools.size === 0) return null;
    // First-inserted: a long-running Bash isn't kicked off the spinner by
    // a quick Read it spawned. iterator.next().value is the oldest entry.
    return inFlightTools.values().next().value;
  }

  function buildSpinnerLabel(base) {
    const elapsedMs = streamStartedAt ? Date.now() - streamStartedAt : 0;
    const suffix = elapsedMs > ELAPSED_SUFFIX_THRESHOLD_MS
      ? "  ·  " + formatElapsed(elapsedMs)
      : "";
    return "✻ " + base + "…" + suffix;
  }

  function markVisibleActivity() {
    lastVisibleActivityAt = Date.now();
    // The CLI keeps the in_progress todo's activeForm pinned across other
    // UI activity — only clear when no todo is driving the spinner.
    if (!currentActiveTodo) setStatus("");
  }

  function startGerunds() {
    // Defensive: clear any existing timers before creating new ones. Without
    // this, a startGerunds() called while gerundTimer is already set orphans
    // the old setInterval handle — the timer keeps firing forever and a
    // later stopGerunds() only clears the most recent one. Reproduces from
    // sendInExistingRun → 404 fallback into sendOne (both call startGerunds),
    // and from any path where an auto_fire event lands before the previous
    // turn's result was cleanly processed. Symptom: spinner cycles verbs
    // perpetually between turns until page reload.
    if (gerundTimer) { clearInterval(gerundTimer); gerundTimer = null; }
    if (gerundSpeakTimer) { clearInterval(gerundSpeakTimer); gerundSpeakTimer = null; }
    let last = -1;
    let lastGerundPickAt = 0;
    lastVisibleActivityAt = 0;  // start from "idle" so the gerund shows right away
    function visualTick() {
      // Priority order, matching the value of each signal:
      //   1. Currently-running tool — the most concrete "what is Claude
      //      actually doing right now" signal we have, so it wins.
      //   2. In-progress todo's activeForm — matches the CLI binary.
      //   3. Random gerund, gated on idle (so it doesn't drown out
      //      streaming text when the assistant is mid-reply).
      // The elapsed-time suffix gets added in all cases past the threshold.
      const tool = currentToolLabel();
      if (tool) {
        const label = tool.summary ? `${tool.name}: ${tool.summary}` : tool.name;
        setStatus(buildSpinnerLabel(label));
        return;
      }
      if (currentActiveTodo) {
        setStatus(buildSpinnerLabel(currentActiveTodo));
        return;
      }
      const idleMs = Date.now() - lastVisibleActivityAt;
      if (idleMs < GERUND_IDLE_MS) return;  // sighted users have other feedback
      // Re-pick the gerund word at the slower cadence; repaint (with
      // updated elapsed time) at the faster cadence.
      if (Date.now() - lastGerundPickAt >= GERUND_PICK_MS) {
        let i;
        do { i = Math.floor(Math.random() * GERUNDS.length); } while (i === last);
        last = i;
        currentGerund = GERUNDS[i];
        lastGerundPickAt = Date.now();
      }
      setStatus(buildSpinnerLabel(currentGerund));
    }
    visualTick();
    gerundTimer = setInterval(visualTick, GERUND_TICK_MS);
    // Speech keeps firing regardless of visual activity — for NVDA users,
    // streaming text isn't auto-spoken, so the gerund heartbeat is still
    // their only "still working" cue. Don't speak elapsed time — it'd be
    // dominant noise every 12s ("Bash pytest 2m 14s, Bash pytest 2m 26s…").
    gerundSpeakTimer = setInterval(() => {
      const tool = currentToolLabel();
      const label = tool
        ? (tool.summary ? `${tool.name}: ${tool.summary}` : tool.name)
        : (currentActiveTodo || currentGerund);
      announce(label + "…");
    }, GERUND_SPEAK_MS);
  }
  function stopGerunds() {
    if (gerundTimer) { clearInterval(gerundTimer); gerundTimer = null; }
    if (gerundSpeakTimer) { clearInterval(gerundSpeakTimer); gerundSpeakTimer = null; }
    // Drop any active todo label so the next run doesn't open with a stale
    // "still working on the last task" spinner. Same for in-flight tools —
    // a turn that ended mid-tool (cancel, error) shouldn't carry "Bash:
    // pytest" into the next turn's spinner.
    currentActiveTodo = null;
    inFlightTools.clear();
    setStatus("");
  }

  // Wire TodoWrite's "in_progress" activeForm to the spinner label, matching
  // the CLI binary (whose activeForm schema literally says "shown in spinner
  // when in_progress"). The Tasks panel still shows the full list separately.
  function setActiveTodoLabel(label) {
    const prev = currentActiveTodo;
    currentActiveTodo = label || null;
    if (currentActiveTodo) {
      // A running tool overrides the todo label in visualTick, so don't
      // overwrite the status here if there's a tool in flight — the next
      // tick will repaint with the tool name.
      if (gerundTimer && !currentToolLabel()) {
        setStatus(buildSpinnerLabel(currentActiveTodo));
      }
      // Announce eagerly on change so NVDA picks up the new task without
      // waiting up to 12s for the next speech tick.
      if (currentActiveTodo !== prev) announce(currentActiveTodo + "…");
    } else if (prev) {
      // Hand the spinner back to the random cycler. Clear immediately;
      // the next visualTick will repaint with a random gerund.
      if (gerundTimer) setStatus("");
    }
  }

  newChatBtn.addEventListener("click", () => {
    sessionId = "";
    // Clear the URL-pinned project so the picker takes over again. Without
    // this, a chat opened from a session URL stayed glued to that project
    // even after the user picked a different one and clicked New chat.
    sessionProject = "";
    transcript.innerHTML = "";
    clearPermQueue();
    updateTodosPanel([]);
    history.replaceState({}, "", location.pathname);
    markActive("");
    stopGerunds();
    setStatus("");
    // Drop the per-run dedup state so a long-lived tab doesn't accumulate
    // a Map entry per chat. Run ids are random UUIDs, so once a chat is
    // closed the entry is just dead weight.
    renderedIdxByRun.clear();
    // Drop every piece of "previous turn" state. Without this the next
    // submit either tries sendInExistingRun against the dead run-id
    // (404 + retry round-trip) or shows the old context-meter fill, and
    // anything still queued from before would get sent into the new chat.
    if (currentAbort) {
      try { currentAbort.abort(); } catch (_) {}
    }
    currentAbort = null;
    currentRunId = null;
    safeRemove(sessionStorage, RUN_KEY);
    // Bump the stream generation so any tail events still in flight from
    // the aborted stream are dropped by handleSSEEvent's gen guard.
    streamGeneration++;
    lastInputTokens = null;
    lastContextThresholdAnnounced = 0;
    if (contextMeter) contextMeter.hidden = true;
    if (messageQueue.length) {
      messageQueue.length = 0;
      renderQueue();
    }
    setStreaming(false);
    updatePageTitle();
    promptEl.focus();
  });

  // Screen wake lock held while a turn is streaming, so a phone doesn't
  // auto-lock (suspending the page and killing the SSE socket) in the
  // middle of a long response. Best-effort: unsupported browsers and
  // denied requests just fall back to the reconnect-on-resume path. The
  // OS releases the lock whenever the page is hidden; the visibilitychange
  // handler re-requests it if a turn is still in flight.
  let wakeLock = null;
  function acquireWakeLock() {
    if (!("wakeLock" in navigator)) return;
    navigator.wakeLock.request("screen")
      .then((lock) => { wakeLock = lock; })
      .catch(() => {});
  }
  function releaseWakeLock() {
    if (!wakeLock) return;
    try { wakeLock.release(); } catch (_) {}
    wakeLock = null;
  }

  function setStreaming(on) {
    // "on" means: a turn is currently in progress (between submit/auto-fire
    // and the next ResultMessage). The SSE may stay open across multiple
    // turns; this state toggles back and forth as result/auto_fire events
    // arrive.
    isStreaming = on;
    if (on) {
      streamStartedAt = Date.now();
      startStallWatchdog();
      acquireWakeLock();
    } else {
      stopStallWatchdog();
      releaseWakeLock();
    }
    sendBtn.hidden = false;
    sendBtn.disabled = false;
    sendBtn.textContent = on ? "Queue" : "Send";
    stopBtn.hidden = !on;
  }

  function streamLooksStalled() {
    if (!isStreaming) return false;
    // Gate ONLY on visible (semantic) activity. Pings update
    // lastNetworkActivityAt so we know the socket isn't dead, but a backend
    // that's looping on a stuck tool call would keep pings flowing while
    // emitting no events — that's the case we need to detect.
    const lastSign = Math.max(lastVisibleActivityAt, streamStartedAt);
    return lastSign > 0 && (Date.now() - lastSign) > STREAM_STALL_MS;
  }

  function startStallWatchdog() {
    // Background watchdog so a stalled run is caught even if the user
    // doesn't try to submit. Re-checked every STREAM_STALL_CHECK_MS; on
    // detection it aborts the SSE fetch and surfaces a recoverable error.
    if (stallWatchdogHandle) return;
    stallWatchdogHandle = setInterval(() => {
      if (!streamLooksStalled()) return;
      stopStallWatchdog();
      announce("Stream looks stalled — send a new message to start a fresh run.");
      setStatus("Stream looks stalled — send a new message to start a fresh run.");
      // Bump the generation before aborting so the in-flight sendOne's
      // AbortError catch stays silent instead of overwriting the stall
      // guidance above with "Stopped." (announce() cancels the pending timer).
      streamGeneration++;
      if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
      currentAbort = null;
      currentRunId = null;
      safeRemove(sessionStorage, RUN_KEY);
      setStreaming(false);
      stopGerunds();
    }, STREAM_STALL_CHECK_MS);
  }

  function stopStallWatchdog() {
    if (stallWatchdogHandle) {
      clearInterval(stallWatchdogHandle);
      stallWatchdogHandle = null;
    }
  }

  function queuePreview(entry) {
    if (entry.text) return entry.text;
    const bits = [];
    if (entry.images && entry.images.length) bits.push(`${entry.images.length} image${entry.images.length === 1 ? "" : "s"}`);
    if (entry.files && entry.files.length) bits.push(`${entry.files.length} file${entry.files.length === 1 ? "" : "s"}`);
    return bits.length ? `[${bits.join(", ")}]` : "(empty)";
  }

  function newQueueId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return "q-" + Date.now() + "-" + Math.round(Math.random() * 1e9);
  }

  function removeQueueEntry(entry) {
    const idx = messageQueue.indexOf(entry);
    if (idx !== -1) {
      messageQueue.splice(idx, 1);
      renderQueue();
    }
  }

  function clearQueueEntryById(qid) {
    const idx = messageQueue.findIndex((e) => e.queue_id === qid);
    if (idx === -1) return false;
    messageQueue.splice(idx, 1);
    renderQueue();
    return true;
  }

  // Cancel a queued message. Three states:
  //   - not yet POSTed (status undefined): drop the local copy; the server
  //     never saw it.
  //   - POSTed into the run (status "sending"): ask the server to recall it.
  //     If it hadn't reached the CLI the server drops it; if it already
  //     committed to delivery we fall back to interrupting the turn it's now
  //     running as, since there's nothing left to recall.
  async function cancelQueuedEntry(entry) {
    if (messageQueue.indexOf(entry) === -1) return;
    if (entry.status !== "sending" || !entry.queue_id || !currentRunId) {
      removeQueueEntry(entry);
      announce("Queued message cancelled.");
      return;
    }
    try {
      const fd = new FormData();
      fd.append("queue_id", entry.queue_id);
      const r = await fetch(
        `/api/chat/cancel-queued/${encodeURIComponent(currentRunId)}`,
        { method: "POST", body: fd },
      );
      if (r.status === 404) {
        removeQueueEntry(entry);  // run gone — it can't run, so clear it
        announce("Queued message cancelled.");
        return;
      }
      const j = await r.json().catch(() => ({}));
      if (j && j.cancelled) {
        // Dropped before the CLI saw it. A matching queued_input_cancelled
        // event also arrives and is idempotent via clearQueueEntryById.
        removeQueueEntry(entry);
        announce("Cancelled before Claude saw it.");
      } else {
        // already_delivered: it's running. Interrupt the turn — the chip
        // clears when the interrupted result lands.
        stopBtn.click();
      }
    } catch (_) {
      announce("Cancel failed — try again.");
    }
  }

  // A queued entry belongs to the run it was typed into. Draining it into a
  // different conversation after a sidebar switch would deliver it to the wrong
  // place (a "yes, delete it" could land in another project). The origin run id
  // is stable across a session's turns and changes when you switch sessions, so
  // gate both draining and rendering on it.
  function entryForCurrentRun(e) {
    return (e.originRunId || null) === (currentRunId || null);
  }

  function renderQueue() {
    if (!queueArea) return;
    queueArea.innerHTML = "";
    // Only show chips for the current conversation's queue.
    const mine = messageQueue.filter(entryForCurrentRun);
    if (!mine.length) {
      queueArea.hidden = true;
      return;
    }
    queueArea.hidden = false;
    const heading = document.createElement("span");
    heading.className = "queue-heading";
    heading.textContent = `${mine.length} queued`;
    queueArea.appendChild(heading);
    mine.forEach((entry) => {
      const chip = document.createElement("span");
      chip.className = "queue-chip";
      const label = document.createElement("span");
      label.className = "queue-text";
      const previewText = queuePreview(entry);
      const shown = previewText.length > 60 ? previewText.slice(0, 60) + "…" : previewText;
      // A "sending" entry has been POSTed into the run; its × recalls it
      // server-side rather than just dropping the local copy (which the
      // server would otherwise still run).
      label.textContent = entry.status === "sending" ? shown + " (sending…)" : shown;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "queue-cancel";
      del.textContent = "×";
      del.setAttribute("aria-label", `Cancel queued message: ${previewText}`);
      // Identify the entry by reference so a concurrent shift() (when a turn
      // ends mid-render) doesn't make us act on the wrong index.
      del.addEventListener("click", () => { cancelQueuedEntry(entry); });
      chip.appendChild(label);
      chip.appendChild(del);
      queueArea.appendChild(chip);
    });
  }

  stopBtn.addEventListener("click", async () => {
    // Stop interrupts the in-flight turn but keeps the run (and this SSE
    // stream) alive, so the next message steers it. Queued messages are left
    // in place — they ARE the steer and drain as the next turn once the
    // interrupted result lands. Anyone wanting a hard stop interrupts, then
    // clears the queue. The interrupted turn ends via a normal `result` event
    // (flagged interrupted), which re-enables the composer.
    if (!currentRunId) {
      if (currentAbort) currentAbort.abort();
      return;
    }
    announce("Interrupting current turn.");
    let interrupted = false;
    try {
      const r = await fetch(`/api/chat/stop/${encodeURIComponent(currentRunId)}`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      interrupted = !!j.interrupted;
    } catch {
      /* network failed — fall through to the abort fallback below */
    }
    // Only tear the stream down when the server couldn't interrupt (no live
    // client, interrupt unsupported, or the POST failed) and the run is
    // therefore ending. On a real interrupt we must NOT abort, or we'd
    // disconnect from a run that's still alive and ready to be steered.
    if (!interrupted && currentAbort) currentAbort.abort();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (slashMenu && !slashMenu.hidden) {
      // Tab/Enter through the slash menu went to the form for some reason —
      // ignore. The menu's own keyboard handler will accept the suggestion.
      return;
    }
    const text = promptEl.value.trim();
    if (!text && pendingImages.length === 0 && pendingFiles.length === 0) return;
    // Intercept client-side slash commands BEFORE we treat the input as a
    // user message to the model. Everything else (e.g. skills like
    // /security-review) flows through as text — the model recognises the
    // syntax even though the SDK doesn't.
    if (text.startsWith("/") && pendingImages.length === 0 && pendingFiles.length === 0) {
      const handled = await handleClientSlashCommand(text);
      if (handled) {
        promptEl.value = "";
        return;
      }
    }
    const entry = { text, images: pendingImages.slice(), files: pendingFiles.slice(), queue_id: newQueueId(), originRunId: currentRunId };
    promptEl.value = "";
    clearAttachments();
    if (forkNextSend) {
      // /fork armed this send: it must go through /api/chat as a fresh
      // spawn (sibling run, new session id), never into the live run.
      forkNextSend = false;
      entry.fork = true;
      if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
      currentAbort = null;
      currentRunId = null;
      safeRemove(sessionStorage, RUN_KEY);
      setStreaming(false);
      await sendOne(entry);
      await drainQueue();
      return;
    }
    if (isStreaming) {
      if (streamLooksStalled()) {
        // No SSE events in STREAM_STALL_MS — assume the stream is dead
        // client-side and recover by sending as a fresh run rather than
        // queueing into a queue that will never drain.
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        safeRemove(sessionStorage, RUN_KEY);
        setStreaming(false);
        setStatus("Previous turn looked stalled — sending as a new run.");
        announce("Previous turn looked stalled. Sending as a new run.");
        await sendOne(entry);
        await drainQueue();
        return;
      }
      // Currently mid-turn — queue this for when the current run finishes.
      // Cap so a long-idle tab can't accumulate messages + attachments
      // unboundedly. The user can clear the queue or wait for the running
      // turn to drain.
      if (messageQueue.length >= MAX_QUEUE_LENGTH) {
        setStatus(`Queue full (${MAX_QUEUE_LENGTH}). Wait for the current turn to finish, or stop it.`);
        announce("Queue is full.");
        return;
      }
      messageQueue.push(entry);
      renderQueue();
      announce(`Queued. ${messageQueue.length} message${messageQueue.length === 1 ? "" : "s"} pending.`);
      promptEl.focus();
      return;
    }
    if (currentRunId) {
      // SSE is still open from a previous turn (long-lived run) — send into
      // it instead of opening a second stream.
      await sendInExistingRun(entry);
      return;
    }
    await sendOne(entry);
    await drainQueue();
  });

  // Returns true if the message was accepted (server 2xx OR fallback to a
  // fresh run was started). Returns false if the user's input could not be
  // delivered at all (401/403 redirect, hard network failure on fallback) so
  // the queue drainer can choose to leave the entry in place for retry.
  async function sendInExistingRun(entry) {
    if (!currentRunId) {
      await sendOne(entry);
      return { ok: true, mode: "fresh" };
    }
    setStreaming(true);
    startGerunds();
    announce("Sent. Claude is responding.");
    // The POST itself isn't bound to currentAbort (which owns the SSE
    // reader). A separate controller lets a Stop click — which calls
    // currentAbort.abort() AND posts /api/chat/stop — also cut the
    // in-flight POST cleanly without waiting for it to time out.
    const sendAbort = new AbortController();
    const onStopForSend = () => { try { sendAbort.abort(); } catch (_) {} };
    if (currentAbort) currentAbort.signal.addEventListener("abort", onStopForSend, { once: true });
    try {
      const fd = new FormData();
      fd.append("message", entry.text || "");
      if (entry.queue_id) fd.append("queue_id", entry.queue_id);
      // Carry the picker's current value so the server can compare it to
      // the run's spawned personality. If they disagree (mid-conversation
      // switch) the server rejects with 409 personality_changed; the
      // browser then falls back to /api/chat which respawns under the
      // new voice. Without this field, /api/chat/send happily injects
      // into the old persona's stdin until task.cancel propagates.
      if (personalitySelect && personalitySelect.value) {
        fd.append("personality_id", personalitySelect.value);
      }
      // Same for the credential slot: a mid-conversation switch 409s
      // account_changed and the browser respawns under the new account.
      if (accountSelect && accountSelect.value) {
        fd.append("account_slot", accountSelect.value);
      }
      for (const img of entry.images) {
        fd.append("images", img.file, sendName(img.file));
      }
      for (const f of (entry.files || [])) {
        fd.append("files", f.file, sendName(f.file));
      }
      const r = await fetch(
        `/api/chat/send/${encodeURIComponent(currentRunId)}`,
        { method: "POST", body: fd, signal: sendAbort.signal },
      );
      if (r.status === 401 || r.status === 403) {
        // Auth expired or CSRF rejected — neither is recoverable by retrying
        // against the same run. Surface to handleStreamError so the user is
        // bounced through the IdP, and keep the entry in the queue so the
        // post-login reload can flush it.
        handleStreamError(new Error("HTTP " + r.status));
        setStreaming(false);
        return { ok: false, mode: "failed" };
      }
      if (!r.ok) {
        // Any other non-2xx — 404 (run gone), 409 (run superseded by a
        // personality/account swap mid-conversation), 5xx (driver
        // crashed), network blip through the proxy — means the existing
        // stream can't carry this message. Abort the old SSE reader so
        // its events don't bleed into the new run's transcript, then open
        // a fresh one. 409 personality_changed/account_changed is the
        // expected fall-through after a mid-conversation switch, so we
        // suppress the failure toast for those.
        let supersededReason = null;
        if (r.status === 409) {
          try {
            const body = await r.clone().json();
            supersededReason = body && body.error;
          } catch (_) {}
        }
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        safeRemove(sessionStorage, RUN_KEY);
        const benign409 = supersededReason === "personality_changed" ||
                          supersededReason === "account_changed";
        if (r.status !== 404 && !benign409) {
          setStatus(`Send failed (HTTP ${r.status}) — starting a new run.`);
        }
        const sent = await sendOne(entry);
        // Only report "fresh" (which drops the chip) when the fresh send
        // actually succeeded. A failed fallback (e.g. server restarting)
        // reports "failed" so the caller keeps the chip rather than silently
        // discarding the user's text.
        return { ok: sent, mode: sent ? "fresh" : "failed" };
      }
      // The existing SSE stream will deliver the new turn's events; nothing
      // else to do here. setStreaming(false) happens on the next result.
      return { ok: true, mode: "injected" };
    } catch (err) {
      if (err.name === "AbortError") {
        // Stop fired during the POST. The SSE side is being torn down by the
        // same Stop click; nothing further to do here.
        return { ok: false, mode: "failed" };
      }
      // Network failure (fetch threw). Abort the dead stream, drop the
      // run handle, surface the error. Caller decides whether to retry.
      if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
      currentAbort = null;
      currentRunId = null;
      safeRemove(sessionStorage, RUN_KEY);
      handleStreamError(err);
      setStreaming(false);
      return { ok: false, mode: "failed" };
    } finally {
      if (currentAbort) currentAbort.signal.removeEventListener("abort", onStopForSend);
    }
  }

  async function drainQueueIfPossible(resultSummary) {
    // A just-ended turn's summary must still reach a screen reader even when we
    // also announce the drain in the same tick — announce() coalesces calls
    // within its gap, so we fold the summary into one announcement instead of
    // letting the drain clobber it. speakResult() covers the no-drain paths.
    const speakResult = () => { if (resultSummary) announce(resultSummary); };
    if (!currentRunId || isStreaming) { speakResult(); return; }
    // Re-entrancy guard: sendInExistingRun's fallback path awaits a full
    // sendOne (a fresh run that streams to completion). That run's `result`
    // event calls drainQueueIfPossible again while we're still parked here —
    // without this guard the same message gets sent twice (dangerous when
    // it's an instruction like "yes, delete it").
    if (queueDraining) { speakResult(); return; }
    // Drain the first entry we haven't POSTed yet. An already-"sending" entry
    // is in the server's queue (or running) and clears itself on its
    // user_prompt / queued_input_cancelled event — re-sending it here would
    // double it up.
    const entry = messageQueue.find((e) => e.status !== "sending" && entryForCurrentRun(e));
    if (!entry) { speakResult(); return; }
    queueDraining = true;
    entry.status = "sending";
    renderQueue();
    announce(resultSummary ? `${resultSummary} Sending next queued message.` : "Sending next queued message.");
    let res = { ok: false, mode: "failed" };
    try {
      res = await sendInExistingRun(entry);
    } catch (err) {
      handleStreamError(err);
      res = { ok: false, mode: "failed" };
    } finally {
      queueDraining = false;
    }
    if (res.mode === "fresh") {
      // Fell back to a brand-new run: the message is that run's initial
      // prompt, not a recallable queue item, so drop the chip now.
      removeQueueEntry(entry);
    } else if (res.mode === "injected") {
      // In the server queue now; keep the "sending" chip (its × recalls it)
      // until the user_prompt / queued_input_cancelled event resolves it.
      renderQueue();
    } else if (messageQueue.indexOf(entry) !== -1) {
      // Failed (network/auth, or server restarting) — return it to plain queued
      // so the user can retry. Re-bind to the current context (the fresh
      // fallback may have dropped the live run), or H6's per-run filter would
      // hide the chip and the text would be silently unreachable.
      entry.status = undefined;
      entry.originRunId = currentRunId;
      renderQueue();
    }
  }

  async function sendOne(entry) {
    setStreaming(true);
    // Bump the stream generation BEFORE any await, so handleSSEEvent calls
    // from a previously-aborted stream see ctx.gen !== streamGeneration and
    // bail out instead of mutating the new run's state. The myAbort capture
    // also gates the finally cleanup so an in-flight catch from the old
    // stream doesn't clobber the new currentAbort/isStreaming.
    const gen = ++streamGeneration;
    const myAbort = new AbortController();
    currentAbort = myAbort;
    startGerunds();
    announce("Sent. Claude is responding.");
    let ok = false;
    try {
      const fd = new FormData();
      fd.append("message", entry.text || "");
      if (entry.queue_id) fd.append("queue_id", entry.queue_id);
      if (sessionId) fd.append("session_id", sessionId);
      const project = currentProject();
      if (project) fd.append("project", project);
      if (modelSelect && modelSelect.value) fd.append("model", modelSelect.value);
      if (permModeSelect && permModeSelect.value) fd.append("permission_mode", permModeSelect.value);
      if (effortSelect && effortSelect.value && effortSupported()) {
        fd.append("effort", effortSelect.value);
      }
      if (entry.fork) fd.append("fork", "true");
      // Picker value as session-scoped personality override. Server uses
      // this to bind the session's personality on first send and to
      // detect mid-conversation switches on subsequent sends. Two tabs
      // each carry their own picker value, so two sessions hold two
      // voices without racing on a user-global pick.
      if (personalitySelect && personalitySelect.value) {
        fd.append("personality_id", personalitySelect.value);
      }
      // Picker value as session-scoped account override — same per-session
      // binding as personality, so two tabs run under two accounts at once.
      if (accountSelect && accountSelect.value) {
        fd.append("account_slot", accountSelect.value);
      }
      for (const img of entry.images) {
        fd.append("images", img.file, sendName(img.file));
      }
      for (const f of (entry.files || [])) {
        fd.append("files", f.file, sendName(f.file));
      }
      const r = await fetch("/api/chat", { method: "POST", body: fd, signal: myAbort.signal });
      if (!r.ok) {
        let code = "";
        try { code = (await r.json()).error || ""; } catch (_) { /* non-JSON body */ }
        if (code === "restart_pending") {
          throw new Error("Server restart in progress — wait a few seconds and resend.");
        }
        throw new Error("HTTP " + r.status);
      }
      await drainStream(r, gen);
      ok = true;
    } catch (err) {
      // Aborts from a Stop click or stall-recovery aren't user-facing
      // errors. Only the active turn shows "Stopped." — a stale abort from
      // a superseded sendOne stays silent so it doesn't clobber the new
      // turn's status.
      if (err.name === "AbortError") {
        if (gen === streamGeneration) {
          stopGerunds();
          setStatus("Stopped.");
          announce("Stopped.");
        }
      } else if (gen === streamGeneration) {
        if (!maybeRecoverFromDrop(err)) handleStreamError(err);
      }
    } finally {
      // Only clean up if we're still the current turn. If a stall-recovery
      // submit started a fresh sendOne while we were aborting, leave its
      // state alone.
      if (gen === streamGeneration && currentAbort === myAbort) {
        currentAbort = null;
        currentRunId = null;
        safeRemove(sessionStorage, RUN_KEY);
        setStreaming(false);
        promptEl.focus();
      }
    }
    return ok;
  }

  async function drainQueue() {
    // The event-driven drainQueueIfPossible owns any entry it is mid-delivering
    // (status "sending") and runs under queueDraining; skip those and bail
    // while it's active, or the same message gets sent twice.
    while (true) {
      if (queueDraining) return;
      const idx = messageQueue.findIndex((e) => e.status !== "sending" && entryForCurrentRun(e));
      if (idx === -1) return;
      const [next] = messageQueue.splice(idx, 1);
      renderQueue();
      announce("Sending next queued message.");
      const sent = await sendOne(next);
      if (!sent) {
        // Send failed (e.g. server restarting). Put it back where it was,
        // re-bound to the current context, and stop draining rather than
        // discarding the text.
        next.status = undefined;
        next.originRunId = currentRunId;
        messageQueue.splice(idx, 0, next);
        renderQueue();
        return;
      }
    }
  }

  async function drainStream(response, gen) {
    // Mutable holder so handleSSEEvent can lazy-create a new assistant
    // article each time text follows a tool call — keeps DOM order matching
    // chronological order. ctx.runId is set when run_started arrives so
    // dedup keys to the stream's own run rather than the global currentRunId
    // (which can change mid-stream during stall-recovery handovers).
    const ctx = { currentAssistantBody: null, runId: null, gen };
    const reader = response.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        // Final flush — flush() on the decoder catches any partial UTF-8
        // sequence at the buffer tail. Without this a truncated multi-byte
        // codepoint would silently drop on stream end.
        buf += dec.decode();
        if (buf.trim()) handleSSEEvent(buf, ctx);
        break;
      }
      // Bytes on the wire prove the connection is alive but DON'T prove the
      // backend is doing useful work — a hung CLI can keep the SSE ping
      // heartbeat going forever. The stall watchdog gates on
      // lastVisibleActivityAt, which is updated only by the per-event
      // handlers below, so this clock is purely for connection-liveness
      // diagnostics.
      if (gen === streamGeneration) lastNetworkActivityAt = Date.now();
      buf += dec.decode(value, { stream: true });
      // Handle both LF and CRLF separators per the SSE spec.
      while (true) {
        const lf = buf.indexOf("\n\n");
        const crlf = buf.indexOf("\r\n\r\n");
        let cut, sep;
        if (lf >= 0 && (crlf < 0 || lf < crlf)) { cut = lf; sep = 2; }
        else if (crlf >= 0) { cut = crlf; sep = 4; }
        else break;
        const evt = buf.slice(0, cut);
        buf = buf.slice(cut + sep);
        handleSSEEvent(evt, ctx);
      }
    }
    // Late EOF: if a newer stream has taken over since this one started,
    // do nothing. The newer stream owns status/announcer/header refresh.
    if (gen !== streamGeneration) return;
    // Premature EOF: the reader hit done=true while a turn was still
    // in flight (no `result` event seen, so isStreaming is still true).
    // Typical cause is a browser tab freeze: Chrome's intensive throttling
    // pauses the fetch reader on backgrounded tabs, cloudflared/Traefik
    // eventually closes the idle response, and on thaw we see EOF. The
    // SDK task lives server-side independently, so reconnect via the
    // event store rather than leaving the UI stuck on stale state.
    if (isStreaming && currentRunId) {
      const rid = currentRunId;
      announce("Stream dropped; reconnecting.");
      // Keep this run's watermark: the transcript DOM is intact, so tryResume
      // resumes incrementally from watermark+1 rather than replaying from 0.
      // Null currentAbort so the caller's finally guard
      // (`currentAbort === myAbort`) fails and skips the RUN_KEY wipe
      // we need for tryResume to find the run.
      currentAbort = null;
      currentRunId = null;
      scheduleStreamRecovery(rid);
      return;
    }
    stopGerunds();
    // Don't overwrite an explicit terminal status (stopped, error) with a
    // generic "Response complete." derived from a missing ctx.lastResult.
    if (ctx.lastResult) {
      const summary = summariseResult(ctx.lastResult);
      setStatus(summary);
      announce(summary);
    }
    refreshSessions();
    refreshHeaderCost();
  }

  function handleStreamError(err) {
    stopGerunds();
    if (err.name === "AbortError") {
      setStatus("Stopped.");
      announce("Stopped.");
      return;
    }
    // Auth expired mid-session: cookie no longer signs in, so fetch returns
    // 401/403 (or follows the 302 to /auth/login and lands on the login
    // page's HTML). Send the user back through the IdP instead of leaving
    // them staring at "HTTP 401". window.location.assign so a Back button
    // can still rescue an in-flight composer.
    const m = /^HTTP (40[13])$/.exec(err.message || "");
    if (m) {
      setStatus("Session expired — redirecting to sign-in.");
      announce("Session expired. Redirecting to sign-in.");
      const next = encodeURIComponent(location.pathname + location.search);
      window.location.assign(`/auth/login?next=${next}`);
      return;
    }
    // A dropped response stream surfaces as a browser-flavored TypeError —
    // Firefox literally says "Error in input stream", Chrome "Failed to
    // fetch", Safari "Load failed". Neither names the real cause: the
    // connection died mid-turn (server restart, proxy idle-drop, network
    // blip). Say that instead. Drops with a live run id never reach here —
    // maybeRecoverFromDrop intercepts them — so this is the no-run-to-
    // rejoin case (the POST died before run_started) and resending is the
    // only recovery.
    if (isNetworkDropError(err)) {
      const msg = "Connection to the server dropped (server restart, proxy timeout, or network blip). Resend your last message.";
      setStatus(msg);
      announce(msg);
      return;
    }
    setStatus("Error: " + err.message);
    announce("Error: " + err.message);
  }

  function isNetworkDropError(err) {
    return (err instanceof TypeError)
      || /input stream|failed to fetch|network ?error|load failed/i.test(err.message || "");
  }

  // Shared recovery for a stream that died mid-turn while a run was live:
  // preserve the run id, then rejoin via tryResume on a backoff schedule.
  // Mobile browsers kill the SSE socket whenever the page is locked or
  // backgrounded, so this is the COMMON path on phones, not an edge case.
  // Callers must have already detached the run from the live globals
  // (currentAbort = null / currentRunId = null) so the owning finally
  // block skips its RUN_KEY wipe.
  function scheduleStreamRecovery(rid, attempt = 0) {
    if (attempt >= STREAM_RECOVERY_DELAYS_MS.length) {
      stopGerunds();
      setStreaming(false);
      const msg = "Couldn't reconnect to the running task. Reload the page to retry, or send a new message.";
      setStatus(msg);
      announce(msg);
      return;
    }
    const myGen = streamGeneration;
    setTimeout(async () => {
      // A newer send/resume has taken over while we waited — it owns the
      // UI now and a stale rejoin would clobber its transcript.
      if (streamGeneration !== myGen) return;
      safeSet(sessionStorage, RUN_KEY, rid);
      let ok;
      try {
        ok = await tryResume();
      } catch (err) {
        if (/^HTTP (40[13])$/.test(err.message || "")) {
          // Auth expired while we were away — retrying can't fix that.
          // handleStreamError owns the redirect-to-IdP dance.
          handleStreamError(err);
          return;
        }
        // Server unreachable (still restarting, network not back yet) —
        // the run may well be alive server-side, so keep trying.
        scheduleStreamRecovery(rid, attempt + 1);
        return;
      }
      if (!ok && streamGeneration === myGen) {
        // The server answered but doesn't know the run (event retention
        // expired or state lost across restart). Terminal: clear the
        // streaming UI rather than leaving a spinner that never resolves.
        stopGerunds();
        setStreaming(false);
        const msg = "The previous response ended while you were away. Reload the page to see it, or send a new message.";
        setStatus(msg);
        announce(msg);
      }
    }, STREAM_RECOVERY_DELAYS_MS[attempt]);
  }

  // Network-drop intercept for sendOne/tryResume catch blocks. When the
  // stream died but we still hold a run id, detach it from the globals
  // (so the caller's finally guard skips the RUN_KEY wipe) and start the
  // recovery schedule. Returns true when recovery was started; false
  // means the caller should fall through to handleStreamError.
  function maybeRecoverFromDrop(err) {
    if (!isNetworkDropError(err) || !currentRunId) return false;
    const rid = currentRunId;
    // Keep this run's watermark so tryResume resumes incrementally — the
    // transcript DOM is intact across a network drop.
    currentAbort = null;
    currentRunId = null;
    setStatus("Connection dropped — reconnecting.");
    announce("Connection dropped. Reconnecting.");
    scheduleStreamRecovery(rid);
    return true;
  }

  // Returns true after a successful rejoin, false when the server says the
  // run is gone (terminal — RUN_KEY is cleared). THROWS when the active
  // probe itself fails (server unreachable): the run may still be alive,
  // so RUN_KEY is kept and the caller decides whether to retry.
  async function tryResume() {
    const savedRunId = safeGet(sessionStorage, RUN_KEY);
    if (!savedRunId) return false;
    const r = await fetch(`/api/chat/active?run_id=${encodeURIComponent(savedRunId)}`);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const info = await r.json();
    if (!info.active && !info.buffered_events) {
      safeRemove(sessionStorage, RUN_KEY);
      return false;
    }
    // If the URL explicitly names a different session than the active run's,
    // the user navigated there deliberately (clicked the sidebar mid-run).
    // Honor that instead of silently resuming — and re-rewriting the URL to —
    // the old run. The old run stays alive and resumable from its own session.
    if (sessionId && info.session_id && info.session_id !== sessionId) {
      return false;
    }
    if (info.project) sessionProject = info.project;
    // Incremental resume: if we still hold this run's high-watermark (the
    // highest _idx already rendered), resume from watermark+1 and keep the
    // transcript/queue/watermark — we append only what we missed while
    // disconnected, and already-answered permission prompts are never replayed
    // back into the queue. A fresh page load or sidebar-open has no watermark,
    // so we fall back to a full replay from 0, which needs the wipe (otherwise
    // the replayed events dedup against the stale watermark and nothing
    // renders). Wipe BEFORE flipping streaming UI on so there's never a frame
    // with the spinner over a stale transcript.
    const wm = renderedIdxByRun.get(savedRunId);
    const incremental = typeof wm === "number";
    if (!incremental) {
      transcript.innerHTML = "";
      clearPermQueue();
      renderedIdxByRun.delete(savedRunId);
    }
    const gen = ++streamGeneration;
    const myAbort = new AbortController();
    currentRunId = savedRunId;
    currentAbort = myAbort;
    setStreaming(true);
    startGerunds();
    announce("Reconnecting to previous response.");
    try {
      const streamUrl = `/api/chat/stream/${encodeURIComponent(savedRunId)}`
        + (incremental ? `?start_index=${wm + 1}` : "");
      const r = await fetch(streamUrl, { signal: myAbort.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      await drainStream(r, gen);
    } catch (err) {
      if (err.name === "AbortError") {
        if (gen === streamGeneration) {
          stopGerunds();
          setStatus("Stopped.");
          announce("Stopped.");
        }
      } else if (gen === streamGeneration) {
        if (!maybeRecoverFromDrop(err)) handleStreamError(err);
      }
    } finally {
      // Same generation guard as sendOne — only clear globals if this resume
      // is still the active stream. A newer sendOne/sendInExistingRun that
      // started during the resume must not be clobbered by our finally.
      if (gen === streamGeneration && currentAbort === myAbort) {
        currentAbort = null;
        currentRunId = null;
        safeRemove(sessionStorage, RUN_KEY);
        setStreaming(false);
        // Don't steal focus: a reconnect/resume isn't user-initiated, so a
        // screen-reader user stays where they were reading.
      }
    }
    return true;
  }

  // Attach to a live run discovered via /api/sessions {live_run} on a fresh
  // page load — no sessionStorage RUN_KEY exists, so tryResume can't fire.
  // loadSession already rendered the disk transcript, so we TAIL-attach from
  // the run's current _next_idx: subscribe() replays nothing already on disk,
  // yet every durable whole-message event from here on (including the in-flight
  // turn's final assistant message, _idx >= next_idx) still arrives. Only the
  // pre-attach live-typing animation is missed — cosmetic, self-heals when the
  // turn completes. The stream is held open even for an idle (between-turns)
  // run because sendInExistingRun has no reader of its own.
  // Render a prompt that was already open when this page attached to a live
  // run. It sits below next_idx so the tail stream won't replay it. Idempotent:
  // a re-attach (stream recovery) can't double-render — the card and the queue
  // entry both dedup on request id.
  function renderPendingPrompt(p) {
    if (!p || !p.id || findRequestCard(p.id)) return;
    if (p.type === "permission_request") {
      renderPermissionCard(p);
      enqueuePermRequest("permission", p);
    } else if (p.type === "question_request") {
      renderQuestionCard(p);
      enqueuePermRequest("question", p);
    } else if (p.type === "plan_review") {
      renderPlanCard(p);
      enqueuePermRequest("plan", p);
    }
  }

  async function attachLiveRun(info) {
    const rid = info && info.run_id;
    if (!rid) return;
    const gen = ++streamGeneration;
    const myAbort = new AbortController();
    currentRunId = rid;
    currentAbort = myAbort;
    safeSet(sessionStorage, RUN_KEY, rid);
    // Seed dedup at the tail so any already-buffered durable events the tail
    // re-emits (next_idx grew between the snapshot and the subscribe) are
    // dropped, not re-rendered. With no run_started on a tail attach,
    // handleSSEEvent keys dedup on currentRunId, which we've just set.
    if (typeof info.next_idx === "number" && info.next_idx > 0) {
      renderedIdxByRun.set(rid, info.next_idx - 1);
    }
    // isStreaming reflects reality: spinner only if a turn is actually mid-
    // flight. An idle live run reads false so the next send routes straight
    // through sendInExistingRun, not the queue.
    const midTurn = !!(info.active && !info.between_turns);
    setStreaming(midTurn);
    if (midTurn) {
      startGerunds();
      announce("Reconnecting to the response in progress.");
    }
    // Surface any prompt that was already open when we attached — otherwise a
    // fresh page sits on a spinner while the tool call silently times out.
    if (Array.isArray(info.pending_prompts) && info.pending_prompts.length) {
      info.pending_prompts.forEach(renderPendingPrompt);
      const n = info.pending_prompts.length;
      announce(`${n} approval${n === 1 ? "" : "s"} waiting for you.`);
    }
    try {
      const start = typeof info.next_idx === "number" ? info.next_idx : 0;
      const streamUrl = `/api/chat/stream/${encodeURIComponent(rid)}?start_index=${start}`;
      const r = await fetch(streamUrl, { signal: myAbort.signal });
      if (!r.ok) {
        // Benign race: the run finished/GC'd between the /api/sessions snapshot
        // and here. Keep the disk transcript already rendered, drop the dead
        // handle so the next send is a clean fresh /api/chat. No error toast —
        // nothing actually went wrong for the user.
        if (gen === streamGeneration && currentAbort === myAbort) {
          currentAbort = null;
          currentRunId = null;
          safeRemove(sessionStorage, RUN_KEY);
          setStreaming(false);
        }
        return;
      }
      await drainStream(r, gen);
    } catch (err) {
      if (err.name === "AbortError") {
        if (gen === streamGeneration) stopGerunds();
      } else if (gen === streamGeneration) {
        if (!maybeRecoverFromDrop(err)) handleStreamError(err);
      }
    } finally {
      // Same generation/abort guard as tryResume — a newer send/load that
      // started during the attach must not be clobbered by our cleanup, and an
      // A→B→A sidebar nav must not leave RUN_KEY pointing at the wrong run.
      if (gen === streamGeneration && currentAbort === myAbort) {
        currentAbort = null;
        currentRunId = null;
        safeRemove(sessionStorage, RUN_KEY);
        setStreaming(false);
        // Don't steal focus: this turn wasn't user-initiated (it's a restore/
        // attach), so a screen-reader user stays where they were reading.
      }
    }
  }

  function handleSSEEvent(evt, ctx) {
    // Drop everything if this stream has been superseded — late events from
    // an aborted stream must not mutate the new one's transcript or status.
    if (ctx && ctx.gen !== undefined && ctx.gen !== streamGeneration) return;
    // Spec-correct SSE data assembly: each `data:` line contributes to the
    // event payload, joined by literal "\n". The leading space after the
    // colon is optional and stripped if present. Avoid trim() which would
    // silently mutate JSON payloads with intentional leading/trailing
    // whitespace — and lines from an event that have been split across SSE
    // frames must not be smashed together without the newline separator.
    const lines = evt.split(/\r?\n/);
    const dataParts = [];
    for (const ln of lines) {
      if (!ln.startsWith("data:")) continue;
      let v = ln.slice(5);
      if (v.startsWith(" ")) v = v.slice(1);
      dataParts.push(v);
    }
    if (!dataParts.length) return;
    const dataLine = dataParts.join("\n");
    let obj;
    try {
      obj = JSON.parse(dataLine);
    } catch (err) {
      // A truncated frame or malformed JSON is rare but real — log so it's
      // diagnosable from devtools without breaking the stream.
      if (typeof console !== "undefined") console.warn("Bad SSE frame", err);
      return;
    }

    // Drop events the DOM has already rendered for THIS stream's run. Key
    // on ctx.runId (set when this stream's own run_started lands), not the
    // global currentRunId — a late event from an aborted stream would
    // otherwise be deduped against a different run's watermark and either
    // poison the new run's dedup state or get rendered as if it belonged
    // to the new run.
    if (typeof obj._idx === "number") {
      let runKey = null;
      if (obj.type === "run_started" && obj.run_id) {
        runKey = obj.run_id;
        if (ctx) ctx.runId = obj.run_id;
      } else if (ctx && ctx.runId) {
        runKey = ctx.runId;
      } else {
        runKey = currentRunId;
      }
      if (runKey) {
        const seen = renderedIdxByRun.get(runKey);
        if (seen !== undefined && obj._idx <= seen) return;
        renderedIdxByRun.set(runKey, obj._idx);
      }
    }

    if (obj.type === "run_started") {
      // Save the run-id so a reload can rejoin via /api/chat/stream/{id}.
      if (obj.run_id) {
        currentRunId = obj.run_id;
        safeSet(sessionStorage, RUN_KEY, obj.run_id);
      }
      if (obj.project) sessionProject = obj.project;
      if (obj.model) lastSeenModel = obj.model;
    } else if (obj.type === "user_prompt") {
      // Single source of truth for "the user's message in the transcript":
      // the server echoes the prompt as an event so both live and resumed
      // streams render it the same way.
      const body = appendMessage("user", obj.text || "");
      if (obj.image_count) appendImagePlaceholder(body, obj.image_count);
      if (obj.file_count) appendFilePlaceholder(body, obj.file_count);
      // The server confirmed this queued message reached the CLI — clear its
      // chip; it's no longer recallable.
      if (obj.queue_id) clearQueueEntryById(obj.queue_id);
    } else if (obj.type === "queued_input_cancelled") {
      // A recall (this tab or another) dropped a queued message before the
      // CLI saw it. Idempotent: no-op if we already cleared it locally.
      if (obj.queue_id && clearQueueEntryById(obj.queue_id)) {
        announce("Queued message cancelled.");
      }
    } else if (obj.type === "stopped") {
      discardPartial(ctx);
      setActiveTodoLabel(null);
      setStatus("Stopped.");
      announce("Stopped.");
    } else if (obj.type === "_done") {
      // Server end-of-stream sentinel. The replay/tail is finished — there
      // is nothing more coming on this run. Drop streaming state so the
      // EOF-recovery code at the bottom of drainStream doesn't loop calling
      // tryResume() for a finished run whose replay carried no ``result``
      // event (e.g. a run killed mid-turn by server restart). The premature-
      // EOF path used to see isStreaming=true after the replay drained and
      // immediately reopen the same stream; that loop was invisible to
      // users but burned CPU and network.
      stopGerunds();
      setStreaming(false);
      // A queued message stranded by an abnormal end (server restart mid-turn,
      // run killed) never saw a `result`. Flush it now — currentRunId still
      // points at the finished run, so sendInExistingRun's 404 path falls back
      // to a fresh run.
      drainQueueIfPossible();
      return;
    } else if (obj.type === "restarted_during_run") {
      // The server was restarted while a previous turn was running. The
      // SDK subprocess is gone, but the conversation jsonl on disk and our
      // session_id are intact — sending a new message will resume cleanly.
      // Belt-and-braces with the _done handler above: marking the stream
      // not-streaming here closes the EOF-retry loop even if a future
      // server change emits restarted_during_run without a trailing _done.
      stopGerunds();
      setStreaming(false);
      drainQueueIfPossible();
      ctx.currentAssistantBody = null;
      const article = document.createElement("article");
      article.className = "msg info";
      const role = document.createElement("h3");
      role.className = "role";
      role.textContent = "Server restarted";
      const body = document.createElement("p");
      body.className = "info-body";
      body.textContent = obj.message || "Server restarted mid-turn.";
      article.appendChild(role);
      article.appendChild(body);
      transcript.appendChild(article);
      maybeAutoScroll();
      announce("Server restarted while the previous turn was running.");
      setStatus("Server restarted mid-turn — send a new message to continue.");
    } else if (obj.type === "system" && obj.subtype === "init") {
      // Adopt whatever session id the SDK reports. Fresh chats start with
      // sessionId="" and learn theirs here; swap-respawn forks (server
      // passes fork_session=True after a personality/credential toggle)
      // arrive with a different id than the URL currently shows, and we
      // want the URL to follow so the old transcript stays navigable via
      // the sidebar while the new one becomes the active session. Replays
      // (system:init re-emitted after a reload) will see obj.session_id
      // match the current sessionId and no-op.
      if (obj.session_id && obj.session_id !== sessionId) {
        sessionId = obj.session_id;
        const url = new URL(location.href);
        url.searchParams.set("session", sessionId);
        if (sessionProject) url.searchParams.set("project", sessionProject);
        history.replaceState({}, "", url.toString());
        // Sidebar may not yet have this session — title gets picked up on
        // the next refreshSessions(). Until then, leave the default in place.
        updatePageTitle();
      }
      if (obj.model) lastSeenModel = obj.model;
    } else if (obj.type === "partial_text") {
      // Transient typing-feel frames (never persisted server-side, never
      // replayed). Rendered as plain text into a provisional bubble; the
      // durable "assistant" event below replaces it wholesale. #transcript
      // is not a live region, so partial churn stays silent for NVDA —
      // completion is announced through the explicit channels as before.
      if (obj.text) {
        const wasPinned = isPinnedToBottom();
        if (!ctx.partialBody) {
          ctx.partialBody = appendMessage("assistant", "");
          (ctx.partialBody.closest("article") || ctx.partialBody).classList.add("partial");
        }
        const raw = (rawByBody.get(ctx.partialBody) || "") + obj.text;
        rawByBody.set(ctx.partialBody, raw);
        ctx.partialBody.textContent = raw;
        maybeAutoScroll(wasPinned);
        markVisibleActivity();
      }
    } else if (obj.type === "assistant" && obj.message) {
      discardPartial(ctx);
      const blocks = obj.message.content || [];
      for (const blk of blocks) {
        if (blk.type === "thinking") {
          // We don't render extended-thinking content (matches the CLI),
          // but we DO want it to count as live activity so the stall
          // watchdog and gerund-idle gate don't trip during long thinks.
          markVisibleActivity();
        } else if (blk.type === "text" && blk.text) {
          if (!ctx.currentAssistantBody) {
            ctx.currentAssistantBody = appendMessage("assistant", "");
          }
          const wasPinned = isPinnedToBottom();
          const raw = (rawByBody.get(ctx.currentAssistantBody) || "") + blk.text;
          rawByBody.set(ctx.currentAssistantBody, raw);
          // Throttle full markdown re-render to roughly one per animation
          // frame (~16ms). Without this, every streaming text chunk
          // re-parses + re-sanitises the entire accumulated message — an
          // O(n²) scan that visibly stutters on long replies. Plain text
          // gets shown immediately; markdown structure (headings, code
          // blocks, etc.) catches up on the next rAF tick.
          ctx.currentAssistantBody.textContent = raw;
          if (!ctx._mdScheduled) {
            ctx._mdScheduled = true;
            const target = ctx.currentAssistantBody;
            requestAnimationFrame(() => {
              ctx._mdScheduled = false;
              if (!target.isConnected) return;
              target.innerHTML = renderMarkdown(rawByBody.get(target) || "");
            });
          }
          maybeAutoScroll(wasPinned);
          markVisibleActivity();
        } else if (blk.type === "tool_use") {
          // Subsequent text blocks should land in a new assistant article
          // *after* this tool call, not into the one above it.
          ctx.currentAssistantBody = null;
          if (blk.name === "Edit" || blk.name === "Write") {
            insertDiffMessage(blk.name, blk.input || {});
          } else {
            insertToolMessage("→ " + blk.name + " " + summariseToolInput(blk.input || {}), blk.name);
          }
          playToolCue();
          // Track this tool as in-flight so the spinner can show the
          // concrete activity ("Bash: pytest -q · 2m 14s") instead of a
          // random gerund. Cleared when the matching tool_result arrives.
          if (blk.id) {
            inFlightTools.set(blk.id, {
              name: blk.name,
              summary: summariseToolInput(blk.input || {}),
            });
            // Repaint immediately so the user doesn't wait up to 3.5s for
            // the next visualTick to flip "Pondering…" → "Bash: …".
            if (gerundTimer) {
              const label = inFlightTools.get(blk.id);
              setStatus(buildSpinnerLabel(
                label.summary ? `${label.name}: ${label.summary}` : label.name,
              ));
            }
          }
          markVisibleActivity();
        }
      }
    } else if (obj.type === "user" && Array.isArray(obj.message?.content)) {
      // tool results
      ctx.currentAssistantBody = null;
      for (const blk of obj.message.content) {
        if (blk.type === "tool_result") {
          const txt = typeof blk.content === "string" ? blk.content : JSON.stringify(blk.content);
          const prefix = blk.is_error ? "✗ " : "← ";
          insertToolMessage(prefix + truncate(txt, 200));
          // Drop the matching in-flight entry so the spinner stops
          // claiming this tool is still running.
          if (blk.tool_use_id) inFlightTools.delete(blk.tool_use_id);
          markVisibleActivity();
        }
      }
    } else if (obj.type === "files_rewound") {
      const label = obj.preview
        ? `Files rewound to before: ${obj.preview}`
        : `Files rewound ${obj.back || 1} message(s) back`;
      insertToolMessage("⟲ " + label, "Rewind");
      announce(label + ".");
    } else if (obj.type === "permission_request") {
      ctx.currentAssistantBody = null;
      announce(`Permission needed for ${obj.tool}.`);
      playCue("permission");
      renderPermissionCard(obj);
      enqueuePermRequest("permission", obj);
      markVisibleActivity();
    } else if (obj.type === "question_request") {
      ctx.currentAssistantBody = null;
      announce("Claude is asking you a question.");
      playCue("permission");
      renderQuestionCard(obj);
      enqueuePermRequest("question", obj);
      markVisibleActivity();
    } else if (obj.type === "plan_review") {
      ctx.currentAssistantBody = null;
      announce("Claude has a plan for you to review.");
      playCue("permission");
      renderPlanCard(obj);
      enqueuePermRequest("plan", obj);
      markVisibleActivity();
    } else if (obj.type === "plan_mode") {
      // The model entered/left read-only planning. Announce for NVDA and flip
      // a visual indicator; the plan itself arrives later as a plan_review.
      if (obj.active) {
        document.body.dataset.planMode = "1";
        if (permModeSelect) permModeSelect.value = "plan";
        announce("Claude entered plan mode. It will research read-only and propose a plan for your approval before making any changes.");
      } else {
        delete document.body.dataset.planMode;
        // Approving a plan moves the CLI to acceptEdits; reflect that in the picker.
        if (permModeSelect && permModeSelect.value === "plan") permModeSelect.value = "acceptEdits";
        announce("Plan approved. Claude is now implementing.");
      }
      markVisibleActivity();
    } else if (obj.type === "plan_model") {
      // A split-model entry (Fableplan) swapped between its plan and base
      // models server-side. The picker key is unchanged; just say which
      // model is spending tokens now.
      if (obj.label) announce(obj.label);
      markVisibleActivity();
    } else if (obj.type === "permission_mode_changed") {
      // Server confirmed a permission-mode change (user picker or model-driven).
      // Keep the picker in sync and announce for NVDA.
      if (permModeSelect && obj.mode) permModeSelect.value = obj.mode;
      if (obj.mode === "plan") {
        document.body.dataset.planMode = "1";
      } else {
        delete document.body.dataset.planMode;
      }
      announce(`Permission mode is now ${permModeLabel(obj.mode)}.`);
      markVisibleActivity();
    } else if (obj.type === "model_changed") {
      // Server confirmed a live model switch. Align the picker + context meter
      // and announce; the switch applies from the next turn.
      if (modelSelect && typeof obj.model === "string" &&
          [...modelSelect.options].some((o) => o.value === obj.model)) {
        modelSelect.value = obj.model;
        lastSeenModel = obj.model || lastSeenModel;
        renderContextMeter();
        syncEffortVisibility();
      }
      announce(`Model switched to ${obj.label || "default"} for the rest of this conversation.`);
      markVisibleActivity();
    } else if (obj.type === "_overflow") {
      // Backend dropped us as a slow subscriber — fetch a fresh stream from
      // the start so we don't miss anything. tryResume's reconnect path
      // replays from index 0 via the persisted store, but we MUST clear
      // this run's dedup watermark first — otherwise the high-water mark
      // from before the overflow drops every replayed event as a duplicate
      // and the transcript stays blank.
      if (currentRunId) {
        announce("Stream backlog overflowed; reconnecting from start.");
        const rid = currentRunId;
        renderedIdxByRun.delete(rid);
        // Bump the generation BEFORE aborting so the abort rejection in the
        // in-flight sendOne sees gen !== streamGeneration and stays silent —
        // otherwise its catch runs announce("Stopped.") which cancels the
        // "reconnecting" announcement queued just above (announce() clears any
        // pending timer). rejoinAfterFreeze does the same for the same reason.
        streamGeneration++;
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        // First recovery attempt is a zero-delay timeout, so the in-flight
        // reader unwinds cleanly before we open a new fetch.
        scheduleStreamRecovery(rid);
      }
      return;
    } else if (obj.type === "permission_timeout") {
      // Server's PENDING entry has been popped — any further click on the
      // matching card would just 404 silently. Disable the card and label
      // it so the user knows what happened. Two reasons land here:
      //  - timed out (the normal case, after PERMISSION_TIMEOUT_SECONDS)
      //  - server restart cleared the in-memory PENDING dict; on resume
      //    we synthesize a timeout so the replayed card isn't clickable.
      ctx.currentAssistantBody = null;
      const restarted = obj.reason === "server_restart";
      // Iterate rather than building a CSS selector — request IDs are
      // server-side UUIDs today but a future change could include
      // characters that need escaping; .find() doesn't care.
      // Matches the generic permission card plus the question/plan cards,
      // all of which carry data-request-id and may be awaiting a response.
      const cards = document.querySelectorAll("article.msg.permission, article.msg.question, article.msg.plan");
      const card = [...cards].find((el) => el.dataset.requestId === String(obj.id));
      if (card) {
        card.dataset.state = "timed_out";
        card.querySelectorAll("button, input, textarea").forEach((b) => (b.disabled = true));
        const note = document.createElement("p");
        note.className = "permission-timeout-note";
        note.textContent = restarted
          ? "Server restarted before this was answered — the request is gone. Send a new message to continue."
          : `Timed out after ${obj.timeout_seconds || "?"}s — Claude was told the request was denied.`;
        card.appendChild(note);
      }
      removeFromPermQueue(obj.id);
      announce(restarted
        ? `Permission request for ${obj.tool || "tool"} discarded due to server restart.`
        : `Permission request for ${obj.tool || "tool"} timed out.`);
      playCue("timeout");
    } else if (obj.type === "permission_resolved") {
      // The request was decided on another surface (other tab, modal vs
      // inline card) or this is a replay of a decision already made.
      // Collapse the pending card into the decision summary and drop it
      // from the modal queue. The deciding surface already replaced its
      // own card, so a missing card here is the common same-tab case.
      const resolvedCard = findRequestCard(obj.id);
      if (resolvedCard && resolvedCard.dataset.state !== "resolved") {
        if (resolvedCard.classList.contains("question")) {
          replaceCardWithSummary(resolvedCard,
            obj.decision === "answer" ? "Answered" : "Question skipped");
        } else if (resolvedCard.classList.contains("plan")) {
          replaceCardWithSummary(resolvedCard,
            obj.decision === "allow" ? "Plan approved — proceeding" : "Kept planning");
        } else {
          resolvePermCardDom(resolvedCard, obj.decision);
        }
      } else if (!resolvedCard) {
        // No pending card — this tab may have collapsed it provisionally after
        // losing a decision race (404). Rewrite that summary with the real
        // decision the server just broadcast.
        correctProvisionalSummary(obj.id, obj.decision);
      }
      removeFromPermQueue(obj.id);
    } else if (obj.type === "todos_update") {
      updateTodosPanel(obj.todos || []);
      markVisibleActivity();
    } else if (obj.type === "task_started") {
      renderTaskEvent("started", obj);
    } else if (obj.type === "task_progress") {
      renderTaskEvent("progress", obj);
    } else if (obj.type === "task_notification") {
      renderTaskEvent("notification", obj);
    } else if (obj.type === "auto_fire_capped") {
      // Server reached MAX_CONSECUTIVE_AUTO_FIRES and dropped the latest
      // batch of background-task notifications instead of auto-firing
      // another synth turn. Without this handler the server's "we stopped
      // pinging Claude" signal was invisible — the user just noticed
      // background events arriving in the transcript and Claude going
      // quiet. Render an info block so the cause is obvious and treat the
      // run as paused-awaiting-user so the composer is hot again.
      ctx.currentAssistantBody = null;
      const dropped = Array.isArray(obj.events) ? obj.events : [];
      const limit = typeof obj.limit === "number" ? obj.limit : null;
      const article = document.createElement("article");
      article.className = "msg info auto-fire-capped";
      const role = document.createElement("h3");
      role.className = "role";
      role.textContent = "Auto-followups paused";
      const body = document.createElement("p");
      body.className = "info-body";
      const limitText = limit !== null ? ` (cap: ${limit})` : "";
      const droppedText = dropped.length
        ? ` ${dropped.length} background notification${dropped.length === 1 ? "" : "s"} were not auto-sent to Claude.`
        : "";
      body.textContent =
        `Hit the consecutive auto-followup limit${limitText}.${droppedText} ` +
        "Send a message to continue.";
      article.appendChild(role);
      article.appendChild(body);
      transcript.appendChild(article);
      maybeAutoScroll();
      stopGerunds();
      setStreaming(false);
      announce("Auto-followups paused — send a message to continue.");
      playCue("attention");
    } else if (obj.type === "auto_fire") {
      // Server is auto-firing a follow-up turn driven by a buffered
      // task notification. Render an info block so the user knows the
      // upcoming Claude reply is *not* responding to something they typed,
      // then flip the spinner back on.
      ctx.currentAssistantBody = null;
      const events = Array.isArray(obj.events) ? obj.events : [];
      const summary = events.length
        ? events.map((e) => {
            const bits = [];
            if (e.kind) bits.push(e.kind);
            if (e.task_id) bits.push("task " + e.task_id);
            if (e.status) bits.push(e.status);
            return bits.join(" · ") || "background event";
          }).join("; ")
        : "background events";
      const article = document.createElement("article");
      article.className = "msg info auto-fire";
      const role = document.createElement("h3");
      role.className = "role";
      role.textContent = "Auto-injected";
      const body = document.createElement("p");
      body.className = "info-body";
      body.textContent = `Background tools settled — auto-firing a follow-up turn (${summary}).`;
      article.appendChild(role);
      article.appendChild(body);
      transcript.appendChild(article);
      maybeAutoScroll();
      setStreaming(true);
      startGerunds();
      announce("Auto-responding to background events.");
      playCue("autofire");
    } else if (obj.type === "result") {
      discardPartial(ctx);
      ctx.lastResult = obj;
      if (typeof obj.input_tokens === "number") {
        lastInputTokens = obj.input_tokens;
        renderContextMeter();
      }
      // End of turn: drop the bubble pointer so the next turn's first text
      // block starts a fresh "Claude" article instead of appending into the
      // bubble we just closed.
      ctx.currentAssistantBody = null;
      // Treat each result as the end of THIS turn even if the SSE stays
      // open for an auto-fire chain. Spinner stops; user can send a new
      // message; if an auto_fire event arrives next we'll flip it back.
      // Clear the sticky in_progress todo label too — without this, a turn
      // that ended mid-todo (rare but possible) would carry the stale
      // activeForm into the next turn's spinner.
      setActiveTodoLabel(null);
      stopGerunds();
      const summary = obj.interrupted ? "Turn interrupted." : summariseResult(obj);
      setStatus(summary);
      refreshSessions();
      refreshHeaderCost();
      setStreaming(false);
      // Drain any client-side queued messages — the user submitted them
      // mid-turn and we promised we'd flush after the turn ends. Pass the
      // result summary so a screen reader still hears it even when the drain
      // announcement fires in the same tick (announce() coalesces calls).
      drainQueueIfPossible(summary);
      playCue(obj.is_error ? "error" : "done");
      if (obj.is_error) {
        const lines = [obj.result || obj.subtype || "Error"];
        if (Array.isArray(obj.errors) && obj.errors.length) {
          lines.push("--- errors ---", ...obj.errors.map((x) => typeof x === "string" ? x : JSON.stringify(x, null, 2)));
        }
        // Last-resort: dump the whole result envelope so we have every clue
        // the SDK gave us. If two passes of debugging still leave us blind,
        // the raw fields (subtype, stop_reason, model_usage, etc.) usually
        // pinpoint it.
        const dump = { ...obj };
        delete dump.type;
        lines.push("--- raw result ---", JSON.stringify(dump, null, 2));
        const detail = lines.join("\n");
        const summary = obj.result || obj.subtype || "see technical details";
        setStatus("Error: " + summary);
        renderErrorBlock(detail, { summary, announce: false });
        announce("Error: " + (obj.result || ""));
      }
    } else if (obj.type === "error") {
      discardPartial(ctx);
      // A lost_input error carries the queue_id of the message that never
      // reached Claude — clear its "(sending…)" chip so it doesn't hang
      // forever (user_prompt / queued_input_cancelled, its usual clearers,
      // never fire for a message that was lost).
      if (obj.queue_id) clearQueueEntryById(obj.queue_id);
      // Driver crashed mid-turn. The server will emit `_done` and close
      // the SSE shortly, but flip the local state now so the input isn't
      // trapped in "Queue" mode while we wait for the close to land.
      setActiveTodoLabel(null);
      stopGerunds();
      setStreaming(false);
      const summary = obj.message || obj.exit_code || "see technical details";
      const detail = obj.stderr ? `${obj.message || "Error"}\n${obj.stderr}` : null;
      setStatus("Error: " + summary);
      renderErrorBlock(detail ? String(detail) : null, { summary, announce: false });
      announce("Error: " + (obj.message || ""));
      playCue("error");
    }
  }

  // Wall-clock start per task block, so long-running subagents show a
  // duration instead of an undated spinner arrow.
  const taskStartedAt = new Map();

  function renderTaskEvent(kind, obj) {
    // Group all events for the same task_id under one collapsible block so
    // a chatty Monitor doesn't flood the transcript. The block updates in
    // place as later events arrive.
    const id = obj.task_id || "?";
    if (kind === "started" && !taskStartedAt.has(id)) taskStartedAt.set(id, Date.now());
    const startedAt = taskStartedAt.get(id);
    const elapsed = startedAt ? formatElapsed(Date.now() - startedAt) : null;
    const blockId = `task-${id}`;
    let block = document.getElementById(blockId);
    if (!block) {
      block = document.createElement("details");
      block.id = blockId;
      block.className = "task-block";
      block.open = true;
      const summary = document.createElement("summary");
      summary.className = "task-summary";
      summary.textContent = "▶ " + (obj.description || "task " + id);
      block.appendChild(summary);
      // Stop control for a still-running background task. Lives in the summary
      // so it's reachable without expanding; stopPropagation keeps the click
      // from toggling the <details>. Removed once the task reports done.
      const stopBtn = document.createElement("button");
      stopBtn.type = "button";
      stopBtn.className = "task-stop";
      stopBtn.textContent = "Stop";
      stopBtn.setAttribute("aria-label", "Stop background task " + (obj.description || id));
      stopBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        stopTask(id, stopBtn);
      });
      summary.appendChild(stopBtn);
      const log = document.createElement("div");
      log.className = "task-log";
      block.appendChild(log);
      transcript.appendChild(block);
    }
    const summary = block.querySelector("summary");
    const log = block.querySelector(".task-log");

    // Update summary with latest description + status.
    if (kind === "started") {
      summary.textContent = "▶ " + (obj.description || "task " + id);
    } else if (kind === "progress") {
      const base = obj.description || "task " + id;
      summary.textContent = "▶ " + base + (elapsed ? ` · ${elapsed}` : "");
    } else if (kind === "notification") {
      const status = obj.status || "done";
      const icon = status === "success" ? "✓" : status === "error" ? "✗" : "●";
      const took = elapsed ? ` in ${elapsed}` : "";
      summary.textContent = `${icon} ${obj.description || obj.summary || "task " + id} (${status}${took})`;
      taskStartedAt.delete(id);
      block.classList.add("task-" + status);
      const sb = block.querySelector(".task-stop");
      if (sb) sb.remove();
    }

    // Append log line.
    const line = document.createElement("div");
    line.className = "task-line task-line-" + kind;
    if (kind === "progress") {
      line.textContent = (obj.last_tool_name ? `[${obj.last_tool_name}] ` : "") + (obj.description || "");
    } else if (kind === "notification") {
      line.textContent = (obj.summary || "(no summary)");
    } else {
      line.textContent = obj.description || "started";
    }
    log.appendChild(line);
    maybeAutoScroll();
    markVisibleActivity();
    if (kind === "notification") {
      announce(`Background task ${id}: ${obj.status || "done"}`);
      playCue(obj.status === "error" ? "task_error" : "task_done");
    }
  }

  // Stop one running background task via the stop_task verb. The CLI then
  // emits a task_notification with status 'stopped', which renderTaskEvent
  // renders (and which removes this button).
  async function stopTask(taskId, btn) {
    if (!sessionId) return;
    btn.disabled = true;
    btn.textContent = "Stopping…";
    announce("Stopping background task " + taskId + ".");
    try {
      const fd = new FormData();
      fd.append("session_id", sessionId);
      fd.append("task_id", taskId);
      const r = await fetch("/api/chat/stop-task", { method: "POST", body: fd });
      if (!r.ok) {
        announce("Couldn't stop task (" + r.status + ").");
        btn.disabled = false;
        btn.textContent = "Stop";
      }
    } catch (e) {
      announce("Couldn't stop task: network error.");
      btn.disabled = false;
      btn.textContent = "Stop";
    }
  }

  function renderErrorBlock(detail, opts) {
    // Two-tier rendering: a short summary line is always visible, and the
    // raw payload (stderr, traceback, model errors) is folded into a
    // <details> disclosure. This keeps the UI honest without dumping
    // filesystem paths and env diagnostics directly into the page when
    // claude-web is shared with other users.
    opts = opts || {};
    const article = document.createElement("article");
    article.className = "msg error";
    if (opts.cls) article.classList.add(opts.cls);
    const role = document.createElement("h3");
    role.className = "role";
    role.textContent = opts.heading || "Error";
    article.appendChild(role);
    if (opts.summary) {
      const lead = document.createElement("p");
      lead.className = "error-summary";
      lead.textContent = opts.summary;
      article.appendChild(lead);
    }
    if (detail) {
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = "Show technical details";
      det.appendChild(sum);
      const body = document.createElement("pre");
      body.className = "error-body";
      body.textContent = detail;
      det.appendChild(body);
      article.appendChild(det);
    }
    transcript.appendChild(article);
    // Announce so screen-reader users hear the error. #status is aria-hidden
    // and #transcript isn't a live region, so without this every slash-command
    // error (/model typo, /effort, /rewind, /help) and inline error block is
    // silent. Callers that announce richer text themselves pass announce:false.
    if (opts.announce !== false) {
      const spoken = opts.summary
        || (detail ? String(detail).split("\n")[0] : "")
        || opts.heading || "Error";
      announce(String(spoken).slice(0, 200));
    }
    // Force-scroll on errors — the user almost always wants to see them.
    maybeAutoScroll(true);
  }

  function renderContextMeter() {
    if (!contextMeter) return;
    const model = (modelSelect && modelSelect.value) || lastSeenModel;
    const max = model && MODEL_CONTEXT[model];
    if (!lastInputTokens) {
      contextMeter.hidden = true;
      return;
    }
    contextMeter.hidden = false;
    const pretty = (n) => n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
    if (max) {
      const pct = Math.min(100, Math.round((lastInputTokens / max) * 100));
      contextText.textContent = `${pretty(lastInputTokens)} / ${pretty(max)} (${pct}%)`;
      contextFill.style.width = pct + "%";
      // Threshold colour driven by class so we can drop 'unsafe-inline' from
      // style-src once every other inline style is gone too. Width can stay
      // inline (it's a numeric percentage, can't be classed cleanly).
      contextFill.classList.toggle("ctx-warn", pct > 70 && pct <= 85);
      contextFill.classList.toggle("ctx-danger", pct > 85);
      let crossed = 0;
      if (pct >= 95) crossed = 95;
      else if (pct >= 80) crossed = 80;
      if (crossed > lastContextThresholdAnnounced) {
        announce(`Context at ${pct}%.`);
        lastContextThresholdAnnounced = crossed;
      } else if (pct < 80) {
        lastContextThresholdAnnounced = 0;
      }
    } else {
      contextText.textContent = pretty(lastInputTokens);
      contextFill.style.width = "0%";
    }
  }

  // Exact context breakdown via the get_context_usage verb. The always-on
  // meter above is an estimate from last-turn input tokens; this fetches the
  // CLI's real per-category numbers (the /context view) on demand and both
  // renders and speaks them.
  const contextDetailsBtn = document.getElementById("context-details-btn");
  const contextBreakdown = document.getElementById("context-breakdown");
  if (contextDetailsBtn) {
    contextDetailsBtn.addEventListener("click", async () => {
      if (contextDetailsBtn.getAttribute("aria-expanded") === "true") {
        contextDetailsBtn.setAttribute("aria-expanded", "false");
        if (contextBreakdown) contextBreakdown.hidden = true;
        return;
      }
      if (!sessionId) {
        announce("Exact context usage needs a running conversation.");
        return;
      }
      contextDetailsBtn.disabled = true;
      try {
        const r = await fetch("/api/chat/context/" + encodeURIComponent(sessionId));
        if (!r.ok) {
          announce("Couldn't load context usage (" + r.status + ").");
          return;
        }
        const data = await r.json();
        renderContextBreakdown((data && data.usage) || {});
      } catch (e) {
        announce("Couldn't load context usage: network error.");
      } finally {
        contextDetailsBtn.disabled = false;
      }
    });
  }

  function renderContextBreakdown(usage) {
    if (!contextBreakdown) return;
    const cats = Array.isArray(usage.categories) ? usage.categories : [];
    const total = usage.totalTokens || 0;
    const max = usage.maxTokens || 0;
    const pct = typeof usage.percentage === "number"
      ? Math.round(usage.percentage)
      : (max ? Math.round((total / max) * 100) : 0);
    const pretty = (n) => n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
    contextBreakdown.textContent = "";
    const heading = document.createElement("p");
    heading.className = "context-breakdown-total";
    heading.textContent = `${pct}% used · ${pretty(total)} of ${pretty(max)} tokens`
      + (usage.model ? ` · ${usage.model}` : "");
    contextBreakdown.appendChild(heading);
    if (cats.length) {
      const ul = document.createElement("ul");
      ul.className = "context-breakdown-list";
      for (const c of cats) {
        if (!c || !c.tokens) continue;
        const li = document.createElement("li");
        li.textContent = `${c.name || "?"}: ${pretty(c.tokens)}`;
        ul.appendChild(li);
      }
      contextBreakdown.appendChild(ul);
    }
    contextBreakdown.hidden = false;
    contextDetailsBtn.setAttribute("aria-expanded", "true");
    const topCats = cats.filter((c) => c && c.tokens).slice(0, 3)
      .map((c) => `${c.name} ${pretty(c.tokens)}`).join(", ");
    announce(`Context ${pct}% used, ${pretty(total)} of ${pretty(max)} tokens.`
      + (topCats ? ` Top: ${topCats}.` : ""));
  }

  function summariseResult(r) {
    if (!r) return "Response complete.";
    const parts = [];
    const reason = r.stop_reason || r.subtype || "end_turn";
    const reasonLabels = {
      end_turn: "Claude stopped",
      max_turns: "Hit turn limit",
      max_tokens: "Hit token limit",
      stop_sequence: "Stop sequence",
      tool_use: "Awaiting tool",
    };
    parts.push(reasonLabels[reason] || ("Stopped: " + reason));
    if (typeof r.num_turns === "number") parts.push(r.num_turns + " turn" + (r.num_turns === 1 ? "" : "s"));
    // Only attach a dollar figure when the server marked the turn as billed
    // (API-key slot). Subscription slots get a synthetic SDK cost that
    // doesn't match the actual bill, so we omit it rather than mislead.
    if (r.cost_is_billed && typeof r.total_cost_usd === "number") {
      parts.push(formatCost(r.total_cost_usd));
    }
    if (r.is_error) parts.unshift("Error");
    return parts.join(" · ");
  }

  // ── Approval modal ────────────────────────────────────────────────────
  // Pending permission requests, AskUserQuestion forms, and plan reviews
  // funnel through one native <dialog> shown with showModal(), in arrival
  // order. The inline transcript card stays as the scrollable record and
  // fallback input surface, but the modal is the primary one: mobile
  // screen readers largely ignore programmatic focus() into the
  // transcript, while a modal dialog makes the rest of the page inert so
  // the swipe order collapses to heading → payload → buttons. Esc /
  // "Decide later" closes without deciding; the header "N approvals
  // waiting" button reopens.
  //
  // Permission entries rebuild their (read-only) payload inside the
  // dialog; question/plan entries MOVE the live card node in instead —
  // their interactive form state (picked radios, typed feedback) must not
  // exist in two places. A hidden placeholder marks the transcript spot
  // and the card moves back on close/advance.
  const permQueue = []; // entries: {kind: "permission"|"question"|"plan", req}
  let permDialog = null;
  let permDialogShowingId = null;
  let permOpenTimer = null;
  // Replay of an already-decided request delivers permission_request and
  // its permission_resolved in quick succession; deferring the open lets
  // the resolution land first so the modal never flashes for stale
  // requests. Live requests just open a beat later.
  const PERM_DIALOG_OPEN_DELAY_MS = 250;
  const permWaitingBtn = document.getElementById("perm-waiting");
  if (permWaitingBtn) {
    permWaitingBtn.addEventListener("click", () => openPermDialogNow());
    permWaitingBtn.setAttribute("aria-keyshortcuts", "Alt+A");
  }

  // Global hotkeys, Alt-modified so they never collide with composer typing.
  //   Alt+A — open the pending-approval dialog from anywhere (the most
  //           latency-sensitive interaction; otherwise it's a tab-hunt).
  //   Alt+J — move focus to the latest assistant reply so a screen-reader
  //           user lands on it directly after "Response complete".
  document.addEventListener("keydown", (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey) return;
    const k = (e.key || "").toLowerCase();
    if (k === "a" && permQueue.length) {
      e.preventDefault();
      openPermDialogNow();
    } else if (k === "j") {
      e.preventDefault();
      focusLastReply();
    }
  });

  function focusLastReply() {
    const replies = transcript.querySelectorAll(".msg.assistant .role");
    const last = replies[replies.length - 1];
    if (!last) {
      announce("No reply yet.");
      return;
    }
    last.setAttribute("tabindex", "-1");
    last.focus();
    last.scrollIntoView({ block: "start" });
  }

  function ensurePermDialog() {
    if (permDialog) return permDialog;
    const dlg = document.createElement("dialog");
    if (typeof dlg.showModal !== "function") return null; // inline cards still work
    dlg.id = "perm-dialog";
    dlg.setAttribute("aria-labelledby", "perm-dialog-title");
    // Esc (the native cancel) means "decide later" — the inline card stays
    // pending and the header badge keeps the count. Deny is explicit only.
    dlg.addEventListener("close", () => {
      restoreDockedCard();
      permDialogShowingId = null;
      updatePermBadge();
    });
    document.body.appendChild(dlg);
    permDialog = dlg;
    return dlg;
  }

  function updatePermBadge() {
    if (!permWaitingBtn) return;
    const n = permQueue.length;
    permWaitingBtn.hidden = n === 0;
    if (n) permWaitingBtn.textContent = `${n} approval${n === 1 ? "" : "s"} waiting`;
  }

  // Live card currently hosted inside the dialog (question/plan kinds) and
  // the hidden marker holding its place in the transcript.
  let dockedCard = null;
  let dockedPlaceholder = null;

  function restoreDockedCard() {
    if (dockedCard) {
      const h = dockedCard.querySelector("h3.role");
      if (h) h.hidden = false;
      if (dockedPlaceholder) dockedPlaceholder.replaceWith(dockedCard);
    } else if (dockedPlaceholder) {
      dockedPlaceholder.remove();
    }
    dockedCard = null;
    dockedPlaceholder = null;
  }

  function entryLabel(entry) {
    if (entry.kind === "question") return "Claude's question";
    if (entry.kind === "plan") return "plan review";
    return entry.req.tool;
  }

  function appendDecideLater(parent) {
    const later = document.createElement("button");
    later.type = "button";
    later.textContent = "Decide later";
    later.className = "btn-secondary";
    later.addEventListener("click", () => permDialog.close());
    parent.appendChild(later);
  }

  // Populate the dialog for the front of the queue. Returns false when
  // nothing renderable remains (queue drained, or every remaining entry's
  // card vanished) — in that case an open dialog is closed.
  function renderPermDialog() {
    if (!permDialog) return false;
    restoreDockedCard();
    // Question/plan entries render by docking their live card; an entry
    // whose card is gone (resolved elsewhere, transcript rewritten) has
    // nothing to show — drop it.
    while (permQueue.length) {
      const e = permQueue[0];
      if (e.kind === "permission" || findRequestCard(e.req.id)) break;
      permQueue.shift();
    }
    updatePermBadge();
    if (!permQueue.length) {
      if (permDialog.open) permDialog.close();
      return false;
    }
    const entry = permQueue[0];
    const req = entry.req;
    permDialogShowingId = req.id;
    permDialog.innerHTML = "";

    const countSuffix = permQueue.length > 1 ? ` — 1 of ${permQueue.length} waiting` : "";
    const title = document.createElement("h2");
    title.id = "perm-dialog-title";
    permDialog.appendChild(title);

    if (entry.kind === "permission") {
      title.textContent = `Claude wants to use ${req.tool}${countSuffix}`;
      const detail = document.createElement("div");
      detail.className = "permission-input-wrap";
      appendPermissionPayload(detail, req.tool, req.input || {});
      permDialog.appendChild(detail);

      const actions = document.createElement("div");
      actions.className = "permission-actions";
      const allowSessionSupported = req.allow_session_supported !== false;
      const sigLabel = req.signature ? ` "${truncate(req.signature, 30)}"` : "";
      const buttons = [
        { decision: "deny", label: "Deny", variant: "danger" },
        { decision: "allow", label: "Allow once", variant: "primary" },
      ];
      if (allowSessionSupported) {
        buttons.push({
          decision: "allow_session",
          label: `Allow this session${sigLabel}`,
          variant: "secondary",
        });
      }
      for (const b of buttons) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = b.label;
        btn.className = "btn-" + b.variant;
        btn.addEventListener("click", () => decideFromDialog(req, b.decision));
        actions.appendChild(btn);
      }
      appendDecideLater(actions);
      permDialog.appendChild(actions);
    } else {
      title.textContent = entry.kind === "question"
        ? `Claude is asking${countSuffix}`
        : `Claude's plan — review${countSuffix}`;
      const card = findRequestCard(req.id);
      dockedPlaceholder = document.createElement("span");
      dockedPlaceholder.hidden = true;
      card.replaceWith(dockedPlaceholder);
      // The card's own h3 duplicates the dialog title — hide while docked
      // so a screen reader hears the heading once.
      const h = card.querySelector("h3.role");
      if (h) h.hidden = true;
      permDialog.appendChild(card);
      dockedCard = card;
      const actions = document.createElement("div");
      actions.className = "permission-actions";
      appendDecideLater(actions);
      permDialog.appendChild(actions);
    }
    return true;
  }

  function focusPermDefault() {
    if (!permDialog) return;
    const entry = permQueue[0];
    if (!entry) return;
    let btn = null;
    if (entry.kind === "permission") {
      // Safest button first for high-risk tools — same policy as the card.
      const isHighRisk = entry.req.tool === "Bash" || entry.req.tool === "Write";
      btn = permDialog.querySelector(isHighRisk ? ".btn-danger" : ".btn-primary");
    } else if (entry.kind === "question") {
      btn = permDialog.querySelector("input");
    } else {
      btn = permDialog.querySelector(".btn-primary");
    }
    if (btn) btn.focus();
  }

  function openPermDialogNow() {
    if (!permQueue.length) return;
    const dlg = ensurePermDialog();
    if (!dlg || dlg.open) return;
    if (!renderPermDialog()) return;
    dlg.showModal();
    focusPermDefault();
  }

  function openPermDialogSoon() {
    if (permOpenTimer) return;
    permOpenTimer = setTimeout(() => {
      permOpenTimer = null;
      openPermDialogNow();
    }, PERM_DIALOG_OPEN_DELAY_MS);
  }

  function enqueuePermRequest(kind, req) {
    if (!req.id || permQueue.some((q) => String(q.req.id) === String(req.id))) return;
    permQueue.push({ kind, req });
    updatePermBadge();
    openPermDialogSoon();
  }

  function removeFromPermQueue(requestId) {
    const i = permQueue.findIndex((q) => String(q.req.id) === String(requestId));
    if (i === -1) return;
    const wasShowing = String(permDialogShowingId) === String(requestId);
    permQueue.splice(i, 1);
    updatePermBadge();
    if (permDialog && permDialog.open && wasShowing) {
      if (renderPermDialog()) {
        focusPermDefault();
        announce(`Next approval: ${entryLabel(permQueue[0])}.`);
      }
    }
  }

  function clearPermQueue() {
    permQueue.length = 0;
    updatePermBadge();
    if (permOpenTimer) { clearTimeout(permOpenTimer); permOpenTimer = null; }
    // close() triggers restoreDockedCard via the close handler; with the
    // transcript already wiped the placeholder is detached so the restore
    // is a harmless no-op that just drops the refs.
    if (permDialog && permDialog.open) permDialog.close();
  }

  function findRequestCard(requestId) {
    const cards = document.querySelectorAll(
      "article.msg.permission, article.msg.question, article.msg.plan",
    );
    return [...cards].find((el) => el.dataset.requestId === String(requestId)) || null;
  }

  async function decideFromDialog(req, decision) {
    const card = findRequestCard(req.id);
    if (!card) {
      // Card gone (resolved from another surface, transcript rewritten by
      // a resume) — nothing to decide against; drop the queue entry.
      removeFromPermQueue(req.id);
      return;
    }
    const btns = permDialog.querySelectorAll("button");
    btns.forEach((b) => (b.disabled = true));
    const ok = await decide(req.id, decision, card);
    // Success funnels through removeFromPermQueue (via decide), which
    // advances to the next request or closes the dialog.
    if (!ok) btns.forEach((b) => (b.disabled = false));
  }

  function renderPermissionCard(req) {
    const card = document.createElement("article");
    card.className = "msg permission";
    // role="group" (not alertdialog): the card is an inline transcript article,
    // not a focus-containing dialog, so alertdialog+aria-modal=false is an ARIA
    // mismatch NVDA can mis-frame. Urgency is already carried by the explicit
    // announce() + earcon; the heading/detail labelling stays via the ids below.
    card.setAttribute("role", "group");
    if (req.id) card.dataset.requestId = req.id;
    card.dataset.state = "pending";

    // h3 (not div) so NVDA's H-key heading navigation finds the card —
    // a permission prompt is the most urgent thing on screen, it should
    // be reachable the same way every other message header is.
    const headingId = `perm-heading-${req.id || Math.random().toString(36).slice(2)}`;
    const detailId = `perm-detail-${req.id || Math.random().toString(36).slice(2)}`;
    const heading = document.createElement("h3");
    heading.className = "role";
    heading.id = headingId;
    heading.textContent = `Claude wants to use ${req.tool}`;
    card.appendChild(heading);
    card.setAttribute("aria-labelledby", headingId);
    card.setAttribute("aria-describedby", detailId);

    // Full payload preview — no truncation. Diff/Edit/Write inputs live
    // inside an expandable <details> so a 50-line replacement doesn't fill
    // the viewport, but the entire content is reachable before the user
    // can approve. Truncating at this stage means the user could approve
    // an `rm -rf` hidden after the truncation point.
    const detail = document.createElement("div");
    detail.id = detailId;
    detail.className = "permission-input-wrap";
    appendPermissionPayload(detail, req.tool, req.input || {});
    card.appendChild(detail);

    const actions = document.createElement("div");
    actions.className = "permission-actions";

    const isHighRisk = req.tool === "Bash" || req.tool === "Write";
    // Server tells us whether this tool's signature is safe to allowlist
    // for the session. For tools where the signature is too coarse (Bash:
    // first word only) the session button is hidden entirely so the user
    // can't accidentally bless future arbitrary commands.
    const allowSessionSupported = req.allow_session_supported !== false;
    const sigLabel = req.signature ? ` "${truncate(req.signature, 30)}"` : "";

    const buttons = [
      { decision: "deny", label: "Deny", variant: "danger" },
      { decision: "allow", label: "Allow once", variant: "primary" },
    ];
    if (allowSessionSupported) {
      buttons.push({
        decision: "allow_session",
        label: `Allow this session${sigLabel}`,
        variant: "secondary",
      });
    }

    for (const b of buttons) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = b.label;
      btn.className = "btn-" + b.variant;
      btn.addEventListener("click", () => decide(req.id, b.decision, card));
      actions.appendChild(btn);
    }
    card.appendChild(actions);
    transcript.appendChild(card);
    // Force-scroll: a permission card needs to be on screen, full stop.
    maybeAutoScroll(true);

    // Focus the safest button by default for high-risk tools — but only
    // when the modal can't open (no <dialog> support): otherwise the modal
    // grabs focus moments later and the double move reads twice on a
    // screen reader.
    if (!ensurePermDialog()) {
      const focusBtn = isHighRisk ? actions.querySelector(".btn-danger") : actions.querySelector(".btn-primary");
      if (focusBtn) focusBtn.focus();
    }

    // Esc denies. Enter is intentionally NOT bound to "allow once" — that
    // would over-ride the focused-button default, so a user with focus on
    // the Deny button (the default for Bash/Write) would still approve by
    // pressing Enter. Native button activation handles Enter/Space on the
    // focused button correctly.
    card.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        decide(req.id, "deny", card);
      }
    });
  }

  function appendPermissionPayload(parent, tool, input) {
    const block = document.createElement("pre");
    block.className = "permission-input";
    if (tool === "Bash" && input.command) {
      block.textContent = "$ " + input.command;
      parent.appendChild(block);
      return;
    }
    if (tool === "Edit") {
      const path = input.file_path || input.path || "";
      const head = document.createElement("p");
      head.className = "permission-input-path";
      head.textContent = path;
      parent.appendChild(head);
      const oldS = input.old_string || "";
      const newS = input.new_string || "";
      const details = document.createElement("details");
      details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = `Replace ${oldS.split("\n").length} line(s) → ${newS.split("\n").length} line(s)`;
      details.appendChild(summary);
      const pre = document.createElement("pre");
      pre.className = "permission-input";
      pre.appendChild(diffLines(oldS, newS));
      details.appendChild(pre);
      parent.appendChild(details);
      return;
    }
    if (tool === "Write") {
      const path = input.file_path || input.path || "";
      const head = document.createElement("p");
      head.className = "permission-input-path";
      head.textContent = path;
      parent.appendChild(head);
      const content = input.content || "";
      const details = document.createElement("details");
      details.open = content.length < 800;
      const summary = document.createElement("summary");
      summary.textContent = `Write ${content.length.toLocaleString()} char${content.length === 1 ? "" : "s"} (${content.split("\n").length} line${content.split("\n").length === 1 ? "" : "s"})`;
      details.appendChild(summary);
      const pre = document.createElement("pre");
      pre.className = "permission-input";
      pre.textContent = content;
      details.appendChild(pre);
      parent.appendChild(details);
      return;
    }
    block.textContent = JSON.stringify(input, null, 2);
    parent.appendChild(block);
  }

  // Replace a pending card with a compact record of the decision. Prefer a
  // semantic summary line (the heading already says "Claude wants to use
  // {tool}", and the path is in .permission-input-path for Edit/Write)
  // over the first line of the diff body.
  const PERM_LABELS = { allow: "Allowed", allow_session: "Allowed (session)", deny: "Denied" };

  function resolvePermCardDom(card, decision, opts) {
    opts = opts || {};
    card.dataset.state = "resolved";
    const summary = document.createElement("article");
    summary.className = "msg permission-resolved";
    const heading = card.querySelector(".role")?.textContent?.replace(/^Claude wants to use\s+/, "") || "tool";
    const path = card.querySelector(".permission-input-path")?.textContent?.trim();
    const firstInput = card.querySelector(".permission-input")?.textContent?.split("\n")[0]?.trim();
    const detail = path || firstInput || "";
    const suffix = detail ? `${heading}: ${detail}` : heading;
    // Carry the request id + suffix onto the collapsed node so an authoritative
    // permission_resolved re-broadcast can re-find and rewrite it. This matters
    // for the provisional case: when this tab loses a two-tab decision race it
    // gets a 404 and doesn't know the real decision, so it must NOT stamp its
    // own — it shows "Handled elsewhere" and lets the re-broadcast correct it.
    if (card.dataset.requestId) summary.dataset.requestId = card.dataset.requestId;
    summary.dataset.state = "resolved";
    summary.dataset.summarySuffix = suffix;
    const verb = opts.provisional ? "Handled elsewhere" : (PERM_LABELS[decision] || decision);
    if (opts.provisional) summary.dataset.provisional = "1";
    summary.textContent = `${verb} — ${suffix}`;
    card.replaceWith(summary);
    removeFromPermQueue(card.dataset.requestId);
    return summary;
  }

  // Rewrite a provisional "Handled elsewhere" summary with the authoritative
  // decision once the server's permission_resolved re-broadcast arrives, so the
  // durable transcript label is the real decision, not this tab's lost click.
  function correctProvisionalSummary(id, decision) {
    if (!id) return false;
    const el = document.querySelector(
      `.permission-resolved[data-provisional][data-request-id="${CSS.escape(String(id))}"]`,
    );
    if (!el) return false;
    el.textContent = `${PERM_LABELS[decision] || decision} — ${el.dataset.summarySuffix || ""}`
      .replace(/ — $/, "");
    delete el.dataset.provisional;
    return true;
  }

  // Returns true when the decision was accepted by the server.
  async function decide(requestId, decision, card) {
    // State machine: pending → deciding → (resolved | timed_out).
    // A second click while a fetch is in flight, or an Esc keypress while
    // the click is mid-POST, would otherwise issue a duplicate decision.
    // Once timed_out arrives, the card is locked even if a request fails.
    if (!card || card.dataset.state === "deciding" || card.dataset.state === "timed_out" || card.dataset.state === "resolved") {
      return false;
    }
    card.dataset.state = "deciding";
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const fd = new FormData();
      fd.append("decision", decision);
      const r = await fetch(
        `/api/permission/${encodeURIComponent(requestId)}`,
        { method: "POST", body: fd },
      );
      if (r.status === 404) {
        // The server already resolved this request (decided on another tab, or
        // a stale prompt replayed after a reconnect). Its future is gone, so
        // retrying would only 404 again — collapse the card. This tab does NOT
        // know the real decision (another tab may have picked the opposite), so
        // show a neutral "Handled elsewhere" and let the authoritative
        // permission_resolved re-broadcast rewrite it with the true decision,
        // rather than stamping this tab's click as the durable label.
        resolvePermCardDom(card, decision, { provisional: true });
        removeFromPermQueue(requestId);
        announce("Already handled elsewhere.");
        return false;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      resolvePermCardDom(card, decision);
      return true;
    } catch (err) {
      // Only re-enable if no terminal state arrived during the in-flight
      // fetch. A late permission_timeout would have flipped state to
      // "timed_out" — re-enabling there would let the user click an
      // already-discarded request.
      if (card.dataset.state === "deciding") {
        card.dataset.state = "pending";
        card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
      // #status is aria-hidden, so a screen-reader user gets no feedback that
      // the decision failed and Claude is still blocked — announce it.
      setStatus("Failed to send decision: " + err.message);
      announce("Failed to send decision. Claude is still waiting — try again.");
      return false;
    }
  }

  // Shared resolver POST for the question/plan cards. `payload` (object|null)
  // is JSON-encoded into the `payload` form field; the backend hands it to the
  // permission callback (answers for AskUserQuestion, feedback for plan).
  async function postDecision(requestId, decision, payload) {
    const fd = new FormData();
    fd.append("decision", decision);
    if (payload != null) fd.append("payload", JSON.stringify(payload));
    const r = await fetch(
      `/api/permission/${encodeURIComponent(requestId)}`,
      { method: "POST", body: fd },
    );
    if (!r.ok) throw new Error("HTTP " + r.status);
  }

  function replaceCardWithSummary(card, text) {
    card.dataset.state = "resolved";
    const summary = document.createElement("article");
    summary.className = "msg permission-resolved";
    summary.textContent = text;
    if (dockedCard === card && dockedPlaceholder) {
      // Card is hosted in the approval dialog: the summary belongs at the
      // card's original transcript spot, and the dialog advances.
      dockedPlaceholder.replaceWith(summary);
      card.remove();
      dockedCard = null;
      dockedPlaceholder = null;
    } else {
      card.replaceWith(summary);
    }
    removeFromPermQueue(card.dataset.requestId);
  }

  // AskUserQuestion → accessible form. Each question is a <fieldset>/<legend>
  // with radio (single-select) or checkbox (multiSelect) options plus an
  // "Other" free-text row. Selections post back keyed by question text, the
  // shape the bundled CLI reads from the tool's `answers` input.
  function renderQuestionCard(req) {
    const card = document.createElement("article");
    card.className = "msg question";
    card.setAttribute("role", "group");
    card.dataset.requestId = req.id || "";
    card.dataset.state = "pending";
    const headingId = `q-heading-${req.id || Math.random().toString(36).slice(2)}`;
    const heading = document.createElement("h3");
    heading.className = "role";
    heading.id = headingId;
    heading.textContent = "Claude is asking";
    card.appendChild(heading);
    card.setAttribute("aria-labelledby", headingId);

    const form = document.createElement("form");
    form.className = "question-form";
    const questions = Array.isArray(req.questions) ? req.questions : [];
    const fieldMeta = [];
    questions.forEach((q, qi) => {
      const fs = document.createElement("fieldset");
      fs.className = "question-fieldset";
      const legend = document.createElement("legend");
      legend.textContent = q.question || q.header || `Question ${qi + 1}`;
      fs.appendChild(legend);
      const multi = !!q.multiSelect;
      const groupName = `q-${req.id}-${qi}`;
      const opts = Array.isArray(q.options) ? q.options : [];
      opts.forEach((opt, oi) => {
        const row = document.createElement("div");
        row.className = "question-option";
        const input = document.createElement("input");
        input.type = multi ? "checkbox" : "radio";
        input.name = groupName;
        input.id = `${groupName}-${oi}`;
        input.value = opt.label;
        const label = document.createElement("label");
        label.setAttribute("for", input.id);
        label.textContent = opt.description ? `${opt.label} — ${opt.description}` : opt.label;
        row.appendChild(input);
        row.appendChild(label);
        fs.appendChild(row);
      });
      // Free-text "Other", matching the native tool's auto-provided option.
      const otherRow = document.createElement("div");
      otherRow.className = "question-option";
      const otherInput = document.createElement("input");
      otherInput.type = multi ? "checkbox" : "radio";
      otherInput.name = groupName;
      otherInput.id = `${groupName}-other`;
      otherInput.value = "__other__";
      const otherLabel = document.createElement("label");
      otherLabel.setAttribute("for", otherInput.id);
      otherLabel.textContent = "Other:";
      const otherText = document.createElement("input");
      otherText.type = "text";
      otherText.className = "question-other-text";
      otherText.setAttribute("aria-label", `Other answer for: ${q.question || q.header || "question"}`);
      otherText.addEventListener("input", () => { if (otherText.value) otherInput.checked = true; });
      otherRow.appendChild(otherInput);
      otherRow.appendChild(otherLabel);
      otherRow.appendChild(otherText);
      fs.appendChild(otherRow);
      form.appendChild(fs);
      fieldMeta.push({ q, groupName, multi, otherText });
    });

    const actions = document.createElement("div");
    actions.className = "permission-actions";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn-primary";
    submit.textContent = "Submit answers";
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "btn-secondary";
    skip.textContent = "Skip";
    actions.appendChild(submit);
    actions.appendChild(skip);
    form.appendChild(actions);
    card.appendChild(form);
    transcript.appendChild(card);
    maybeAutoScroll(true);
    // Skip the inline focus move when the approval modal will host this
    // card moments later — double focus reads twice on a screen reader.
    if (!ensurePermDialog()) {
      const firstInput = form.querySelector("input");
      if (firstInput) firstInput.focus();
    }

    function unlock() {
      card.dataset.state = "pending";
      card.querySelectorAll("button, input").forEach((el) => (el.disabled = false));
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (card.dataset.state !== "pending") return;
      const answers = {};
      for (const fm of fieldMeta) {
        const qtext = fm.q.question || fm.q.header;
        if (!qtext) continue;
        // Iterate rather than build a selector from a server-supplied id: a
        // CSS-special char in req.id would break the selector and silently drop
        // answers (matches the project's avoid-selectors-from-ids convention).
        const checked = [...form.querySelectorAll("input:checked")].filter((i) => i.name === fm.groupName);
        const vals = [];
        for (const c of checked) {
          if (c.value === "__other__") {
            const t = fm.otherText.value.trim();
            if (t) vals.push(t);
          } else {
            vals.push(c.value);
          }
        }
        if (vals.length) answers[qtext] = fm.multi ? vals : vals[0];
      }
      card.dataset.state = "deciding";
      card.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
      const entries = Object.entries(answers);
      try {
        // Nothing selected is a genuine skip — send `dismiss` so the model
        // gets the same signal the Skip button sends, instead of an empty
        // `answer` payload while the UI claims "Question skipped".
        if (entries.length) {
          await postDecision(req.id, "answer", { answers });
          replaceCardWithSummary(card,
            `Answered — ${entries.map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join("; ")}`);
        } else {
          await postDecision(req.id, "dismiss", null);
          replaceCardWithSummary(card, "Question skipped");
        }
      } catch (err) {
        unlock();
        setStatus("Failed to send answer: " + err.message);
        announce("Failed to send answer. Claude is still waiting — try again.");
      }
    });
    skip.addEventListener("click", async () => {
      if (card.dataset.state !== "pending") return;
      card.dataset.state = "deciding";
      card.querySelectorAll("button, input").forEach((el) => (el.disabled = true));
      try {
        await postDecision(req.id, "dismiss", null);
        replaceCardWithSummary(card, "Question skipped");
      } catch (err) {
        unlock();
        setStatus("Failed: " + err.message);
        announce("Failed to skip the question. Claude is still waiting — try again.");
      }
    });
  }

  // ExitPlanMode → plan review card. Approve lets the tool run (CLI exits plan
  // mode and implements); Keep planning denies it with optional feedback so
  // the model revises and presents again.
  function renderPlanCard(req) {
    const card = document.createElement("article");
    card.className = "msg plan";
    card.setAttribute("role", "group");
    card.dataset.requestId = req.id || "";
    card.dataset.state = "pending";
    const headingId = `plan-heading-${req.id || Math.random().toString(36).slice(2)}`;
    const heading = document.createElement("h3");
    heading.className = "role";
    heading.id = headingId;
    heading.textContent = "Claude's plan — review";
    card.appendChild(heading);
    card.setAttribute("aria-labelledby", headingId);

    const body = document.createElement("div");
    body.className = "plan-body";
    body.innerHTML = renderMarkdown(req.plan || "");
    card.appendChild(body);

    const fbId = `plan-fb-${req.id || Math.random().toString(36).slice(2)}`;
    const fbLabel = document.createElement("label");
    fbLabel.setAttribute("for", fbId);
    fbLabel.textContent = "Feedback (used only if you keep planning):";
    const fb = document.createElement("textarea");
    fb.id = fbId;
    fb.className = "plan-feedback";
    fb.rows = 2;
    card.appendChild(fbLabel);
    card.appendChild(fb);

    const actions = document.createElement("div");
    actions.className = "permission-actions";
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "btn-primary";
    approve.textContent = "Approve & proceed";
    const keep = document.createElement("button");
    keep.type = "button";
    keep.className = "btn-secondary";
    keep.textContent = "Keep planning";
    actions.appendChild(approve);
    actions.appendChild(keep);
    card.appendChild(actions);
    transcript.appendChild(card);
    maybeAutoScroll(true);
    if (!ensurePermDialog()) approve.focus();

    async function send(decision, payload, resolvedText) {
      if (card.dataset.state !== "pending") return;
      card.dataset.state = "deciding";
      card.querySelectorAll("button, textarea").forEach((el) => (el.disabled = true));
      try {
        await postDecision(req.id, decision, payload);
        replaceCardWithSummary(card, resolvedText);
      } catch (err) {
        card.dataset.state = "pending";
        card.querySelectorAll("button, textarea").forEach((el) => (el.disabled = false));
        setStatus("Failed: " + err.message);
        announce("Failed to send your plan decision. Claude is still waiting — try again.");
      }
    }
    approve.addEventListener("click", () => send("allow", null, "Plan approved — proceeding"));
    keep.addEventListener("click", () => send("deny", { feedback: fb.value.trim() }, "Kept planning"));
  }

  // ─── Tasks panel ────────────────────────────────────────────────────────
  const todosPanel = document.getElementById("todos-panel");
  const todosList = document.getElementById("todos-list");

  const STATUS_LABELS = {
    pending: "To do",
    in_progress: "In progress",
    completed: "Done",
  };

  function updateTodosPanel(todos) {
    // The CLI uses the in_progress todo's activeForm as the spinner label.
    // Keep the spinner in sync with the panel from a single source.
    const inProgress = todos.find((t) => (t.status || "pending") === "in_progress");
    setActiveTodoLabel(inProgress ? (inProgress.activeForm || inProgress.content || "") : null);
    if (!todos.length) {
      todosPanel.hidden = true;
      todosList.innerHTML = "";
      return;
    }
    // Hide the panel once everything is done. The TodoWrite tool calls are
    // still visible as chips in the transcript for anyone who wants to scroll
    // back, so we lose nothing by collapsing the live panel.
    const allDone = todos.every((t) => (t.status || "pending") === "completed");
    if (allDone) {
      todosPanel.hidden = true;
      todosList.innerHTML = "";
      return;
    }
    todosPanel.hidden = false;
    todosList.innerHTML = "";
    for (const t of todos) {
      const li = document.createElement("li");
      const status = t.status || "pending";
      li.className = status;
      const label = status === "in_progress" && t.activeForm ? t.activeForm : (t.content || "");
      // Status conveyed in real text (not just CSS) so NVDA reads "Done:
      // Foo" / "In progress: Bar" / "To do: Baz" instead of the same flat
      // string for every item.
      const statusEl = document.createElement("span");
      statusEl.className = "task-status";
      statusEl.textContent = STATUS_LABELS[status] || status;
      const sep = document.createTextNode(": ");
      const labelEl = document.createElement("span");
      labelEl.className = "task-label";
      labelEl.textContent = label;
      li.append(statusEl, sep, labelEl);
      if (status === "in_progress") li.setAttribute("aria-current", "step");
      todosList.appendChild(li);
    }
  }

  // ─── Usage modal ────────────────────────────────────────────────────────
  const usageBtn = document.getElementById("show-usage");
  const usageDialog = document.getElementById("usage-dialog");
  const usageBody = document.getElementById("usage-body");

  usageBtn.addEventListener("click", async () => {
    usageBody.textContent = "Loading…";
    if (typeof usageDialog.showModal === "function") usageDialog.showModal();
    else usageDialog.setAttribute("open", "open");
    try {
      // Today's breakdown and the 30-day history come from two endpoints;
      // fetch them together. History is best-effort — a failure there still
      // shows Today rather than erroring the whole dialog.
      const [uR, hR] = await Promise.all([
        fetch("/api/usage"),
        fetch("/api/usage/history?days=30").catch(() => null),
      ]);
      if (!uR.ok) throw new Error("HTTP " + uR.status);
      renderUsage(await uR.json());
      if (hR && hR.ok) {
        try { renderUsageHistory(await hR.json()); } catch (_) { /* leave Today */ }
      }
    } catch (err) {
      usageBody.textContent = "Could not load usage: " + err.message;
    }
  });

  function renderUsage(data) {
    const t = data.today || {};
    const rl = data.rate_limit && data.rate_limit.info ? data.rate_limit.info : null;
    const showCost = !!t.has_billed_usage;

    const fmt = new Intl.NumberFormat();

    let html = "";

    if (rl) {
      const reset = rl.resetsAt ? new Date(rl.resetsAt * 1000) : null;
      const resetText = reset ? `${reset.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} (${humanIn(reset)})` : "—";
      html += `<div class="summary">
        <strong>Rate limit (${htmlEscape(rl.rateLimitType || "")})</strong><br>
        <span>Status: ${htmlEscape(rl.status || "—")}</span>
        <span>Resets: ${resetText}</span>
        ${rl.overageStatus ? `<span>Overage: ${htmlEscape(rl.overageStatus)}</span>` : ""}
      </div>`;
    } else {
      html += `<div class="summary"><em>No rate-limit info captured yet — send one message and reopen.</em></div>`;
    }

    html += `<h3 class="usage-section">Today</h3>`;
    const costCell = showCost ? `<span><strong>${formatCost(t.cost_usd)}</strong></span>` : "";
    const hitCell = t.cache_hit_pct == null
      ? ""
      : `<span title="Prompt input tokens served from the prompt cache. Higher is cheaper.">${formatPct(t.cache_hit_pct)} cache hit</span>`;
    html += `<div class="summary">
      <span><strong>${t.turns || 0}</strong> turns</span>
      ${costCell}
      <span>${fmt.format(t.input_tokens || 0)} in</span>
      <span>${fmt.format(t.output_tokens || 0)} out</span>
      ${hitCell}
    </div>`;
    if ((t.cache_read_input_tokens || t.cache_creation_input_tokens || 0) > 0) {
      html += `<div class="summary" aria-label="Prompt cache breakdown">
        <span title="Prompt tokens served from the cache at 0.1× cost.">${fmt.format(t.cache_read_input_tokens || 0)} cache read</span>
        <span title="Prompt tokens written to the cache (1.25× cost for 5-minute TTL, 2× for 1-hour TTL).">${fmt.format(t.cache_creation_input_tokens || 0)} cache write</span>
        <span title="Of the writes, this many requested the 5-minute TTL.">${fmt.format(t.cache_5m_input_tokens || 0)} @ 5m</span>
        <span title="Of the writes, this many requested the 1-hour TTL.">${fmt.format(t.cache_1h_input_tokens || 0)} @ 1h</span>
      </div>`;
    }

    if (t.sessions && t.sessions.length) {
      const costHeader = showCost ? "<th>Cost</th>" : "";
      html += `<table><thead><tr><th>Session</th><th>Turns</th>${costHeader}<th title="Prompt input tokens served from the prompt cache.">Cache hit</th></tr></thead><tbody>`;
      for (const s of t.sessions) {
        const costRow = showCost ? `<td>${s.billed_turns > 0 ? formatCost(s.cost_usd) : "—"}</td>` : "";
        const hitRow = `<td>${s.cache_hit_pct == null ? "—" : formatPct(s.cache_hit_pct)}</td>`;
        html += `<tr><td>${htmlEscape(s.title)}</td><td>${s.turns}</td>${costRow}${hitRow}</tr>`;
      }
      html += `</tbody></table>`;
    } else {
      html += `<p class="usage-empty">No turns recorded yet today.</p>`;
    }

    if (!showCost && (t.turns || 0) > 0) {
      html += `<p class="usage-note">Costs are hidden because today's turns ran on subscription credentials — Anthropic bills you the flat plan rate, not per-token. Switch to an API-key slot to see real per-turn costs.</p>`;
    }
    html += `<p class="usage-note">Anthropic doesn't expose plan-level capacity via API. "Today" is what this app has logged since it started.</p>`;
    usageBody.innerHTML = html;
  }

  // Append a per-day spend/token history table (from /api/usage/history) below
  // the Today view. An accessible <table> so a screen reader can navigate it by
  // row/column. Cost column shown only when the window has API-key (billed)
  // turns — subscription turns have synthetic per-turn costs.
  function renderUsageHistory(data) {
    const days = (data && data.days) || [];
    if (!days.length) return;
    const fmt = new Intl.NumberFormat();
    const totals = data.totals || {};
    const showCost = !!totals.has_billed_usage;
    let html = `<h3 class="usage-section">History — last ${data.window_days || 30} days</h3>`;
    const costHeader = showCost ? "<th>Cost</th>" : "";
    html += `<table><thead><tr><th>Date</th><th>Turns</th>${costHeader}<th>In</th><th>Out</th></tr></thead><tbody>`;
    for (const d of days.slice().reverse()) {  // newest first
      const costCell = showCost
        ? `<td>${d.billed_turns > 0 ? formatCost(d.cost_usd) : "—"}</td>` : "";
      html += `<tr><td>${htmlEscape(d.date)}</td><td>${d.turns}</td>${costCell}`
        + `<td>${fmt.format(d.input_tokens || 0)}</td><td>${fmt.format(d.output_tokens || 0)}</td></tr>`;
    }
    html += `</tbody></table>`;
    const totalCost = showCost ? ` · ${formatCost(totals.cost_usd || 0)} total` : "";
    html += `<p class="usage-note">${totals.turns || 0} turns over `
      + `${days.length} active day${days.length === 1 ? "" : "s"}${totalCost}.</p>`;
    usageBody.insertAdjacentHTML("beforeend", html);
  }

  function humanIn(date) {
    const diff = (date.getTime() - Date.now()) / 1000;
    if (diff < 0) return "passed";
    if (diff < 60) return "<1m";
    if (diff < 3600) return Math.floor(diff / 60) + "m";
    return Math.floor(diff / 3600) + "h " + Math.floor((diff % 3600) / 60) + "m";
  }

  function htmlEscape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function formatPct(frac) {
    const n = Number(frac);
    if (!Number.isFinite(n)) return "—";
    return (n * 100).toFixed(n >= 0.1 ? 0 : 1) + "%";
  }

  // Diffs longer than this default to collapsed. The threshold is set by
  // the keyboard / screen-reader cost of stepping through a long diff line
  // by line — past ~40 lines the body dominates the transcript and pushes
  // newer content off-screen for sighted users too. Tuned by feel; lower
  // it if 40 still feels noisy.
  const DIFF_COLLAPSE_THRESHOLD_LINES = 40;

  function insertDiffMessage(toolName, input) {
    // Edit/Write get a real diff block instead of the one-line tool chip,
    // so reviewing what Claude wrote doesn't require expanding tool output.
    const path = input.file_path || input.path || "";
    const wrap = document.createElement("article");
    wrap.className = "msg diff";

    // Build the diff body first (pre + diffLines) and decide whether the
    // total line count warrants default-collapsing.
    const pre = document.createElement("pre");
    pre.className = "diff-body";
    let lineCounts = "";
    let totalLines = 0;
    if (toolName === "Edit") {
      const oldS = input.old_string || "";
      const newS = input.new_string || "";
      const oldN = oldS.split("\n").length;
      const newN = newS.split("\n").length;
      lineCounts = `−${oldN} +${newN}`;
      totalLines = oldN + newN;
      pre.appendChild(diffLines(oldS, newS));
    } else {
      // Write: just preview the new content (no "old" side).
      const content = input.content || "";
      const max = 4000;
      const preview = content.length > max ? content.slice(0, max) + "\n… (" + (content.length - max) + " more chars)" : content;
      const lines = preview.split("\n");
      totalLines = lines.length;
      lineCounts = `+${totalLines}`;
      lines.forEach((line) => {
        const span = document.createElement("span");
        span.className = "diff-add";
        const sr = document.createElement("span");
        sr.className = "sr-only";
        sr.textContent = "Added line: ";
        span.appendChild(sr);
        span.appendChild(document.createTextNode("+ " + line));
        pre.appendChild(span);
        pre.appendChild(document.createTextNode("\n"));
      });
    }

    const headerText = `${toolName === "Edit" ? "✎ Edit" : "✎ Write"} ${path}  ·  ${lineCounts}`;
    if (totalLines > DIFF_COLLAPSE_THRESHOLD_LINES) {
      // Big diff: collapse by default. Native <details> handles the
      // keyboard / aria-expanded plumbing for free; clicking the summary
      // expands without javascript.
      const det = document.createElement("details");
      det.className = "diff-collapsible";
      const sum = document.createElement("summary");
      sum.className = "diff-header";
      sum.textContent = headerText;
      det.appendChild(sum);
      det.appendChild(pre);
      wrap.appendChild(det);
    } else {
      const header = document.createElement("div");
      header.className = "diff-header";
      header.textContent = headerText;
      wrap.appendChild(header);
      wrap.appendChild(pre);
    }

    transcript.appendChild(wrap);
    maybeAutoScroll();
  }

  // Tiny, dependency-free LCS-based line diff. Output is a fragment of
  // line spans (.diff-add / .diff-del / .diff-ctx) interleaved with newlines.
  function diffLines(a, b) {
    const A = (a || "").split("\n");
    const B = (b || "").split("\n");
    // LCS via dynamic programming. Cap inputs so a multi-megabyte string can't
    // freeze the tab — Edits are usually small but Write payloads can be huge.
    const cap = 400;
    const At = A.length > cap ? A.slice(0, cap) : A;
    const Bt = B.length > cap ? B.slice(0, cap) : B;
    const m = At.length, n = Bt.length;
    const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
    for (let i = m - 1; i >= 0; i--) {
      for (let j = n - 1; j >= 0; j--) {
        if (At[i] === Bt[j]) dp[i][j] = dp[i + 1][j + 1] + 1;
        else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const frag = document.createDocumentFragment();
    let i = 0, j = 0;
    function pushLine(cls, prefix, text) {
      const span = document.createElement("span");
      span.className = cls;
      // sr-only label so NVDA reads "Added line: foo" / "Removed line: bar"
      // instead of "plus space foo" (which depends on punctuation level).
      // Context lines stay unannotated — labelling every unchanged line would
      // drown out the actual change in long diffs.
      if (cls === "diff-add" || cls === "diff-del") {
        const sr = document.createElement("span");
        sr.className = "sr-only";
        sr.textContent = cls === "diff-add" ? "Added line: " : "Removed line: ";
        span.appendChild(sr);
      }
      span.appendChild(document.createTextNode(prefix + text));
      frag.appendChild(span);
      frag.appendChild(document.createTextNode("\n"));
    }
    while (i < m && j < n) {
      if (At[i] === Bt[j]) {
        pushLine("diff-ctx", "  ", At[i]);
        i++; j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        pushLine("diff-del", "- ", At[i]);
        i++;
      } else {
        pushLine("diff-add", "+ ", Bt[j]);
        j++;
      }
    }
    while (i < m) { pushLine("diff-del", "- ", At[i++]); }
    while (j < n) { pushLine("diff-add", "+ ", Bt[j++]); }
    if (A.length > cap || B.length > cap) {
      pushLine("diff-ctx", "  ", `… diff truncated at ${cap} lines`);
    }
    return frag;
  }

  function insertToolMessage(text, toolName) {
    // Group consecutive tool messages under a collapsible <details> so the
    // dance doesn't drown out Claude's actual response. The summary lists
    // distinct tool names so you can tell what's inside without expanding.
    let group = transcript.lastElementChild;
    if (!group || !group.classList || !group.classList.contains("tool-group")) {
      group = document.createElement("details");
      group.className = "tool-group";
      const summary = document.createElement("summary");
      summary.textContent = "Tools (0)";
      group.appendChild(summary);
      transcript.appendChild(group);
    }
    const el = document.createElement("div");
    el.className = "tool-line";
    el.textContent = text;
    group.appendChild(el);
    if (toolName) {
      const seen = (group.dataset.tools || "").split(",").filter(Boolean);
      if (!seen.includes(toolName)) {
        seen.push(toolName);
        group.dataset.tools = seen.join(",");
      }
    }
    const count = group.querySelectorAll(".tool-line").length;
    const tools = (group.dataset.tools || "").split(",").filter(Boolean);
    group.querySelector("summary").textContent = tools.length
      ? `Tools: ${tools.join(", ")} (${count})`
      : `Tools (${count})`;
    maybeAutoScroll();
  }

  function summariseToolInput(input) {
    if (input.command) return JSON.stringify(input.command).slice(0, 100);
    if (input.file_path) return input.file_path;
    if (input.path) return input.path;
    if (input.pattern) return input.pattern;
    return JSON.stringify(input).slice(0, 100);
  }

  function truncate(s, n) {
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  function renderSessionList(list, opts) {
    opts = opts || {};
    sessionList.innerHTML = "";
    for (const s of list) {
      const li = document.createElement("li");
      li.dataset.project = s.project || "";
      const a = document.createElement("a");
      const url = new URL(location.origin + "/");
      url.searchParams.set("session", s.id);
      if (s.project) url.searchParams.set("project", s.project);
      a.href = url.pathname + url.search;
      a.dataset.session = s.id;
      a.dataset.project = s.project || "";
      a.dataset.mtime = s.mtime;
      const title = document.createElement("span");
      title.className = "session-title";
      title.textContent = s.title;
      a.appendChild(title);
      if (opts.snippet && s.snippet) {
        const snip = document.createElement("span");
        snip.className = "session-snippet";
        snip.textContent = s.snippet;
        a.appendChild(snip);
      }
      const meta = document.createElement("span");
      meta.className = "session-meta";
      const time = document.createElement("time");
      time.className = "session-time";
      time.setAttribute("datetime", s.mtime);
      time.textContent = formatTime(s.mtime);
      meta.appendChild(time);
      if (s.project_path || s.project) {
        const proj = document.createElement("span");
        proj.className = "session-project";
        const path = s.project_path || (s.project || "").replace(/-/g, "/");
        proj.textContent = path.split("/").filter(Boolean).pop() || path;
        meta.appendChild(proj);
      }
      a.appendChild(meta);
      li.appendChild(a);
      const exp = document.createElement("button");
      exp.type = "button";
      exp.className = "session-export";
      exp.textContent = "↗";
      exp.title = "Copy as Markdown";
      exp.setAttribute("aria-label", `Copy session as Markdown: ${s.title}`);
      exp.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        exportSession(s.id, s.project || "", s.title);
      });
      li.appendChild(exp);
      const del = document.createElement("button");
      del.type = "button";
      del.className = "session-delete";
      del.textContent = "×";
      del.setAttribute("aria-label", `Delete session: ${s.title}`);
      del.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        deleteSession(s.id, s.project || "", s.title, li);
      });
      li.appendChild(del);
      sessionList.appendChild(li);
    }
    markActive(sessionId);
    updatePageTitle();
  }

  async function exportSession(id, project, title) {
    const url = new URL(`/api/sessions/${encodeURIComponent(id)}/export.md`, location.origin);
    if (project) url.searchParams.set("project", project);
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const md = await r.text();
      // Clipboard requires a secure context. Behind Cloudflare/HTTPS that's
      // fine; on plain http://localhost it's also allowed by browsers, but
      // an http://lan-ip deployment will fail — fall back to a download in
      // that case so the export still works.
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(md);
        announce(`Copied "${title}" as Markdown.`);
        setStatus(`Copied "${truncate(title, 40)}" to clipboard.`);
        return;
      }
      throw new Error("clipboard unavailable");
    } catch (err) {
      // Fallback: trigger a real download via a data URL so the user still
      // gets the markdown out even when clipboard write is blocked.
      try {
        const a = document.createElement("a");
        a.href = url.toString();
        a.download = `claude-session-${id.slice(0, 12)}.md`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        announce("Downloading session as Markdown.");
        setStatus("Downloaded.");
      } catch (err2) {
        setStatus("Export failed: " + (err.message || err2.message));
        announce("Export failed.");
      }
    }
  }

  async function refreshSessions() {
    if (searchActive) return;
    try {
      const r = await fetch("/api/sessions");
      if (!r.ok) return;
      const list = await r.json();
      renderSessionList(list);
      filterSessions();
    } catch { /* ignore */ }
  }

  async function deleteSession(id, project, title, li) {
    if (!confirm(`Delete session "${title}"?`)) return;
    try {
      const url = new URL(`/api/sessions/${encodeURIComponent(id)}`, location.origin);
      if (project) url.searchParams.set("project", project);
      const r = await fetch(url, { method: "DELETE" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      li.remove();
      announce("Session deleted.");
      if (sessionId === id) {
        sessionId = "";
        sessionProject = "";
        transcript.innerHTML = "";
        clearPermQueue();
        updateTodosPanel([]);
        history.replaceState({}, "", location.pathname);
        setStatus("Session deleted.");
        updatePageTitle();
      }
    } catch (err) {
      setStatus("Delete failed: " + err.message);
      announce("Delete failed.");
    }
  }

  // ─── Sidebar search: short queries filter titles locally; longer ones
  //     hit /api/sessions/search to grep across every session transcript.
  const sessionSearchEl = document.getElementById("session-search");
  let searchActive = false;
  let searchTimer = null;
  let searchToken = 0;

  function filterSessions() {
    const q = (sessionSearchEl?.value || "").toLowerCase();
    sessionList.querySelectorAll("li").forEach((li) => {
      const t = (li.querySelector(".session-title")?.textContent || "").toLowerCase();
      li.hidden = q.length > 0 && !t.includes(q);
    });
  }

  async function runTranscriptSearch(q) {
    const myToken = ++searchToken;
    const wasActive = searchActive;
    searchActive = true;
    if (searchModeEl) {
      searchModeEl.hidden = false;
      searchModeLabel.textContent = "Searching transcripts…";
    }
    if (!wasActive) announce("Searching transcripts.");
    try {
      const r = await fetch(`/api/sessions/search?q=${encodeURIComponent(q)}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (myToken !== searchToken) return;  // a newer query already fired
      renderSessionList(data.hits || [], { snippet: true });
      if (searchModeLabel) {
        const n = (data.hits || []).length;
        searchModeLabel.textContent = `${n} match${n === 1 ? "" : "es"} for "${q}"`;
        announce(`${n} match${n === 1 ? "" : "es"}.`);
      }
    } catch (err) {
      if (myToken !== searchToken) return;
      if (searchModeLabel) searchModeLabel.textContent = "Search failed: " + err.message;
      announce("Search failed.");
    }
  }

  function clearSearch() {
    if (sessionSearchEl) sessionSearchEl.value = "";
    if (searchModeEl) searchModeEl.hidden = true;
    searchActive = false;
    searchToken++;
    refreshSessions();
  }

  sessionSearchEl?.addEventListener("input", () => {
    const q = (sessionSearchEl.value || "").trim();
    if (searchTimer) clearTimeout(searchTimer);
    if (q.length < 2) {
      // Short query: revert to local title filter on the standard list.
      if (searchActive) {
        searchActive = false;
        if (searchModeEl) searchModeEl.hidden = true;
        announce("Filtering session titles.");
        refreshSessions();
      } else {
        filterSessions();
      }
      return;
    }
    // Two or more characters: debounce, then run a transcript search.
    searchTimer = setTimeout(() => runTranscriptSearch(q), 250);
  });

  searchClearBtn?.addEventListener("click", clearSearch);

  // ─── Image attachments ──────────────────────────────────────────────────
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const ALLOWED_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
  const MAX_IMAGES = 10;

  // Attachment epoch — bumped every time we clear pending attachments. An
  // in-flight async read (FileReader → data URL for pasted images) carries
  // the epoch it started under; on completion it only pushes if the epoch
  // still matches. Without this, a paste-then-Enter sequence finishes the
  // read AFTER clearAttachments and resurrects the attachment as a ghost
  // for the next message.
  let attachmentEpoch = 0;

  function clearAttachments() {
    attachmentEpoch++;
    pendingImages = [];
    pendingFiles = [];
    if (attachmentsEl) {
      attachmentsEl.innerHTML = "";
      attachmentsEl.hidden = true;
    }
    if (fileAttachmentsEl) {
      fileAttachmentsEl.innerHTML = "";
      fileAttachmentsEl.hidden = true;
    }
  }

  function attachmentSignature(file) {
    return `${file.name}:${file.size}:${file.lastModified || 0}:${file.type || ""}`;
  }

  // A File from the clipboard or some drag sources can have an empty .name.
  // Appended as-is, its multipart part carries filename="" and the server
  // decodes it as a string, not a file — which 422s the whole request
  // (message and valid files included). Always put a non-empty filename on
  // the wire; derive a sensible extension from the MIME type when we can.
  function sendName(file) {
    if (file && file.name) return file.name;
    const t = ((file && file.type) || "").toLowerCase();
    const ext = t.startsWith("text/") ? ".txt"
      : t === "application/json" ? ".json"
      : t === "application/pdf" ? ".pdf"
      : t.startsWith("image/") ? "." + t.slice(6).split(/[;+]/)[0]
      : "";
    return "attachment" + ext;
  }

  function renderAttachments() {
    if (!attachmentsEl) return;
    attachmentsEl.innerHTML = "";
    if (!pendingImages.length) {
      attachmentsEl.hidden = true;
      return;
    }
    attachmentsEl.hidden = false;
    pendingImages.forEach((entry, idx) => {
      const wrap = document.createElement("div");
      wrap.className = "attachment";
      const img = document.createElement("img");
      img.src = entry.dataUrl;
      img.alt = entry.file.name || "attached image";
      const meta = document.createElement("div");
      meta.className = "attachment-meta";
      meta.textContent = `${entry.file.name} (${Math.round(entry.file.size / 1024)} KB)`;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "attachment-delete";
      del.textContent = "×";
      del.setAttribute("aria-label", `Remove attachment ${entry.file.name}`);
      del.addEventListener("click", () => {
        pendingImages.splice(idx, 1);
        renderAttachments();
      });
      wrap.appendChild(img);
      wrap.appendChild(meta);
      wrap.appendChild(del);
      attachmentsEl.appendChild(wrap);
    });
  }

  function readAsDataURL(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error || new Error("read failed"));
      reader.onload = () => resolve(reader.result);
      reader.readAsDataURL(file);
    });
  }

  async function addImageFile(file) {
    if (!file) return;
    if (!ALLOWED_IMAGE_TYPES.has(file.type)) {
      setStatus(`Skipped ${file.name || "image"}: unsupported type`);
      announce("Image type not allowed.");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setStatus(`Skipped ${file.name}: larger than 10 MB`);
      announce("Image too large.");
      return;
    }
    if (pendingImages.length >= MAX_IMAGES) {
      setStatus(`At most ${MAX_IMAGES} images per message`);
      announce(`At most ${MAX_IMAGES} images per message.`);
      return;
    }
    const sig = attachmentSignature(file);
    if (pendingImages.some((e) => attachmentSignature(e.file) === sig)) {
      announce(`${file.name} is already attached.`);
      return;
    }
    const epoch = attachmentEpoch;
    try {
      const dataUrl = await readAsDataURL(file);
      // The user might have submitted (clearing attachments) while the
      // FileReader was still working. Don't resurrect the attachment.
      if (epoch !== attachmentEpoch) return;
      pendingImages.push({ file, dataUrl });
      renderAttachments();
      announce(`Attached ${file.name}.`);
    } catch (err) {
      setStatus("Could not read image: " + err.message);
      announce("Could not read image: " + err.message);
    }
  }

  attachInput?.addEventListener("change", async (e) => {
    const files = Array.from(e.target.files || []);
    for (const f of files) {
      if ((f.type || "").startsWith("image/")) {
        await addImageFile(f);
      } else {
        await addFile(f);
      }
    }
    attachInput.value = "";  // allow picking the same file again later
  });

  // ─── Generic file attachments ──────────────────────────────────────────
  // Non-image uploads. The server saves these to disk and prepends a
  // [Attached files: …] block to the user's message so Claude can Read
  // them with its tools. We don't pre-load the bytes client-side — file
  // uploads can be much larger than images.
  const MAX_FILE_BYTES = 25 * 1024 * 1024;
  const MAX_FILES = 10;

  function fileIconFor(name) {
    const ext = (name || "").split(".").pop().toLowerCase();
    if (["pdf"].includes(ext)) return "📕";
    if (["zip", "tar", "gz", "tgz", "bz2", "xz", "7z"].includes(ext)) return "🗜️";
    if (["mp3", "wav", "flac", "ogg", "m4a", "opus"].includes(ext)) return "🎵";
    if (["mp4", "mkv", "mov", "webm", "avi"].includes(ext)) return "🎞️";
    if (["csv", "tsv", "xlsx", "xls"].includes(ext)) return "📊";
    if (["json", "yaml", "yml", "toml", "xml", "ini"].includes(ext)) return "🧾";
    if (["py", "js", "ts", "tsx", "jsx", "go", "rs", "c", "cpp", "h", "java", "rb", "swift", "sh", "html", "css"].includes(ext)) return "📜";
    return "📄";
  }

  function renderFileAttachments() {
    if (!fileAttachmentsEl) return;
    fileAttachmentsEl.innerHTML = "";
    if (!pendingFiles.length) {
      fileAttachmentsEl.hidden = true;
      return;
    }
    fileAttachmentsEl.hidden = false;
    pendingFiles.forEach((entry, idx) => {
      const wrap = document.createElement("div");
      wrap.className = "attachment";
      const icon = document.createElement("span");
      icon.className = "attachment-icon";
      icon.textContent = fileIconFor(entry.file.name);
      icon.setAttribute("aria-hidden", "true");
      const name = document.createElement("div");
      name.className = "attachment-name";
      name.textContent = entry.file.name;
      const meta = document.createElement("div");
      meta.className = "attachment-meta";
      meta.textContent = `${Math.round(entry.file.size / 1024)} KB`;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "attachment-delete";
      del.textContent = "×";
      del.setAttribute("aria-label", `Remove attachment ${entry.file.name}`);
      del.addEventListener("click", () => {
        pendingFiles.splice(idx, 1);
        renderFileAttachments();
      });
      wrap.appendChild(icon);
      wrap.appendChild(name);
      wrap.appendChild(meta);
      wrap.appendChild(del);
      fileAttachmentsEl.appendChild(wrap);
    });
  }

  async function addFile(file) {
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) {
      setStatus(`Skipped ${file.name}: larger than 25 MB`);
      announce("File too large.");
      return;
    }
    if (pendingFiles.length >= MAX_FILES) {
      setStatus(`At most ${MAX_FILES} files per message`);
      announce(`At most ${MAX_FILES} files per message.`);
      return;
    }
    const sig = attachmentSignature(file);
    if (pendingFiles.some((e) => attachmentSignature(e.file) === sig)) {
      announce(`${file.name} is already attached.`);
      return;
    }
    pendingFiles.push({ file });
    renderFileAttachments();
    announce(`Attached ${file.name}.`);
  }

  promptEl.addEventListener("paste", async (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    let consumed = false;
    for (const item of items) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile();
      if (!file) continue;
      consumed = true;
      if ((item.type || "").startsWith("image/")) {
        await addImageFile(file);
      } else {
        await addFile(file);
      }
    }
    if (consumed) e.preventDefault();
  });

  // Drop zone covers the whole prompt region so the user doesn't have to
  // aim. Visual feedback via a class on the <main>. dragenter / dragleave
  // fire on every child element, so naively toggling the class would
  // flicker off as the cursor passes over a button or chip. Counter the
  // events instead — only remove the class when the leave count returns
  // to zero (cursor truly outside the prompt region).
  const dropTarget = document.querySelector(".prompt-region") || document.body;
  let dragDepth = 0;
  dropTarget.addEventListener("dragenter", (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
    e.preventDefault();
    dragDepth++;
    dropTarget.classList.add("dragging");
  });
  dropTarget.addEventListener("dragover", (e) => {
    if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
    e.preventDefault();
  });
  dropTarget.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropTarget.classList.remove("dragging");
  });
  dropTarget.addEventListener("drop", async (e) => {
    dragDepth = 0;
    dropTarget.classList.remove("dragging");
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    for (const f of e.dataTransfer.files) {
      if ((f.type || "").startsWith("image/")) {
        await addImageFile(f);
      } else {
        await addFile(f);
      }
    }
  });

  // ─── Client-side slash commands ─────────────────────────────────────────
  // These are commands that map to UI actions or harness behaviour. The
  // CLI's stream-json mode doesn't intercept slash commands, so anything not
  // listed here falls through to the model as text (which still works for
  // skills like /security-review).
  const CLIENT_SLASH_COMMANDS = {
    clear: { description: "Start a new chat (alias: /new)", run: () => newChatBtn.click() },
    new: { description: "Start a new chat", run: () => newChatBtn.click() },
    cost: { description: "Open the usage dialog", run: () => document.getElementById("show-usage").click() },
    usage: { description: "Open the usage dialog", run: () => document.getElementById("show-usage").click() },
    stop: { description: "Stop the current turn", run: () => { if (!stopBtn.hidden) stopBtn.click(); } },
    help: { description: "Show what slash commands work in claude-web", run: () => showSlashHelp() },
    model: { description: "Switch model: /model <id> (e.g. /model claude-sonnet-4-6)", run: (arg) => switchModel(arg) },
    effort: { description: "Set effort for new turns: /effort low|medium|high|xhigh|max (no arg = default)", run: (arg) => switchEffort(arg) },
    fork: { description: "Branch this chat into a new session: /fork [first message] — the original stays intact", run: (arg) => forkChat(arg) },
    rewind: { description: "Undo file changes: /rewind [n] — restore files to before your nth-last message (default 1)", run: (arg) => rewindFiles(arg) },
  };

  function showSlashHelp() {
    const supported = Object.entries(CLIENT_SLASH_COMMANDS)
      .map(([name, def]) => `/${name} — ${def.description}`)
      .join("\n");
    const lines = [
      "Slash commands handled by claude-web:",
      supported,
      "",
      "Keyboard shortcuts:",
      "Alt+A — open the pending approval dialog from anywhere",
      "Alt+J — jump focus to the latest reply",
      "",
      "Anything else (e.g. /security-review, /init, /loop, /skill <name>) is",
      "sent to Claude as text. The model recognises the convention and runs",
      "the corresponding skill — but it's the model doing it, not the harness.",
    ];
    renderErrorBlock(lines.join("\n"), {
      heading: "Slash commands",
      summary: "Commands handled in the browser. Anything else falls through to the model.",
      cls: "info-block",
    });
    // Re-tag the just-appended article as info, not error.
    const article = transcript.lastElementChild;
    if (article && article.classList) {
      article.classList.remove("error");
      article.classList.add("info");
    }
  }

  function switchModel(arg) {
    if (!modelSelect) {
      announce("Model picker is not enabled in this UI.");
      return;
    }
    const target = (arg || "").trim();
    const opt = [...modelSelect.options].find((o) => o.value === target || o.text.toLowerCase() === target.toLowerCase());
    if (!opt) {
      const valid = [...modelSelect.options].map((o) => o.value || "(default)").join(", ");
      renderErrorBlock(`Unknown model "${target}". Try one of: ${valid}`);
      return;
    }
    modelSelect.value = opt.value;
    modelSelect.dispatchEvent(new Event("change"));
    announce(`Model set to ${opt.text}.`);
  }

  async function rewindFiles(arg) {
    if (!sessionId) {
      renderErrorBlock("No session yet — nothing to rewind.");
      return;
    }
    const back = Math.max(1, parseInt((arg || "1").trim(), 10) || 1);
    try {
      const fd = new FormData();
      fd.append("session_id", sessionId);
      fd.append("back", String(back));
      const r = await fetch("/api/chat/rewind", { method: "POST", body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        renderErrorBlock("Rewind failed: " + (j.detail || j.error || ("HTTP " + r.status)));
        return;
      }
      // The files_rewound SSE event renders the transcript line + announce
      // for every attached tab; nothing more to do here.
    } catch (err) {
      renderErrorBlock("Rewind failed: " + (err.message || err));
    }
  }

  function forkChat(arg) {
    if (!sessionId) {
      announce("Nothing to fork yet — this chat has no session.");
      return;
    }
    forkNextSend = true;
    const text = (arg || "").trim();
    if (text) {
      promptEl.value = text;
      form.requestSubmit();
    } else {
      announce("Fork armed: the next message starts a branched chat. This session stays intact.");
    }
  }

  function switchEffort(arg) {
    if (!effortSelect) {
      announce("Effort picker is not enabled in this UI.");
      return;
    }
    if (!effortSupported()) {
      renderErrorBlock("The selected model has no effort levels.");
      return;
    }
    const target = (arg || "").trim().toLowerCase();
    const opt = [...effortSelect.options].find((o) => o.value === target || (!target && !o.value));
    if (!opt) {
      const valid = [...effortSelect.options].map((o) => o.value || "(default)").join(", ");
      renderErrorBlock(`Unknown effort "${target}". Try one of: ${valid}`);
      return;
    }
    effortSelect.value = opt.value;
    effortSelect.dispatchEvent(new Event("change"));
    announce(`Effort set to ${opt.value || "default"}.`);
  }

  async function handleClientSlashCommand(text) {
    // Parse "/word [arg ...]". Returns true if we handled it locally.
    const m = /^\/([a-zA-Z0-9_-]+)(?:\s+(.*))?$/.exec(text.trim());
    if (!m) return false;
    const name = m[1].toLowerCase();
    const arg = m[2] || "";
    const def = CLIENT_SLASH_COMMANDS[name];
    if (!def) return false;
    try {
      await def.run(arg);
    } catch (err) {
      renderErrorBlock(`/${name} failed: ${err.message || err}`);
    }
    return true;
  }

  // ─── Slash command autocomplete ─────────────────────────────────────────
  let allCommands = [];
  let slashItems = [];
  let slashActive = -1;

  fetch("/api/commands").then((r) => r.ok ? r.json() : null).then((data) => {
    const fromServer = (data && Array.isArray(data.commands)) ? data.commands : [];
    // Prepend client-side commands (the ones that actually do something
    // mapped to UI actions). Mark them "client" so users can tell them
    // apart from text-only / model-handled ones.
    const clientEntries = Object.entries(CLIENT_SLASH_COMMANDS).map(([name, def]) => ({
      name,
      description: def.description,
      kind: "client",
    }));
    const seen = new Set(clientEntries.map((c) => c.name));
    // Re-tag server-side built-ins as "text" so the menu is honest about
    // them: in the SDK's stream-json mode they reach the model as plain
    // text, not as harness-invoked commands.
    const tagged = fromServer
      .filter((c) => !seen.has(c.name))
      .map((c) => ({ ...c, kind: c.kind === "builtin" ? "text" : c.kind }));
    allCommands = [...clientEntries, ...tagged];
  }).catch(() => { /* non-fatal; menu just stays empty */ });

  function hideSlashMenu() {
    if (!slashMenu) return;
    slashMenu.hidden = true;
    slashMenu.innerHTML = "";
    slashItems = [];
    slashActive = -1;
    if (promptEl) {
      promptEl.setAttribute("aria-expanded", "false");
      promptEl.removeAttribute("aria-activedescendant");
    }
  }

  function showSlashMenu(matches) {
    if (!slashMenu) return;
    slashMenu.innerHTML = "";
    slashItems = matches;
    slashActive = matches.length ? 0 : -1;
    matches.forEach((cmd, i) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "slash-item";
      // Stable id per option so aria-activedescendant on the textarea can
      // point screen readers to the highlighted choice. tabindex=-1 so Tab
      // doesn't dive into the menu — focus stays on the textarea and the
      // listbox is driven by aria-activedescendant.
      row.id = `slash-option-${i}`;
      row.dataset.index = String(i);
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");
      row.tabIndex = -1;
      const name = document.createElement("span");
      name.className = "slash-name";
      name.textContent = "/" + cmd.name;
      const kind = document.createElement("span");
      kind.className = "slash-kind";
      kind.textContent = cmd.kind;
      const desc = document.createElement("span");
      desc.className = "slash-desc";
      desc.textContent = cmd.description || "";
      row.appendChild(name);
      row.appendChild(kind);
      if (desc.textContent) row.appendChild(desc);
      row.addEventListener("mousedown", (ev) => {
        ev.preventDefault();  // keep textarea focus
        acceptSlash(i);
      });
      slashMenu.appendChild(row);
    });
    const open = matches.length > 0;
    slashMenu.hidden = !open;
    if (promptEl) {
      promptEl.setAttribute("aria-expanded", open ? "true" : "false");
    }
    updateSlashHighlight();
  }

  function updateSlashHighlight() {
    if (!slashMenu) return;
    [...slashMenu.children].forEach((el, i) => {
      const active = i === slashActive;
      el.classList.toggle("active", active);
      el.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (promptEl) {
      const activeEl = slashMenu.children[slashActive];
      if (activeEl && activeEl.id) {
        promptEl.setAttribute("aria-activedescendant", activeEl.id);
      } else {
        promptEl.removeAttribute("aria-activedescendant");
      }
    }
  }

  function acceptSlash(index) {
    if (index < 0 || index >= slashItems.length) return;
    const cmd = slashItems[index];
    promptEl.value = "/" + cmd.name + " ";
    hideSlashMenu();
    promptEl.focus();
    // Place cursor at end.
    promptEl.selectionStart = promptEl.selectionEnd = promptEl.value.length;
  }

  function maybeUpdateSlashMenu() {
    if (!slashMenu) return;
    const v = promptEl.value;
    // Only suggest while the textarea matches /^\/[a-z0-9_-]*$/ — once the
    // user types past the command name we get out of the way.
    const m = /^\/([a-zA-Z0-9_-]*)$/.exec(v);
    if (!m) {
      hideSlashMenu();
      return;
    }
    const q = m[1].toLowerCase();
    const matches = allCommands
      .filter((c) => c.name.toLowerCase().startsWith(q))
      .slice(0, 8);
    if (!matches.length) hideSlashMenu();
    else showSlashMenu(matches);
  }

  promptEl.addEventListener("input", maybeUpdateSlashMenu);
  promptEl.addEventListener("blur", () => setTimeout(hideSlashMenu, 150));

  // Chrome's intensive throttling / tab freezing pauses the SSE fetch
  // reader on backgrounded tabs after a few minutes, and mobile browsers
  // suspend the page outright on screen lock / app switch. If the
  // connection dies during the freeze (cloudflared/Traefik idle timeout
  // while pings can't be drained) the UI stays stuck on stale state until
  // the user manually pokes the page. On return, if we have an in-flight
  // run and the network has been quiet long enough that the server's 25s
  // pings clearly haven't been arriving (or `force`, when the socket is
  // known-dead), abort and rejoin so the transcript catches up from the
  // event store.
  const VISIBILITY_STALE_MS = 35_000;
  function rejoinAfterFreeze(force) {
    if (!isStreaming || !currentRunId) return;
    if (!force && Date.now() - lastNetworkActivityAt < VISIBILITY_STALE_MS) return;
    const rid = currentRunId;
    announce("Reconnecting to running task.");
    // Keep this run's watermark so tryResume resumes incrementally from
    // watermark+1 — the transcript DOM survives a tab freeze.
    // Bump streamGeneration BEFORE abort so the old sendOne/tryResume's
    // AbortError catch sees gen !== streamGeneration and skips the
    // "Stopped." setStatus/announce — otherwise NVDA gets two contradictory
    // announcements ("Stopped." then "Reconnecting to previous response.").
    streamGeneration++;
    if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
    // Same finally-guard trick as the _overflow handler: nulling
    // currentAbort makes the in-flight sendOne/tryResume skip its
    // RUN_KEY wipe so the recovery rejoin can find the run.
    currentAbort = null;
    currentRunId = null;
    scheduleStreamRecovery(rid);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    // The OS drops the screen wake lock whenever the page is hidden;
    // re-request it if a turn is still in flight.
    if (isStreaming) acquireWakeLock();
    rejoinAfterFreeze(false);
  });
  // bfcache restore (mobile Safari especially): the page comes back with
  // all JS state intact but every socket dead, and no error ever fires on
  // the frozen reader. Rejoin unconditionally.
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted) return;
    if (isStreaming) acquireWakeLock();
    rejoinAfterFreeze(true);
  });
})();
