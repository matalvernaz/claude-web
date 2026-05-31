// Surfaces a missing `claude` CLI in the UI and offers a one-click install.
// The CLI is the Node binary the Agent SDK shells out to for every model
// turn; without it the app loads but every chat turn fails. Kept fully
// self-contained (own polling, own DOM, no dependency on app.js internals)
// so it can't entangle the main chat module.
(function () {
  "use strict";
  const banner = document.getElementById("cli-banner");
  if (!banner) return;
  const msg = document.getElementById("cli-banner-msg");
  const installBtn = document.getElementById("cli-install-btn");
  const recheckBtn = document.getElementById("cli-recheck-btn");
  const logEl = document.getElementById("cli-log");

  let polling = false;

  function describe(status) {
    if (status.npm_present) {
      return "claude-web needs the Claude command-line tool to reach the model. " +
        "Node.js is already installed, so this is a one-step install.";
    }
    if (status.node_installer) {
      return "claude-web needs the Claude command-line tool, which runs on Node.js. " +
        "Node.js isn't installed — the button will install Node.js (via " +
        status.node_installer + ") and then the Claude CLI. This can take a few minutes.";
    }
    return "claude-web needs the Claude command-line tool, which runs on Node.js. " +
      "Node.js isn't installed and there's no automated installer for this " +
      "platform. Install Node.js from https://nodejs.org/, then click Re-check.";
  }

  async function checkStatus() {
    let status;
    try {
      const r = await fetch("/api/claude-cli/status", { credentials: "same-origin" });
      if (!r.ok) return;
      status = await r.json();
    } catch (_) { return; }
    if (status.cli_present) { banner.hidden = true; return; }
    msg.textContent = describe(status);
    // No automated path => the button can't help; leave only Re-check + the
    // manual nodejs.org instruction in the message.
    installBtn.hidden = !status.npm_present && !status.node_installer;
    installBtn.disabled = false;
    banner.hidden = false;
  }

  function appendLog(lines) {
    if (!lines || !lines.length) return;
    logEl.hidden = false;
    logEl.textContent = lines.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function poll() {
    let s;
    try {
      const r = await fetch("/api/claude-cli/install/status", { credentials: "same-origin" });
      s = await r.json();
    } catch (_) {
      polling = false;
      installBtn.disabled = false;
      msg.textContent = "Lost contact with the install. Click Re-check.";
      return;
    }
    appendLog(s.log);
    if (s.state === "running") {
      setTimeout(poll, 1500);
      return;
    }
    polling = false;
    installBtn.disabled = false;
    if (s.cli_present) {
      msg.textContent = "Claude CLI installed. You're ready to chat.";
      installBtn.hidden = true;
      recheckBtn.hidden = true;
      setTimeout(() => { banner.hidden = true; }, 4000);
    } else if (s.error) {
      msg.textContent = "Install failed: " + s.error;
    } else {
      msg.textContent = "Install finished. Click Re-check to confirm.";
    }
  }

  async function startInstall() {
    if (polling) return;
    installBtn.disabled = true;
    logEl.hidden = false;
    logEl.textContent = "Starting install…";
    msg.textContent = "Installing — this can take a few minutes. Leave this page open.";
    let data;
    try {
      const r = await fetch("/api/claude-cli/install", {
        method: "POST", credentials: "same-origin",
      });
      data = await r.json();
      if (!r.ok) {
        installBtn.disabled = false;
        msg.textContent = "Couldn't start install: " + (data.detail || r.status);
        return;
      }
    } catch (_) {
      installBtn.disabled = false;
      msg.textContent = "Couldn't start the install. Check your connection.";
      return;
    }
    if (data.already_present) {
      installBtn.disabled = false;
      await checkStatus();
      return;
    }
    polling = true;
    poll();
  }

  installBtn.addEventListener("click", startInstall);
  recheckBtn.addEventListener("click", () => { logEl.hidden = true; checkStatus(); });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkStatus);
  } else {
    checkStatus();
  }
})();
