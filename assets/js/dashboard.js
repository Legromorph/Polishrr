(() => {
  "use strict";

  const state = {
    authenticated: false,
    eventSource: null,
    lastTab: "all",
    lastSort: null,
  };

  const els = {};
  const qs = (selector) => document.querySelector(selector);
  const qsa = (selector) => Array.from(document.querySelectorAll(selector));

  const log = (level, message) => {
    if (!els.logOutput) return;
    const prefixMap = { info: "[i] ", ok: "[ok] ", warn: "[!] ", start: "[*] " };
    els.logOutput.textContent += `${prefixMap[level] ?? "[ ] "}${message}\n`;
    els.logOutput.scrollTop = els.logOutput.scrollHeight;
  };

  const clearNode = (node) => {
    while (node.firstChild) node.removeChild(node.firstChild);
  };

  const createCell = (value, className = "") => {
    const cell = document.createElement("td");
    cell.textContent = value == null ? "-" : String(value);
    if (className) cell.className = className;
    return cell;
  };

  const buildStatusClass = (status) => {
    const normalized = String(status || "").toLowerCase();
    if (normalized.includes("fail")) return "status-failed";
    if (normalized.includes("download")) return "status-downloading";
    if (normalized.includes("complete")) return "status-completed";
    return "";
  };

  async function rawFetch(path, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";

    try {
      const response = await fetch(path, {
        ...options,
        headers,
        credentials: "same-origin",
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!response.ok) {
        const text = await response.text();
        throw new Error(`HTTP ${response.status}: ${text}`);
      }
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) return response.json();
      return response.text();
    } catch (error) {
      clearTimeout(timer);
      throw error;
    }
  }

  function isAuthError(error) {
    return /HTTP 401/.test(String(error?.message || ""));
  }

  async function ensureSession(forcePrompt = false) {
    if (state.authenticated && !forcePrompt) return;

    if (!forcePrompt) {
      try {
        await rawFetch("/api/status");
        state.authenticated = true;
        return;
      } catch (error) {
        if (!isAuthError(error)) throw error;
      }
    }

    const token = window.prompt("Enter your POLISHRR_TOKEN:");
    if (!token) throw new Error("No token provided.");
    await rawFetch("/api/session", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    state.authenticated = true;
  }

  async function apiFetch(path, options = {}, timeoutMs = 15000, retryAuth = true) {
    try {
      return await rawFetch(path, options, timeoutMs);
    } catch (error) {
      if (retryAuth && isAuthError(error)) {
        state.authenticated = false;
        await ensureSession(true);
        return apiFetch(path, options, timeoutMs, false);
      }
      log("warn", `API ${path} failed: ${error.message}`);
      throw error;
    }
  }

  function renderSummary(data) {
    clearNode(els.summary);

    const radarr = data.radarr || {};
    const sonarr = data.sonarr || {};
    const lines = [
      `Radarr: below cutoff ${radarr.total_below_cutoff ?? "-"}, eligible ${radarr.eligible_for_upgrade ?? "-"}`,
      `Sonarr: below cutoff ${sonarr.total_below_cutoff ?? "-"}, eligible ${sonarr.eligible_for_upgrade ?? "-"}`,
    ];

    for (const line of lines) {
      const row = document.createElement("div");
      row.textContent = line;
      els.summary.appendChild(row);
    }
  }

  function applySort(table, index, asc) {
    const rows = Array.from(table.querySelectorAll("tbody tr"));
    rows.sort((left, right) => {
      const a = left.children[index]?.innerText?.toLowerCase() || "";
      const b = right.children[index]?.innerText?.toLowerCase() || "";
      return asc ? a.localeCompare(b) : b.localeCompare(a);
    });
    const tbody = table.querySelector("tbody");
    rows.forEach((row) => tbody.appendChild(row));
  }

  function buildTable(title, headers, rows, sortable = false) {
    const wrapper = document.createElement("div");
    wrapper.className = "queue-table";

    const heading = document.createElement("h3");
    heading.textContent = title;
    wrapper.appendChild(heading);

    const scroll = document.createElement("div");
    scroll.className = "table-body-scroll";
    wrapper.appendChild(scroll);

    const table = document.createElement("table");
    if (sortable) table.classList.add("sortable");
    table.dataset.tableTitle = title;
    scroll.appendChild(table);

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    headers.forEach((header, index) => {
      const th = document.createElement("th");
      th.textContent = header;
      if (sortable) {
        th.className = "sortable-header";
        th.dataset.index = String(index);
      }
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    rows.forEach((row) => tbody.appendChild(row));
    table.appendChild(tbody);

    if (sortable && state.lastSort && state.lastSort.table === title) {
      const { index, asc } = state.lastSort;
      const th = headerRow.children[index];
      if (th) th.classList.add(asc ? "asc" : "desc");
      applySort(table, index, asc);
    }

    return wrapper;
  }

  function buildEligibleRow(item, target) {
    const row = document.createElement("tr");
    if (target === "radarr") {
      row.appendChild(createCell(item.title));
      row.appendChild(createCell(item.status, buildStatusClass(item.status)));
    } else {
      row.appendChild(createCell(item.series));
      row.appendChild(createCell(item.episode));
      row.appendChild(createCell(item.status, buildStatusClass(item.status)));
    }

    const actionCell = document.createElement("td");
    const upgradeButton = document.createElement("button");
    upgradeButton.className = "btn-mini upgrade-btn";
    upgradeButton.dataset.id = String(item.id);
    upgradeButton.dataset.target = target;
    upgradeButton.textContent = "Upgrade";
    actionCell.appendChild(upgradeButton);

    const forceButton = document.createElement("button");
    forceButton.className = "btn-mini force-btn";
    forceButton.dataset.id = String(item.id);
    forceButton.dataset.target = target;
    forceButton.textContent = "Force";
    actionCell.appendChild(forceButton);
    row.appendChild(actionCell);

    return row;
  }

  function buildQueueRow(item, target) {
    const row = document.createElement("tr");
    if (target === "radarr") row.appendChild(createCell(item.title));
    if (target === "sonarr") {
      row.appendChild(createCell(item.series));
      row.appendChild(createCell(item.episode));
    }
    row.appendChild(createCell(item.status, buildStatusClass(item.status)));
    row.appendChild(createCell(`${item.sizeleft ?? "-"} GB`));
    row.appendChild(createCell(item.timeleft));
    row.appendChild(createCell(item.indexer));
    return row;
  }

  async function loadSummary() {
    const data = await apiFetch("/api/upgrade-summary");
    renderSummary(data);
  }

  async function loadQueue(tab = "all") {
    const targetDiv = qs(`#queue-${tab}`);
    if (!targetDiv) return;
    clearNode(targetDiv);

    const query = tab === "tagged" ? "?tagged=true" : "";
    const payload = await apiFetch(`/api/${tab === "eligible" ? "eligible" : `download-queue${query}`}`);
    const tables = [];

    if (tab === "eligible") {
      if (payload.radarr?.length) tables.push(buildTable("Radarr Eligible", ["Title", "Status", "Actions"], payload.radarr.map((item) => buildEligibleRow(item, "radarr")), true));
      if (payload.sonarr?.length) tables.push(buildTable("Sonarr Eligible", ["Series", "Episode", "Status", "Actions"], payload.sonarr.map((item) => buildEligibleRow(item, "sonarr")), true));
      if (!tables.length) targetDiv.textContent = "No eligible items.";
    } else {
      if (payload.radarr?.length) tables.push(buildTable("Radarr Queue", ["Title", "Status", "Left", "Time", "Indexer"], payload.radarr.map((item) => buildQueueRow(item, "radarr"))));
      if (payload.sonarr?.length) tables.push(buildTable("Sonarr Queue", ["Series", "Episode", "Status", "Left", "Time", "Indexer"], payload.sonarr.map((item) => buildQueueRow(item, "sonarr"))));
      if (!tables.length) targetDiv.textContent = "No active downloads.";
    }

    tables.forEach((table) => targetDiv.appendChild(table));
  }

  async function handleUpgrade(target, id, force = false) {
    const endpoint = force ? "/api/force-upgrade-item" : "/api/upgrade-item";
    const action = force ? "Force upgrade" : "Upgrade";
    log("start", `${action} -> ${target} (${id})`);
    await apiFetch(endpoint, { method: "POST", body: JSON.stringify({ target, id }) });
    log("ok", `${action} accepted`);
    await loadQueue(state.lastTab);
  }

  function initEventStream() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = new EventSource("/api/events");
    state.eventSource.addEventListener("info", (event) => log("info", event.data));
    state.eventSource.addEventListener("error", (event) => log("warn", event.data || "Event stream warning"));
    state.eventSource.addEventListener("done", async (event) => {
      log("ok", event.data);
      await loadSummary();
      await loadQueue(state.lastTab);
    });
    state.eventSource.onerror = () => log("warn", "Event stream disconnected.");
  }

  async function triggerUpgrade(target = "both") {
    log("start", `Triggering ${target} upgrade...`);
    await apiFetch("/api/trigger", { method: "POST", body: JSON.stringify({ target }) });
    log("ok", `${target} trigger accepted`);
  }

  async function loadSettings() {
    const settings = await apiFetch("/api/settings");
    qs("#cron-input").value = settings.cron ?? "";
    qs("#chk-radarr").checked = !!settings.process_radarr;
    qs("#chk-sonarr").checked = !!settings.process_sonarr;
    qs("#num-movies").value = settings.num_movies ?? 1;
    qs("#num-episodes").value = settings.num_episodes ?? 1;
    qs("#chk-force").checked = !!settings.force_enabled;
  }

  async function saveCurrentSettings() {
    const settings = {
      cron: qs("#cron-input").value.trim(),
      process_radarr: qs("#chk-radarr").checked,
      process_sonarr: qs("#chk-sonarr").checked,
      num_movies: parseInt(qs("#num-movies").value, 10),
      num_episodes: parseInt(qs("#num-episodes").value, 10),
      force_enabled: qs("#chk-force").checked,
    };
    await apiFetch("/api/settings", { method: "POST", body: JSON.stringify(settings) });
    log("ok", "Settings saved");
  }

  function activateTab(targetId) {
    state.lastTab = targetId.replace("queue-", "");
    qsa(".tab-btn").forEach((button) => button.classList.toggle("active", button.dataset.target === targetId));
    qsa(".queue-tab").forEach((panel) => panel.classList.toggle("active", panel.id === targetId));
    loadQueue(state.lastTab).catch((error) => log("warn", error.message));
  }

  async function initDashboard() {
    els.summary = qs("#upgrade-summary");
    els.logOutput = qs("#log-output");
    els.triggerUpgrade = qs("#trigger-upgrade");
    els.triggerRadarr = qs("#trigger-radarr");
    els.triggerSonarr = qs("#trigger-sonarr");

    await ensureSession();
    initEventStream();

    els.triggerUpgrade?.addEventListener("click", () => triggerUpgrade("both").catch((error) => log("warn", error.message)));
    els.triggerRadarr?.addEventListener("click", () => triggerUpgrade("radarr").catch((error) => log("warn", error.message)));
    els.triggerSonarr?.addEventListener("click", () => triggerUpgrade("sonarr").catch((error) => log("warn", error.message)));
    qs("#save-settings")?.addEventListener("click", () => saveCurrentSettings().catch((error) => log("warn", error.message)));
    qs("#logout-btn")?.addEventListener("click", async () => {
      await rawFetch("/api/logout", { method: "POST" });
      state.authenticated = false;
      if (state.eventSource) state.eventSource.close();
      window.location.reload();
    });

    qsa(".tab-btn").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.target)));

    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      if (target.classList.contains("upgrade-btn")) handleUpgrade(target.dataset.target, target.dataset.id, false).catch((error) => log("warn", error.message));
      if (target.classList.contains("force-btn")) {
        if (window.confirm("Force upgrade deletes the current file first. Continue?")) {
          handleUpgrade(target.dataset.target, target.dataset.id, true).catch((error) => log("warn", error.message));
        }
      }
      if (target.matches("th.sortable-header")) {
        const table = target.closest("table.sortable");
        if (!table) return;
        const index = parseInt(target.dataset.index || "0", 10);
        const asc = !target.classList.contains("asc");
        table.querySelectorAll("th").forEach((th) => th.classList.remove("asc", "desc"));
        target.classList.add(asc ? "asc" : "desc");
        state.lastSort = { table: table.dataset.tableTitle, index, asc };
        applySort(table, index, asc);
      }
    });

    await loadSummary();
    await loadSettings();
    activateTab("queue-all");
    setInterval(() => loadSummary().catch((error) => log("warn", error.message)), 60000);
    setInterval(() => loadQueue(state.lastTab).catch((error) => log("warn", error.message)), 15000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    initDashboard().catch((error) => {
      console.error(error);
      log("warn", error.message || "Dashboard initialization failed.");
    });
  });
})();
