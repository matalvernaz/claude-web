"use strict";

(function () {
  const list = document.getElementById("skill-list");
  if (!list) return;

  list.addEventListener("change", async (ev) => {
    const cb = ev.target;
    if (!(cb instanceof HTMLInputElement) || !cb.classList.contains("skill-toggle")) return;
    const name = cb.getAttribute("data-name") || "";
    if (!name) return;
    const enabled = cb.checked;
    cb.disabled = true;
    try {
      const body = new URLSearchParams();
      body.set("enabled", enabled ? "true" : "false");
      const r = await fetch("/api/skills/" + encodeURIComponent(name) + "/toggle", {
        method: "POST",
        body,
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      // Reload so the hidden-count and the "hidden" badge re-render
      // consistently — same shortcut personalities.js takes.
      window.location.reload();
    } catch (err) {
      cb.checked = !enabled;
      cb.disabled = false;
      alert("Could not update skill: " + err.message);
    }
  });

  list.addEventListener("click", async (ev) => {
    const btn = ev.target;
    if (!(btn instanceof HTMLButtonElement) || !btn.classList.contains("skill-view")) return;
    const name = btn.getAttribute("data-name") || "";
    if (!name) return;
    const li = btn.closest(".skill-item");
    const body = li ? li.querySelector(".skill-body") : null;
    if (!body) return;
    if (!body.hidden) {
      body.hidden = true;
      btn.textContent = "View SKILL.md";
      return;
    }
    btn.disabled = true;
    try {
      const r = await fetch("/api/skills/" + encodeURIComponent(name) + "/content");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      body.textContent = data.skill_md || "";
      body.hidden = false;
      btn.textContent = "Hide SKILL.md";
    } catch (err) {
      alert("Could not load SKILL.md: " + err.message);
    } finally {
      btn.disabled = false;
    }
  });
})();
