"use strict";

const state = {
    page: "dashboard",
    sidebarCollapsed: localStorage.getItem("packagehub-sidebar-collapsed") === "1",
    activeOperations: new Map(),
    operationRows: new Map()
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
    const response = await fetch(url, {
        ...options,
        headers: {
            Accept: "application/json",
            ...(options.body ? { "Content-Type": "application/json" } : {}),
            ...(options.headers || {})
        }
    });

    if (!response.ok) {
        let message = `${response.status} ${response.statusText}`;
        try {
            const body = await response.json();
            message = body.detail || body.message || message;
        } catch (_) { }

        if (response.status === 401 && (message === "sudo_auth_required" || String(message).toLowerCase().includes("sudo"))) {
            if (!options._retried) {
                const authenticated = await promptSudoAuth();
                if (authenticated) {
                    return api(url, { ...options, _retried: true });
                }
            }
        }

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

function emptyState(title, description = "", icon = "○") {
    return `
        <div class="empty-state">
            <div style="width:34px;height:34px;display:grid;place-items:center;border:1px solid var(--border);border-radius:9px;background:var(--surface-3);color:var(--muted);margin-bottom:1px;">
                ${esc(icon)}
            </div>
            <strong>${esc(title)}</strong>
            ${description ? `<p>${esc(description)}</p>` : ""}
        </div>
    `;
}

function managerClass(name) {
    return String(name || "").toLowerCase();
}

function managerLetter(name) {
    const m = managerClass(name);
    return m === "apt" ? "A" : m === "snap" ? "S" : m === "flatpak" ? "F" : "?";
}

function applySidebarState() {
    $("#sidebar").classList.toggle("collapsed", state.sidebarCollapsed);
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

function navigate(page) {
    const allowed = new Set(["dashboard", "packages", "updates", "history", "maintenance"]);
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
        updates: "Updates",
        history: "History",
        maintenance: "Maintenance"
    };

    $("#pageTitle").textContent = titles[page];

    if (page === "packages") loadPackages();
    if (page === "updates") loadUpdates();
    if (page === "history") loadHistory();
}

function formatCount(count) {
    return Number(count || 0).toLocaleString();
}

function formatDate(value) {
    if (!value) return "Unknown time";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function packageRows(items, includeActions = true) {
    if (!items.length) {
        return emptyState("Nothing to show", "No packages match the current filters.", "⌕");
    }

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
        const canUpdate = includeActions && pkg.status === "update_available";
        const action = canUpdate
            ? `
                <div class="row-action">
                    <button class="update-button"
                            data-update-package
                            data-manager="${esc(pkg.manager)}"
                            data-package="${esc(pkg.name)}">
                        Update
                    </button>
                </div>
            `
            : `<div class="row-action"><span class="status-normal">—</span></div>`;

        html += `
            <div class="package-row"
                 data-package-row
                 data-manager="${esc(pkg.manager)}"
                 data-package="${esc(pkg.name)}"
                 tabindex="0">
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
                ${pkg.status === "update_available"
                ? `<span class="status-stable">Stable update</span>`
                : `<span class="status-normal">Up to date</span>`}
                ${action}
            </div>
        `;
    }

    return html;
}

function miniUpdates(items) {
    if (!items.length) {
        return emptyState("You're up to date", "No stable updates are currently available.", "✓");
    }

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

function historyRows(items) {
    if (!items.length) {
        return emptyState("No updates yet", "Successful package updates will appear here.", "◷");
    }

    return items.map(item => `
        <div class="activity-item">
            <div class="activity-icon">↑</div>
            <div class="activity-main">
                <strong>${esc(item.package || "Package update")}</strong>
                <small>${esc(item.manager || "system")} · ${esc(item.operation || "update")}</small>
            </div>
            <span class="activity-time">${esc(formatDate(item.finished_at || item.started_at))}</span>
        </div>
    `).join("");
}

async function loadSystem() {
    const data = await api("/api/system");
    const managers = data.managers || {};
    const total = Object.values(managers).reduce((sum, item) => sum + Number(item.installed || 0), 0);
    const available = Object.values(managers).filter(item => item.available).length;

    $("#host").textContent = `${data.hostname || "Local system"} · ${data.release || ""}`;
    $("#sidebarHost").textContent = data.hostname || "Local system";

    $("#summary").innerHTML = `
        <article class="metric-card">
            <small>Installed</small>
            <strong>${formatCount(total)}</strong>
            <span>Packages detected</span>
        </article>
        <article class="metric-card">
            <small>Managers</small>
            <strong>${available}</strong>
            <span>Available on this host</span>
        </article>
        <article class="metric-card">
            <small>Updates</small>
            <strong id="metricUpdates">—</strong>
            <span>Stable upgrades available</span>
        </article>
        <article class="metric-card">
            <small>Mode</small>
            <strong>Local</strong>
            <span>Operations run on this host</span>
        </article>
    `;

    $("#managers").innerHTML = Object.entries(managers).map(([name, item]) => `
        <div class="manager-card">
            <div class="manager-top">
                <div class="manager-logo ${managerClass(name)}">${managerLetter(name)}</div>
                <span class="manager-state">${item.available ? "Available" : "Unavailable"}</span>
            </div>
            <div class="manager-name">${esc(name.toUpperCase())}</div>
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
    const q = $("#search").value.trim();
    const manager = $("#managerFilter").value;
    const hideSystem = $("#hideSystem").checked;

    if (q) params.set("q", q);
    if (manager) params.set("manager", manager);
    params.set("hide_system", String(hideSystem));

    try {
        const data = await api(`/api/packages?${params.toString()}`);
        $("#packageCount").textContent = formatCount(data.count || 0);
        $("#packageTableCaption").textContent = `${formatCount(data.count || 0)} packages`;
        $("#packageTable").innerHTML = packageRows(data.packages || [], true);
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
        toast(error.message, "error");
    }
}

async function refreshAll() {
    const jobs = [loadSystem(), loadUpdates(), loadHistory()];
    if (state.page === "packages") jobs.push(loadPackages());
    const results = await Promise.allSettled(jobs);
    const failure = results.find(result => result.status === "rejected");
    if (failure) throw failure.reason;
}

function setRowProgress(row, operation) {
    if (!row) return;
    const cell = row.querySelector(".row-action");
    if (!cell) return;

    cell.innerHTML = `
        <div class="inline-progress">
            <div class="progress-track">
                <div class="progress-value" style="width:${Math.max(4, Number(operation.progress || 0))}%"></div>
            </div>
            <span class="progress-label">${esc(operation.message || "Starting…")}</span>
        </div>
    `;
}

async function pollOperation(operationId, row = null) {
    if (state.activeOperations.has(operationId)) return;
    state.activeOperations.set(operationId, true);

    try {
        while (true) {
            const data = await api(`/api/operations/${encodeURIComponent(operationId)}`);
            const operation = data.operation;

            if (row) {
                setRowProgress(row, operation);
            }

            if (operation.stdout || operation.stderr) {
                setLog((operation.stdout || "") + (operation.stderr ? `\n${operation.stderr}` : ""));
            }

            if (operation.status === "running") {
                await new Promise(resolve => setTimeout(resolve, 650));
                continue;
            }

            if (operation.status === "success") {
                toast(operation.package ? `${operation.package} updated successfully.` : "Operation completed successfully.");
            } else {
                const errText = (operation.stderr || operation.stdout || "").toLowerCase();
                if (errText.includes("sudo_auth_required") || errText.includes("password is required") || errText.includes("incorrect password")) {
                    const authed = await promptSudoAuth("Authentication required for sudo operation. Please enter your password:");
                    if (!authed) {
                        toast("Authentication required.", "error");
                    }
                } else {
                    toast(operation.stderr || "The operation failed.", "error");
                }
            }

            break;
        }
    } catch (error) {
        toast(error.message, "error");
    } finally {
        state.activeOperations.delete(operationId);
        await refreshAll().catch(() => { });
    }
}

async function updatePackage(manager, packageName, row) {
    const key = `${manager}:${packageName}`;
    if (state.activeOperations.has(key)) return;

    if (manager === "apt" || manager === "snap") {
        const authed = await ensureSudoAuth();
        if (!authed) return;
    }

    state.activeOperations.set(key, "starting");

    const button = row?.querySelector("[data-update-package]");
    if (button) button.disabled = true;

    try {
        const result = await api("/api/packages/update", {
            method: "POST",
            body: JSON.stringify({ manager, package: packageName })
        });

        state.activeOperations.delete(key);
        state.activeOperations.set(result.operation_id, true);
        if (row) setRowProgress(row, { progress: 5, message: "Starting…" });
        await pollOperation(result.operation_id, row);
    } catch (error) {
        state.activeOperations.delete(key);
        toast(error.message, "error");
        if (button) button.disabled = false;
    }
}

async function runUpdateAll() {
    try {
        const authed = await ensureSudoAuth();
        if (!authed) return;
        setLog("Starting stable updates across supported package managers…");
        const result = await api("/api/update-all", { method: "POST" });
        toast("Update-all operation started.");
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

async function refreshIndexes() {
    try {
        const authed = await ensureSudoAuth();
        if (!authed) return;
        setLog("Refreshing package indexes…");
        const result = await api("/api/refresh", { method: "POST" });
        toast("Index refresh started.");
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

async function maintenanceAction(action) {
    if (action === "update-all") {
        return runUpdateAll();
    }

    if (action === "apt-autoremove") {
        const authed = await ensureSudoAuth();
        if (!authed) return;
    }

    const endpoint = action === "apt-autoremove"
        ? "/api/maintenance/apt-autoremove"
        : "/api/maintenance/flatpak-unused";

    try {
        setLog(`Starting ${action}…`);
        const result = await api(endpoint, { method: "POST" });
        toast("Maintenance operation started.");
        await pollOperation(result.operation_id);
    } catch (error) {
        setLog(error.message);
        toast(error.message, "error");
    }
}

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

function promptSudoAuth(customMessage = null) {
    const modal = $("#sudoAuthModal");
    const input = $("#sudoPasswordInput");
    const errorDiv = $("#sudoAuthError");
    const cancelBtn = $("#sudoAuthCancel");
    const form = $("#sudoAuthForm");
    const submitBtn = $("#sudoAuthSubmit");
    const msgElement = $("#sudoAuthMessage");

    if (customMessage) {
        msgElement.textContent = customMessage;
    } else {
        msgElement.textContent = "Administrator (sudo) privileges are required for this operation. Please enter your password.";
    }

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
        if (backdrop) {
            backdrop.onclick = () => {
                cleanup();
                resolve(false);
            };
        }

        form.onsubmit = async (e) => {
            if (e) e.preventDefault();
            const password = input.value.trim();
            if (!password) return;

            submitBtn.disabled = true;
            errorDiv.hidden = true;

            try {
                const res = await api("/api/auth/sudo-verify", {
                    method: "POST",
                    body: JSON.stringify({ password })
                });
                if (res && res.success) {
                    cleanup();
                    resolve(true);
                } else {
                    errorDiv.textContent = "Authentication failed.";
                    errorDiv.hidden = false;
                    submitBtn.disabled = false;
                }
            } catch (err) {
                errorDiv.textContent = err.message || "Incorrect password. Please try again.";
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
        return await promptSudoAuth();
    } catch (_) {
        return await promptSudoAuth();
    }
}

async function showDetails(manager, packageName) {
    const drawer = $("#drawer");
    drawer.hidden = false;
    $("#detailName").textContent = packageName;
    $("#detailBody").innerHTML = `
        <div class="empty-state">
            <div class="spinner"></div>
            <strong>Loading details…</strong>
        </div>
    `;

    try {
        const data = await api(`/api/packages/${encodeURIComponent(manager)}/${encodeURIComponent(packageName)}`);
        const pkg = data.package || data;
        const stable = pkg.status === "update_available";

        $("#detailName").textContent = pkg.name || packageName;
        $("#detailBody").innerHTML = `
            <div class="detail-grid">
                <div class="detail-item"><small>Package manager</small><strong>${esc(pkg.manager || manager)}</strong></div>
                <div class="detail-item"><small>Architecture</small><strong>${esc(pkg.architecture || "—")}</strong></div>
                <div class="detail-item"><small>Installed version</small><strong>${esc(pkg.version || "—")}</strong></div>
                <div class="detail-item"><small>Latest version</small><strong>${esc(pkg.latest_version || pkg.version || "—")}</strong></div>
                <div class="detail-item"><small>Installed size</small><strong>${esc(pkg.size || "—")}</strong></div>
                <div class="detail-item"><small>Source</small><strong>${esc(pkg.source || pkg.origin || "—")}</strong></div>
            </div>
            <div class="detail-status">
                ${stable ? `Stable update available → ${esc(pkg.latest_version)}` : "No stable update currently available"}
            </div>
            <div class="detail-description">
                <span class="panel-kicker">DESCRIPTION</span>
                <p>${esc(pkg.description || "No description is available for this package.")}</p>
            </div>
            ${stable ? `
                <div class="detail-actions">
                    <button class="button primary" data-drawer-update data-manager="${esc(pkg.manager || manager)}" data-package="${esc(pkg.name || packageName)}">Update package</button>
                </div>
            ` : ""}
        `;
    } catch (error) {
        $("#detailBody").innerHTML = emptyState("Unable to load details", error.message, "!");
        toast(error.message, "error");
    }
}

/* navigation */
$$('.nav-item').forEach(button => button.addEventListener("click", () => navigate(button.dataset.page)));
$$('[data-go]').forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));

/* sidebar */
$("#sidebarCollapse").addEventListener("click", toggleSidebar);
applySidebarState();

/* filters */
let searchTimer = null;
$("#search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadPackages(), 220);
});
$("#managerFilter").addEventListener("change", loadPackages);
$("#hideSystem").addEventListener("change", loadPackages);
$("#refreshPackageTable").addEventListener("click", loadPackages);

/* top actions */
$("#refreshIndexes").addEventListener("click", () => confirmAction(
    "Refresh package indexes?",
    "This is the only normal dashboard action that refreshes APT metadata and may request administrator authentication.",
    refreshIndexes
));
$("#updateAll").addEventListener("click", () => confirmAction(
    "Update all stable packages?",
    "PackageHub will apply the currently available stable updates across supported package managers.",
    runUpdateAll
));

/* maintenance */
$$('[data-maintenance]').forEach(button => button.addEventListener("click", () => {
    const action = button.dataset.maintenance;
    const title = action === "update-all" ? "Update all stable packages?" : "Run maintenance?";
    const message = action === "apt-autoremove"
        ? "APT will remove packages it considers no longer required."
        : action === "flatpak-unused"
            ? "Unused Flatpak runtimes will be removed."
            : "Available stable updates will be applied across supported package managers.";

    confirmAction(title, message, () => maintenanceAction(action));
}));

/* drawer */
$("#closeDrawer").addEventListener("click", () => { $("#drawer").hidden = true; });
$("[data-close-drawer]").addEventListener("click", () => { $("#drawer").hidden = true; });
$("[data-close-modal]").addEventListener("click", () => { $("#confirmModal").hidden = true; });

/* table interactions */
document.addEventListener("click", event => {
    const updateButton = event.target.closest("[data-update-package]");
    if (updateButton) {
        event.stopPropagation();
        const row = updateButton.closest("[data-package-row]");
        confirmAction(
            `Update ${updateButton.dataset.package}?`,
            "Only the stable candidate currently offered by the package manager will be installed.",
            () => updatePackage(updateButton.dataset.manager, updateButton.dataset.package, row)
        );
        return;
    }

    const drawerButton = event.target.closest("[data-drawer-update]");
    if (drawerButton) {
        const manager = drawerButton.dataset.manager;
        const pkg = drawerButton.dataset.package;
        $("#drawer").hidden = true;
        confirmAction(
            `Update ${pkg}?`,
            "The currently available stable package version will be installed.",
            async () => {
                const row = $$('[data-package-row]').find(node => node.dataset.manager === manager && node.dataset.package === pkg);
                await updatePackage(manager, pkg, row || null);
            }
        );
        return;
    }

    const row = event.target.closest("[data-package-row]");
    if (row && !event.target.closest("button")) {
        showDetails(row.dataset.manager, row.dataset.package);
    }
});

document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
        $("#drawer").hidden = true;
        $("#confirmModal").hidden = true;
        const sudoModal = $("#sudoAuthModal");
        if (sudoModal && !sudoModal.hidden) {
            $("#sudoAuthCancel").click();
        }
    }

    const row = event.target.closest("[data-package-row]");
    if (row && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        showDetails(row.dataset.manager, row.dataset.package);
    }
});

/* initial */
api("/api/auth/sudo-clear", { method: "POST" }).catch(() => { });
window.addEventListener("beforeunload", () => {
    try { navigator.sendBeacon("/api/auth/sudo-clear"); } catch (_) { }
});

refreshAll().catch(error => {
    setLog(error.message);
    toast(error.message, "error");
});
