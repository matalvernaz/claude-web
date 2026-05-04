// Drives the /setup page: starts an OAuth flow, surfaces the URL, submits
// the pasted code, and redirects to / once Claude is configured.
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

  function applyFlowState(flow) {
    if (!flow) return;
    if (flow.url) {
      oauthUrl.href = flow.url;
      oauthUrl.textContent = "Open the Claude sign-in page";
      show(oauthLinkBlock);
    }
    switch (flow.status) {
      case "starting":
        setText(oauthStatus, "Starting sign-in…");
        break;
      case "awaiting_code":
        setText(oauthStatus, "Waiting for the auth code from your browser.");
        oauthCodeInput.focus();
        break;
      case "exchanging":
        setText(oauthStatus, "Exchanging code with Anthropic…");
        break;
      case "done":
        setText(oauthStatus, "Signed in. Redirecting…");
        window.location.href = "/";
        break;
      case "failed":
        setText(oauthStatus, "Sign-in failed.");
        showError(oauthError, flow.error || "Sign-in failed.");
        oauthStart.disabled = false;
        break;
      case "cancelled":
        setText(oauthStatus, "Sign-in cancelled.");
        oauthStart.disabled = false;
        break;
    }
  }

  if (oauthForm) {
    oauthForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      clearOauthError();
      hide(oauthLinkBlock);
      show(oauthProgress);
      setText(oauthStatus, "Starting sign-in…");
      oauthStart.disabled = true;

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
