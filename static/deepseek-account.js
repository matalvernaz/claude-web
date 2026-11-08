// DeepSeek account management for /account: link, replace, rename and remove API keys.
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const list = $("deepseek-cred-list");
  const addForm = $("deepseek-add-form");
  const addLabel = $("deepseek-add-label");
  const addKey = $("deepseek-add-key");
  const addShow = $("deepseek-add-show");
  const addSubmit = $("deepseek-add-submit");
  const addStatus = $("deepseek-add-status");
  const addError = $("deepseek-add-error");
  const replaceSection = $("deepseek-replace-section");
  const replaceLabel = $("deepseek-replace-label");
  const replaceForm = $("deepseek-replace-form");
  const replaceKey = $("deepseek-replace-key");
  const replaceShow = $("deepseek-replace-show");
  const replaceSubmit = $("deepseek-replace-submit");
  const replaceCancel = $("deepseek-replace-cancel");
  const replaceStatus = $("deepseek-replace-status");
  const replaceError = $("deepseek-replace-error");

  // The section only exists when DeepSeek is turned on for this server.
  if (!list || !addForm) return;

  let replacingCredId = null;

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }
  function setText(el, value) { if (el) el.textContent = value || ""; }

  function showError(el, message) {
    setText(el, message);
    show(el);
  }

  function clearError(el) {
    setText(el, "");
    hide(el);
  }

  async function responseData(response) {
    let data = null;
    try { data = await response.json(); } catch (_) {}
    if (!response.ok) {
      throw new Error((data && (data.detail || data.error)) || `HTTP ${response.status}`);
    }
    return data || {};
  }

  function credentialUrl(credId, tail) {
    const base = `/api/account/deepseek/credentials/${encodeURIComponent(credId)}`;
    return tail ? `${base}/${tail}` : base;
  }

  // A checkbox reveals the key so it can be checked by ear before saving.
  function wireShowKey(checkbox, input) {
    if (!checkbox || !input) return;
    checkbox.addEventListener("change", () => {
      input.type = checkbox.checked ? "text" : "password";
    });
  }

  wireShowKey(addShow, addKey);
  wireShowKey(replaceShow, replaceKey);

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const label = (addLabel.value || "").trim();
    const apiKey = (addKey.value || "").trim();
    if (!label || !apiKey) return;
    clearError(addError);
    setText(addStatus, "Checking the key with DeepSeek.");
    addSubmit.disabled = true;
    try {
      const response = await fetch("/api/account/deepseek/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, api_key: apiKey }),
      });
      await responseData(response);
      addKey.value = "";
      setText(addStatus, "Saved. It's now your default DeepSeek account. Reloading the list.");
      window.location.reload();
    } catch (error) {
      setText(addStatus, "");
      showError(addError, "Couldn't add the DeepSeek account: " + error.message);
      addSubmit.disabled = false;
    }
  });

  function openReplace(credId, label) {
    replacingCredId = credId;
    setText(replaceLabel, label);
    replaceKey.value = "";
    replaceKey.type = "password";
    if (replaceShow) replaceShow.checked = false;
    clearError(replaceError);
    setText(replaceStatus, "");
    replaceSubmit.disabled = false;
    show(replaceSection);
    replaceSection.scrollIntoView({ behavior: "smooth", block: "start" });
    replaceKey.focus();
  }

  function closeReplace() {
    replacingCredId = null;
    replaceKey.value = "";
    hide(replaceSection);
  }

  if (replaceForm) {
    replaceForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const apiKey = (replaceKey.value || "").trim();
      if (replacingCredId == null || !apiKey) return;
      clearError(replaceError);
      setText(replaceStatus, "Checking the key with DeepSeek.");
      replaceSubmit.disabled = true;
      try {
        const response = await fetch(credentialUrl(replacingCredId, "apikey"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey }),
        });
        await responseData(response);
        replaceKey.value = "";
        setText(replaceStatus, "Saved. Chats on this account use the new key from their next message. Reloading the list.");
        window.location.reload();
      } catch (error) {
        setText(replaceStatus, "");
        showError(replaceError, "Couldn't replace the key: " + error.message);
        replaceSubmit.disabled = false;
      }
    });
  }

  if (replaceCancel) replaceCancel.addEventListener("click", closeReplace);

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const credId = button.getAttribute("data-deepseek-cred-id");
    if (!credId) return;
    const item = button.closest(".cred-item");
    const label = item ? item.querySelector(".cred-label").textContent : "";

    if (button.classList.contains("deepseek-replace")) {
      openReplace(credId, label);
      return;
    }
    if (button.classList.contains("deepseek-rename")) {
      const current = button.getAttribute("data-current-label") || label;
      const next = prompt("New name for this DeepSeek account:", current);
      if (next == null || !next.trim() || next.trim() === current) return;
      button.disabled = true;
      try {
        const response = await fetch(credentialUrl(credId), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: next.trim() }),
        });
        await responseData(response);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        alert("Renaming the DeepSeek account failed: " + error.message);
      }
      return;
    }
    if (button.classList.contains("deepseek-delete")) {
      const current = button.getAttribute("data-current-label") || label;
      if (!confirm(`Remove "${current}"? Its key will be erased from this server. This can't be undone.`)) return;
      button.disabled = true;
      try {
        const response = await fetch(credentialUrl(credId), { method: "DELETE" });
        await responseData(response);
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        alert("Removing the DeepSeek account failed: " + error.message);
      }
    }
  });
})();
