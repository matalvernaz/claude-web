// Drives the /setup page: starts an OAuth flow, surfaces the URL, submits
// the pasted code, and redirects to / once Claude is configured. Polls the
// server's flow state so a slow CLI subprocess, a reload mid-flow, or a
// background failure all stay in sync with what the UI is showing.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);

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

  const signoutForm = $("signout-form");
  const signoutSubmit = $("signout-submit");
  const signoutStatus = $("signout-status");

  const whoamiEl = $("whoami");

  // Watch the server's view of the flow while we're in any non-terminal
  // state. Survives reloads (because /api/setup/status is the source of
  // truth) and recovers from server-side failures the start/code endpoints
  // can't surface (driver task crashed after returning, claude CLI died
  // after printing the URL, etc.).
  const POLL_MS = 1500;
  const ACTIVE_STATUSES = new Set(["starting", "awaiting_code", "exchanging"]);
  let pollHandle = null;
  let codeFocused = false;

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(el, text) { if (el) el.textContent = text || ""; }

  function showError(el, msg) {
    setText(el, msg);
    show(el);
  }

  function clearOauthError() {
    setText(oauthError, "");
    hide(oauthError);
  }

  function describeWhoami(w) {
    if (!w || w.mode === "none") return "";
    if (w.mode === "api_key") return "Connected via Anthropic API key.";
    if (w.mode === "oauth") {
      const sub = w.subscription_type;
      return sub
        ? `Connected via Claude OAuth (${sub} subscription).`
        : "Connected via Claude OAuth.";
    }
    return "";
  }

  function applyWhoami(w) {
    if (!whoamiEl) return;
    const text = describeWhoami(w);
    if (!text) { hide(whoamiEl); return; }
    setText(whoamiEl, text);
    show(whoamiEl);
  }

  function applyFlowState(flow) {
    if (!flow) return;
    if (flow.url) {
      oauthUrl.href = flow.url;
      oauthUrl.textContent = "Open the Claude sign-in page";
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
        if (!codeFocused && document.activeElement !== oauthCodeInput) {
          oauthCodeInput.focus();
          codeFocused = true;
        }
        break;
      case "exchanging":
        setText(oauthStatus, "Exchanging code with Anthropic…");
        show(oauthProgress);
        oauthStart.disabled = true;
        oauthCodeSubmit.disabled = true;
        break;
      case "done":
        setText(oauthStatus, "Signed in. Redirecting…");
        stopPolling();
        window.location.href = "/";
        break;
      case "failed":
        setText(oauthStatus, "Sign-in failed.");
        showError(oauthError, flow.error || "Sign-in failed.");
        oauthStart.disabled = false;
        oauthCodeSubmit.disabled = false;
        codeFocused = false;
        stopPolling();
        break;
      case "cancelled":
        setText(oauthStatus, "Sign-in cancelled.");
        oauthStart.disabled = false;
        oauthCodeSubmit.disabled = false;
        codeFocused = false;
        stopPolling();
        break;
    }
  }

  async function fetchStatus() {
    try {
      const r = await fetch("/api/setup/status");
      if (!r.ok) return null;
      return await r.json();
    } catch { return null; }
  }

  function startPolling() {
    if (pollHandle != null) return;
    pollHandle = setInterval(async () => {
      const data = await fetchStatus();
      if (!data) return;
      if (data.whoami) applyWhoami(data.whoami);
      if (data.flow) applyFlowState(data.flow);
      // The /setup page is reachable even when configured (re-auth flow), so
      // only auto-redirect if a flow we're tracking just finished.
      if (data.configured && data.flow && data.flow.status === "done") {
        stopPolling();
        window.location.href = "/";
        return;
      }
      const status = data.flow && data.flow.status;
      if (!status || !ACTIVE_STATUSES.has(status)) {
        stopPolling();
      }
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollHandle != null) {
      clearInterval(pollHandle);
      pollHandle = null;
    }
  }

  // Restore in-progress state on page load. If a flow is still going (the
  // user reloaded after starting it but before pasting the code, or the
  // server is mid-exchange) we re-render the progress UI and rejoin via
  // polling instead of forcing them to start over.
  (async () => {
    const data = await fetchStatus();
    if (!data) return;
    applyWhoami(data.whoami);
    if (data.flow && ACTIVE_STATUSES.has(data.flow.status)) {
      applyFlowState(data.flow);
      startPolling();
    }
  })();

  if (oauthForm) {
    oauthForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      clearOauthError();
      hide(oauthLinkBlock);
      show(oauthProgress);
      setText(oauthStatus, "Starting sign-in…");
      oauthStart.disabled = true;
      codeFocused = false;

      const variant = (new FormData(oauthForm)).get("variant") || "claudeai";
      try {
        const res = await fetch("/api/setup/oauth/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ variant }),
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const flow = await res.json();
        applyFlowState(flow);
        // If the CLI hasn't printed the URL yet (status=="starting"), or it
        // has but the exchange hasn't started, the polling loop will catch
        // up to whatever happens next.
        if (ACTIVE_STATUSES.has(flow.status)) startPolling();
      } catch (e) {
        showError(oauthError, "Could not start sign-in: " + e.message);
        oauthStart.disabled = false;
      }
    });
  }

  if (oauthCodeForm) {
    oauthCodeForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      clearOauthError();
      const code = (oauthCodeInput.value || "").trim();
      if (!code) return;
      oauthCodeSubmit.disabled = true;
      setText(oauthStatus, "Exchanging code with Anthropic…");
      // The server holds the response open up to ~65s for the exchange. If
      // the user reloads in that window we still want to know what
      // happened, so polling stays running until a terminal status lands.
      startPolling();

      try {
        const res = await fetch("/api/setup/oauth/code", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || `HTTP ${res.status}`);
        }
        applyFlowState(data.flow);
        if (data.configured) {
          stopPolling();
          window.location.href = "/";
        }
      } catch (e) {
        showError(oauthError, "Code exchange failed: " + e.message);
        oauthCodeSubmit.disabled = false;
        oauthStart.disabled = false;
      }
    });
  }

  if (oauthCancel) {
    oauthCancel.addEventListener("click", async () => {
      stopPolling();
      try {
        await fetch("/api/setup/oauth/cancel", { method: "POST" });
      } catch (_) { /* ignore */ }
      hide(oauthProgress);
      hide(oauthLinkBlock);
      clearOauthError();
      setText(oauthStatus, "");
      oauthStart.disabled = false;
      oauthCodeSubmit.disabled = false;
      oauthCodeInput.value = "";
      codeFocused = false;
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
      setText(apikeyError, "");
      hide(apikeyError);
      const apiKey = (apikeyInput.value || "").trim();
      if (!apiKey) return;
      apikeySubmit.disabled = true;
      setText(apikeyStatus, "Saving…");

      try {
        const res = await fetch("/api/setup/apikey", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || `HTTP ${res.status}`);
        }
        if (data.configured) {
          setText(apikeyStatus, "Saved. Redirecting…");
          window.location.href = "/";
        } else {
          setText(apikeyStatus, "");
          showError(apikeyError, "Save reported success but Claude still isn't configured.");
          apikeySubmit.disabled = false;
        }
      } catch (e) {
        setText(apikeyStatus, "");
        showError(apikeyError, "Save failed: " + e.message);
        apikeySubmit.disabled = false;
      }
    });
  }

  if (signoutForm) {
    signoutForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      signoutSubmit.disabled = true;
      setText(signoutStatus, "Signing out…");
      try {
        const res = await fetch("/api/setup/signout", { method: "POST" });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        setText(signoutStatus, "Signed out. Reloading…");
        window.location.reload();
      } catch (e) {
        setText(signoutStatus, "Sign-out failed: " + e.message);
        signoutSubmit.disabled = false;
      }
    });
  }
})();
