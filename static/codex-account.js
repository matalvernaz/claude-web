// OpenAI subscription-account management for /account.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const list = $("codex-cred-list");
  const addForm = $("codex-add-form");
  const addLabel = $("codex-add-label");
  const addSubmit = $("codex-add-submit");
  const addStatus = $("codex-add-status");
  const addError = $("codex-add-error");
  const signinSection = $("codex-signin-section");
  const signinLabel = $("codex-signin-label");
  const signinClose = $("codex-signin-close");
  const loginStart = $("codex-login-start");
  const loginProgress = $("codex-login-progress");
  const loginStatus = $("codex-login-status");
  const deviceBlock = $("codex-device-block");
  const verificationUrl = $("codex-verification-url");
  const deviceCode = $("codex-device-code");
  const copyCode = $("codex-copy-code");
  const loginCancel = $("codex-login-cancel");
  const loginError = $("codex-login-error");

  if (!list || !addForm || !signinSection) return;

  const POLL_MS = 1500;
  let activeCredId = null;
  let activeLoginId = null;
  let pollHandle = null;
  let pollInFlight = false;

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(el, value) { if (el) el.textContent = value || ""; }
  function showError(message) { setText(loginError, message); show(loginError); }

  async function responseData(response) {
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      throw new Error((data && (data.detail || data.error)) || `HTTP ${response.status}`);
    }
    return data || {};
  }

  function credentialUrl(credId, tail) {
    return `/api/account/codex/credentials/${encodeURIComponent(credId)}/${tail}`;
  }

  function stopPolling() {
    if (pollHandle !== null) clearInterval(pollHandle);
    pollHandle = null;
    pollInFlight = false;
  }

  function resetSignin() {
    stopPolling();
    activeLoginId = null;
    setText(loginStatus, "");
    setText(loginError, "");
    hide(loginError);
    hide(loginProgress);
    hide(deviceBlock);
    if (deviceCode) deviceCode.value = "";
    if (verificationUrl) verificationUrl.href = "#";
    loginStart.disabled = false;
    loginCancel.disabled = false;
  }

  function openSignin(credId, label) {
    activeCredId = credId;
    setText(signinLabel, label);
    resetSignin();
    show(signinSection);
    signinSection.scrollIntoView({ behavior: "smooth", block: "start" });
    loginStart.focus();
  }

  async function cancelLogin() {
    if (activeCredId == null || !activeLoginId) return;
    const loginId = activeLoginId;
    activeLoginId = null;
    try {
      await fetch(credentialUrl(activeCredId, "login/cancel"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login_id: loginId }),
      });
    } catch (_) {}
  }

  async function closeSignin() {
    stopPolling();
    await cancelLogin();
    activeCredId = null;
    resetSignin();
    hide(signinSection);
  }

  async function pollStatus() {
    if (pollInFlight || activeCredId == null || !activeLoginId) return;
    pollInFlight = true;
    try {
      const query = new URLSearchParams({ login_id: activeLoginId });
      const response = await fetch(
        credentialUrl(activeCredId, "status") + "?" + query.toString(),
      );
      const data = await responseData(response);
      if (data.credential && data.credential.configured) {
        stopPolling();
        setText(loginStatus, "Signed in. Reloading account list.");
        window.location.reload();
        return;
      }
      if (data.flow && data.flow.status === "failed") {
        stopPolling();
        setText(loginStatus, "Sign-in failed.");
        showError(data.flow.error || "OpenAI sign-in failed.");
        loginStart.disabled = false;
      }
    } catch (error) {
      stopPolling();
      setText(loginStatus, "Could not check sign-in status.");
      showError(error.message);
      loginStart.disabled = false;
    } finally {
      pollInFlight = false;
    }
  }

  function startPolling() {
    stopPolling();
    pollHandle = setInterval(pollStatus, POLL_MS);
  }

  loginStart.addEventListener("click", async () => {
    if (activeCredId == null) return;
    setText(loginError, "");
    hide(loginError);
    show(loginProgress);
    hide(deviceBlock);
    setText(loginStatus, "Starting OpenAI sign-in.");
    loginStart.disabled = true;
    try {
      const response = await fetch(credentialUrl(activeCredId, "login/start"), {
        method: "POST",
      });
      const data = await responseData(response);
      if (!data.login_id || !data.verification_url || !data.user_code) {
        throw new Error("Codex returned an incomplete device-code response.");
      }
      activeLoginId = data.login_id;
      verificationUrl.href = data.verification_url;
      deviceCode.value = data.user_code;
      show(deviceBlock);
      setText(
        loginStatus,
        "Open the OpenAI sign-in page and enter the one-time code shown below.",
      );
      verificationUrl.focus();
      startPolling();
    } catch (error) {
      setText(loginStatus, "Sign-in could not start.");
      showError(error.message);
      loginStart.disabled = false;
    }
  });

  loginCancel.addEventListener("click", async () => {
    stopPolling();
    loginCancel.disabled = true;
    await cancelLogin();
    resetSignin();
    show(loginProgress);
    setText(loginStatus, "Sign-in cancelled.");
  });

  signinClose.addEventListener("click", closeSignin);

  copyCode.addEventListener("click", async () => {
    const value = deviceCode.value || "";
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setText(loginStatus, "One-time code copied.");
    } catch (_) {
      deviceCode.focus();
      deviceCode.select();
      setText(loginStatus, "Code selected. Use your browser's copy command.");
    }
  });

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const label = (addLabel.value || "").trim();
    if (!label) return;
    setText(addError, "");
    hide(addError);
    setText(addStatus, "Creating OpenAI account.");
    addSubmit.disabled = true;
    try {
      const response = await fetch("/api/account/codex/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      const data = await responseData(response);
      addLabel.value = "";
      setText(addStatus, "");
      openSignin(data.id, data.label);
    } catch (error) {
      setText(addStatus, "");
      setText(addError, "Could not add OpenAI account: " + error.message);
      show(addError);
    } finally {
      addSubmit.disabled = false;
    }
  });

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const credId = button.getAttribute("data-codex-cred-id");
    if (!credId) return;
    const item = button.closest(".cred-item");
    const label = item ? item.querySelector(".cred-label").textContent : "";

    if (button.classList.contains("codex-setup")) {
      openSignin(credId, label);
      return;
    }
    if (button.classList.contains("codex-signout")) {
      if (!confirm(`Sign out "${label}"? Active chats using it must be stopped first.`)) return;
      button.disabled = true;
      try {
        const response = await fetch(credentialUrl(credId, "signout"), { method: "POST" });
        await responseData(response);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        alert("OpenAI sign-out failed: " + error.message);
      }
      return;
    }
    if (button.classList.contains("codex-rename")) {
      const current = button.getAttribute("data-current-label") || label;
      const next = prompt("New label for this OpenAI account:", current);
      if (next == null || !next.trim() || next.trim() === current) return;
      button.disabled = true;
      try {
        const response = await fetch(`/api/account/codex/credentials/${credId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: next.trim() }),
        });
        await responseData(response);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        alert("OpenAI account rename failed: " + error.message);
      }
      return;
    }
    if (button.classList.contains("codex-delete")) {
      const current = button.getAttribute("data-current-label") || label;
      if (!confirm(`Remove "${current}"? Its OpenAI sign-in will be erased. This can't be undone.`)) return;
      button.disabled = true;
      try {
        const response = await fetch(`/api/account/codex/credentials/${credId}`, {
          method: "DELETE",
        });
        await responseData(response);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        alert("OpenAI account removal failed: " + error.message);
      }
    }
  });
})();
