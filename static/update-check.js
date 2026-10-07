// Surfaces an app update (portable Windows build) and offers a one-click
// install. Kept fully self-contained like cli-check.js: own polling, own
// DOM, no dependency on app.js internals.
//
// States from GET /api/admin/update-app:
//   available  a newer release exists; show it with Install (when this
//              build can self-replace) or a link to the release page.
//   staging    the download is running.
//   staged     downloaded and verified; the server restarts into it once no
//              conversation is mid-turn, so just say so.
//   anything else (current, never, disabled, error, off) hides the banner;
//   errors are logged server-side and must not nag.
(function () {
  "use strict";
  const banner = document.getElementById("update-banner");
  if (!banner) return;
  const title = document.getElementById("update-banner-title");
  const msg = document.getElementById("update-banner-msg");
  const installBtn = document.getElementById("update-install-btn");
  const laterBtn = document.getElementById("update-later-btn");
  const link = document.getElementById("update-release-link");

  const POLL_MS = 30 * 60 * 1000;
  let dismissedTag = null;
  let busy = false;

  function show(state) {
    const tag = (state.available && state.available.tag) || state.staged_tag || "";
    const current = state.current_version ? ` (you have ${state.current_version})` : "";
    if (state.status === "staging") {
      title.textContent = `Downloading claude-web ${tag}`;
      msg.textContent = "The update is being downloaded and verified. Chats keep working meanwhile.";
      installBtn.hidden = true; laterBtn.hidden = true; link.hidden = true;
    } else if (state.status === "staged") {
      title.textContent = `claude-web ${tag} is ready`;
      msg.textContent = "It installs and restarts as soon as no conversation is mid-turn. " +
        "The page reconnects on its own afterwards.";
      installBtn.hidden = true; laterBtn.hidden = true; link.hidden = true;
    } else {
      title.textContent = `claude-web ${tag} is available${current}`;
      if (state.can_self_replace) {
        msg.textContent = "Install downloads it now and restarts once no conversation is mid-turn.";
        installBtn.hidden = false; installBtn.disabled = busy;
      } else {
        msg.textContent = "This copy cannot update itself; download the new version from the release page.";
        installBtn.hidden = true;
      }
      laterBtn.hidden = false;
      if (state.available && state.available.release_url) {
        link.href = state.available.release_url; link.hidden = false;
      } else {
        link.hidden = true;
      }
    }
    banner.hidden = false;
  }

  async function check() {
    let state;
    try {
      const r = await fetch("/api/admin/update-app", { credentials: "same-origin" });
      if (!r.ok) return;  // 403 for non-admins in multi-user mode: nothing to show
      state = await r.json();
    } catch (_) { return; }
    const tag = (state.available && state.available.tag) || state.staged_tag || "";
    if (!["available", "staging", "staged"].includes(state.status)) { banner.hidden = true; return; }
    if (state.status === "available" && dismissedTag && dismissedTag === tag) { banner.hidden = true; return; }
    show(state);
  }

  installBtn.addEventListener("click", async () => {
    busy = true; installBtn.disabled = true;
    try {
      const r = await fetch("/api/admin/update-app/install", { method: "POST", credentials: "same-origin" });
      const state = await r.json();
      if (!r.ok) {
        msg.textContent = `Could not start the update: ${(state && (state.detail || state.error)) || r.status}`;
      } else {
        show(state);
      }
    } catch (e) {
      msg.textContent = `Could not start the update: ${e.message}`;
    } finally {
      busy = false; installBtn.disabled = false;
    }
  });

  laterBtn.addEventListener("click", () => {
    dismissedTag = (title.textContent.match(/claude-web (\S+) is available/) || [])[1] || "x";
    banner.hidden = true;
  });

  check();
  setInterval(check, POLL_MS);
})();
