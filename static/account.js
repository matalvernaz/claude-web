// Drives /account: list/add/rename/delete a signed-in user's Claude
// credentials, plus the OAuth or API-key flow used to sign each one in.
//
// All endpoints are scoped to the caller's OIDC sub on the server, so the
// page can't reveal another user's slots even if the client lies about ids.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

  const credList = $("cred-list");
  const addForm = $("add-form");
  const addLabel = $("add-label");
  const addSubmit = $("add-submit");
  const addStatus = $("add-status");
  const addError = $("add-error");

  const signinSection = $("signin-section");
  const signinLabel = $("signin-label");
  const signinClose = $("signin-close");

  const oauthForm = $("oauth-form");
  const oauthStart = $("oauth-start");
  const oauthProgress = $("oauth-progress");
  const oauthStatus = $("oauth-status");
  const oauthLinkBlock = $("oauth-link-block");
  const oauthUrl = $("oauth-url");
  const oauthCodeForm = $("oauth-code-form");
  const oauthCodeInput = $("oauth-code");
  const oauthCodeSubmit = $("oauth-code-submit");
  const oauthCancel = $("oauth-cancel");
  const oauthError = $("oauth-error");

  const apikeyForm = $("apikey-form");
  const apikeyInput = $("apikey-input");
  const apikeyShow = $("apikey-show");
  const apikeySubmit = $("apikey-submit");
  const apikeyStatus = $("apikey-status");
  const apikeyError = $("apikey-error");

  let activeCredId = null;
  // Per-credential endpoints are /api/account/credentials/<id>/...; the
  // shared slot uses the pseudo-id "shared" and its own /api/account/shared/
  // family (admin-gated server-side). Same flow shapes either way, so the
  // rest of this file doesn't care which kind is active.
  function credUrl(id, tail) {
    return id === "shared"
      ? `/api/account/shared/${tail}`
      : `/api/account/credentials/${id}/${tail}`;
  }
  let pollHandle = null;
  let pollInFlight = false;
  let lastOauthFlowStatus = null;
  const POLL_MS = 1500;
  const ACTIVE_STATUSES = new Set(["starting", "awaiting_code", "exchanging"]);

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(el, text) { if (el) el.textContent = text || ""; }
  function showError(el, msg) { setText(el, msg); show(el); }

  function clearOauthState() {
    lastOauthFlowStatus = null;
    setText(oauthStatus, "");
    hide(oauthProgress);
    hide(oauthLinkBlock);
    setText(oauthError, "");
    hide(oauthError);
    oauthStart.disabled = false;
    oauthCodeSubmit.disabled = false;
    if (oauthCodeInput) oauthCodeInput.value = "";
    setText(apikeyStatus, "");
    setText(apikeyError, "");
    hide(apikeyError);
    if (apikeyInput) apikeyInput.value = "";
    if (apikeyShow) apikeyShow.checked = false;
    if (apikeyInput) apikeyInput.type = "password";
    apikeySubmit.disabled = false;
  }

  function openSignin(credId, label) {
    activeCredId = credId;
    setText(signinLabel, label);
    const intro = $("signin-intro");
    if (intro) {
      intro.textContent = credId === "shared"
        ? "Pick whichever flow matches the Claude account you're connecting. "
          + "This is the shared account — everyone on this claude-web uses it."
        : "Pick whichever flow matches the Claude account you're connecting. "
          + "This is your account — only you can use it from claude-web.";
    }
    clearOauthState();
    show(signinSection);
    signinSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeSignin() {
    stopPolling();
    if (activeCredId != null) {
      // Best-effort: cancel any flow we kicked off for this cred so a
      // background driver doesn't keep a stale subprocess alive.
      fetch(credUrl(activeCredId, "oauth/cancel"), { method: "POST" })
        .catch(() => {});
    }
    activeCredId = null;
    hide(signinSection);
  }

  function applyFlowState(flow, onConfigured) {
    if (!flow) return;
    const statusChanged = flow.status !== lastOauthFlowStatus;
    lastOauthFlowStatus = flow.status;
    // stage is set only during the auto-signin driver. When present, it
    // supersedes the generic status line and hides the manual paste-back
    // form (the driver is doing the paste for you).
    const auto = !!flow.stage;
    if (flow.url) {
      oauthUrl.href = flow.url;
      if (!auto) show(oauthLinkBlock); else hide(oauthLinkBlock);
    }
    switch (flow.status) {
      case "starting":
        setText(oauthStatus, "Starting sign-in… (this can take a few seconds)");
        show(oauthProgress);
        oauthStart.disabled = true;
        break;
      case "awaiting_code":
        setText(
          oauthStatus,
          auto
            ? "Signing in automatically: " + flow.stage + "…"
            : "Waiting for the auth code from your browser."
        );
        show(oauthProgress);
        oauthStart.disabled = true;
        oauthCodeSubmit.disabled = false;
        if (statusChanged && !auto && oauthUrl) oauthUrl.focus();
        break;
      case "exchanging":
        setText(
          oauthStatus,
          auto
            ? "Signing in automatically: " + flow.stage + "…"
            : "Exchanging code with Anthropic…"
        );
        show(oauthProgress);
        oauthStart.disabled = true;
        oauthCodeSubmit.disabled = true;
        break;
      case "done":
        setText(oauthStatus, "Signed in.");
        stopPolling();
        if (onConfigured) onConfigured();
        break;
      case "failed":
        setText(oauthStatus, "Sign-in failed.");
        showError(oauthError, flow.error || "Sign-in failed.");
        oauthStart.disabled = false;
        oauthCodeSubmit.disabled = false;
        stopPolling();
        break;
      case "cancelled":
        setText(oauthStatus, "Sign-in cancelled.");
        oauthStart.disabled = false;
        oauthCodeSubmit.disabled = false;
        stopPolling();
        break;
    }
  }

  async function fetchCredStatus(credId) {
    try {
      const r = await fetch(credUrl(credId, "status"));
      if (!r.ok) return null;
      return await r.json();
    } catch { return null; }
  }

  function startPolling(credId) {
    stopPolling();
    pollHandle = setInterval(async () => {
      if (pollInFlight) return;
      pollInFlight = true;
      try {
        const data = await fetchCredStatus(credId);
        if (!data) return;
        if (data.flow) applyFlowState(data.flow, () => onCredConfigured(credId));
        const status = data.flow && data.flow.status;
        if (!status || !ACTIVE_STATUSES.has(status)) {
          stopPolling();
          if (data.credential && data.credential.configured) {
            onCredConfigured(credId);
          }
        }
      } finally {
        pollInFlight = false;
      }
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollHandle != null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
    pollInFlight = false;
  }

  function onCredConfigured() {
    // Refreshing reloads the list with the new "Signed in" state and the
    // right actions wired up. Cheaper than reproducing the server-side
    // render in JS, and the page is small.
    window.location.reload();
  }

  if (oauthForm) {
    oauthForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (activeCredId == null) return;
      setText(oauthError, "");
      hide(oauthError);
      hide(oauthLinkBlock);
      show(oauthProgress);
      setText(oauthStatus, "Starting sign-in…");
      oauthStart.disabled = true;
      const variant = (new FormData(oauthForm)).get("variant") || "claudeai";
      try {
        const r = await fetch(credUrl(activeCredId, "oauth/start"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ variant }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const flow = await r.json();
        applyFlowState(flow, () => onCredConfigured(activeCredId));
        if (ACTIVE_STATUSES.has(flow.status)) startPolling(activeCredId);
      } catch (e) {
        showError(oauthError, "Could not start sign-in: " + e.message);
        oauthStart.disabled = false;
      }
    });
  }

  if (oauthCodeForm) {
    oauthCodeForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (activeCredId == null) return;
      const code = (oauthCodeInput.value || "").trim();
      if (!code) return;
      setText(oauthError, "");
      hide(oauthError);
      oauthCodeSubmit.disabled = true;
      setText(oauthStatus, "Exchanging code with Anthropic…");
      startPolling(activeCredId);
      try {
        const r = await fetch(credUrl(activeCredId, "oauth/code"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        applyFlowState(data.flow, () => onCredConfigured(activeCredId));
        if (data.configured) onCredConfigured(activeCredId);
      } catch (e) {
        showError(oauthError, "Code exchange failed: " + e.message);
        oauthCodeSubmit.disabled = false;
        oauthStart.disabled = false;
        if (oauthCodeInput) { oauthCodeInput.value = ""; oauthCodeInput.focus(); }
      }
    });
  }

  if (oauthCancel) {
    oauthCancel.addEventListener("click", async () => {
      if (activeCredId == null) return;
      stopPolling();
      try {
        await fetch(credUrl(activeCredId, "oauth/cancel"), { method: "POST" });
      } catch (_) {}
      clearOauthState();
    });
  }

  if (apikeyShow) {
    apikeyShow.addEventListener("change", () => {
      apikeyInput.type = apikeyShow.checked ? "text" : "password";
    });
  }

  if (apikeyForm) {
    apikeyForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (activeCredId == null) return;
      setText(apikeyError, "");
      hide(apikeyError);
      const apiKey = (apikeyInput.value || "").trim();
      if (!apiKey) return;
      apikeySubmit.disabled = true;
      setText(apikeyStatus, "Saving…");
      try {
        const r = await fetch(credUrl(activeCredId, "apikey"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        if (data.configured) {
          setText(apikeyStatus, "Saved.");
          onCredConfigured(activeCredId);
        } else {
          showError(apikeyError, "Saved, but the credential still reports as unsigned.");
          apikeySubmit.disabled = false;
        }
      } catch (e) {
        setText(apikeyStatus, "");
        showError(apikeyError, "Save failed: " + e.message);
        apikeySubmit.disabled = false;
      }
    });
  }

  if (signinClose) signinClose.addEventListener("click", closeSignin);

  if (addForm) {
    addForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      setText(addError, "");
      hide(addError);
      const label = (addLabel.value || "").trim();
      if (!label) return;
      addSubmit.disabled = true;
      setText(addStatus, "Creating…");
      try {
        const r = await fetch("/api/account/credentials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        setText(addStatus, "");
        addLabel.value = "";
        openSignin(data.id, data.label);
      } catch (e) {
        setText(addStatus, "");
        showError(addError, "Could not add account: " + e.message);
      } finally {
        addSubmit.disabled = false;
      }
    });
  }

  if (credList) {
    credList.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const credId = btn.getAttribute("data-cred-id");
      if (!credId) return;
      if (btn.classList.contains("cred-setup")) {
        const item = btn.closest(".cred-item");
        const label = item ? item.querySelector(".cred-label").textContent : "";
        openSignin(credId, label);
        return;
      }
      if (btn.classList.contains("cred-auto-signin")) {
        const label = btn.getAttribute("data-label")
          || (btn.closest(".cred-item")?.querySelector(".cred-label")?.textContent ?? "");
        const email = btn.getAttribute("data-auto-email") || "";
        if (!confirm(
              "Automatically sign in \"" + label + "\" via " + email + "? "
              + "This will send one magic-link email to that address."
            )) return;
        openSignin(credId, label);
        setText(oauthStatus, "Starting auto sign-in…");
        show(oauthProgress);
        oauthStart.disabled = true;
        hide(oauthLinkBlock);
        btn.disabled = true;
        try {
          const r = await fetch(credUrl(credId, "oauth/auto_signin"), { method: "POST" });
          const data = await r.json();
          if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
          applyFlowState(data, () => onCredConfigured(credId));
          if (ACTIVE_STATUSES.has(data.status)) startPolling(credId);
        } catch (e) {
          showError(oauthError, "Auto sign-in failed to start: " + e.message);
          oauthStart.disabled = false;
        } finally {
          btn.disabled = false;
        }
        return;
      }
      if (btn.classList.contains("cred-signout")) {
        if (!confirm("Sign this account out? You'll need to sign in again to use it.")) return;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/account/credentials/${credId}/signout`, { method: "POST" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          window.location.reload();
        } catch (e) {
          btn.disabled = false;
          alert("Sign-out failed: " + e.message);
        }
        return;
      }
      if (btn.classList.contains("cred-rename")) {
        const current = btn.getAttribute("data-current-label") || "";
        const next = prompt("New label for this account:", current);
        if (next == null) return;
        const trimmed = next.trim();
        if (!trimmed || trimmed === current) return;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/account/credentials/${credId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ label: trimmed }),
          });
          const data = await r.json();
          if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
          window.location.reload();
        } catch (e) {
          btn.disabled = false;
          alert("Rename failed: " + e.message);
        }
        return;
      }
      if (btn.classList.contains("cred-delete")) {
        const current = btn.getAttribute("data-current-label") || "this account";
        if (!confirm(`Remove "${current}"? Its sign-in will be erased. This can't be undone.`)) return;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/account/credentials/${credId}`, { method: "DELETE" });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          window.location.reload();
        } catch (e) {
          btn.disabled = false;
          alert("Remove failed: " + e.message);
        }
        return;
      }
    });
  }
  // ─── Automatic account switching ────────────────────────────────────────
  // Renders the failover ring as an ordered list with per-row Move up / Move
  // down buttons rather than drag-and-drop: reordering has to be reachable by
  // keyboard and each control has to say what it does out of context, so the
  // buttons carry the account name in their accessible label. Every mutation
  // re-announces the resulting order, because a reordered list is otherwise a
  // silent change to a screen reader.
  const failoverEnabled = $("failover-enabled");
  const failoverPolicy = $("failover-policy");
  const failoverList = $("failover-list");
  const failoverGated = $("failover-gated");
  const failoverStatus = $("failover-status");
  const failoverError = $("failover-error");

  let failoverState = null;

  function orderedSlots(state) {
    // Ring members in priority order, then everything else the user owns.
    const inRing = state.slots.filter((s) => s.in_ring);
    inRing.sort((a, b) => a.rank - b.rank);
    const rest = state.slots.filter((s) => !s.in_ring);
    return inRing.concat(rest);
  }

  function describeSlot(row, position, total) {
    const bits = [];
    if (row.in_ring) bits.push(`position ${position} of ${total}`);
    else bits.push("not used automatically");
    bits.push(`plan ${row.health}`);
    if (row.metered_models && row.metered_models.length) {
      bits.push(`includes ${row.metered_models.join(", ")}`);
    }
    if (!row.configured) bits.push("not signed in");
    return bits.join("; ");
  }

  function renderFailover(state) {
    failoverState = state;
    failoverEnabled.checked = !!state.enabled;
    failoverPolicy.value = state.spend_policy || "free_first";
    const rows = orderedSlots(state);
    const ringCount = rows.filter((r) => r.in_ring).length;
    failoverList.innerHTML = "";
    rows.forEach((row, i) => {
      const li = document.createElement("li");
      const position = row.in_ring ? i + 1 : null;

      const toggle = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !!row.in_ring;
      box.disabled = !row.configured;
      box.setAttribute("data-slot", row.slot);
      box.className = "failover-include";
      toggle.appendChild(box);
      toggle.appendChild(document.createTextNode(` ${row.label}`));
      li.appendChild(toggle);

      const detail = document.createElement("span");
      detail.className = "failover-detail";
      detail.textContent = ` — ${describeSlot(row, position, ringCount)}`;
      li.appendChild(detail);

      if (row.in_ring) {
        [["up", "Move up", i > 0],
         ["down", "Move down", position < ringCount]].forEach(([dir, text, enabled]) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "failover-move";
          b.textContent = text;
          b.setAttribute("data-slot", row.slot);
          b.setAttribute("data-dir", dir);
          // Out-of-context name: "Move up" alone is meaningless in a rotor.
          b.setAttribute("aria-label", `${text}: ${row.label}`);
          b.disabled = !enabled;
          li.appendChild(document.createTextNode(" "));
          li.appendChild(b);
        });
      }
      failoverList.appendChild(li);
    });
    const notes = [];
    notes.push(state.include_all
      ? "Using every subscription account you have. Unticking one below narrows it to just the ones you pick."
      : "Using only the accounts ticked below, in this order.");
    if (state.gated_models && state.gated_models.length) {
      notes.push(`Models only some of your plans include: ${state.gated_models.join(", ")}.`);
    }
    failoverGated.textContent = notes.join(" ");
  }

  function announceOrder(state) {
    const ring = orderedSlots(state).filter((r) => r.in_ring).map((r) => r.label);
    setText(
      failoverStatus,
      ring.length
        ? `Saved. Accounts are tried in this order: ${ring.join(", ")}.`
        : "Saved. No accounts are used automatically.",
    );
  }

  async function saveFailover(fields) {
    hide(failoverError);
    const body = new URLSearchParams(fields);
    try {
      const r = await fetch("/api/account/failover", { method: "POST", body });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      renderFailover(data);
      announceOrder(data);
    } catch (e) {
      setText(failoverError, "Could not save: " + e.message);
      show(failoverError);
    }
  }

  function currentRing(state) {
    return orderedSlots(state).filter((r) => r.in_ring).map((r) => r.slot);
  }

  async function loadFailover() {
    try {
      const r = await fetch("/api/account/failover");
      if (!r.ok) return;
      renderFailover(await r.json());
    } catch (e) {
      /* the section just stays empty; the rest of the page still works */
    }
  }

  if (failoverList) {
    failoverEnabled.addEventListener("change", () => {
      saveFailover({ enabled: failoverEnabled.checked ? "true" : "false" });
    });
    failoverPolicy.addEventListener("change", () => {
      saveFailover({ spend_policy: failoverPolicy.value });
    });
    failoverList.addEventListener("change", (ev) => {
      const box = ev.target.closest(".failover-include");
      if (!box || !failoverState) return;
      const slot = box.getAttribute("data-slot");
      let ring = currentRing(failoverState);
      if (box.checked) { if (!ring.includes(slot)) ring.push(slot); }
      else ring = ring.filter((s) => s !== slot);
      saveFailover({ ring: ring.join(",") });
    });
    failoverList.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".failover-move");
      if (!btn || !failoverState) return;
      const slot = btn.getAttribute("data-slot");
      const ring = currentRing(failoverState);
      const i = ring.indexOf(slot);
      const j = btn.getAttribute("data-dir") === "up" ? i - 1 : i + 1;
      if (i < 0 || j < 0 || j >= ring.length) return;
      [ring[i], ring[j]] = [ring[j], ring[i]];
      saveFailover({ ring: ring.join(",") });
    });
    loadFailover();
  }
})();
