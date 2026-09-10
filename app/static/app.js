"use strict";

const state = {
    page: "dashboard",
    sidebarCollapsed: localStorage.getItem("packagehub-sidebar-collapsed") === "1",
    activeOperations: new Map(),
    searchResults: new Map(),
    availableManagers: []
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function esc(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

async function api(url, options = {}) {
    const { _retried, ...fetchOptions } = options;
    const response = await fetch(url, {
        ...fetchOptions,
        headers: {
            Accept: "application/json",
            ...(fetchOptions.body ? { "Content-Type": "application/json" } : {}),
            ...(fetchOptions.headers || {})
        }
    });

    if (!response.ok) {
        let message = `${response.status} ${response.statusText}`;
        try {
            const body = await response.json();
            message = body.detail || body.message || message;
        } catch (_) {}
        throw new Error(message);
    }

    return response.json();
}

function toast(message, type = "success") {
    const container = $("#toastContainer");
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.textContent = message;
    container.appendChild(node);
    setTimeout(() => node.remove(), 4200);
}

function setLog(text) {
    $("#log").textContent = text || "No operation has been run.";
}

function formatCount(value) {
    return Number(value || 0).toLocaleString();
}

function formatDate(value) {
    if (!value) return "Unknown time";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function emptyState(title, description = "", icon = "○") {
    return `
        <div class="empty-state">
            <div style="width:36px;height:36px;display:grid;place-items:center;border:1px solid var(--border);border-radius:10px;background:var(--surface-3);color:var(--muted);margin-bottom:2px;">
                ${esc(icon)}
            </div>
            <strong>${esc(title)}</strong>
            ${description ? `<p>${esc(description)}</p>` : ""}
        </div>
    `;
}

function managerClass(manager) {
    return String(manager || "").toLowerCase();
}

function managerLetter(manager) {
    const letters = {
        apt: "A",
        snap: "S",
        flatpak: "F",
        dnf: "D",
        pacman: "P",
        zypper: "Z"
    };
    return letters[managerClass(manager)] || "?";
}

function applySidebarState() {
    const sidebar = $("#sidebar");
    sidebar.classList.toggle("collapsed", state.sidebarCollapsed);
    document.documentElement.style.setProperty(
        "--current-sidebar-width",
        state.sidebarCollapsed ? "72px" : "250px"
    );
}

function toggleSidebar() {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    localStorage.setItem("packagehub-sidebar-collapsed", state.sidebarCollapsed ? "1" : "0");
    applySidebarState();
}

$("#sidebarCollapse").addEventListener("click", toggleSidebar);
applySidebarState();

function navigate(page) {
    const allowed = new Set(["dashboard", "packages", "discover", "updates", "history", "maintenance"]);
    if (!allowed.has(page)) return;

    state.page = page;

    $$(".nav-item").forEach(button => {
        button.classList.toggle("active", button.dataset.page === page);
    });

    $$(".page").forEach(section => {
        section.classList.toggle("active", section.id === page);
    });

    const titles = {
        dashboard: "Overview",
        packages: "Packages",
        discover: "Find software",
        updates: "Updates",
        history: "History",
        maintenance: "Maintenance"
    };

    $("#pageTitle").textContent = titles[page];

    if (page === "packages") loadPackages();
    if (page === "discover") loadDiscoverManagers();
    if (page === "updates") loadUpdates();
    if (page === "history") loadHistory();
}

$$(".nav-item").forEach(item => {
    item.addEventListener("click", () => navigate(item.dataset.page));
});

$$("[data-go]").forEach(button => {
    button.addEventListener("click", () => navigate(button.dataset.go));
});

function packageRows(items, includeActions = true) {
    if (!items.length) return emptyState("Nothing to show", "No packages match the current view.", "⌕");

    let html = `
        <div class="package-row head">
            <span>Package</span>
            <span>Manager</span>
            <span>Installed</span>
            <span>Latest</span>
            <span>Status</span>
            <span>Action</span>
        </div>
    `;

    for (const pkg of items) {
        const canUpdate = includeActions && pkg.status === "update_available" && pkg.installed;
        const action = canUpdate
            ? `<div class="row-action"><button class="update-button" data-update-package data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}">Update</button></div>`
            : `<div class="row-action"><span class="status-normal">—</span></div>`;

        html += `
            <div class="package-row" data-package-row data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}" tabindex="0">
                <div class="pkg-name">
                    <div class="pkg-title">
                        <span>${esc(pkg.name)}</span>
                        ${pkg.system ? `<span class="system-badge">system</span>` : ""}
                    </div>
                    <span class="pkg-sub">${esc(pkg.architecture || "package")}</span>
                </div>
                <span class="manager-badge">${esc(pkg.manager)}</span>
                <span class="version">${esc(pkg.version || "—")}</span>
                <span class="latest">${esc(pkg.latest_version || pkg.version || "—")}</span>
                ${pkg.status === "update_available" ? `<span class="status-stable">Stable update</span>` : `<span class="status-normal">Up to date</span>`}
                ${action}
            </div>
        `;
    }

    return html;
}

function historyRows(items) {
    if (!items.length) return emptyState("No updates yet", "Successful package operations will appear here.", "◷");
    return items.map(item => `
        <div class="activity-item">
            <div class="activity-icon">${item.operation === "install" ? "+" : item.operation === "remove" ? "−" : "↑"}</div>
            <div class="activity-main">
                <strong>${esc(item.package || "Package operation")}</strong>
                <small>${esc(item.manager || "system")} · ${esc(item.operation || "update")}</small>
            </div>
            <span class="activity-time">${esc(formatDate(item.finished_at || item.started_at))}</span>
        </div>
    `).join("");
}

function miniUpdates(items) {
    if (!items.length) return emptyState("You're up to date", "No stable updates are currently available.", "✓");
    return items.slice(0, 7).map(pkg => `
        <div class="mini-update">
            <div>
                <div class="mini-name">${esc(pkg.name)}</div>
                <div class="mini-meta">${esc(pkg.manager)} · ${esc(pkg.version || "installed")}</div>
            </div>
            <span class="version-chip">${esc(pkg.latest_version || "update")}</span>
        </div>
    `).join("");
}

async function loadSystem() {
    const data = await api("/api/system");
    const managers = data.managers || {};
    state.availableManagers = Object.entries(managers).filter(([, item]) => item.available).map(([name]) => name);

    const total = Object.values(managers).reduce((sum, item) => sum + Number(item.installed || 0), 0);
    const available = Object.values(managers).filter(item => item.available).length;

    $("#host").textContent = `${data.hostname || "Local system"} · ${data.release || ""}`;
    $("#sidebarHost").textContent = data.hostname || "Local system";

    $("#summary").innerHTML = `
        <article class="metric-card"><small>Installed</small><strong>${formatCount(total)}</strong><span>Packages detected</span></article>
        <article class="metric-card"><small>Managers</small><strong>${formatCount(available)}</strong><span>Available package sources</span></article>
        <article class="metric-card"><small>Updates</small><strong id="metricUpdates">—</strong><span>Stable upgrades available</span></article>
        <article class="metric-card"><small>Mode</small><strong>Local</strong><span>Operations run on this host</span></article>
    `;

    $("#managers").innerHTML = Object.entries(managers).map(([name, item]) => `
        <div class="manager-card">
            <div class="manager-top">
                <div class="manager-logo ${managerClass(name)}">${managerLetter(name)}</div>
                <span class="manager-state">${item.available ? "Available" : "Unavailable"}</span>
            </div>
            <div class="manager-name">${esc(item.display_name || name.toUpperCase())}</div>
            <div class="manager-count">${item.available ? `${formatCount(item.installed)} installed` : "Not installed"}</div>
        </div>
    `).join("");
}

async function loadUpdates() {
    const data = await api("/api/updates");
    const updates = data.updates || [];
    const count = Number(data.count ?? updates.length);

    $("#updateCount").textContent = formatCount(count);
    $("#navUpdateCount").textContent = formatCount(count);
    const metric = $("#metricUpdates");
    if (metric) metric.textContent = formatCount(count);

    $("#updatesTable").innerHTML = packageRows(updates, true);
    $("#dashboardUpdates").innerHTML = miniUpdates(updates);
}

async function loadPackages() {
    const params = new URLSearchParams();
    const query = $("#search").value.trim();
    const manager = $("#managerFilter").value;
    const hideSystem = $("#hideSystem").checked;

    if (query) params.set("q", query);
    if (manager) params.set("manager", manager);
    params.set("hide_system", String(hideSystem));

    try {
        const data = await api(`/api/packages?${params.toString()}`);
        const items = data.packages || [];
        $("#packageCount").textContent = formatCount(data.count || 0);
        $("#packageTableCaption").textContent = `${formatCount(data.count || 0)} packages`;
        $("#packageTable").innerHTML = packageRows(items, true);
    } catch (error) {
        $("#packageTable").innerHTML = emptyState("Unable to load packages", error.message, "!");
        toast(error.message, "error");
    }
}

async function loadHistory() {
    try {
        const data = await api("/api/history");
        const items = data.items || [];
        $("#historyList").innerHTML = historyRows(items);
        $("#dashboardHistory").innerHTML = historyRows(items.slice(0, 6));
    } catch (error) {
        $("#historyList").innerHTML = emptyState("Unable to load history", error.message, "!");
        $("#dashboardHistory").innerHTML = emptyState("Unable to load activity", error.message, "!");
    }
}

function discoverGroup(results) {
    const byManager = new Map();
    for (const result of results) {
        if (!byManager.has(result.manager)) byManager.set(result.manager, []);
        byManager.get(result.manager).push(result);
    }

    if (!byManager.size) {
        return `
            <div class="discover-empty">
                <div class="discover-empty-icon">⌕</div>
                <strong>No matching packages found</strong>
                <p>Try a broader search term or another package source.</p>
            </div>
        `;
    }

    return [...byManager.entries()].map(([manager, items]) => {
        const managerSource = items.map(item => item.source).filter(Boolean)[0] || `${manager} source`;
        return `
            <section class="discover-group">
                <div class="discover-group-header">
                    <div class="discover-group-title">
                        <div class="discover-group-logo ${managerClass(manager)}">${managerLetter(manager)}</div>
                        <div><strong>${esc(manager.toUpperCase())}</strong><small>${esc(managerSource)}</small></div>
                    </div>
                    <span class="discover-group-count">${formatCount(items.length)} results</span>
                </div>
                <div class="discover-list">
                    ${items.map(renderDiscoverCard).join("")}
                </div>
            </section>
        `;
    }).join("");
}

function renderDiscoverCard(pkg) {
    const key = `${pkg.manager}:${pkg.name}:${pkg.source || ""}`;
    const action = pkg.status === "update_available"
        ? `<span class="discover-state update">Update available</span>`
        : pkg.installed
            ? `<span class="discover-state">Installed</span>`
            : `<span class="discover-state available">Available</span>`;

    return `
        <article class="discover-card" data-search-result="${esc(key)}" tabindex="0">
            <div class="discover-main">
                <div class="discover-package-name">${esc(pkg.name)}</div>
                <div class="discover-description">${esc(pkg.description || "No description available.")}</div>
            </div>
            <div class="discover-source">
                <small>Source</small>
                <strong>${esc(pkg.source || "—")}</strong>
            </div>
            <div class="discover-version">
                <small>Latest</small>
                <strong>${esc(pkg.latest_version || pkg.version || "—")}</strong>
            </div>
            <div class="discover-actions">${action}<span style="color:var(--muted);font-size:14px">›</span></div>
        </article>
    `;
}

async function loadDiscoverManagers() {
    try {
        const data = await api("/api/system");
        const managers = data.managers || {};
        const select = $("#discoverManager");
        select.innerHTML = `<option value="">All available managers</option>`;
        for (const [name, item] of Object.entries(managers)) {
            if (!item.available) continue;
            const option = document.createElement("option");
            option.value = name;
            option.textContent = item.display_name || name.toUpperCase();
            select.appendChild(option);
        }
        state.availableManagers = Object.keys(managers).filter(name => managers[name].available);
    } catch (error) {
        toast(error.message, "error");
    }
}

async function performDiscoverSearch() {
    const query = $("#discoverQuery").value.trim();
    const manager = $("#discoverManager").value;
    const resultsContainer = $("#discoverResults");
    const sourceCount = $("#discoverSourceCount");
    const button = $("#discoverSearchButton");

    if (!query) return;

    button.disabled = true;
    button.textContent = "Searching…";
    resultsContainer.innerHTML = `
        <div class="discover-empty">
            <div class="spinner"></div>
            <strong>Searching package sources…</strong>
            <p>Checking every available package manager.</p>
        </div>
    `;

    try {
        const params = new URLSearchParams({ q: query });
        if (manager) params.set("manager", manager);

        const data = await api(`/api/search?${params.toString()}`);
        const results = data.results || [];

        state.searchResults.clear();
        results.forEach(item => state.searchResults.set(`${item.manager}:${item.name}:${item.source || ""}`, item));

        sourceCount.textContent = `${data.managers?.length || 0} sources`;
        resultsContainer.innerHTML = discoverGroup(results);

        if (data.errors?.length) {
            const warning = document.createElement("div");
            warning.className = "info-panel";
            warning.innerHTML = `<div class="info-symbol">!</div><div><strong>Some sources could not be searched</strong><p>${data.errors.map(error => `${esc(error.manager)}: ${esc(error.error)}`).join(" · ")}</p></div>`;
            resultsContainer.prepend(warning);
        }
    } catch (error) {
        resultsContainer.innerHTML = `
            <div class="discover-empty">
                <div class="discover-empty-icon">!</div>
                <strong>Search failed</strong>
                <p>${esc(error.message)}</p>
            </div>
        `;
        toast(error.message, "error");
    } finally {
        button.disabled = false;
        button.textContent = "Search";
    }
}

$("#discoverForm").addEventListener("submit", event => {
    event.preventDefault();
    performDiscoverSearch();
});

$("#openDiscover").addEventListener("click", () => navigate("discover"));

function renderDetailsActions(pkg) {
    if (pkg.status === "update_available" && pkg.installed) {
        return `
            <button class="button primary" data-drawer-action="update" data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}">Update package</button>
            <button class="button secondary danger-action" data-drawer-action="remove" data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}">Remove</button>
        `;
    }

    if (pkg.installed) {
        return `<button class="button secondary danger-action" data-drawer-action="remove" data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}">Remove package</button>`;
    }

    return `<button class="button primary" data-drawer-action="install" data-source-ref="${esc(pkg.source_ref || "")}" data-manager="${esc(pkg.manager)}" data-package="${esc(pkg.name)}">Install package</button>`;
}

async function showDetails(manager, packageName) {
    const drawer = $("#drawer");
    drawer.hidden = false;
    $("#detailName").textContent = packageName;
    $("#detailBody").innerHTML = `<div class="empty-state"><div class="spinner"></div><strong>Loading details…</strong></div>`;

    try {
        const data = await api(`/api/packages/${encodeURIComponent(manager)}/${encodeURIComponent(packageName)}`);
        const pkg = data.package || data;

        $("#detailName").textContent = pkg.name || packageName;
        $("#detailBody").innerHTML = `
            <div class="detail-grid">
                <div class="detail-item"><small>Package manager</small><strong>${esc(pkg.manager || manager)}</strong></div>
                <div class="detail-item"><small>Architecture</small><strong>${esc(pkg.architecture || "—")}</strong></div>
                <div class="detail-item"><small>Installed version</small><strong>${esc(pkg.version || "—")}</strong></div>
                <div class="detail-item"><small>Latest version</small><strong>${esc(pkg.latest_version || pkg.version || "—")}</strong></div>
                <div class="detail-item"><small>Installed size</small><strong>${esc(pkg.size || "—")}</strong></div>
                <div class="detail-item"><small>Installed</small><strong>${pkg.installed ? "Yes" : "No"}</strong></div>
            </div>
            <div class="detail-source-block">
                <strong>${esc(pkg.source || "Package source unavailable")}</strong>
                <small>Source / repository</small>
            </div>
            <div class="detail-status">
                ${pkg.status === "update_available" ? `Stable update available → ${esc(pkg.latest_version)}` : pkg.installed ? "Installed" : "Available to install"}
            </div>
            <div class="detail-description">
                <span class="panel-kicker">DESCRIPTION</span>
                <p>${esc(pkg.description || "No description is available for this package.")}</p>
            </div>
            <div class="detail-actions" id="drawerActions">
                ${renderDetailsActions(pkg)}
            </div>
        `;
    } catch (error) {
        $("#detailBody").innerHTML = emptyState("Unable to load details", error.message, "!");
    }
}

function closeDrawer() {
    $("#drawer").hidden = true;
}

$("#closeDrawer").addEventListener("click", closeDrawer);
$("[data-close-drawer]").addEventListener("click", closeDrawer);

function confirmAction(title, message, callback) {
    const modal = $("#confirmModal");
    const proceed = $("#confirmProceed");
    const cancel = $("#confirmCancel");

    $("#confirmTitle").textContent = title;
    $("#confirmMessage").textContent = message;
    modal.hidden = false;

    const close = () => {
        modal.hidden = true;
        proceed.onclick = null;
        cancel.onclick = null;
    };

    cancel.onclick = close;
    proceed.onclick = async () => {
        close();
        await callback();
    };
}

$("[data-close-modal]").addEventListener("click", () => $("#confirmModal").hidden = true);
$("#confirmCancel").addEventListener("click", () => $("#confirmModal").hidden = true);

function promptSudoAuth() {
    const modal = $("#sudoAuthModal");
    const input = $("#sudoPasswordInput");
    const errorDiv = $("#sudoAuthError");
    const cancelBtn = $("#sudoAuthCancel");
    const form = $("#sudoAuthForm");
    const submitBtn = $("#sudoAuthSubmit");

    input.value = "";
    errorDiv.hidden = true;
    errorDiv.textContent = "";
    submitBtn.disabled = false;
    modal.hidden = false;
    setTimeout(() => input.focus(), 50);

    return new Promise(resolve => {
        const cleanup = () => {
            modal.hidden = true;
            form.onsubmit = null;
            cancelBtn.onclick = null;
            const backdrop = $("[data-close-sudo-modal]");
            if (backdrop) backdrop.onclick = null;
        };

        cancelBtn.onclick = () => {
            cleanup();
            resolve(false);
        };

        const backdrop = $("[data-close-sudo-modal]");
        if (backdrop) backdrop.onclick = () => {
            cleanup();
            resolve(false);
        };

        form.onsubmit = async event => {
            event.preventDefault();
            const password = input.value;
            if (!password) return;
            submitBtn.disabled = true;
            errorDiv.hidden = true;

            try {
                const response = await api("/api/auth/sudo-verify", {
                    method: "POST",
                    body: JSON.stringify({ password })
                });
                if (response.success) {
                    cleanup();
                    resolve(true);
                    return;
                }
                throw new Error("Authentication failed.");
            } catch (error) {
                errorDiv.textContent = error.message || "Incorrect password.";
                errorDiv.hidden = false;
                submitBtn.disabled = false;
                input.value = "";
                input.focus();
            }
        };
    });
}

async function ensureSudoAuth() {
    try {
        const status = await api("/api/auth/sudo-status");
        if (status.authenticated) return true;
    } catch (_) {}
    return promptSudoAuth();
}

function setRowProgress(row, operation) {
    const cell = row?.querySelector(".row-action");
    if (!cell) return;
    cell.innerHTML = `
        <div class="inline-progress">
            <div class="progress-track"><div class="progress-value" style="width:${Math.max(4, Number(operation.progress || 0))}%"></div></div>
            <span class="progress-label">${esc(operation.message || "Starting…")}</span>
        </div>
    `;
}

async function pollOperation(operationId, row = null, onDone = null) {
    if (state.activeOperations.has(operationId)) return;
    state.activeOperations.set(operationId, true);

    try {
        while (true) {
            const data = await api(`/api/operations/${encodeURIComponent(operationId)}`);
            const operation = data.operation;

            if (row) setRowProgress(row, operation);
            if (operation.stdout || operation.stderr) {
                setLog((operation.stdout || "") + (operation.stderr ? `\n${operation.stderr}` : ""));
            }

            if (operation.status === "running") {
                await new Promise(resolve => setTimeout(resolve, 650));
                continue;
            }

            if (operation.status === "success") {
                toast(operation.package ? `${operation.package}: ${operation.operation} completed.` : "Operation completed successfully.");
            } else {
                toast(operation.stderr || "The operation failed.", "error");
            }

            if (onDone) await onDone(operation);
            break;
        }
    } catch (error) {
        toast(error.message, "error");
    } finally {
        state.activeOperations.delete(operationId);
        await refreshAll().catch(() => {});
    }
}

async function startPackageAction(manager, packageName, operation, row = null, sourceRef = null) {
    const key = `${operation}:${manager}:${packageName}`;
    if (state.activeOperations.has(key)) return;

    if (["apt", "snap", "dnf", "pacman", "zypper"].includes(manager)) {
        const authenticated = await ensureSudoAuth();
        if (!authenticated) return;
    }

    state.activeOperations.set(key, true);

    try {
        const endpoint = operation === "install"
            ? "/api/packages/install"
            : operation === "remove"
                ? "/api/packages/remove"
                : "/api/packages/update";

        const result = await api(endpoint, {
            method: "POST",
            body: JSON.stringify({ manager, package: packageName, ...(sourceRef ? { source_ref: sourceRef } : {}) })
        });

        if (row) setRowProgress(row, { progress: 5, message: `Starting ${operation}…` });
        await pollOperation(result.operation_id, row);
    } catch (error) {
        toast(error.message, "error");
    } finally {
        state.activeOperations.delete(key);
    }
}

async function runUpdateAll() {
    const authenticated = await ensureSudoAuth();
    if (!authenticated) return;

    try {
        setLog("Starting stable updates across supported package managers…");
        const result = await api("/api/update-all", { method: "POST" });
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

async function refreshIndexes() {
    const authenticated = await ensureSudoAuth();
    if (!authenticated) return;

    try {
        setLog("Refreshing APT package indexes…");
        const result = await api("/api/refresh", { method: "POST" });
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

async function maintenanceAction(action) {
    if (action === "update-all") return runUpdateAll();

    if (action === "apt-autoremove") {
        const authenticated = await ensureSudoAuth();
        if (!authenticated) return;
    }

    const endpoint = action === "apt-autoremove"
        ? "/api/maintenance/apt-autoremove"
        : "/api/maintenance/flatpak-unused";

    try {
        const result = await api(endpoint, { method: "POST" });
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

async function refreshAll() {
    const jobs = [loadSystem(), loadUpdates(), loadHistory()];
    if (state.page === "packages") jobs.push(loadPackages());
    if (state.page === "discover") jobs.push(loadDiscoverManagers());
    await Promise.allSettled(jobs);
}

/* Package table and discovery event delegation */
document.addEventListener("click", event => {
    const update = event.target.closest("[data-update-package]");
    if (update) {
        event.stopPropagation();
        const row = update.closest("[data-package-row]");
        confirmAction(
            `Update ${update.dataset.package}?`,
            "Only the stable candidate currently offered by the package manager will be installed.",
            () => startPackageAction(update.dataset.manager, update.dataset.package, "update", row)
        );
        return;
    }

    const searchCard = event.target.closest("[data-search-result]");
    if (searchCard) {
        const pkg = state.searchResults.get(searchCard.dataset.searchResult);
        if (pkg) showDetails(pkg.manager, pkg.name);
        return;
    }

    const row = event.target.closest("[data-package-row]");
    if (row && !event.target.closest("button")) {
        showDetails(row.dataset.manager, row.dataset.package);
        return;
    }

    const drawerAction = event.target.closest("[data-drawer-action]");
    if (drawerAction) {
        event.stopPropagation();
        const operation = drawerAction.dataset.drawerAction;
        const manager = drawerAction.dataset.manager;
        const packageName = drawerAction.dataset.package;
        const message = operation === "install"
            ? "The selected package will be installed from the chosen source."
            : operation === "remove"
                ? "The installed package will be removed from this system."
                : "The stable update currently offered by the package manager will be installed.";

        confirmAction(
            `${operation[0].toUpperCase()}${operation.slice(1)} ${packageName}?`,
            message,
            async () => {
                closeDrawer();
                await startPackageAction(manager, packageName, operation, null, drawerAction.dataset.sourceRef || null);
            }
        );
    }
});

document.addEventListener("keydown", event => {
    const row = event.target.closest("[data-package-row]");
    const searchCard = event.target.closest("[data-search-result]");

    if (row && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        showDetails(row.dataset.manager, row.dataset.package);
    }

    if (searchCard && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        const pkg = state.searchResults.get(searchCard.dataset.searchResult);
        if (pkg) showDetails(pkg.manager, pkg.name);
    }

    if (event.key === "Escape") {
        if (!$("#drawer").hidden) closeDrawer();
        if (!$("#confirmModal").hidden) $("#confirmModal").hidden = true;
        if (!$("#sudoAuthModal").hidden) $("#sudoAuthModal").hidden = true;
    }
});

let searchTimer = null;
$("#search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadPackages, 250);
});
$("#managerFilter").addEventListener("change", loadPackages);
$("#hideSystem").addEventListener("change", loadPackages);
$("#refreshPackageTable").addEventListener("click", loadPackages);

$("#refreshIndexes").addEventListener("click", () => {
    confirmAction(
        "Refresh package indexes?",
        "APT package metadata will be refreshed. This requires administrator authentication.",
        refreshIndexes
    );
});

$("#updateAll").addEventListener("click", () => {
    confirmAction(
        "Update all stable packages?",
        "PackageHub will run the available stable updates across supported package managers.",
        runUpdateAll
    );
});

$$("[data-maintenance]").forEach(button => {
    button.addEventListener("click", () => {
        const action = button.dataset.maintenance;
        if (action === "update-all") {
            confirmAction("Update everything?", "Run available stable updates across supported package managers.", runUpdateAll);
        } else if (action === "apt-autoremove") {
            confirmAction("Run APT autoremove?", "APT will remove dependencies it determines are no longer required.", () => maintenanceAction(action));
        } else {
            confirmAction("Clean unused Flatpak runtimes?", "Unused Flatpak runtimes will be removed.", () => maintenanceAction(action));
        }
    });
});

refreshAll().catch(error => {
    setLog(error.message);
    toast(error.message, "error");
});
