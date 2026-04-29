(() => {
  "use strict";

  const transcript = document.getElementById("transcript");
  const form = document.getElementById("chat-form");
  const promptEl = document.getElementById("prompt");
  const sendBtn = document.getElementById("send");
  const statusEl = document.getElementById("status");
  const announcer = document.getElementById("status-announcer");
  const newChatBtn = document.getElementById("new-chat");
  const sessionList = document.getElementById("session-list");

  // Quietly tell NVDA / VoiceOver something interesting happened. The
  // visible "Thinking…" text in #status is also live, but this region is
  // sr-only and used for milestone announcements (response complete,
  // permission needed) rather than every chunk.
  function announce(text) {
    if (!announcer) return;
    announcer.textContent = "";
    // Force a tick so the live-region change actually fires.
    setTimeout(() => { announcer.textContent = text; }, 10);
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

  const params = new URLSearchParams(location.search);
  let sessionId = params.get("session") || "";

  if (sessionId) {
    loadSession(sessionId).catch((err) => setStatus("Could not load session: " + err.message));
    markActive(sessionId);
  }

  // Always refresh the sidebar from /api/sessions on load — the server-rendered
  // list can be stale if the HTML was cached or another tab created sessions.
  refreshSessions();

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
  promptEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (sendBtn.disabled) return;
      form.requestSubmit();
    }
  });

  function markActive(id) {
    sessionList.querySelectorAll("li").forEach((li) => li.classList.remove("active"));
    if (!id) return;
    const link = sessionList.querySelector(`a[data-session="${id}"]`);
    if (link) link.parentElement.classList.add("active");
  }

  async function loadSession(id) {
    const r = await fetch(`/api/sessions/${id}`);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    transcript.innerHTML = "";
    for (const m of data.messages) {
      if (m.role === "user" || m.role === "assistant") {
        appendMessage(m.role, m.text);
      } else if (m.role === "tool_use") {
        insertToolMessage("→ " + m.name + (m.summary ? " " + m.summary : ""));
      } else if (m.role === "tool_result") {
        insertToolMessage((m.is_error ? "✗ " : "← ") + m.text);
      }
    }
    transcript.scrollTop = transcript.scrollHeight;
  }

  // marked is loaded globally from /static/marked.min.js
  if (window.marked && typeof window.marked.setOptions === "function") {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  function renderMarkdown(text) {
    if (window.marked && typeof window.marked.parse === "function") {
      return window.marked.parse(text || "");
    }
    // Fallback: textContent if marked failed to load.
    const div = document.createElement("div");
    div.textContent = text || "";
    return div.innerHTML;
  }

  function appendMessage(role, text) {
    const el = document.createElement("article");
    el.className = "msg " + role;
    // Heading per message so NVDA's H key cycles through them.
    const r = document.createElement("h3");
    r.className = "role";
    r.textContent = role === "user" ? "You" : role === "assistant" ? "Claude" : role;
    const b = document.createElement("div");
    b.className = "body";
    if (role === "assistant") {
      b.innerHTML = renderMarkdown(text);
      b.dataset.raw = text || "";
    } else {
      b.textContent = text;
    }
    el.appendChild(r);
    el.appendChild(b);
    transcript.appendChild(el);
    transcript.scrollTop = transcript.scrollHeight;
    return b;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  // Cosmetic gerund cycler — same vibe as the CLI's "✻ Pondering…" animation.
  // Visual only; the sr-only announcer is what actually talks to screen readers.
  // Verbatim list mined from the Claude Code 2.1.123 binary so the web UI
  // matches the CLI's TTY animation. Refresh on next major version bump.
  const GERUNDS = [
    'Accomplishing', 'Actioning', 'Actualizing', 'Architecting', 'Baking', 'Beaming', 'Befuddling', 'Billowing',
    'Blanching', 'Bloviating', 'Boogieing', 'Boondoggling', 'Booping', 'Bootstrapping', 'Brewing', 'Bunning',
    'Burrowing', 'Calculating', 'Canoodling', 'Caramelizing', 'Cascading', 'Catapulting', 'Cerebrating', 'Channeling',
    'Channelling', 'Choreographing', 'Churning', 'Clauding', 'Coalescing', 'Cogitating', 'Combobulating', 'Composing',
    'Computing', 'Concocting', 'Considering', 'Contemplating', 'Cooking', 'Crafting', 'Creating', 'Crunching',
    'Crystallizing', 'Cultivating', 'Deciphering', 'Deliberating', 'Determining', 'Discombobulating', 'Doing', 'Doodling',
    'Drizzling', 'Ebbing', 'Effecting', 'Elucidating', 'Embellishing', 'Enchanting', 'Envisioning', 'Evaporating',
    'Fermenting', 'Finagling', 'Flibbertigibbeting', 'Flowing', 'Flummoxing', 'Fluttering', 'Forging', 'Forming',
    'Frolicking', 'Frosting', 'Gallivanting', 'Galloping', 'Garnishing', 'Generating', 'Gesticulating', 'Germinating',
    'Gitifying', 'Grooving', 'Gusting', 'Harmonizing', 'Hashing', 'Hatching', 'Herding', 'Honking',
    'Hullaballooing', 'Hyperspacing', 'Ideating', 'Imagining', 'Improvising', 'Incubating', 'Inferring', 'Infusing',
    'Ionizing', 'Jitterbugging', 'Julienning', 'Kneading', 'Leavening', 'Levitating', 'Lollygagging', 'Manifesting',
    'Marinating', 'Meandering', 'Metamorphosing', 'Misting', 'Moonwalking', 'Moseying', 'Mulling', 'Mustering',
    'Musing', 'Nebulizing', 'Nesting', 'Newspapering', 'Noodling', 'Nucleating', 'Orbiting', 'Orchestrating',
    'Osmosing', 'Perambulating', 'Percolating', 'Perusing', 'Philosophising', 'Photosynthesizing', 'Pollinating', 'Pondering',
    'Pontificating', 'Pouncing', 'Precipitating', 'Prestidigitating', 'Processing', 'Proofing', 'Propagating', 'Puttering',
    'Puzzling', 'Quantumizing', 'Razzmatazzing', 'Recombobulating', 'Reticulating', 'Roosting', 'Ruminating', 'Scampering',
    'Schlepping', 'Scurrying', 'Seasoning', 'Shenaniganing', 'Shimmying', 'Simmering', 'Skedaddling', 'Sketching',
    'Slithering', 'Smooshing', 'Spelunking', 'Spinning', 'Sprouting', 'Stewing', 'Sublimating', 'Swirling',
    'Swooping', 'Symbioting', 'Synthesizing', 'Tempering', 'Thinking', 'Thundering', 'Tinkering', 'Tomfoolering',
    'Transfiguring', 'Transmuting', 'Twisting', 'Undulating', 'Unfurling', 'Unravelling', 'Vibing', 'Waddling',
    'Wandering', 'Warping', 'Whatchamacalliting', 'Whirlpooling', 'Whirring', 'Whisking', 'Wibbling', 'Working',
    'Wrangling', 'Zesting', 'Zigzagging',
  ];
  let gerundTimer = null;
  function startGerunds() {
    let last = -1;
    function tick() {
      let i;
      do { i = Math.floor(Math.random() * GERUNDS.length); } while (i === last);
      last = i;
      setStatus("✻ " + GERUNDS[i] + "…");
    }
    tick();
    gerundTimer = setInterval(tick, 3500);
  }
  function stopGerunds() {
    if (gerundTimer) { clearInterval(gerundTimer); gerundTimer = null; }
    setStatus("");
  }

  newChatBtn.addEventListener("click", () => {
    sessionId = "";
    transcript.innerHTML = "";
    history.replaceState({}, "", location.pathname);
    markActive("");
    setStatus("");
    promptEl.focus();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (sendBtn.disabled) return;  // belt-and-braces against double-submit
    const text = promptEl.value.trim();
    if (!text) return;
    appendMessage("user", text);
    promptEl.value = "";
    sendBtn.disabled = true;
    startGerunds();
    announce("Sent. Claude is responding.");

    // Mutable holder so handleSSEEvent can lazy-create a new assistant
    // article each time text follows a tool call — keeps DOM order matching
    // chronological order.
    const ctx = { currentAssistantBody: null };

    try {
      const fd = new FormData();
      fd.append("message", text);
      if (sessionId) fd.append("session_id", sessionId);

      const r = await fetch("/api/chat", { method: "POST", body: fd });
      if (!r.ok) throw new Error("HTTP " + r.status);

      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        // Parse SSE: events separated by \n\n
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const evt = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          handleSSEEvent(evt, ctx);
        }
      }
      stopGerunds();
      const summary = summariseResult(ctx.lastResult);
      setStatus(summary);
      announce(summary);
      // refresh session list (titles may have updated)
      refreshSessions();
    } catch (err) {
      stopGerunds();
      setStatus("Error: " + err.message);
      announce("Error: " + err.message);
    } finally {
      sendBtn.disabled = false;
      promptEl.focus();
    }
  });

  function handleSSEEvent(evt, ctx) {
    const lines = evt.split("\n");
    let dataLine = "";
    for (const ln of lines) {
      if (ln.startsWith("data:")) dataLine += ln.slice(5).trim();
    }
    if (!dataLine) return;
    let obj;
    try { obj = JSON.parse(dataLine); } catch { return; }

    if (obj.type === "system" && obj.subtype === "init") {
      // first chunk has session_id — record it for resume
      if (obj.session_id && !sessionId) {
        sessionId = obj.session_id;
        const url = new URL(location.href);
        url.searchParams.set("session", sessionId);
        history.replaceState({}, "", url.toString());
      }
    } else if (obj.type === "assistant" && obj.message) {
      const blocks = obj.message.content || [];
      for (const blk of blocks) {
        if (blk.type === "text" && blk.text) {
          if (!ctx.currentAssistantBody) {
            ctx.currentAssistantBody = appendMessage("assistant", "");
          }
          const raw = (ctx.currentAssistantBody.dataset.raw || "") + blk.text;
          ctx.currentAssistantBody.dataset.raw = raw;
          ctx.currentAssistantBody.innerHTML = renderMarkdown(raw);
          transcript.scrollTop = transcript.scrollHeight;
        } else if (blk.type === "tool_use") {
          // Subsequent text blocks should land in a new assistant article
          // *after* this tool call, not into the one above it.
          ctx.currentAssistantBody = null;
          insertToolMessage("→ " + blk.name + " " + summariseToolInput(blk.input || {}));
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
        }
      }
    } else if (obj.type === "permission_request") {
      ctx.currentAssistantBody = null;
      announce(`Permission needed for ${obj.tool}.`);
      renderPermissionCard(obj);
    } else if (obj.type === "todos_update") {
      updateTodosPanel(obj.todos || []);
    } else if (obj.type === "result") {
      ctx.lastResult = obj;
      if (obj.is_error) setStatus("Error: " + (obj.result || obj.subtype));
    } else if (obj.type === "error") {
      setStatus("Error: " + (obj.message || obj.stderr || obj.exit_code));
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
    card.setAttribute("aria-label", `Permission request: ${req.tool} ${summariseToolInput(req.input || {})}`);

    const heading = document.createElement("div");
    heading.className = "role";
    heading.textContent = `Claude wants to use ${req.tool}`;
    card.appendChild(heading);

    const detail = document.createElement("pre");
    detail.className = "permission-input";
    detail.textContent = formatToolInput(req.tool, req.input || {});
    card.appendChild(detail);

    const actions = document.createElement("div");
    actions.className = "permission-actions";

    const sigLabel = req.signature ? ` "${truncate(req.signature, 30)}"` : "";
    const buttons = [
      { decision: "deny", label: "Deny", variant: "danger" },
      { decision: "allow", label: "Allow once", variant: "primary" },
      { decision: "allow_session", label: `Allow this session${sigLabel}`, variant: "secondary" },
    ];

    const isHighRisk = req.tool === "Bash" || req.tool === "Write";
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
    transcript.scrollTop = transcript.scrollHeight;

    // Focus the safest button by default for high-risk tools.
    const focusBtn = isHighRisk ? actions.querySelector(".btn-danger") : actions.querySelector(".btn-primary");
    if (focusBtn) focusBtn.focus();

    // Esc denies, Enter allows once.
    card.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        decide(req.id, "deny", card);
      }
    });
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

  function updateTodosPanel(todos) {
    if (!todos.length) {
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
      li.textContent = label;
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
        <strong>Rate limit (${escape(rl.rateLimitType || "")})</strong><br>
        <span>Status: ${escape(rl.status || "—")}</span>
        <span>Resets: ${resetText}</span>
        ${rl.overageStatus ? `<span>Overage: ${escape(rl.overageStatus)}</span>` : ""}
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
        html += `<tr><td>${escape(s.title)}</td><td>${s.turns}</td><td>${cost(s.cost_usd)}</td></tr>`;
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

  function escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function insertToolMessage(text) {
    // Group consecutive tool messages under a collapsible <details> so the
    // dance doesn't drown out Claude's actual response.
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
    const count = group.querySelectorAll(".tool-line").length;
    group.querySelector("summary").textContent = `Tools (${count})`;
    transcript.scrollTop = transcript.scrollHeight;
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

  async function refreshSessions() {
    try {
      const r = await fetch("/api/sessions");
      if (!r.ok) return;
      const list = await r.json();
      sessionList.innerHTML = "";
      for (const s of list) {
        const li = document.createElement("li");
        const a = document.createElement("a");
        a.href = `?session=${s.id}`;
        a.dataset.session = s.id;
        a.dataset.mtime = s.mtime;
        const title = document.createElement("span");
        title.className = "session-title";
        title.textContent = s.title;
        const time = document.createElement("time");
        time.className = "session-time";
        time.setAttribute("datetime", s.mtime);
        time.textContent = formatTime(s.mtime);
        a.appendChild(title);
        a.appendChild(time);
        li.appendChild(a);
        sessionList.appendChild(li);
      }
      markActive(sessionId);
    } catch { /* ignore */ }
  }
})();
