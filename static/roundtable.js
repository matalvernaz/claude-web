// Roundtable browser controller.
//
// Two modes share this page:
//   - "assistant" (default): one input box + "Ask the panel" button.
//     Each submission fires the orchestration endpoint that does
//     parallel-ask + synthesizer behind the scenes and returns ONE
//     consolidated reply. The conversation persists in a roundtable
//     thread; subsequent turns continue it.
//   - "advanced": the underlying thread browser + manual ask/post/
//     attach/close panels. Same UX as before — kept for power use.
//
// All DOM updates use textContent (no innerHTML) — message bodies are
// arbitrary AI output and must never be parsed as HTML.

(() => {
  const body = document.body;
  const announcer = document.getElementById("announcer");
  const projectFilter = document.getElementById("project-filter");
  const newConvBtn = document.getElementById("new-conv-btn");
  const toggleAdvanced = document.getElementById("toggle-advanced");

  // ── Assistant elements ──────────────────────────────────────────
  const asstHistory = document.getElementById("assistant-history");
  const asstEmpty = document.getElementById("assistant-empty");
  const asstForm = document.getElementById("assistant-form");
  const asstInput = document.getElementById("assistant-input");
  const asstFile = document.getElementById("assistant-file");
  const asstFileList = document.getElementById("assistant-file-list");
  const asstSubmit = document.getElementById("assistant-submit");
  const asstStatus = document.getElementById("assistant-status");
  const asstError = document.getElementById("assistant-error");
  const overrideParticipants = document.getElementById("override-participants");
  const overrideSynthesizer = document.getElementById("override-synthesizer");
  const overrideEffort = document.getElementById("override-effort");
  const overrideWebSearch = document.getElementById("override-web-search");

  // ── Advanced elements (lazy-bound below) ────────────────────────
  const showClosed = document.getElementById("show-closed");
  const listEl = document.getElementById("thread-list");
  const listStatus = document.getElementById("thread-list-status");
  const detailEmpty = document.getElementById("thread-detail-empty");
  const detailArticle = document.getElementById("thread-detail");
  const detailTopic = document.getElementById("thread-detail-topic");
  const detailProject = document.getElementById("thread-detail-project");
  const detailParticipants = document.getElementById("thread-detail-participants");
  const detailCount = document.getElementById("thread-detail-count");
  const detailCreated = document.getElementById("thread-detail-created");
  const detailActivity = document.getElementById("thread-detail-activity");
  const detailStatus = document.getElementById("thread-detail-status");
  const detailMessages = document.getElementById("thread-messages");
  const detailError = document.getElementById("thread-detail-error");
  const closeThreadBtn = document.getElementById("close-thread-btn");
  const postForm = document.getElementById("post-form");
  const postContent = document.getElementById("post-content");
  const askForm = document.getElementById("ask-form");
  const askParticipant = document.getElementById("ask-participant");
  const askEffort = document.getElementById("ask-effort");
  const askWebSearch = document.getElementById("ask-web-search");
  const askPrompt = document.getElementById("ask-prompt");
  const askParallelForm = document.getElementById("ask-parallel-form");
  const askParallelChecklist = document.getElementById("ask-parallel-checklist");
  const askParallelEffort = document.getElementById("ask-parallel-effort");
  const askParallelWebSearch = document.getElementById("ask-parallel-web-search");
  const askParallelPrompt = document.getElementById("ask-parallel-prompt");
  const attachForm = document.getElementById("attach-form");
  const attachPath = document.getElementById("attach-path");
  const attachName = document.getElementById("attach-name");
  const attachHint = document.getElementById("attach-hint");

  const participantsJsonEl = document.getElementById("participants-json");
  const PARTICIPANTS = participantsJsonEl
    ? JSON.parse(participantsJsonEl.textContent)
    : [];

  // ── State ───────────────────────────────────────────────────────
  // Active assistant conversation thread. null = next "Ask" starts fresh.
  let assistantThreadId = null;
  // Track which thread is selected in the advanced view.
  let currentThreadId = null;

  // ── Helpers ─────────────────────────────────────────────────────
  // Quietly tell NVDA / VoiceOver something interesting happened.
  // Matches the main chat UI's announcer: NVDA coalesces mutations
  // that arrive too close together; 30ms wasn't enough, 120ms is
  // reliable in NVDA + Chrome/Firefox without feeling laggy. We
  // cancel any pending announcement so multiple calls within the gap
  // don't fire out of order — the most recent message is what matters.
  let announceTimer = null;
  function announce(text) {
    if (!announcer) return;
    if (announceTimer) clearTimeout(announceTimer);
    announcer.textContent = "";
    announceTimer = setTimeout(() => {
      announcer.textContent = text;
      announceTimer = null;
    }, 120);
  }

  function formatTs(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        dateStyle: "medium", timeStyle: "short",
      });
    } catch (_) { return iso; }
  }

  async function jsonFetch(url, opts = {}) {
    const finalOpts = { credentials: "same-origin", ...opts };
    if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
      finalOpts.headers = {
        "Content-Type": "application/json",
        ...(opts.headers || {}),
      };
      finalOpts.body = JSON.stringify(opts.body);
    }
    const resp = await fetch(url, finalOpts);
    if (!resp.ok) {
      let detail;
      try {
        const j = await resp.json();
        detail = j.detail || JSON.stringify(j);
      } catch (_) {
        detail = `HTTP ${resp.status}`;
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return resp.json();
  }

  // ── Mode toggle ─────────────────────────────────────────────────
  toggleAdvanced.addEventListener("click", () => {
    const advanced = body.classList.toggle("mode-advanced");
    body.classList.toggle("mode-assistant", !advanced);
    toggleAdvanced.setAttribute("aria-pressed", String(advanced));
    document.getElementById("assistant-pane").hidden = advanced;
    document.getElementById("advanced-pane").hidden = !advanced;
    if (advanced) {
      announce("Switched to advanced view: thread browser and manual controls.");
      loadThreads();
    } else {
      announce("Switched to assistant view: ask the panel directly.");
      // If the user picked a different thread in advanced view, the
      // assistant view should pick it up — otherwise the next "Ask the
      // panel" silently continues whatever thread the assistant view
      // was last on, which is confusing. Empty currentThreadId (advanced
      // view never selected anything) → no adoption; identical id →
      // nothing to do.
      if (currentThreadId != null && currentThreadId !== assistantThreadId) {
        adoptAdvancedThread(currentThreadId);
      }
    }
  });

  // Pull a thread's history into the assistant view as user + synth
  // pairs. The full panel debate isn't replayed (that's what the
  // advanced view is for) — assistant mode is a chat abstraction over
  // user-prompt → synthesised-response, so the replay matches that
  // shape. Threads created outside the assistant flow (e.g. via MCP)
  // may not have any synth-labelled turns; in that case the user
  // prompts are still rendered but without a paired assistant reply.
  async function adoptAdvancedThread(threadId) {
    let payload;
    try {
      payload = await jsonFetch(`/api/roundtable/threads/${threadId}`);
    } catch (err) {
      asstError.textContent = `Could not load thread ${threadId}: ${err.message}`;
      asstError.hidden = false;
      announce(`Could not load thread ${threadId}: ${err.message}`);
      return;
    }
    assistantThreadId = threadId;
    // Clear any prior assistant history before redrawing — we're now
    // showing a different conversation.
    while (asstHistory.firstChild !== asstEmpty) {
      asstHistory.removeChild(asstHistory.firstChild);
    }
    asstEmpty.hidden = true;

    const messages = payload.messages || [];
    const participantLabels = new Set((PARTICIPANTS || []).map(p => p.label));
    const isUserSpeaker = sp => sp !== "orchestrator" && !participantLabels.has(sp);
    const isParticipant = sp => participantLabels.has(sp);

    // Walk: for each user turn, the last participant turn before the
    // next user turn is the synthesis. Panel turns between them are
    // intentionally skipped — the debate expander lives in the live
    // flow only (we don't have panel/error metadata after the fact).
    let i = 0;
    let rendered = 0;
    while (i < messages.length) {
      const m = messages[i];
      if (!isUserSpeaker(m.speaker)) { i++; continue; }
      let j = i + 1;
      while (j < messages.length && !isUserSpeaker(messages[j].speaker)) j++;
      let synthIdx = -1;
      for (let k = i + 1; k < j; k++) {
        if (isParticipant(messages[k].speaker)) synthIdx = k;
      }
      appendUserTurn(m.content, []);
      if (synthIdx >= 0) {
        renderReplayedSynthTurn(messages[synthIdx]);
      }
      rendered++;
      i = j;
    }

    if (rendered === 0) {
      asstEmpty.hidden = false;
      announce(`Continuing thread ${threadId}: ${payload.thread.topic}. No prior user turns to replay.`);
    } else {
      announce(`Continuing thread ${threadId}: ${payload.thread.topic}. ${rendered} prior turn${rendered === 1 ? "" : "s"} replayed.`);
    }
  }

  // Render a past synth turn from raw thread-message data. Skips the
  // patches + debate expander (the source data doesn't carry the panel
  // responses post-hoc) — just speaker, timestamp, and markdown body.
  function renderReplayedSynthTurn(msg) {
    const article = document.createElement("article");
    article.className = "asst-turn asst-turn-assistant";
    const h = document.createElement("header");
    h.className = "asst-turn-header";
    const who = document.createElement("h3");
    who.className = "asst-turn-speaker";
    who.textContent = msg.speaker;
    who.tabIndex = -1;
    h.appendChild(who);
    const t = document.createElement("time");
    t.dateTime = msg.ts || "";
    t.textContent = formatTs(msg.ts);
    h.appendChild(t);
    article.appendChild(h);
    article.appendChild(renderMarkdown(msg.content || ""));
    asstHistory.appendChild(article);
  }

  // ── Assistant flow ──────────────────────────────────────────────
  newConvBtn.addEventListener("click", () => {
    assistantThreadId = null;
    while (asstHistory.firstChild !== asstEmpty) {
      asstHistory.removeChild(asstHistory.firstChild);
    }
    asstEmpty.hidden = false;
    asstInput.value = "";
    asstFile.value = "";
    updateFileList();
    asstStatus.textContent = "";
    asstError.hidden = true;
    announce("New conversation started.");
  });

  function updateFileList() {
    const files = Array.from(asstFile.files || []);
    if (files.length === 0) {
      asstFileList.textContent = "";
      return;
    }
    asstFileList.textContent =
      files.length === 1
        ? `${files[0].name} (${files[0].size} bytes)`
        : `${files.length} files attached`;
  }
  asstFile.addEventListener("change", updateFileList);

  function appendUserTurn(promptText, fileNames) {
    asstEmpty.hidden = true;
    const article = document.createElement("article");
    article.className = "asst-turn asst-turn-user";
    const h = document.createElement("header");
    h.className = "asst-turn-header";
    // h3 (not span) so NVDA's H-key cycles through turns — matches the
    // main chat UI's per-message heading pattern.
    const who = document.createElement("h3");
    who.className = "asst-turn-speaker";
    who.textContent = "You";
    h.appendChild(who);
    const t = document.createElement("time");
    t.dateTime = new Date().toISOString();
    t.textContent = formatTs(new Date().toISOString());
    h.appendChild(t);
    article.appendChild(h);
    const body = document.createElement("pre");
    body.className = "asst-turn-body";
    body.textContent = promptText;
    article.appendChild(body);
    if (fileNames.length) {
      const meta = document.createElement("p");
      meta.className = "asst-turn-attached";
      meta.textContent = `Attached: ${fileNames.join(", ")}`;
      article.appendChild(meta);
    }
    asstHistory.appendChild(article);
    return article;
  }

  // Render a chunk of AI text as sanitized HTML. marked + DOMPurify are
  // global from the script tags in the template. If either is missing
  // (e.g. CDN cached fail), fall back to plain pre-rendered text so
  // we never insert unsanitized markup.
  function renderMarkdown(text) {
    if (typeof window.marked === "undefined" || typeof window.DOMPurify === "undefined") {
      const pre = document.createElement("pre");
      pre.className = "asst-turn-body";
      pre.textContent = text;
      return pre;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "asst-turn-body asst-turn-md";
    const html = window.marked.parse(text || "", { breaks: true, gfm: true });
    wrapper.innerHTML = window.DOMPurify.sanitize(html);
    return wrapper;
  }

  // Render the assistant's synthesis turn. Builds the empty shell that
  // streaming events will populate incrementally; the final 'done'
  // event swaps the placeholder body with the rendered markdown and
  // adds apply buttons + the debate expander.
  function createAssistantShell() {
    asstEmpty.hidden = true;
    const article = document.createElement("article");
    article.className = "asst-turn asst-turn-assistant asst-turn-pending";
    const h = document.createElement("header");
    h.className = "asst-turn-header";
    // h3 so the user can H-key navigate to it; tabindex=-1 lets us
    // programmatically move focus here when the synthesis arrives.
    const who = document.createElement("h3");
    who.className = "asst-turn-speaker";
    who.textContent = "Panel + synthesizer";
    who.tabIndex = -1;
    h.appendChild(who);
    const t = document.createElement("time");
    t.dateTime = new Date().toISOString();
    t.textContent = formatTs(new Date().toISOString());
    h.appendChild(t);
    article.appendChild(h);

    const progress = document.createElement("p");
    progress.className = "asst-turn-progress";
    progress.textContent = "Working…";
    article.appendChild(progress);

    asstHistory.appendChild(article);
    article.scrollIntoView({ block: "nearest" });
    return { article, progress };
  }

  function finalizeAssistantTurn(article, progress, payload) {
    article.classList.remove("asst-turn-pending");

    // Replace the speaker label with the actual synthesizer's name.
    const speakerEl = article.querySelector(".asst-turn-speaker");
    if (speakerEl) speakerEl.textContent = payload.synthesizer.label;
    progress.remove();

    // Move focus to the speaker heading so screen-reader users land on
    // the new turn instead of being stranded in the input area. The
    // heading is tabindex=-1 (set in createAssistantShell) so it accepts
    // programmatic focus without becoming part of the normal tab order.
    // Wrapped in requestAnimationFrame so the focus call runs after the
    // markdown body has been appended below and the DOM has settled.
    if (speakerEl) {
      requestAnimationFrame(() => {
        try { speakerEl.focus({ preventScroll: false }); } catch (_) {}
      });
    }

    // Markdown-render the synthesis body. AI output is sanitized via
    // DOMPurify so a synthesizer that produces <script> doesn't get
    // to execute it. Diff fences inside the markdown stay readable as
    // <pre><code> blocks; the apply buttons below sit beside them.
    article.appendChild(renderMarkdown(payload.synthesis));

    // One "Apply" button per detected patch. We confirm before posting
    // and announce success/failure.
    if ((payload.patches || []).length) {
      const patchPanel = document.createElement("section");
      patchPanel.className = "asst-patches";
      const heading = document.createElement("h4");
      heading.textContent = `Suggested patches (${payload.patches.length})`;
      patchPanel.appendChild(heading);
      for (const patch of payload.patches) {
        const row = document.createElement("div");
        row.className = "asst-patch-row";
        const label = document.createElement("span");
        label.className = "asst-patch-target";
        label.textContent = patch.target;
        row.appendChild(label);
        const applyBtn = document.createElement("button");
        applyBtn.type = "button";
        applyBtn.className = "asst-apply-btn";
        applyBtn.textContent = "Apply";
        applyBtn.addEventListener("click", () =>
          handleApplyPatch(payload.thread_id, patch.target, patch.diff, applyBtn, row),
        );
        row.appendChild(applyBtn);
        const status = document.createElement("span");
        status.className = "asst-patch-status";
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        row.appendChild(status);
        patchPanel.appendChild(row);
      }
      article.appendChild(patchPanel);
    }

    // Debate expander (audit trail).
    if (Object.keys(payload.panel_responses || {}).length
        || Object.keys(payload.panel_errors || {}).length
        || (payload.panel_unavailable && payload.panel_unavailable.length)) {
      const det = document.createElement("details");
      det.className = "asst-debate-expander";
      const sum = document.createElement("summary");
      const panelCount = Object.keys(payload.panel_responses).length;
      const errCount = Object.keys(payload.panel_errors || {}).length;
      const unavailCount = (payload.panel_unavailable || []).length;
      sum.textContent =
        `Show the full debate (${panelCount} responded` +
        (errCount ? `, ${errCount} errored` : "") +
        (unavailCount ? `, ${unavailCount} unavailable` : "") +
        `)`;
      det.appendChild(sum);
      for (const [name, text] of Object.entries(payload.panel_responses || {})) {
        const sub = document.createElement("article");
        sub.className = "asst-panel-reply";
        const sh = document.createElement("header");
        const ss = document.createElement("strong");
        ss.textContent = name;
        sh.appendChild(ss);
        sub.appendChild(sh);
        sub.appendChild(renderMarkdown(text));
        det.appendChild(sub);
      }
      for (const [name, err] of Object.entries(payload.panel_errors || {})) {
        const sub = document.createElement("article");
        sub.className = "asst-panel-error";
        sub.textContent = `${name} errored: ${err}`;
        det.appendChild(sub);
      }
      if ((payload.panel_unavailable || []).length) {
        const sub = document.createElement("p");
        sub.className = "asst-panel-error";
        sub.textContent = `Unavailable in this deployment: ${payload.panel_unavailable.join(", ")}`;
        det.appendChild(sub);
      }
      article.appendChild(det);
    }

    // Audit link: open the underlying thread in the advanced view.
    if (payload.thread_id) {
      const audit = document.createElement("p");
      audit.className = "asst-audit-link";
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = `Open this thread in advanced view (#${payload.thread_id})`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        toggleAdvanced.click();
        selectThread(payload.thread_id);
      });
      audit.appendChild(a);
      article.appendChild(audit);
    }
  }

  async function handleApplyPatch(threadId, target, diff, btn, row) {
    const status = row.querySelector(".asst-patch-status");
    if (!confirm(`Apply this diff to ${target}? The original will be backed up alongside as ${target}.rt-orig.`)) {
      return;
    }
    btn.disabled = true;
    status.textContent = "Applying…";
    announce(`Applying patch to ${target}.`);
    try {
      const result = await jsonFetch("/api/roundtable/assistant/apply", {
        method: "POST",
        body: { thread_id: threadId, target, diff },
      });
      btn.disabled = true;
      btn.textContent = "Applied";
      status.textContent = `Applied. Backup: ${result.backup}`;
      announce(`Patch applied to ${target}. Backup saved alongside.`);
    } catch (err) {
      btn.disabled = false;
      status.textContent = `Apply failed: ${err.message}`;
      announce(`Patch apply failed: ${err.message}`);
    }
  }

  // Render a permission-request card inside the live assistant article.
  // Mirrors the main chat's renderPermissionCard pattern but scoped to
  // the roundtable's per-turn article so the prompt appears next to the
  // panel/synth that asked for it. Posts to the SAME /api/permission/
  // endpoint the main chat uses — server-side PENDING is shared.
  function renderRoundtablePermissionCard(req, hostArticle, progress) {
    const card = document.createElement("article");
    card.className = "rt-permission";
    card.setAttribute("role", "alertdialog");
    card.setAttribute("aria-modal", "false");
    if (req.id) card.dataset.requestId = req.id;
    card.dataset.state = "pending";

    const headingId = `rt-perm-heading-${req.id || Math.random().toString(36).slice(2)}`;
    const detailId = `rt-perm-detail-${req.id || Math.random().toString(36).slice(2)}`;
    const heading = document.createElement("h4");
    heading.id = headingId;
    heading.className = "rt-permission-heading";
    const who = req.participant_label || "A panelist";
    heading.textContent = `${who} wants to use ${req.tool}`;
    card.appendChild(heading);
    card.setAttribute("aria-labelledby", headingId);
    card.setAttribute("aria-describedby", detailId);

    const detail = document.createElement("div");
    detail.id = detailId;
    detail.className = "rt-permission-detail";
    const pre = document.createElement("pre");
    pre.className = "rt-permission-input";
    // Read/Grep/Glob inputs are small JSON dicts — no special-casing
    // needed (the main chat has Bash/Edit/Write rendering because those
    // tools aren't in the roundtable allowlist for Layer 1).
    pre.textContent = JSON.stringify(req.input || {}, null, 2);
    detail.appendChild(pre);
    card.appendChild(detail);

    const actions = document.createElement("div");
    actions.className = "rt-permission-actions";
    const allowSessionSupported = req.allow_session_supported !== false;
    const sig = req.signature ? ` "${(req.signature.length > 30 ? req.signature.slice(0, 27) + "…" : req.signature)}"` : "";

    const buttons = [
      { decision: "deny", label: "Deny" },
      { decision: "allow", label: "Allow once" },
    ];
    if (allowSessionSupported) {
      buttons.push({ decision: "allow_session", label: `Allow this turn${sig}` });
    }
    for (const b of buttons) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = b.label;
      btn.className = `rt-permission-btn rt-permission-${b.decision}`;
      btn.addEventListener("click", () => decideRoundtablePermission(req.id, b.decision, card));
      actions.appendChild(btn);
    }
    card.appendChild(actions);

    if (progress && progress.parentNode === hostArticle) {
      hostArticle.insertBefore(card, progress);
    } else {
      hostArticle.appendChild(card);
    }
    card.scrollIntoView({ block: "nearest" });

    // Esc denies; nothing is bound to Enter (so the focused-button
    // default — Deny — isn't accidentally overridden into an Allow).
    card.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        decideRoundtablePermission(req.id, "deny", card);
      }
    });

    // Focus Deny by default — every permission request asks the user to
    // make an active choice, and "approve by inertia" is the wrong default.
    const denyBtn = actions.querySelector(".rt-permission-deny");
    if (denyBtn) denyBtn.focus();
    return card;
  }

  async function decideRoundtablePermission(requestId, decision, card) {
    if (!card || card.dataset.state !== "pending") return;
    card.dataset.state = "deciding";
    card.querySelectorAll("button").forEach(b => (b.disabled = true));
    try {
      const fd = new FormData();
      fd.append("decision", decision);
      const r = await fetch(`/api/permission/${encodeURIComponent(requestId)}`, {
        method: "POST",
        credentials: "same-origin",
        body: fd,
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      card.dataset.state = "resolved";
      const labels = { allow: "Allowed", allow_session: "Allowed (this turn)", deny: "Denied" };
      const note = document.createElement("p");
      note.className = "rt-permission-resolved";
      const heading = card.querySelector(".rt-permission-heading")?.textContent || "tool";
      note.textContent = `${labels[decision] || decision} — ${heading}`;
      card.replaceWith(note);
      announce(`${labels[decision] || decision} ${heading}.`);
    } catch (err) {
      card.dataset.state = "pending";
      card.querySelectorAll("button").forEach(b => (b.disabled = false));
      const errLine = document.createElement("p");
      errLine.className = "rt-permission-error";
      errLine.textContent = `Decision failed: ${err.message}`;
      card.appendChild(errLine);
      announce(`Decision failed: ${err.message}`);
    }
  }

  function markRoundtablePermissionTimedOut(requestId, hostArticle, timeoutSeconds) {
    const cards = hostArticle.querySelectorAll("article.rt-permission");
    const card = [...cards].find(el => el.dataset.requestId === String(requestId));
    if (!card || card.dataset.state === "resolved") return;
    card.dataset.state = "timed_out";
    card.querySelectorAll("button").forEach(b => (b.disabled = true));
    const note = document.createElement("p");
    note.className = "rt-permission-error";
    note.textContent = `Timed out after ${timeoutSeconds || "?"}s — treated as denied.`;
    card.appendChild(note);
    announce("Permission request timed out.");
  }

  // Parse a chunk of an SSE stream and dispatch events.
  // SSE format: each event is `event: NAME\ndata: JSON\n\n`. We buffer
  // across reads since chunks can split mid-frame.
  function makeSSEParser(onEvent) {
    let buffer = "";
    return function feed(chunk) {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let eventName = "message";
        const dataLines = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) {
            eventName = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
          }
          // Ignore other SSE fields (id, retry) — we don't need them.
        }
        if (dataLines.length) {
          let data;
          try { data = JSON.parse(dataLines.join("\n")); }
          catch (e) { data = { raw: dataLines.join("\n") }; }
          onEvent(eventName, data);
        }
      }
    };
  }

  // Shared by the live POST stream and the rejoin path: one handler
  // updating one assistant shell. state.doneSeen tells the read loop a
  // clean `done` arrived (vs a dropped connection worth rejoining).
  const RT_STREAM_KEY = "claude-web.rt-stream";
  let assistantStreamId = null;

  function assistantEventHandler(state, asstArticle, progress) {
    return (event, data) => {
      switch (event) {
        case "created":
          assistantThreadId = data.thread_id;
          progress.textContent = data.thread_was_new
            ? `Thread #${data.thread_id} created.`
            : `Continuing thread #${data.thread_id}.`;
          announce(progress.textContent);
          break;
        case "attached":
          progress.textContent = `Attached ${data.name} (v${data.version}, ${data.bytes} bytes).`;
          announce(progress.textContent);
          break;
        case "prompt_posted":
          progress.textContent = "Prompt posted. Asking the panel…";
          break;
        case "panel_start": {
          const labels = (data.participants || []).map(p => p.label).join(" + ");
          const webBit = data.web_search ? ", web search on" : "";
          progress.textContent = `Panel working: ${labels} (${data.effort || "default"} effort${webBit})…`;
          announce(`Panel working: ${labels}.`);
          break;
        }
        case "panel_done": {
          const sizes = Object.entries(data.responses || {})
            .map(([k, v]) => `${k} ${v.chars}c`)
            .join(", ");
          const errs = Object.keys(data.errors || {}).length;
          progress.textContent = `Panel done: ${sizes || "(no panel)"}${errs ? ` — ${errs} errored` : ""}. Synthesizing…`;
          announce(`Panel done. ${sizes || "no panel"}. Synthesizing now.`);
          break;
        }
        case "synth_start":
          progress.textContent = `Synthesizing with ${data.synthesizer.label}…`;
          break;
        case "stream":
          // Server-side detached stream id: survives tab close. Saved so a
          // reload can rejoin the run and replay what it missed.
          assistantStreamId = data.stream_id;
          try { sessionStorage.setItem(RT_STREAM_KEY, data.stream_id); } catch (_) {}
          break;
        case "done":
          state.doneSeen = true;
          try { sessionStorage.removeItem(RT_STREAM_KEY); } catch (_) {}
          assistantThreadId = data.thread_id;
          finalizeAssistantTurn(asstArticle, progress, data);
          announce(
            `Response from ${data.synthesizer.label} ready` +
            ` (${data.synthesis.length} characters` +
            (data.patches && data.patches.length ? `, ${data.patches.length} patches suggested` : "") +
            `).`,
          );
          break;
        case "permission_request":
          progress.textContent = `${data.participant_label || "Panelist"} wants to use ${data.tool}. Awaiting your decision…`;
          announce(`${data.participant_label || "A panelist"} wants to use ${data.tool}. Decide allow or deny.`);
          renderRoundtablePermissionCard(data, asstArticle, progress);
          break;
        case "permission_timeout":
          markRoundtablePermissionTimedOut(data.id, asstArticle, data.timeout_seconds);
          progress.textContent = `Permission request for ${data.tool || "tool"} timed out.`;
          break;
        case "error":
          try { sessionStorage.removeItem(RT_STREAM_KEY); } catch (_) {}
          progress.textContent = `Error: ${data.message}`;
          announce(`Assistant error: ${data.message}`);
          break;
      }
    };
  }

  asstForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = asstInput.value.trim();
    if (!prompt) return;
    asstError.hidden = true;

    const fileNames = Array.from(asstFile.files || []).map(f => f.name);
    appendUserTurn(prompt, fileNames);
    const { article: asstArticle, progress } = createAssistantShell();

    const formData = new FormData();
    formData.append("prompt", prompt);
    formData.append("project_key", projectFilter.value);
    if (assistantThreadId != null) formData.append("thread_id", String(assistantThreadId));
    if (overrideParticipants.value.trim()) {
      formData.append("participants_csv", overrideParticipants.value.trim());
    }
    if (overrideSynthesizer.value) {
      formData.append("synthesizer", overrideSynthesizer.value);
    }
    if (overrideEffort.value) formData.append("effort", overrideEffort.value);
    if (overrideWebSearch && overrideWebSearch.checked) {
      formData.append("web_search", "true");
    }
    for (const f of asstFile.files || []) formData.append("files", f);

    asstSubmit.disabled = true;
    asstStatus.textContent = "Asking the panel…";
    announce("Asking the panel. Streaming progress as it happens.");

    let resp;
    try {
      resp = await fetch("/api/roundtable/assistant", {
        method: "POST",
        credentials: "same-origin",
        body: formData,
      });
    } catch (err) {
      asstStatus.textContent = "";
      asstError.textContent = `Network error: ${err.message}`;
      asstError.hidden = false;
      announce(`Network error: ${err.message}`);
      asstSubmit.disabled = false;
      asstArticle.remove();
      return;
    }
    if (!resp.ok) {
      let detail;
      try { detail = (await resp.json()).detail || `HTTP ${resp.status}`; }
      catch (_) { detail = `HTTP ${resp.status}`; }
      asstStatus.textContent = "";
      asstError.textContent = `Ask failed: ${detail}`;
      asstError.hidden = false;
      announce(`Ask failed: ${detail}`);
      asstSubmit.disabled = false;
      asstArticle.remove();
      return;
    }

    // SSE event handlers — update the placeholder article as events arrive.
    const state = { doneSeen: false };
    const parser = makeSSEParser(assistantEventHandler(state, asstArticle, progress));

    // Read the SSE stream. The producer is detached server-side, so a
    // dropped connection is recoverable: rejoin by stream id and the
    // replay catches us up. Three attempts, then give up loudly.
    let lastResp = resp;
    let rejoinAttempts = 0;
    while (true) {
      try {
        const reader = lastResp.body.getReader();
        const decoder = new TextDecoder();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          parser(decoder.decode(value, { stream: true }));
        }
        parser(decoder.decode());
      } catch (err) {
        progress.textContent = `Stream error: ${err.message}`;
      }
      if (state.doneSeen || !assistantStreamId || rejoinAttempts >= 3) break;
      rejoinAttempts += 1;
      announce("Panel stream dropped — rejoining.");
      try {
        const r = await fetch(`/api/roundtable/assistant/stream/${encodeURIComponent(assistantStreamId)}`, { credentials: "same-origin" });
        if (!r.ok) break;
        lastResp = r;
      } catch (_) { break; }
    }

    asstStatus.textContent = "";
    asstSubmit.disabled = false;
    asstInput.value = "";
    asstFile.value = "";
    updateFileList();

    if (!state.doneSeen) {
      progress.textContent = progress.textContent || "Stream ended before completion.";
    }
  });

  // ── Advanced flow ───────────────────────────────────────────────
  if (listEl) {
    // Build participant checklists / dropdowns on the advanced surface.
    function buildChecklist(container, prefix) {
      while (container.firstChild) container.removeChild(container.firstChild);
      for (const p of PARTICIPANTS) {
        const li = document.createElement("li");
        const id = `${prefix}-cb-${p.key}`;
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.id = id;
        cb.value = p.key;
        cb.disabled = !p.available;
        const lbl = document.createElement("label");
        lbl.htmlFor = id;
        lbl.textContent = p.available
          ? `${p.label} (${p.key})`
          : `${p.label} (${p.key}) — unavailable`;
        li.appendChild(cb);
        li.appendChild(lbl);
        container.appendChild(li);
      }
    }
    buildChecklist(askParallelChecklist, "askp");

    while (askParticipant.firstChild) askParticipant.removeChild(askParticipant.firstChild);
    for (const p of PARTICIPANTS) {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.available
        ? `${p.label} (${p.key})`
        : `${p.label} (${p.key}) — unavailable`;
      opt.disabled = !p.available;
      askParticipant.appendChild(opt);
    }
  }

  function setDetailError(text) {
    if (!detailError) return;
    if (!text) {
      detailError.hidden = true;
      detailError.textContent = "";
      return;
    }
    detailError.hidden = false;
    detailError.textContent = text;
    announce(`Error: ${text}`);
  }

  async function loadThreads() {
    if (!listEl) return;
    listStatus.textContent = "Loading…";
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);

    const params = new URLSearchParams();
    params.set("open_only", showClosed.checked ? "false" : "true");
    if (projectFilter.value) params.set("project", projectFilter.value);

    let payload;
    try {
      payload = await jsonFetch(`/api/roundtable/threads?${params}`);
    } catch (err) {
      listStatus.textContent = `Failed to load threads: ${err.message}`;
      return;
    }

    const threads = payload.threads || [];
    if (threads.length === 0) {
      listStatus.textContent = "No threads match the current filters.";
      return;
    }
    listStatus.textContent = `${threads.length} thread${threads.length === 1 ? "" : "s"}`;

    for (const t of threads) {
      const li = document.createElement("li");
      li.className = "thread-item";
      if (!t.open) li.classList.add("thread-item-closed");

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "thread-item-button";
      btn.dataset.threadId = String(t.thread_id);
      btn.setAttribute("aria-label",
        `Open thread ${t.thread_id}: ${t.topic}. ${t.messages} messages.` +
        (t.project_key ? ` Project ${t.project_key}.` : "") +
        ` Last activity ${formatTs(t.last_activity)}.`,
      );

      const topic = document.createElement("span");
      topic.className = "thread-item-topic";
      topic.textContent = t.topic || `(thread ${t.thread_id})`;
      btn.appendChild(topic);

      const meta = document.createElement("span");
      meta.className = "thread-item-meta";
      const parts = [];
      if (t.project_key) parts.push(`📁 ${t.project_key}`);
      if (t.participants && t.participants.length) parts.push(t.participants.join(", "));
      parts.push(`${t.messages} msg${t.messages === 1 ? "" : "s"}`);
      if (!t.open) parts.push("closed");
      meta.textContent = parts.join(" · ");
      btn.appendChild(meta);

      const time = document.createElement("time");
      time.className = "thread-item-time";
      time.dateTime = t.last_activity || "";
      time.textContent = formatTs(t.last_activity);
      btn.appendChild(time);

      btn.addEventListener("click", () => selectThread(t.thread_id, btn));
      li.appendChild(btn);
      listEl.appendChild(li);
    }

    if (currentThreadId != null) {
      const sel = listEl.querySelector(`button[data-thread-id="${currentThreadId}"]`);
      if (sel) sel.setAttribute("aria-current", "true");
    }
  }

  async function selectThread(threadId, originButton) {
    setDetailError("");
    currentThreadId = threadId;
    if (listEl) {
      for (const b of listEl.querySelectorAll(".thread-item-button[aria-current]")) {
        b.removeAttribute("aria-current");
      }
      if (originButton) {
        originButton.setAttribute("aria-current", "true");
      } else {
        const sel = listEl.querySelector(`button[data-thread-id="${threadId}"]`);
        if (sel) sel.setAttribute("aria-current", "true");
      }
    }
    detailEmpty.hidden = true;
    detailArticle.hidden = false;
    detailTopic.textContent = "Loading…";
    while (detailMessages.firstChild) detailMessages.removeChild(detailMessages.firstChild);

    let payload;
    try {
      payload = await jsonFetch(`/api/roundtable/threads/${threadId}`);
    } catch (err) {
      detailTopic.textContent = `Thread ${threadId}`;
      setDetailError(`Failed to load thread: ${err.message}`);
      return;
    }

    renderThreadDetail(payload);
    document.getElementById("thread-detail-heading").focus({ preventScroll: false });
    announce(`Loaded thread ${threadId}: ${payload.thread.topic}`);
  }

  function renderThreadDetail(payload) {
    const t = payload.thread;
    detailTopic.textContent = t.topic || `Thread ${t.thread_id}`;
    detailProject.textContent = t.project_key || "(unbound)";
    detailParticipants.textContent =
      (t.participants && t.participants.length) ? t.participants.join(", ") : "(none registered)";
    detailCount.textContent = String(t.messages);
    detailCreated.dateTime = t.created_at || "";
    detailCreated.textContent = formatTs(t.created_at);
    detailActivity.dateTime = t.last_activity || "";
    detailActivity.textContent = formatTs(t.last_activity);
    detailStatus.textContent = t.open ? "open" : `closed (${formatTs(t.closed_at)})`;

    if (attachHint) {
      attachHint.textContent = t.project_key
        ? `Reads from project '${t.project_key}'. Paths can be relative to the project root.`
        : `This thread isn't bound to a project. Use an absolute path inside one of your configured projects.`;
    }
    closeThreadBtn.disabled = !t.open;

    while (detailMessages.firstChild) detailMessages.removeChild(detailMessages.firstChild);
    for (const m of (payload.messages || [])) {
      const li = document.createElement("li");
      li.className = "thread-message";
      li.setAttribute("data-speaker", m.speaker);
      const art = document.createElement("article");
      art.setAttribute("aria-labelledby", `msg-speaker-${t.thread_id}-${m.idx}`);
      const head = document.createElement("header");
      head.className = "thread-message-header";
      const speaker = document.createElement("span");
      speaker.className = "thread-message-speaker";
      speaker.id = `msg-speaker-${t.thread_id}-${m.idx}`;
      speaker.textContent = m.speaker;
      head.appendChild(speaker);
      const time = document.createElement("time");
      time.className = "thread-message-time";
      time.dateTime = m.ts || "";
      time.textContent = formatTs(m.ts);
      head.appendChild(time);
      art.appendChild(head);
      const bodyEl = document.createElement("pre");
      bodyEl.className = "thread-message-body";
      bodyEl.textContent = m.content;
      art.appendChild(bodyEl);
      li.appendChild(art);
      detailMessages.appendChild(li);
    }
  }

  async function refreshCurrentThread() {
    if (currentThreadId == null) return;
    try {
      const payload = await jsonFetch(`/api/roundtable/threads/${currentThreadId}`);
      renderThreadDetail(payload);
    } catch (err) {
      setDetailError(`Refresh failed: ${err.message}`);
    }
  }

  function withBusy(form, label, fn) {
    const buttons = form.querySelectorAll("button");
    buttons.forEach(b => { b.disabled = true; });
    announce(label);
    return fn().finally(() => buttons.forEach(b => { b.disabled = false; }));
  }

  if (postForm) {
    postForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (currentThreadId == null) return;
      const content = postContent.value;
      if (!content.trim()) return;
      setDetailError("");
      try {
        await withBusy(postForm, "Posting note…", () =>
          jsonFetch(`/api/roundtable/threads/${currentThreadId}/post`, {
            method: "POST", body: { content },
          }),
        );
        postContent.value = "";
        announce("Note posted.");
        await refreshCurrentThread();
        await loadThreads();
      } catch (err) { setDetailError(`Post failed: ${err.message}`); }
    });

    askForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (currentThreadId == null) return;
      const participant = askParticipant.value;
      if (!participant) return;
      setDetailError("");
      try {
        const result = await withBusy(askForm, `Asking ${participant}…`, () =>
          jsonFetch(`/api/roundtable/threads/${currentThreadId}/ask`, {
            method: "POST",
            body: {
              participant,
              prompt: askPrompt.value,
              effort: askEffort.value,
              web_search: !!(askWebSearch && askWebSearch.checked),
            },
          }),
        );
        announce(`Response from ${participant} received (${result.response.length} chars).`);
        askPrompt.value = "";
        await refreshCurrentThread();
        await loadThreads();
      } catch (err) { setDetailError(`Ask failed: ${err.message}`); }
    });

    askParallelForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (currentThreadId == null) return;
      const picked = Array.from(
        askParallelChecklist.querySelectorAll("input[type=checkbox]:checked"),
      ).map(cb => cb.value);
      if (picked.length === 0) {
        setDetailError("Pick at least one participant.");
        return;
      }
      setDetailError("");
      try {
        const result = await withBusy(askParallelForm, `Asking ${picked.length} participants in parallel…`, () =>
          jsonFetch(`/api/roundtable/threads/${currentThreadId}/ask_parallel`, {
            method: "POST",
            body: {
              participants: picked,
              prompt: askParallelPrompt.value,
              effort: askParallelEffort.value,
              web_search: !!(askParallelWebSearch && askParallelWebSearch.checked),
            },
          }),
        );
        const okCount = Object.keys(result.responses || {}).length;
        const errCount = Object.keys(result.errors || {}).length;
        announce(`Parallel ask complete: ${okCount} responded, ${errCount} errored.`);
        askParallelPrompt.value = "";
        await refreshCurrentThread();
        await loadThreads();
      } catch (err) { setDetailError(`Parallel ask failed: ${err.message}`); }
    });

    attachForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (currentThreadId == null) return;
      const path = attachPath.value.trim();
      if (!path) return;
      setDetailError("");
      try {
        const result = await withBusy(attachForm, `Attaching ${path}…`, () =>
          jsonFetch(`/api/roundtable/threads/${currentThreadId}/artifact`, {
            method: "POST",
            body: { path, name: attachName.value.trim() },
          }),
        );
        announce(`Attached ${result.name} as v${result.version} (${result.bytes} bytes).`);
        attachPath.value = "";
        attachName.value = "";
        await refreshCurrentThread();
        await loadThreads();
      } catch (err) { setDetailError(`Attach failed: ${err.message}`); }
    });

    closeThreadBtn.addEventListener("click", async () => {
      if (currentThreadId == null) return;
      if (!confirm("Close this thread? Existing turns stay readable but no new turns can be posted.")) return;
      setDetailError("");
      try {
        closeThreadBtn.disabled = true;
        await jsonFetch(`/api/roundtable/threads/${currentThreadId}/close`, { method: "POST" });
        announce("Thread closed.");
        await refreshCurrentThread();
        await loadThreads();
      } catch (err) {
        setDetailError(`Close failed: ${err.message}`);
        closeThreadBtn.disabled = false;
      }
    });
  }

  if (showClosed) showClosed.addEventListener("change", loadThreads);
  // Project filter affects assistant context AND advanced thread list.
  projectFilter.addEventListener("change", () => {
    if (body.classList.contains("mode-advanced")) loadThreads();
  });

  // A panel run survives tab close server-side. If this page load follows
  // one, rejoin it: full replay then live tail into a fresh shell.
  (async function resumeDetachedAssistant() {
    let sid = null;
    try { sid = sessionStorage.getItem(RT_STREAM_KEY); } catch (_) {}
    if (!sid || !asstForm) return;
    let r;
    try {
      r = await fetch(`/api/roundtable/assistant/stream/${encodeURIComponent(sid)}`, { credentials: "same-origin" });
    } catch (_) { return; }
    if (!r.ok) {
      try { sessionStorage.removeItem(RT_STREAM_KEY); } catch (_) {}
      return;
    }
    announce("Rejoining a panel run that was still in flight.");
    assistantStreamId = sid;
    const { article, progress } = createAssistantShell();
    const state = { doneSeen: false };
    const parser = makeSSEParser(assistantEventHandler(state, article, progress));
    try {
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        parser(decoder.decode(value, { stream: true }));
      }
      parser(decoder.decode());
    } catch (_) { /* dropped again; the saved id allows another reload */ }
  })();
})();
