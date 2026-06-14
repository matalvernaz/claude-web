// Live MCP status panel for the /mcp page. Queries the session-keyed
// /api/chat/mcp/* verbs (get_mcp_status / toggle_mcp_server /
// reconnect_mcp_server) against the user's running conversation and renders
// per-server connection state with enable/disable + reconnect. Bails quietly
// when there's no live conversation (the container isn't rendered then).
(function () {
  "use strict";
  const container = document.getElementById("live-mcp-status");
  if (!container) return;
  const feedback = document.getElementById("live-status-feedback");
  const sessionSelect = document.getElementById("live-session-select");

  let sessions = [];
  try {
    const dataEl = document.getElementById("live-sessions-data");
    sessions = JSON.parse((dataEl && dataEl.textContent) || "[]");
  } catch (e) {
    sessions = [];
  }

  // Re-fill an aria-live region after a clear so NVDA / VoiceOver reliably
  // re-announce even when the text is unchanged.
  function announce(msg) {
    if (!feedback) return;
    feedback.textContent = "";
    setTimeout(() => { feedback.textContent = msg; }, 100);
  }

  function currentSession() {
    if (sessionSelect) return sessionSelect.value;
    return sessions.length ? sessions[0].session_id : "";
  }

  function statusBadge(status) {
    const span = document.createElement("span");
    span.className = status === "connected" ? "badge badge-active" : "badge";
    span.textContent = status || "unknown";
    return span;
  }

  async function load() {
    const sid = currentSession();
    if (!sid) return;
    container.setAttribute("aria-busy", "true");
    container.textContent = "Loading live status…";
    let data;
    try {
      const r = await fetch("/api/chat/mcp/" + encodeURIComponent(sid));
      if (!r.ok) {
        container.textContent =
          "Couldn't load live status (" + r.status + "). The conversation may have ended.";
        container.removeAttribute("aria-busy");
        return;
      }
      data = await r.json();
    } catch (e) {
      container.textContent = "Couldn't load live status: network error.";
      container.removeAttribute("aria-busy");
      return;
    }
    render(data.mcpServers || [], sid);
    container.removeAttribute("aria-busy");
  }

  function render(servers, sid) {
    container.textContent = "";
    if (!servers.length) {
      container.textContent = "No MCP servers attached to this conversation.";
      return;
    }
    const table = document.createElement("table");
    table.className = "mcp-table";
    table.innerHTML =
      "<thead><tr><th>Name</th><th>Status</th><th>Tools</th><th>Actions</th></tr></thead>";
    const tbody = document.createElement("tbody");
    for (const s of servers) {
      const tr = document.createElement("tr");

      const nameTd = document.createElement("td");
      nameTd.textContent = s.name || "(unnamed)";

      const statusTd = document.createElement("td");
      statusTd.appendChild(statusBadge(s.status));
      if (s.error) {
        const err = document.createElement("div");
        err.className = "mcp-addr";
        err.textContent = s.error;
        statusTd.appendChild(err);
      }

      const toolsTd = document.createElement("td");
      toolsTd.textContent = Array.isArray(s.tools) ? String(s.tools.length) : "—";

      const actionsTd = document.createElement("td");
      const enabled = s.status !== "disabled";
      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      toggleBtn.textContent = enabled ? "Disable" : "Enable";
      toggleBtn.setAttribute("aria-label", (enabled ? "Disable " : "Enable ") + (s.name || "server"));
      toggleBtn.addEventListener("click", () => toggle(sid, s.name, !enabled, toggleBtn));
      actionsTd.appendChild(toggleBtn);
      if (s.status === "failed" || s.status === "needs-auth") {
        const reconnectBtn = document.createElement("button");
        reconnectBtn.type = "button";
        reconnectBtn.textContent = "Reconnect";
        reconnectBtn.setAttribute("aria-label", "Reconnect " + (s.name || "server"));
        reconnectBtn.addEventListener("click", () => reconnect(sid, s.name, reconnectBtn));
        actionsTd.appendChild(reconnectBtn);
      }

      tr.append(nameTd, statusTd, toolsTd, actionsTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  async function post(url, fields, btn, busyLabel) {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyLabel;
    try {
      const fd = new FormData();
      for (const k of Object.keys(fields)) fd.append(k, fields[k]);
      const r = await fetch(url, { method: "POST", body: fd });
      if (!r.ok) {
        announce("Action failed (" + r.status + ").");
        btn.disabled = false;
        btn.textContent = original;
        return false;
      }
      return true;
    } catch (e) {
      announce("Action failed: network error.");
      btn.disabled = false;
      btn.textContent = original;
      return false;
    }
  }

  async function toggle(sid, server, enabled, btn) {
    const ok = await post(
      "/api/chat/mcp/toggle",
      { session_id: sid, server: server, enabled: enabled ? "true" : "false" },
      btn, enabled ? "Enabling…" : "Disabling…");
    if (ok) {
      announce(server + (enabled ? " enabled." : " disabled."));
      load();
    }
  }

  async function reconnect(sid, server, btn) {
    const ok = await post(
      "/api/chat/mcp/reconnect",
      { session_id: sid, server: server }, btn, "Reconnecting…");
    if (ok) {
      announce(server + " reconnect requested.");
      load();
    }
  }

  if (sessionSelect) sessionSelect.addEventListener("change", load);
  load();
})();
