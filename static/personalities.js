"use strict";

(function () {
  // Server-rendered payload — same shape as /api/personalities returns. We
  // read it once for the initial render then fetch fresh data after every
  // mutation so the list stays in sync without a full page reload.
  const dataEl = document.getElementById("personalities-data");
  let payload = {personalities: [], active: null};
  if (dataEl) {
    try { payload = JSON.parse(dataEl.textContent); } catch (_) {}
  }

  const list = document.getElementById("personality-list");
  const editorSection = document.getElementById("editor-section");
  const editorMode = document.getElementById("editor-mode");
  const editorId = document.getElementById("editor-id");
  const editorName = document.getElementById("editor-name");
  const editorDescription = document.getElementById("editor-description");
  const editorPrompt = document.getElementById("editor-prompt");
  const editorForm = document.getElementById("editor-form");
  const editorSave = document.getElementById("editor-save");
  const editorCancel = document.getElementById("editor-cancel");
  const editorError = document.getElementById("editor-error");
  const editorStatus = document.getElementById("editor-status");
  const addBtn = document.getElementById("add-new");

  function findPersonality(id) {
    const num = Number(id);
    return payload.personalities.find((p) => p.id === num);
  }

  function setText(el, text) {
    if (el) el.textContent = text;
  }

  function showError(el, text) {
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
  }

  function openEditor(mode, prefill) {
    editorSection.hidden = false;
    editorMode.textContent = mode === "edit" ? "Edit" : "Add";
    editorId.value = (prefill && mode === "edit") ? prefill.id : "";
    editorName.value = prefill ? (prefill.name || "") : "";
    editorDescription.value = prefill ? (prefill.description || "") : "";
    editorPrompt.value = prefill ? (prefill.system_prompt || "") : "";
    showError(editorError, "");
    setText(editorStatus, "");
    editorName.focus();
    editorSection.scrollIntoView({behavior: "smooth", block: "start"});
  }

  function closeEditor() {
    editorSection.hidden = true;
    editorForm.reset();
    editorId.value = "";
    showError(editorError, "");
    setText(editorStatus, "");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/personalities");
      if (!r.ok) throw new Error("HTTP " + r.status);
      payload = await r.json();
      // Cheap re-render: reload the whole page. Cheaper than maintaining a
      // mirrored DOM, and personality edits are rare enough that the
      // round-trip cost doesn't matter.
      window.location.reload();
    } catch (e) {
      alert("Could not reload personalities: " + e.message);
    }
  }

  if (addBtn) {
    addBtn.addEventListener("click", () => {
      openEditor("add", null);
    });
  }

  if (editorCancel) {
    editorCancel.addEventListener("click", closeEditor);
  }

  if (editorForm) {
    editorForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      showError(editorError, "");
      const id = editorId.value;
      const body = {
        name: editorName.value.trim(),
        description: editorDescription.value.trim(),
        system_prompt: editorPrompt.value,
      };
      if (!body.name) {
        showError(editorError, "Name is required.");
        return;
      }
      editorSave.disabled = true;
      setText(editorStatus, "Saving…");
      try {
        const url = id ? `/api/personalities/${encodeURIComponent(id)}` : "/api/personalities";
        const method = id ? "PATCH" : "POST";
        const r = await fetch(url, {
          method,
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        setText(editorStatus, "Saved.");
        await refresh();
      } catch (e) {
        setText(editorStatus, "");
        showError(editorError, "Save failed: " + e.message);
      } finally {
        editorSave.disabled = false;
      }
    });
  }

  if (list) {
    list.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const id = btn.getAttribute("data-personality-id");
      if (!id) return;
      const personality = findPersonality(id);

      if (btn.classList.contains("personality-activate")) {
        btn.disabled = true;
        try {
          const fd = new FormData();
          fd.append("personality_id", id);
          const r = await fetch("/api/personalities/active", {
            method: "POST",
            body: fd,
          });
          if (!r.ok) {
            const data = await r.json().catch(() => ({}));
            throw new Error(data.detail || `HTTP ${r.status}`);
          }
          await refresh();
        } catch (e) {
          btn.disabled = false;
          alert("Could not switch personality: " + e.message);
        }
        return;
      }

      if (btn.classList.contains("personality-view")) {
        // Toggle the inline prompt preview; lets the user see the actual
        // text of a built-in without opening the editor (and without us
        // pretending built-ins are editable).
        const item = btn.closest(".personality-item");
        const pre = item.querySelector(".personality-prompt");
        if (pre.hidden) {
          pre.textContent = (personality && personality.system_prompt) ||
            "(empty — defers to auto-memory)";
          pre.hidden = false;
          btn.textContent = "Hide";
        } else {
          pre.hidden = true;
          btn.textContent = "View";
        }
        return;
      }

      if (btn.classList.contains("personality-edit")) {
        if (personality) openEditor("edit", personality);
        return;
      }

      if (btn.classList.contains("personality-clone")) {
        if (!personality) return;
        const copy = {
          name: personality.name + " (copy)",
          description: personality.description || "",
          system_prompt: personality.system_prompt || "",
        };
        openEditor("add", copy);
        return;
      }

      if (btn.classList.contains("personality-delete")) {
        if (!personality) return;
        const ok = confirm(`Remove "${personality.name}"? This can't be undone.`);
        if (!ok) return;
        btn.disabled = true;
        try {
          const r = await fetch(`/api/personalities/${encodeURIComponent(id)}`, {
            method: "DELETE",
          });
          if (!r.ok) {
            const data = await r.json().catch(() => ({}));
            throw new Error(data.detail || `HTTP ${r.status}`);
          }
          await refresh();
        } catch (e) {
          btn.disabled = false;
          alert("Delete failed: " + e.message);
        }
        return;
      }
    });
  }
})();
