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
  // Highest server-assigned `_idx` rendered per run. Events arrive in idx
  // order within any one SSE stream, so a monotonic guard is enough to
  // collapse duplicates that come from the same event reaching the DOM
  // twice (overlapping subscribers, replay-then-tail races, etc.).
  const renderedIdxByRun = new Map();
  const RUN_KEY = "claude-web.active-run";
  const MODEL_KEY = "claude-web.model";
  const PROJECT_KEY = "claude-web.project";

  // Pending image attachments for the next send. Each entry is {file, dataUrl}.
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
  const queueArea = document.getElementById("queue-area");

  // Stream stall watchdog. Two clocks:
  //   lastNetworkActivityAt — every byte read from the SSE socket, including
  //     `: ping` heartbeat comments. Detects dead TCP connections.
  //   lastVisibleActivityAt — only updated for renderable events (assistant
  //     text, tool use/result, task progress, etc.). Detects a hung CLI that
  //     keeps emitting heartbeats but isn't actually working.
  // The watchdog gates on lastVisibleActivityAt so a backend that stops
  // producing real events trips the timeout even while pings keep flowing.
  const STREAM_STALL_MS = 4 * 60 * 1000;
  const STREAM_STALL_CHECK_MS = 15 * 1000;
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
  const MODEL_CONTEXT = (() => {
    const out = {};
    let data = [];
    const dataEl = document.getElementById("models-data");
    if (dataEl) {
      try { data = JSON.parse(dataEl.textContent); } catch (_) { data = []; }
    }
    for (const m of data) {
      if (m.key && m.context) out[m.key] = m.context;
    }
    return out;
  })();
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
  function announce(text) {
    if (!announcer) return;
    // NVDA needs a real gap between clearing and re-filling, otherwise the
    // mutation gets coalesced and nothing speaks. 10ms wasn't enough; 120ms
    // is reliable in NVDA + Chrome/Firefox without feeling laggy.
    announcer.textContent = "";
    setTimeout(() => { announcer.textContent = text; }, 120);
  }

  // Format unix timestamp as a short human-friendly relative/absolute string.
  function formatTime(unixSec) {
    const ms = unixSec * 1000;
    const diff = (Date.now() - ms) / 1000;
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

  const params = new URLSearchParams(location.search);
  let sessionId = params.get("session") || "";
  let sessionProject = params.get("project") || "";

  // Restore model + project from localStorage so the picks persist across reloads.
  if (modelSelect) {
    const savedModel = localStorage.getItem(MODEL_KEY);
    if (savedModel !== null && [...modelSelect.options].some((o) => o.value === savedModel)) {
      modelSelect.value = savedModel;
    }
    modelSelect.addEventListener("change", () => {
      localStorage.setItem(MODEL_KEY, modelSelect.value);
      lastSeenModel = modelSelect.value || lastSeenModel;
      renderContextMeter();
    });
  }
  if (projectSelect) {
    const savedProject = localStorage.getItem(PROJECT_KEY);
    if (savedProject !== null && [...projectSelect.options].some((o) => o.value === savedProject)) {
      projectSelect.value = savedProject;
    }
    if (sessionProject && [...projectSelect.options].some((o) => o.value === sessionProject)) {
      projectSelect.value = sessionProject;
    }
    projectSelect.addEventListener("change", () => {
      localStorage.setItem(PROJECT_KEY, projectSelect.value);
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
    const resumed = await tryResume();
    if (resumed) return;
    if (sessionId) {
      try {
        await loadSession(sessionId, sessionProject);
      } catch (err) {
        setStatus("Could not load session: " + err.message);
      }
      markActive(sessionId);
    }
    renderContextMeter();
  })();

  async function refreshHeaderCost() {
    if (!headerCostEl) return;
    try {
      const r = await fetch("/api/usage");
      if (!r.ok) return;
      const data = await r.json();
      const cost = (data.today && data.today.cost_usd) || 0;
      headerCostEl.textContent = "$" + cost.toFixed(4);
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
  const saved = localStorage.getItem(SIDEBAR_KEY);
  applySidebar(saved === "1" || (saved === null && window.matchMedia("(max-width: 720px)").matches));
  toggleBtn.addEventListener("click", () => {
    const willCollapse = !document.body.classList.contains("sidebar-collapsed");
    applySidebar(willCollapse);
    localStorage.setItem(SIDEBAR_KEY, willCollapse ? "1" : "0");
  });

  // Enter to send, Shift+Enter for newline. Skip when the Send button is
  // disabled so we don't fire a second /api/chat over a still-streaming one.
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
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        acceptSlash(slashActive);
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
    const url = new URL(`/api/sessions/${id}`, location.origin);
    if (project) url.searchParams.set("project", project);
    const r = await fetch(url);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    sessionProject = data.project || sessionProject || "";
    transcript.innerHTML = "";
    // Switching sessions: any prior run's dedup state belongs to a different
    // conversation now and shouldn't influence rendering of the new one.
    renderedIdxByRun.clear();
    for (const m of data.messages) {
      if (m.role === "user") {
        const body = appendMessage("user", m.text || "");
        if (m.image_count) appendImagePlaceholder(body, m.image_count);
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
    updatePageTitle();
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
      return window.DOMPurify.sanitize(window.marked.parse(text || ""));
    }
    const div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
  }

  function appendMessage(role, text) {
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
      b.dataset.raw = text || "";
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
    // is the right default for both.
    maybeAutoScroll();
    return b;
  }

  function makeCopyButton(bodyEl) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "msg-copy";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy reply to clipboard");
    btn.addEventListener("click", async () => {
      const raw = bodyEl.dataset.raw || bodyEl.textContent || "";
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
  // Base list mined from the Claude Code 2.1.126 binary (current latest mined
  // version, all 187 entries present); locally appended entries are tagged
  // "(local)" in this file.
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
  // Visual cycle stays fast (3.5s) so the gerund actually feels alive when
  // it does appear. Speech is paced wider (~12s) so NVDA isn't cut off
  // mid-word. Sighted users already have streaming text + tool lines as
  // "still working" feedback; we only show the gerund when the rest of
  // the UI has been quiet for IDLE_MS.
  const GERUND_VISUAL_MS = 3500;
  const GERUND_SPEAK_MS = 12000;
  const GERUND_IDLE_MS = 3000;

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
    lastVisibleActivityAt = 0;  // start from "idle" so the gerund shows right away
    function visualTick() {
      // When a todo is in_progress, the binary uses its activeForm as the
      // spinner label (no idle gate, no random pick). Match that.
      if (currentActiveTodo) {
        setStatus("✻ " + currentActiveTodo + "…");
        return;
      }
      const idleMs = Date.now() - lastVisibleActivityAt;
      if (idleMs < GERUND_IDLE_MS) return;  // sighted users have other feedback
      let i;
      do { i = Math.floor(Math.random() * GERUNDS.length); } while (i === last);
      last = i;
      currentGerund = GERUNDS[i];
      setStatus("✻ " + currentGerund + "…");
    }
    visualTick();
    gerundTimer = setInterval(visualTick, GERUND_VISUAL_MS);
    // Speech keeps firing regardless of visual activity — for NVDA users,
    // streaming text isn't auto-spoken, so the gerund heartbeat is still
    // their only "still working" cue.
    gerundSpeakTimer = setInterval(() => announce((currentActiveTodo || currentGerund) + "…"), GERUND_SPEAK_MS);
  }
  function stopGerunds() {
    if (gerundTimer) { clearInterval(gerundTimer); gerundTimer = null; }
    if (gerundSpeakTimer) { clearInterval(gerundSpeakTimer); gerundSpeakTimer = null; }
    // Drop any active todo label so the next run doesn't open with a stale
    // "still working on the last task" spinner.
    currentActiveTodo = null;
    setStatus("");
  }

  // Wire TodoWrite's "in_progress" activeForm to the spinner label, matching
  // the CLI binary (whose activeForm schema literally says "shown in spinner
  // when in_progress"). The Tasks panel still shows the full list separately.
  function setActiveTodoLabel(label) {
    const prev = currentActiveTodo;
    currentActiveTodo = label || null;
    if (currentActiveTodo) {
      if (gerundTimer) setStatus("✻ " + currentActiveTodo + "…");
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
    sessionStorage.removeItem(RUN_KEY);
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

  function setStreaming(on) {
    // "on" means: a turn is currently in progress (between submit/auto-fire
    // and the next ResultMessage). The SSE may stay open across multiple
    // turns; this state toggles back and forth as result/auto_fire events
    // arrive.
    isStreaming = on;
    if (on) {
      streamStartedAt = Date.now();
      startStallWatchdog();
    } else {
      stopStallWatchdog();
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
      announce("Stream looks stalled. Cancelling.");
      setStatus("Stream looks stalled — send a new message to start a fresh run.");
      if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
      currentAbort = null;
      currentRunId = null;
      sessionStorage.removeItem(RUN_KEY);
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

  function renderQueue() {
    if (!queueArea) return;
    queueArea.innerHTML = "";
    if (!messageQueue.length) {
      queueArea.hidden = true;
      return;
    }
    queueArea.hidden = false;
    const heading = document.createElement("span");
    heading.className = "queue-heading";
    heading.textContent = `${messageQueue.length} queued`;
    queueArea.appendChild(heading);
    messageQueue.forEach((entry) => {
      const chip = document.createElement("span");
      chip.className = "queue-chip";
      const label = document.createElement("span");
      label.className = "queue-text";
      const previewText = queuePreview(entry);
      label.textContent = previewText.length > 60 ? previewText.slice(0, 60) + "…" : previewText;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "queue-cancel";
      del.textContent = "×";
      del.setAttribute("aria-label", `Cancel queued message: ${previewText}`);
      // Identify the entry by reference so a concurrent shift() (when a
      // turn ends mid-render) doesn't make us splice the wrong index.
      del.addEventListener("click", () => {
        const idx = messageQueue.indexOf(entry);
        if (idx === -1) return;  // already drained, nothing to cancel
        messageQueue.splice(idx, 1);
        renderQueue();
        announce("Queued message cancelled.");
      });
      chip.appendChild(label);
      chip.appendChild(del);
      queueArea.appendChild(chip);
    });
  }

  stopBtn.addEventListener("click", async () => {
    // Tell the server to cancel the SDK task. The fetch will then end on
    // its own when the run finishes; abort is fallback insurance. Also
    // empty the outgoing queue — Stop means stop, not "stop just this one".
    if (messageQueue.length) {
      messageQueue.length = 0;
      renderQueue();
    }
    if (currentRunId) {
      try {
        await fetch(`/api/chat/stop/${encodeURIComponent(currentRunId)}`, { method: "POST" });
      } catch { /* fall through to abort */ }
    }
    if (currentAbort) currentAbort.abort();
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
    const entry = { text, images: pendingImages.slice(), files: pendingFiles.slice() };
    promptEl.value = "";
    clearAttachments();
    if (isStreaming) {
      if (streamLooksStalled()) {
        // No SSE events in STREAM_STALL_MS — assume the stream is dead
        // client-side and recover by sending as a fresh run rather than
        // queueing into a queue that will never drain.
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        sessionStorage.removeItem(RUN_KEY);
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
      return true;
    }
    setStreaming(true);
    startGerunds();
    announce("Sent. Claude is responding.");
    try {
      const fd = new FormData();
      fd.append("message", entry.text || "");
      for (const img of entry.images) {
        fd.append("images", img.file, img.file.name);
      }
      for (const f of (entry.files || [])) {
        fd.append("files", f.file, f.file.name);
      }
      const r = await fetch(`/api/chat/send/${encodeURIComponent(currentRunId)}`, { method: "POST", body: fd });
      if (r.status === 401 || r.status === 403) {
        // Auth expired or CSRF rejected — neither is recoverable by retrying
        // against the same run. Surface to handleStreamError so the user is
        // bounced through the IdP, and keep the entry in the queue so the
        // post-login reload can flush it.
        handleStreamError(new Error("HTTP " + r.status));
        setStreaming(false);
        return false;
      }
      if (!r.ok) {
        // Any other non-2xx — 404 (run gone), 5xx (driver crashed), network
        // blip through the proxy — means the existing stream can't carry
        // this message. Abort the old SSE reader so its events don't bleed
        // into the new run's transcript, then open a fresh one.
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        sessionStorage.removeItem(RUN_KEY);
        if (r.status !== 404) {
          setStatus(`Send failed (HTTP ${r.status}) — starting a new run.`);
        }
        await sendOne(entry);
        return true;
      }
      // The existing SSE stream will deliver the new turn's events; nothing
      // else to do here. setStreaming(false) happens on the next result.
      return true;
    } catch (err) {
      // Network failure (fetch threw). Abort the dead stream, drop the
      // run handle, surface the error. Caller decides whether to retry.
      if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
      currentAbort = null;
      currentRunId = null;
      sessionStorage.removeItem(RUN_KEY);
      handleStreamError(err);
      setStreaming(false);
      return false;
    }
  }

  async function drainQueueIfPossible() {
    if (!messageQueue.length || !currentRunId || isStreaming) return;
    // Peek-then-shift: leave the entry in the queue until the server has
    // acknowledged it, so a network blip doesn't silently drop the message
    // (and any attachments). The next drainQueueIfPossible call will retry
    // the same entry; the user can also remove it manually via its
    // queue-cancel button.
    const entry = messageQueue[0];
    announce("Sending next queued message.");
    let ok = false;
    try {
      ok = await sendInExistingRun(entry);
    } catch (err) {
      handleStreamError(err);
      ok = false;
    }
    if (ok && messageQueue[0] === entry) {
      messageQueue.shift();
      renderQueue();
    }
  }

  async function sendOne(entry) {
    setStreaming(true);
    // Capture our own abort/signal so the finally block can tell whether
    // it's still the active turn. The stall-recovery path aborts an old
    // sendOne and immediately starts a new one — without this guard the
    // old finally would run after the new sendOne had already set
    // currentAbort/isStreaming, clobbering them mid-flight.
    const myAbort = new AbortController();
    currentAbort = myAbort;
    startGerunds();
    announce("Sent. Claude is responding.");
    try {
      const fd = new FormData();
      fd.append("message", entry.text || "");
      if (sessionId) fd.append("session_id", sessionId);
      const project = currentProject();
      if (project) fd.append("project", project);
      if (modelSelect && modelSelect.value) fd.append("model", modelSelect.value);
      for (const img of entry.images) {
        fd.append("images", img.file, img.file.name);
      }
      for (const f of (entry.files || [])) {
        fd.append("files", f.file, f.file.name);
      }
      const r = await fetch("/api/chat", { method: "POST", body: fd, signal: myAbort.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      await drainStream(r);
    } catch (err) {
      handleStreamError(err);
    } finally {
      // Only clean up if we're still the current turn. If a stall-recovery
      // submit started a fresh sendOne while we were aborting, leave its
      // state alone.
      if (currentAbort === myAbort) {
        currentAbort = null;
        currentRunId = null;
        sessionStorage.removeItem(RUN_KEY);
        setStreaming(false);
        promptEl.focus();
      }
    }
  }

  async function drainQueue() {
    while (messageQueue.length) {
      const next = messageQueue.shift();
      renderQueue();
      announce("Sending next queued message.");
      await sendOne(next);
    }
  }

  async function drainStream(response) {
    // Mutable holder so handleSSEEvent can lazy-create a new assistant
    // article each time text follows a tool call — keeps DOM order matching
    // chronological order.
    const ctx = { currentAssistantBody: null };
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
      lastNetworkActivityAt = Date.now();
      buf += dec.decode(value, { stream: true });
      let idx;
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
    stopGerunds();
    const summary = summariseResult(ctx.lastResult);
    setStatus(summary);
    announce(summary);
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
    setStatus("Error: " + err.message);
    announce("Error: " + err.message);
  }

  async function tryResume() {
    const savedRunId = sessionStorage.getItem(RUN_KEY);
    if (!savedRunId) return false;
    let info;
    try {
      const r = await fetch(`/api/chat/active?run_id=${encodeURIComponent(savedRunId)}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      info = await r.json();
    } catch {
      sessionStorage.removeItem(RUN_KEY);
      return false;
    }
    if (!info.active && !info.buffered_events) {
      sessionStorage.removeItem(RUN_KEY);
      return false;
    }
    if (info.project) sessionProject = info.project;
    // Wipe and announce BEFORE flipping streaming UI on, so there's never a
    // frame where the spinner is overlaid on the stale transcript.
    transcript.innerHTML = "";
    currentRunId = savedRunId;
    currentAbort = new AbortController();
    setStreaming(true);
    startGerunds();
    announce("Reconnecting to previous response.");
    try {
      const r = await fetch(`/api/chat/stream/${encodeURIComponent(savedRunId)}`, { signal: currentAbort.signal });
      if (!r.ok) throw new Error("HTTP " + r.status);
      await drainStream(r);
    } catch (err) {
      handleStreamError(err);
    } finally {
      currentAbort = null;
      currentRunId = null;
      sessionStorage.removeItem(RUN_KEY);
      setStreaming(false);
      promptEl.focus();
    }
    return true;
  }

  function handleSSEEvent(evt, ctx) {
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
    try { obj = JSON.parse(dataLine); } catch { return; }

    // Drop events the DOM has already rendered for this run. Same _idx
    // arriving twice means a duplicate delivery (overlapping SSE subscribers
    // or an out-of-band replay) — render once, ignore the rest.
    if (typeof obj._idx === "number") {
      const runKey = (obj.type === "run_started" && obj.run_id) ? obj.run_id : currentRunId;
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
        sessionStorage.setItem(RUN_KEY, obj.run_id);
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
    } else if (obj.type === "stopped") {
      setActiveTodoLabel(null);
      setStatus("Stopped.");
      announce("Stopped.");
    } else if (obj.type === "restarted_during_run") {
      // The server was restarted while a previous turn was running. The
      // SDK subprocess is gone, but the conversation jsonl on disk and our
      // session_id are intact — sending a new message will resume cleanly.
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
      // first chunk has session_id — record it for resume
      if (obj.session_id && !sessionId) {
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
    } else if (obj.type === "assistant" && obj.message) {
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
          const raw = (ctx.currentAssistantBody.dataset.raw || "") + blk.text;
          ctx.currentAssistantBody.dataset.raw = raw;
          ctx.currentAssistantBody.innerHTML = renderMarkdown(raw);
          maybeAutoScroll();
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
          markVisibleActivity();
        }
      }
    } else if (obj.type === "permission_request") {
      ctx.currentAssistantBody = null;
      announce(`Permission needed for ${obj.tool}.`);
      renderPermissionCard(obj);
    } else if (obj.type === "_overflow") {
      // Backend dropped us as a slow subscriber — fetch a fresh stream from
      // the start so we don't miss anything. tryResume's reconnect path
      // will replay the entire run from index 0 via the persisted store.
      if (currentRunId) {
        announce("Stream backlog overflowed; reconnecting from start.");
        const rid = currentRunId;
        if (currentAbort) { try { currentAbort.abort(); } catch (_) {} }
        currentAbort = null;
        currentRunId = null;
        sessionStorage.removeItem(RUN_KEY);
        sessionStorage.setItem(RUN_KEY, rid);
        // Schedule the resume on the next microtask so the in-flight reader
        // can unwind cleanly before we open a new fetch.
        setTimeout(() => { tryResume().catch(() => {}); }, 0);
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
      // CSS.escape defensively in case the id format ever changes from
      // dash-hex UUID to something with special characters.
      const safeId = (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape(obj.id) : obj.id;
      const card = document.querySelector(`article.msg.permission[data-request-id="${safeId}"]`);
      if (card) {
        card.querySelectorAll("button").forEach((b) => (b.disabled = true));
        const note = document.createElement("p");
        note.className = "permission-timeout-note";
        note.textContent = restarted
          ? "Server restarted before this was answered — the request is gone. Send a new message to continue."
          : `Timed out after ${obj.timeout_seconds || "?"}s — Claude was told the request was denied.`;
        card.appendChild(note);
      }
      announce(restarted
        ? `Permission request for ${obj.tool || "tool"} discarded due to server restart.`
        : `Permission request for ${obj.tool || "tool"} timed out.`);
    } else if (obj.type === "todos_update") {
      updateTodosPanel(obj.todos || []);
    } else if (obj.type === "task_started") {
      renderTaskEvent("started", obj);
    } else if (obj.type === "task_progress") {
      renderTaskEvent("progress", obj);
    } else if (obj.type === "task_notification") {
      renderTaskEvent("notification", obj);
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
    } else if (obj.type === "result") {
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
      const summary = summariseResult(obj);
      setStatus(summary);
      announce(summary);
      refreshSessions();
      refreshHeaderCost();
      setStreaming(false);
      // Drain any client-side queued messages — the user submitted them
      // mid-turn and we promised we'd flush after the turn ends.
      drainQueueIfPossible();
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
        renderErrorBlock(detail, { summary });
        announce("Error: " + (obj.result || ""));
      }
    } else if (obj.type === "error") {
      // Driver crashed mid-turn. The server will emit `_done` and close
      // the SSE shortly, but flip the local state now so the input isn't
      // trapped in "Queue" mode while we wait for the close to land.
      setActiveTodoLabel(null);
      stopGerunds();
      setStreaming(false);
      const summary = obj.message || obj.exit_code || "see technical details";
      const detail = obj.stderr ? `${obj.message || "Error"}\n${obj.stderr}` : null;
      setStatus("Error: " + summary);
      renderErrorBlock(detail ? String(detail) : null, { summary });
      announce("Error: " + (obj.message || ""));
    }
  }

  function renderTaskEvent(kind, obj) {
    // Group all events for the same task_id under one collapsible block so
    // a chatty Monitor doesn't flood the transcript. The block updates in
    // place as later events arrive.
    const id = obj.task_id || "?";
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
    } else if (kind === "notification") {
      const status = obj.status || "done";
      const icon = status === "success" ? "✓" : status === "error" ? "✗" : "●";
      summary.textContent = `${icon} ${obj.description || obj.summary || "task " + id} (${status})`;
      block.classList.add("task-" + status);
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
      contextFill.style.background = pct > 85 ? "#d96868" : pct > 70 ? "#d8a657" : "var(--accent)";
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
    if (typeof r.total_cost_usd === "number") parts.push("$" + r.total_cost_usd.toFixed(4));
    if (r.is_error) parts.unshift("Error");
    return parts.join(" · ");
  }

  function renderPermissionCard(req) {
    const card = document.createElement("article");
    card.className = "msg permission";
    card.setAttribute("role", "alertdialog");
    card.setAttribute("aria-modal", "false"); // inline, not a screen-blocking modal
    if (req.id) card.dataset.requestId = req.id;

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

    // Focus the safest button by default for high-risk tools.
    const focusBtn = isHighRisk ? actions.querySelector(".btn-danger") : actions.querySelector(".btn-primary");
    if (focusBtn) focusBtn.focus();

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

  async function decide(requestId, decision, card) {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const fd = new FormData();
      fd.append("decision", decision);
      const r = await fetch(`/api/permission/${requestId}`, { method: "POST", body: fd });
      if (!r.ok) throw new Error("HTTP " + r.status);
      // Replace card with a compact record of the decision.
      const summary = document.createElement("article");
      summary.className = "msg permission-resolved";
      const labels = { allow: "Allowed", allow_session: "Allowed (session)", deny: "Denied" };
      summary.textContent = `${labels[decision] || decision}: ${card.querySelector(".permission-input").textContent.split("\n")[0]}`;
      card.replaceWith(summary);
    } catch (err) {
      card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      setStatus("Failed to send decision: " + err.message);
    }
  }

  function formatToolInput(tool, input) {
    if (tool === "Bash" && input.command) return "$ " + input.command;
    if (tool === "Edit" || tool === "Write" || tool === "Read") {
      return (input.file_path || input.path || "") + (input.old_string ? "\n--- replace ---\n" + truncate(input.old_string, 200) : "");
    }
    return JSON.stringify(input, null, 2);
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
      const r = await fetch("/api/usage");
      if (!r.ok) throw new Error("HTTP " + r.status);
      renderUsage(await r.json());
    } catch (err) {
      usageBody.textContent = "Could not load usage: " + err.message;
    }
  });

  function renderUsage(data) {
    const t = data.today || {};
    const rl = data.rate_limit && data.rate_limit.info ? data.rate_limit.info : null;

    const fmt = new Intl.NumberFormat();
    const cost = (n) => "$" + (n || 0).toFixed(4);

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

    html += `<h3 style="font-size:0.9rem;margin:0.5rem 0 0.25rem">Today</h3>`;
    html += `<div class="summary">
      <span><strong>${t.turns || 0}</strong> turns</span>
      <span><strong>${cost(t.cost_usd)}</strong></span>
      <span>${fmt.format(t.input_tokens || 0)} in</span>
      <span>${fmt.format(t.output_tokens || 0)} out</span>
    </div>`;

    if (t.sessions && t.sessions.length) {
      html += `<table><thead><tr><th>Session</th><th>Turns</th><th>Cost</th></tr></thead><tbody>`;
      for (const s of t.sessions) {
        html += `<tr><td>${htmlEscape(s.title)}</td><td>${s.turns}</td><td>${cost(s.cost_usd)}</td></tr>`;
      }
      html += `</tbody></table>`;
    } else {
      html += `<p style="color:#aaa;font-size:0.9rem">No turns recorded yet today.</p>`;
    }

    html += `<p style="color:#888;font-size:0.75rem;margin-top:0.75rem">Anthropic doesn't expose plan-level capacity via API. "Today" is what this app has logged since it started.</p>`;
    usageBody.innerHTML = html;
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

  function insertDiffMessage(toolName, input) {
    // Edit/Write get a real diff block instead of the one-line tool chip,
    // so reviewing what Claude wrote doesn't require expanding tool output.
    const path = input.file_path || input.path || "";
    const wrap = document.createElement("article");
    wrap.className = "msg diff";
    const header = document.createElement("div");
    header.className = "diff-header";
    header.textContent = (toolName === "Edit" ? "✎ Edit " : "✎ Write ") + path;
    wrap.appendChild(header);

    const pre = document.createElement("pre");
    pre.className = "diff-body";
    if (toolName === "Edit") {
      const oldS = input.old_string || "";
      const newS = input.new_string || "";
      pre.appendChild(diffLines(oldS, newS));
    } else {
      // Write: just preview the new content (no "old" side).
      const content = input.content || "";
      const max = 4000;
      const preview = content.length > max ? content.slice(0, max) + "\n… (" + (content.length - max) + " more chars)" : content;
      preview.split("\n").forEach((line) => {
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
    wrap.appendChild(pre);
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

  function clearAttachments() {
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
      return;
    }
    try {
      const dataUrl = await readAsDataURL(file);
      pendingImages.push({ file, dataUrl });
      renderAttachments();
      announce(`Attached ${file.name}.`);
    } catch (err) {
      setStatus("Could not read image: " + err.message);
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
  // aim. Visual feedback via a class on the <main>.
  const dropTarget = document.querySelector(".prompt-region") || document.body;
  ["dragenter", "dragover"].forEach((ev) => {
    dropTarget.addEventListener(ev, (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes("Files")) return;
      e.preventDefault();
      dropTarget.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    dropTarget.addEventListener(ev, () => dropTarget.classList.remove("dragging"));
  });
  dropTarget.addEventListener("drop", async (e) => {
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
  };

  function showSlashHelp() {
    const supported = Object.entries(CLIENT_SLASH_COMMANDS)
      .map(([name, def]) => `/${name} — ${def.description}`)
      .join("\n");
    const lines = [
      "Slash commands handled by claude-web:",
      supported,
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
      // point screen readers to the highlighted choice.
      row.id = `slash-option-${i}`;
      row.dataset.index = String(i);
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");
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
})();
