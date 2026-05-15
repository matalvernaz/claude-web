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
  let pollHandle = null;
  let pollInFlight = false;
  const POLL_MS = 1500;
  const ACTIVE_STATUSES = new Set(["starting", "awaiting_code", "exchanging"]);

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(el, text) { if (el) el.textContent = text || ""; }
  function showError(el, msg) { setText(el, msg); show(el); }

  function clearOauthState() {
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
    clearOauthState();
    show(signinSection);
    signinSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeSignin() {
    stopPolling();
    if (activeCredId != null) {
      // Best-effort: cancel any flow we kicked off for this cred so a
      // background driver doesn't keep a stale subprocess alive.
      fetch(`/api/account/credentials/${activeCredId}/oauth/cancel`, { method: "POST" })
        .catch(() => {});
    }
    activeCredId = null;
    hide(signinSection);
  }

  function applyFlowState(flow, onConfigured) {
    if (!flow) return;
    if (flow.url) {
      oauthUrl.href = flow.url;
      show(oauthLinkBlock);
    }
    switch (flow.status) {
      case "starting":
        setText(oauthStatus, "Starting sign-in… (this can take a few seconds)");
        show(oauthProgress);
        oauthStart.disabled = true;
        break;
      case "awaiting_code":
        setText(oauthStatus, "Waiting for the auth code from your browser.");
        show(oauthProgress);
        oauthStart.disabled = true;
        oauthCodeSubmit.disabled = false;
        if (oauthCodeInput && document.activeElement !== oauthCodeInput) {
          oauthCodeInput.focus();
        }
        break;
      case "exchanging":
        setText(oauthStatus, "Exchanging code with Anthropic…");
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
      const r = await fetch(`/api/account/credentials/${credId}/status`);
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
        const r = await fetch(`/api/account/credentials/${activeCredId}/oauth/start`, {
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
        const r = await fetch(`/api/account/credentials/${activeCredId}/oauth/code`, {
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
        await fetch(`/api/account/credentials/${activeCredId}/oauth/cancel`, { method: "POST" });
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
        const r = await fetch(`/api/account/credentials/${activeCredId}/apikey`, {
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
})();
