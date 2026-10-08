import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const BASE = "/robot";
const pages = ["Files", "Downloads", "History", "Models", "Workflows", "Outputs", "Node Installer", "Backup & Restore", "Tasks", "Terminal", "Trash", "Settings"];
const state = {
    page: "Files", root: "output", path: "", roots: [], entries: [], selected: new Set(),
    history: [], historyIndex: -1, clipboard: null, view: "list", sort: "name", source: null,
    importId: null, importSummary: null, tasks: [], currentWorkflow: null, modelTab: "Installed",
    taskFilter: "all", logFilter: "all",
    treeExpanded: new Set(), treeFolders: new Map(), resourceRoot: "output", resourcePath: "", statsBusy: false, pollBusy: false,
};
let shell;

function element(tag, className = "", value = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value) node.textContent = value;
    return node;
}

function $(selector) { return shell.querySelector(selector); }
function bytes(value) {
    if (value == null) return "—";
    if (value < 1024) return `${value} B`;
    const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
    return `${(value / 1024 ** unit).toFixed(1)} ${["B", "KB", "MB", "GB", "TB"][unit]}`;
}
function date(value) { return value ? new Date(value * 1000).toLocaleString() : "—"; }
function pathOf(name) { return [state.path, name].filter(Boolean).join("/"); }
function rootOptions() {
    const names = { comfy: "ComfyUI", models: "Models", output: "Output", input: "Input", temp: "Temp",
        user: "User", custom_nodes: "Custom nodes" };
    return state.roots.map(item => [item.id, item.path === "/" ? "Linux filesystem (/)" :
        names[item.id] || (item.id.startsWith("model:") ?
            `Model folder: ${item.id.split(":")[1]} (${Number(item.id.split(":")[2]) + 1})` : item.id.replace(/^extra:/, ""))]);
}
function url(path, params = {}) { return api.apiURL(`${BASE}${path}${Object.keys(params).length ? "?" + new URLSearchParams(params) : ""}`); }
async function request(path, data) {
    const options = data === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
    const response = await api.fetchApi(`${BASE}${path}`, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
}
async function act(callback) {
    try { await callback(); }
    catch (error) { notify(error.message, true); }
}
function notify(message, error = false) {
    const box = $(".rfm-toast");
    box.textContent = message;
    box.classList.toggle("error", error);
    box.hidden = false;
    clearTimeout(box.timer);
    box.timer = setTimeout(() => { box.hidden = true; }, 5000);
}
function button(label, callback, className = "") {
    const node = element("button", `rfm-button ${className}`, label);
    node.type = "button";
    node.addEventListener("click", callback);
    return node;
}
function input(type, placeholder = "", value = "") {
    const node = element("input", "rfm-input");
    node.type = type;
    node.placeholder = placeholder;
    node.value = value;
    return node;
}
function select(options, chosen) {
    const node = element("select", "rfm-input");
    for (const [value, label] of options) {
        const option = element("option", "", label);
        option.value = value;
        option.selected = value === chosen;
        node.append(option);
    }
    return node;
}
function check(label, checked = false) {
    const wrapper = element("label", "rfm-check");
    const box = input("checkbox");
    box.checked = checked;
    wrapper.append(box, document.createTextNode(label));
    return [wrapper, box];
}
function section(title) {
    const node = element("section", "rfm-card");
    node.append(element("h2", "", title));
    return node;
}
function empty(parent, message) { parent.replaceChildren(element("p", "rfm-empty", message)); }
function confirmAction(message) { return window.confirm(message); }

function chooseDeletion(label) {
    return new Promise(resolve => {
        const cover = element("div", "rfm-picker-cover");
        const panel = section(`Delete ${label}?`);
        panel.classList.add("rfm-picker"); panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
        panel.setAttribute("aria-label", `Delete ${label}`);
        panel.append(element("p", "rfm-muted", "Choose how to delete. Files sent to Trash can be restored from the Trash tab."));
        const finish = value => { cover.remove(); document.removeEventListener("keydown", onKey); resolve(value); };
        const onKey = event => { if (event.key === "Escape") { event.stopPropagation(); finish(null); } };
        const actions = element("div", "rfm-toolbar rfm-wrap");
        const trash = button("Send to Trash", () => finish(false), "rfm-primary");
        actions.append(trash, button("Delete permanently", () => finish(true), "rfm-danger"), button("Cancel", () => finish(null)));
        panel.append(actions); cover.append(panel); shell.append(cover);
        document.addEventListener("keydown", onKey); trash.focus();
    });
}

function queueNotice(response) {
    notify(`${response.tasks.length} download(s) queued${response.skipped?.length ? `; ${response.skipped.length} skipped: ${response.skipped[0].reason}` : ""}${response.errors.length ? `; ${response.errors.length} rejected: ${response.errors[0].error}` : ""}`, !!response.errors.length);
}

async function refreshResources(refreshModels = false) {
    if (state.statsBusy) return;
    state.statsBusy = true;
    const root = state.resourceRoot, path = state.resourcePath;
    try {
        const data = await request(`/system/stats?${new URLSearchParams({ root, path, refresh_models: refreshModels ? "1" : "0" })}`);
        if (root !== state.resourceRoot || path !== state.resourcePath) return;
        const percent = value => `${Math.round(value)}%`;
        const paint = (name, label, value, detail) => {
            const cell = $(`[data-resource="${name}"]`);
            cell.querySelector("strong").textContent = label;
            cell.querySelector("small").textContent = detail;
            cell.querySelector("progress").value = value;
        };
        paint("disk", `Disk ${percent(data.disk.used / data.disk.total * 100)}`, data.disk.used / data.disk.total * 100,
            `${bytes(data.disk.used)} used · ${bytes(data.disk.free)} free · ${bytes(data.disk.total)} total`);
        paint("cpu", `CPU ${percent(data.cpu)}`, data.cpu, "Server processor usage");
        paint("ram", `RAM ${percent(data.ram.percent)}`, data.ram.percent, `${bytes(data.ram.used)} / ${bytes(data.ram.total)}`);
        $(".rfm-model-total").textContent = `${bytes(data.models.size)} · ${data.models.count} models`;
        $(".rfm-resources").title = `Updated ${new Date().toLocaleTimeString()} · ComfyUI server resources`;
        const known = state.roots.find(item => item.id === root);
        if (known) Object.assign(known, data.disk);
        const usage = $(".rfm-folder-storage");
        if (usage && state.root === root) usage.textContent = `${bytes(data.disk.free)} free of ${bytes(data.disk.total)}`;
    } catch (error) {
        $(".rfm-resources").title = `Resource update failed: ${error.message}`;
    } finally { state.statsBusy = false; }
}

async function pickFolder(initialRoot, initialPath, callback) {
    const cover = element("div", "rfm-picker-cover");
    const panel = section("Choose destination folder");
    panel.classList.add("rfm-picker"); cover.append(panel); shell.append(cover);
    const controls = element("div", "rfm-toolbar");
    const root = select(rootOptions(), initialRoot);
    const current = element("span", "rfm-grow rfm-muted");
    controls.append(root, button("↑", () => act(() => open(root.value, path.split("/").slice(0, -1).join("/")))), current);
    panel.append(controls);
    const folders = element("div", "rfm-picker-list"); panel.append(folders);
    panel.append(button("New folder", () => act(async () => {
        const name = prompt("New folder name");
        if (!name) return;
        await request("/files/mkdir", { root: root.value, path, name });
        await open(root.value, [path, name].filter(Boolean).join("/"));
    })));
    const footer = element("div", "rfm-toolbar");
    footer.append(button("Use this folder", () => { callback(root.value, path); cover.remove(); }, "rfm-primary"),
        button("Cancel", () => cover.remove()));
    panel.append(footer);
    let path = initialPath;
    root.onchange = () => act(() => open(root.value, ""));
    async function open(rootName, relative) {
        const data = await request(`/files/list?${new URLSearchParams({ root: rootName, path: relative })}`);
        path = relative;
        root.value = rootName;
        current.textContent = `${rootName}/${path}`;
        folders.replaceChildren();
        for (const item of data.entries.filter(item => item.folder)) {
            folders.append(button(`📁 ${item.name}`, () => act(() => open(rootName, [path, item.name].filter(Boolean).join("/"))), "rfm-picker-item"));
        }
        if (!folders.children.length) empty(folders, "No subfolders here.");
    }
    try { await open(initialRoot, initialPath); }
    catch (error) {
        try { await open(initialRoot, ""); }
        catch { cover.remove(); throw error; }
    }
}

async function init() {
    const data = await request("/files/roots");
    state.roots = data.roots;
    buildShell();
    await navigate(state.roots.some(item => item.id === "output") ? "output" : state.roots[0].id, "", false);
    const pendingInstall = localStorage.getItem("Robot.FileManager.pendingInstall");
    showPage(pendingInstall ? "Node Installer" : "Files");
    if (pendingInstall) {
        shell.hidden = false;
        localStorage.removeItem("Robot.FileManager.pendingInstall");
        notify("ComfyUI restarted. Installation status is shown below.");
    }
    setInterval(() => { if (shell && (!shell.hidden || state.installTask && state.installAutoRestart && !state.installHandled) && !state.pollBusy && !state.restarting) act(async () => {
        state.pollBusy = true;
        try {
            await refreshTasks(state.page === "Tasks");
            if (!shell.hidden) {
                await refreshResources();
                if (state.page === "Downloads" && state.drawDownloadQueue) await state.drawDownloadQueue();
                if (state.page === "Terminal" && state.drawTerminal) await state.drawTerminal();
            }
        } finally { state.pollBusy = false; }
    }); }, 2000);
}

function buildShell() {
    shell = element("div", "rfm-shell");
    shell.hidden = true;
    shell.innerHTML = `
        <div class="rfm-frame">
          <header class="rfm-header"><div><strong>Robot File Manager</strong><small>ComfyUI workspace</small></div><span class="rfm-status"></span><button class="rfm-close" aria-label="Close">×</button></header>
          <div class="rfm-resources" aria-label="Server resources">
            <div class="rfm-storage-root"></div>
            <div class="rfm-resource rfm-resource-disk" data-resource="disk"><strong>Disk</strong><progress max="100" value="0"></progress><small>Loading storage…</small></div>
            <div class="rfm-resource" data-resource="cpu"><strong>CPU</strong><progress max="100" value="0"></progress><small>Loading…</small></div>
            <div class="rfm-resource" data-resource="ram"><strong>RAM</strong><progress max="100" value="0"></progress><small>Loading…</small></div>
            <div class="rfm-resource"><strong>Total model size</strong><span class="rfm-model-total">Loading…</span></div>
          </div>
          <div class="rfm-layout"><nav class="rfm-nav"></nav><main class="rfm-main"></main></div>
          <div class="rfm-toast" hidden></div>
        </div>`;
    $(".rfm-close").onclick = () => { shell.hidden = true; };
    const diskRoot = select(rootOptions(), state.resourceRoot);
    diskRoot.title = "Storage location"; diskRoot.setAttribute("aria-label", "Storage location");
    diskRoot.onchange = () => { state.resourceRoot = diskRoot.value; state.resourcePath = ""; act(() => refreshResources()); };
    $(".rfm-storage-root").append(diskRoot);
    const nav = $(".rfm-nav");
    for (const page of pages) {
        const item = button(page, () => showPage(page), "rfm-nav-item");
        item.dataset.page = page;
        nav.append(item);
    }
    document.body.append(shell);
    const launch = button("", () => { shell.hidden = !shell.hidden; if (!shell.hidden) { showPage(state.page); act(() => refreshResources(true)); } }, "rfm-sidebar-launch");
    launch.title = "Open Robot File Manager";
    launch.setAttribute("aria-label", "Files");
    launch.innerHTML = '<span class="rfm-sidebar-content"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2M3 7h18l-2 13H3z"/></svg><span>Files</span></span>';
    const placeLaunch = () => {
        const templates = document.querySelector(".templates-tab-button");
        if (!templates || templates.nextElementSibling === launch) return;
        launch.className = `${templates.className.replace("templates-tab-button", "")} rfm-sidebar-launch`;
        templates.after(launch);
    };
    new MutationObserver(placeLaunch).observe(document.body, { childList: true, subtree: true });
    placeLaunch();
}

function showPage(page) {
    state.page = page;
    for (const node of shell.querySelectorAll(".rfm-nav-item")) node.classList.toggle("active", node.dataset.page === page);
    const main = $(".rfm-main");
    main.onkeydown = null;
    main.replaceChildren();
    if (page === "Files" || page === "Outputs") {
        if (page === "Outputs" && state.root !== "output") navigate("output", "");
        renderFiles();
    } else if (page === "Downloads") renderDownloads();
    else if (page === "History") act(renderHistory);
    else if (page === "Models") act(renderModels);
    else if (page === "Workflows") act(renderWorkflows);
    else if (page === "Node Installer") act(renderInstaller);
    else if (page === "Backup & Restore") renderBackup();
    else if (page === "Tasks") act(() => refreshTasks(true));
    else if (page === "Terminal") act(renderTerminal);
    else if (page === "Trash") act(renderTrash);
    else if (page === "Settings") act(renderSettings);
}

async function navigate(root, path, record = true) {
    const data = await request(`/files/list?${new URLSearchParams({ root, path })}`);
    state.root = root;
    state.path = path;
    state.entries = data.entries;
    state.resourceRoot = root;
    state.resourcePath = path;
    $(".rfm-storage-root select").value = root;
    state.treeFolders.set(JSON.stringify([root, path]), data.entries.filter(item => item.folder));
    const parts = path.split("/").filter(Boolean);
    for (let i = 0; i <= parts.length; i++) state.treeExpanded.add(JSON.stringify([root, parts.slice(0, i).join("/")]));
    state.selected.clear();
    if (record) {
        state.history = state.history.slice(0, state.historyIndex + 1);
        state.history.push({ root, path });
        state.historyIndex = state.history.length - 1;
    } else if (!state.history.length) {
        state.history = [{ root, path }];
        state.historyIndex = 0;
    }
    if (shell && (state.page === "Files" || state.page === "Outputs")) renderFiles();
    if (!shell.hidden) act(() => refreshResources());
}

async function drawFolderTree(tree) {
    const renderId = tree.renderId = (tree.renderId || 0) + 1;
    tree.replaceChildren(element("h3", "", "This PC"));
    const labels = new Map(rootOptions());
    async function branch(parent, root, path, name, depth) {
        if (!tree.isConnected || tree.renderId !== renderId) return;
        const key = JSON.stringify([root, path]);
        const row = element("div", `rfm-tree-row ${state.root === root && state.path === path ? "active" : ""}`);
        row.style.paddingLeft = `${8 + depth * 14}px`;
        const expanded = state.treeExpanded.has(key);
        const toggle = button(expanded ? "▾" : "▸", () => act(async () => {
            if (state.treeExpanded.has(key)) state.treeExpanded.delete(key);
            else { state.treeExpanded.add(key); state.treeFolders.delete(key); }
            await drawFolderTree(tree);
        }), "rfm-tree-toggle");
        toggle.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${name}`);
        toggle.setAttribute("aria-expanded", String(expanded));
        const open = button(`📁 ${name}`, () => act(() => navigate(root, path)), "rfm-tree-name");
        open.title = `${root}/${path}`;
        row.append(toggle, open); parent.append(row);
        if (!expanded) return;
        let folders = state.treeFolders.get(key);
        if (!folders) {
            try {
                const data = await request(`/files/list?${new URLSearchParams({ root, path })}`);
                folders = data.entries.filter(item => item.folder);
                state.treeFolders.set(key, folders);
            } catch { row.title = "Folder is unavailable"; return; }
        }
        if (!row.isConnected) return;
        for (const folder of folders) await branch(parent, root, [path, folder.name].filter(Boolean).join("/"), folder.name, depth + 1);
    }
    for (const root of state.roots.filter(item => !item.id.startsWith("model:"))) await branch(tree, root.id, "", labels.get(root.id), 0);
    if (!tree.isConnected || tree.renderId !== renderId) return;
    const models = element("details", "rfm-tree-models");
    models.append(element("summary", "", "Configured model locations")); tree.append(models);
    models.open = state.root.startsWith("model:");
    models.ontoggle = () => { if (models.open && models.children.length === 1) act(async () => {
        for (const root of state.roots.filter(item => item.id.startsWith("model:"))) await branch(models, root.id, "", labels.get(root.id), 0);
    }); };
}

function selectedItems() { return [...state.selected].map(path => ({ root: state.root, path })); }
function renderFiles() {
    const main = $(".rfm-main");
    main.replaceChildren();
    const title = element("div", "rfm-titlebar");
    title.append(element("h1", "", state.page === "Outputs" ? "Output Browser" : "Files"));
    const usage = state.roots.find(item => item.id === state.root);
    if (usage) title.append(element("span", "rfm-muted rfm-folder-storage", `${bytes(usage.free)} free of ${bytes(usage.total)}`));
    main.append(title);
    const toolbar = element("div", "rfm-toolbar");
    const roots = select(rootOptions(), state.root);
    roots.title = "Allowed root";
    roots.onchange = () => act(() => navigate(roots.value, ""));
    toolbar.append(roots);
    toolbar.append(button("←", () => act(() => history(-1)), "rfm-icon"));
    toolbar.append(button("→", () => act(() => history(1)), "rfm-icon"));
    toolbar.append(button("↑", () => act(() => navigate(state.root, state.path.split("/").slice(0, -1).join("/"))), "rfm-icon"));
    toolbar.append(button("↻", () => act(() => navigate(state.root, state.path, false)), "rfm-icon"));
    const crumbs = element("div", "rfm-crumbs");
    crumbs.append(button(state.root, () => act(() => navigate(state.root, "")), "rfm-crumb"));
    let current = "";
    for (const part of state.path.split("/").filter(Boolean)) {
        current = [current, part].filter(Boolean).join("/");
        const to = current;
        crumbs.append(element("span", "rfm-muted", "/"), button(part, () => act(() => navigate(state.root, to)), "rfm-crumb"));
    }
    toolbar.append(crumbs);
    main.append(toolbar);
    const tools = element("div", "rfm-toolbar rfm-wrap");
    const search = input("search", "Search this folder");
    search.oninput = () => drawEntries();
    search.dataset.role = "search";
    tools.append(search);
    const sort = select([["name", "Name"], ["modified", "Modified"], ["size", "Size"], ["type", "Type"]], state.sort);
    sort.onchange = () => { state.sort = sort.value; drawEntries(); };
    tools.append(sort);
    const filter = select([["all", "All files"], ["folder", "Folders"], ["image", "Images"], ["video", "Videos"], ["model", "Models"]], "all");
    filter.dataset.role = "filter";
    filter.onchange = drawEntries;
    tools.append(filter);
    let extension = null, after = null, before = null;
    if (state.page === "Outputs") {
        extension = input("text", "Extension, e.g. png"); extension.classList.add("rfm-short"); extension.oninput = drawEntries;
        after = input("date"); after.title = "Modified on or after"; after.onchange = drawEntries;
        before = input("date"); before.title = "Modified on or before"; before.onchange = drawEntries;
        tools.append(extension, after, before);
    }
    tools.append(button(state.view === "grid" ? "List" : "Grid", () => { state.view = state.view === "grid" ? "list" : "grid"; renderFiles(); }));
    tools.append(button("New folder", () => act(async () => { const name = prompt("Folder name"); if (!name) return; await request("/files/mkdir", { root: state.root, path: state.path, name }); await navigate(state.root, state.path, false); })));
    tools.append(button("New file", () => act(async () => { const name = prompt("Text filename (.txt, .md, .json, .csv, .yaml)"); if (!name) return; await request("/files/create", { root: state.root, path: state.path, name }); await navigate(state.root, state.path, false); })));
    tools.append(button("Upload from PC", () => $(".rfm-upload").click()));
    tools.append(button("Extract ZIP…", () => act(extractSelected)));
    tools.append(button("Install custom node…", () => showPage("Node Installer")));
    const upload = input("file"); upload.multiple = true; upload.classList.add("rfm-upload"); upload.hidden = true;
    upload.onchange = () => act(async () => { await uploadFiles(upload.files); upload.value = ""; await navigate(state.root, state.path, false); });
    tools.append(upload);
    main.append(tools);
    const drop = element("div", "rfm-drop-zone", `Drop files or ZIP archives from your PC here → ${state.root}/${state.path || ""}`);
    drop.append(element("small", "", "ZIP uploads are kept as archives. Select a ZIP and choose Extract ZIP to unpack it into a folder."));
    drop.ondragover = event => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; drop.classList.add("dragging"); };
    drop.ondragleave = () => drop.classList.remove("dragging");
    drop.ondrop = event => {
        event.preventDefault(); drop.classList.remove("dragging");
        const files = [...event.dataTransfer.files];
        act(async () => { await uploadFiles(files); await navigate(state.root, state.path, false); });
    };
    main.append(drop);
    const chosen = element("div", "rfm-toolbar rfm-selection");
    main.append(chosen);
    const explorer = element("div", "rfm-explorer");
    const tree = element("aside", "rfm-folder-tree"); tree.setAttribute("aria-label", "Folder navigation");
    const content = element("div", "rfm-explorer-content");
    const list = element("div", "rfm-file-list");
    const footer = element("div", "rfm-folder-summary");
    content.append(list, footer); explorer.append(tree, content); main.append(explorer);
    act(() => drawFolderTree(tree));
    main.tabIndex = 0;
    main.onkeydown = event => {
        if (event.target.closest("input, select, textarea") || $(".rfm-picker-cover")) return;
        const key = event.key.toLowerCase();
        const control = event.ctrlKey || event.metaKey;
        let action;
        if (control && key === "a") action = () => { state.selected = new Set(state.entries.map(item => pathOf(item.name))); drawEntries(); };
        else if (control && key === "c") action = () => clipboard("copy");
        else if (control && key === "x") action = () => clipboard("move");
        else if (control && key === "v") action = paste;
        else if (key === "delete") action = deleteSelected;
        else if (key === "f2") action = renameSelected;
        else if (event.altKey && key === "arrowleft") action = () => history(-1);
        else if (event.altKey && key === "arrowright") action = () => history(1);
        if (action) { event.preventDefault(); act(action); }
    };
    function drawEntries() {
        chosen.replaceChildren();
        const all = check(`${state.selected.size} selected`);
        all[1].checked = state.entries.length > 0 && state.selected.size === state.entries.length;
        all[1].onchange = () => { state.selected = all[1].checked ? new Set(state.entries.map(item => pathOf(item.name))) : new Set(); drawEntries(); };
        chosen.append(all[0]);
        chosen.append(button("Copy", () => clipboard("copy")), button("Cut", () => clipboard("move")));
        chosen.append(button("Paste", () => act(paste), state.clipboard ? "" : "rfm-disabled"));
        chosen.append(button("Rename", () => act(renameSelected)));
        chosen.append(button("Duplicate", () => act(duplicateSelected)));
        chosen.append(button("Delete", () => act(deleteSelected), "rfm-danger"));
        chosen.append(button("Download", () => act(downloadSelected)));
        chosen.append(button("ZIP selected", () => act(zipSelected)));
        let entries = state.entries.filter(item => item.name.toLowerCase().includes(search.value.toLowerCase()));
        const kind = filter.value;
        if (kind === "folder") entries = entries.filter(item => item.folder);
        if (kind === "image") entries = entries.filter(item => /\.(png|jpe?g|webp|gif)$/i.test(item.name));
        if (kind === "video") entries = entries.filter(item => /\.(mp4|webm|mov|mkv)$/i.test(item.name));
        if (kind === "model") entries = entries.filter(item => /\.(safetensors|ckpt|pt|gguf)$/i.test(item.name));
        if (extension?.value.trim()) entries = entries.filter(item => item.extension === `.${extension.value.trim().toLowerCase().replace(/^\./, "")}`);
        if (after?.value) entries = entries.filter(item => item.modified >= new Date(`${after.value}T00:00:00`).getTime() / 1000);
        if (before?.value) entries = entries.filter(item => item.modified <= new Date(`${before.value}T23:59:59`).getTime() / 1000);
        entries.sort((a, b) => a.folder !== b.folder ? (a.folder ? -1 : 1) :
            state.sort === "modified" ? b.modified - a.modified : state.sort === "size" ? (b.size || 0) - (a.size || 0) :
            state.sort === "type" ? a.extension.localeCompare(b.extension) : a.name.localeCompare(b.name));
        list.replaceChildren();
        list.className = `rfm-file-list ${state.view === "grid" ? "grid" : ""}`;
        const selectedSize = state.entries.filter(item => state.selected.has(pathOf(item.name))).reduce((sum, item) => sum + (item.size || 0), 0);
        footer.textContent = `${entries.length} item(s) · ${bytes(entries.reduce((sum, item) => sum + (item.size || 0), 0))} in files${state.selected.size ? ` · ${state.selected.size} selected (${bytes(selectedSize)})` : ""}`;
        if (state.view !== "grid") {
            const header = element("div", "rfm-file rfm-file-header");
            header.append(element("span"), element("span"));
            for (const [label, key] of [["Name", "name"], ["Date modified", "modified"], ["Type", "type"], ["Size", "size"]])
                header.append(button(label, () => { state.sort = key; sort.value = key; drawEntries(); }, "rfm-column-heading"));
            list.append(header);
        }
        if (!entries.length) { empty(list, "This folder is empty or no items match the filter."); return; }
        for (const item of entries) {
            const relative = pathOf(item.name);
            const row = element("div", `rfm-file ${state.selected.has(relative) ? "selected" : ""}`);
            const box = input("checkbox"); box.checked = state.selected.has(relative);
            box.onchange = () => { box.checked ? state.selected.add(relative) : state.selected.delete(relative); drawEntries(); };
            row.append(box);
            if (state.view === "grid" && state.root === "output" && /\.(png|jpe?g|webp|gif)$/i.test(item.name)) {
                const picture = element("img", "rfm-thumb");
                picture.src = url("/files/preview", { root: state.root, path: relative });
                picture.loading = "lazy";
                row.append(picture);
            } else if (state.view === "grid" && state.root === "output" && /\.(mp4|webm)$/i.test(item.name)) {
                const video = element("video", "rfm-thumb");
                video.src = url("/files/preview", { root: state.root, path: relative });
                video.controls = true;
                video.preload = "metadata";
                row.append(video);
            } else row.append(element("span", "rfm-file-icon", item.folder ? "📁" : "📄"));
            const name = button(item.name, () => {
                if (item.folder) return act(() => navigate(state.root, relative));
                state.selected.has(relative) ? state.selected.delete(relative) : state.selected.add(relative); drawEntries();
            }, "rfm-name");
            name.title = item.name;
            row.ondblclick = () => { if (item.folder) act(() => navigate(state.root, relative)); };
            row.append(name, element("span", "rfm-muted rfm-file-date", date(item.modified)),
                element("span", "rfm-muted rfm-file-type", item.folder ? "File folder" : (item.extension.slice(1).toUpperCase() || "File")),
                element("span", "rfm-muted rfm-file-size", item.folder ? "—" : bytes(item.size)));
            row.addEventListener("contextmenu", event => { event.preventDefault(); state.selected = new Set([relative]); drawEntries(); showContext(event.clientX, event.clientY, item); });
            list.append(row);
        }
    }
    drawEntries();
}

async function history(delta) {
    const index = state.historyIndex + delta;
    if (index < 0 || index >= state.history.length) return;
    state.historyIndex = index;
    await navigate(state.history[index].root, state.history[index].path, false);
}
function clipboard(action) {
    if (!state.selected.size) return notify("Select files first", true);
    state.clipboard = { action, sources: selectedItems() };
    notify(`${state.selected.size} item(s) ready to ${action}`);
}
async function paste() {
    if (!state.clipboard) return;
    await request("/files/action", { action: state.clipboard.action, sources: state.clipboard.sources, destination: { root: state.root, path: state.path } });
    state.clipboard = null;
    notify("Transfer queued");
    await navigate(state.root, state.path, false);
}
async function renameSelected() {
    if (state.selected.size !== 1) return notify("Select one item to rename", true);
    const source = selectedItems()[0];
    const name = prompt("New name", source.path.split("/").pop());
    if (!name) return;
    await request("/files/action", { action: "rename", sources: [source], destination: { root: state.root, path: state.path }, name });
    notify("Rename queued");
    setTimeout(() => act(() => navigate(state.root, state.path, false)), 500);
}
async function duplicateSelected() {
    if (state.selected.size !== 1) return notify("Select one item to duplicate", true);
    const source = selectedItems()[0];
    const name = prompt("Copy name", `Copy of ${source.path.split("/").pop()}`);
    if (!name) return;
    await request("/files/action", { action: "duplicate", sources: [source], destination: { root: state.root, path: state.path }, name });
    notify("Duplicate queued");
}
async function deleteSelected() {
    if (!state.selected.size) return;
    const sources = selectedItems();
    const permanent = await chooseDeletion(`${sources.length} selected item(s)`);
    if (permanent === null) return;
    await request("/files/action", { action: "delete", sources, permanent });
    notify("Delete queued");
    setTimeout(() => act(() => navigate(state.root, state.path, false)), 500);
}
async function downloadSelected() {
    if (state.selected.size !== 1) return notify("Select one file, or ZIP selected outputs", true);
    window.open(url("/files/download", { root: state.root, path: [...state.selected][0] }), "_blank", "noopener");
}
async function zipSelected() {
    if (!state.selected.size) return notify("Select files first", true);
    const result = await request("/files/zip", { root: state.root, paths: [...state.selected] });
    notify(`ZIP task ${result.task.slice(0, 8)} queued. Download it from Tasks when complete.`);
}
async function uploadFiles(files) {
    if (!files.length) return;
    const data = new FormData();
    for (const file of files) data.append("file", file, file.name);
    const response = await api.fetchApi(`/robot/files/upload?${new URLSearchParams({ root: state.root, path: state.path })}`, { method: "POST", body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Upload failed");
    notify(`${result.uploaded.length} file(s) uploaded to ${state.root}/${state.path}`);
    await refreshResources(true);
}

async function extractSelected() {
    if (state.selected.size !== 1 || ![...state.selected][0].toLowerCase().endsWith(".zip")) return notify("Select one ZIP file to extract", true);
    await extractZip(state.root, [...state.selected][0]);
}

async function extractZip(root, path) {
    await pickFolder(root, path.split("/").slice(0, -1).join("/"), (destination_root, destination_folder) => act(async () => {
        await request("/files/extract", { root, path, destination_root, destination_folder });
        notify(`ZIP extraction queued into ${destination_root}/${destination_folder}`);
    }));
}
function showContext(x, y, item) {
    document.querySelector(".rfm-context")?.remove();
    const menu = element("div", "rfm-context");
    menu.style.left = `${Math.min(x, window.innerWidth - 180)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - 190)}px`;
    if (item.folder) menu.append(button("Open", () => act(() => navigate(state.root, pathOf(item.name)))));
    else menu.append(button("Download", () => act(downloadSelected)));
    menu.append(button("Calculate size", () => act(async () => {
        await request("/files/size", { root: state.root, path: pathOf(item.name) });
        notify("Size calculation queued; see Tasks for the result");
    })));
    if (!item.folder && item.name.toLowerCase().endsWith(".zip")) menu.append(button("Extract ZIP…", () => act(() => extractZip(state.root, pathOf(item.name)))));
    menu.append(button("Copy", () => clipboard("copy")), button("Cut", () => clipboard("move")),
        button("Rename", () => act(renameSelected)), button("Delete", () => act(deleteSelected)));
    shell.append(menu);
    setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
}

function renderDownloads() {
    const main = $(".rfm-main"); main.replaceChildren();
    main.append(element("h1", "", "Server Downloads"));
    main.append(element("p", "rfm-muted", "Paste a direct HTTPS URL, Hugging Face repository or file, or Civitai model link. Files download on the ComfyUI server."));
    const source = section("Find files");
    const line = element("div", "rfm-toolbar");
    const value = input("url", "https://… or owner/repository"); value.classList.add("rfm-grow");
    line.append(value, button("Inspect source", () => act(async () => {
        const result = await request("/source/info", { value: value.value.trim() });
        if (value.value.includes("?")) value.value = "";
        state.source = result;
        drawSource();
    }), "rfm-primary"));
    source.append(line);
    const results = element("div", "rfm-source-results"); source.append(results);
    main.append(source);
    const queue = section("Queue");
    const queueActions = element("div", "rfm-toolbar");
    queueActions.append(button("Retry failed", () => act(async () => {
        const result = await request("/tasks/bulk", { action: "retry_failed_downloads" });
        notify(`${result.count} failed download(s) retried`); await drawQueue();
    })), button("Clear completed", () => act(async () => {
        if (!confirmAction("Remove completed downloads from task history?")) return;
        const result = await request("/tasks/bulk", { action: "clear_completed_downloads" });
        notify(`${result.count} completed download(s) removed`); await drawQueue();
    })));
    queue.append(queueActions);
    const list = element("div", "rfm-task-list"); queue.append(list); main.append(queue);
    function drawSource() {
        results.replaceChildren();
        const info = state.source;
        if (!info) return;
        results.append(element("h3", "", `${info.platform}: ${info.model_name || "Direct file"}`));
        if (!info.files.length) { empty(results, "No files returned by the source."); return; }
        const controls = element("div", "rfm-toolbar rfm-wrap");
        const destination = select(rootOptions(), "models");
        destination.title = "Download to allowed root";
        const folder = input("text", "None — use root folder", "");
        folder.title = "Leave empty to save directly in the selected root";
        const showStorage = () => {
            state.resourceRoot = destination.value; state.resourcePath = folder.value.trim();
            $(".rfm-storage-root select").value = destination.value; act(() => refreshResources());
        };
        destination.onchange = () => { folder.value = ""; showStorage(); };
        folder.onchange = showStorage;
        showStorage();
        const browse = button("Browse…", () => act(() => pickFolder(destination.value, folder.value, (chosenRoot, chosenPath) => {
            destination.value = chosenRoot; folder.value = chosenPath; showStorage();
        })));
        const selectAll = check("Select all files");
        controls.append(element("span", "rfm-muted", "Download to"), destination, folder, browse, selectAll[0]);
        results.append(controls);
        const rows = element("div", "rfm-download-files");
        const choices = [];
        for (const file of info.files) {
            const row = element("div", "rfm-download-row");
            const box = input("checkbox"); box.checked = !!file.selected || info.files.length === 1;
            const filename = input("text", "Filename", file.name || "download");
            filename.title = "Saved filename";
            row.append(box, filename, element("span", "rfm-muted", bytes(file.size)));
            if (file.version) row.append(element("span", "rfm-muted", file.version));
            rows.append(row);
            choices.push({ file, box, filename });
        }
        selectAll[1].onchange = () => { for (const item of choices) item.box.checked = selectAll[1].checked; };
        results.append(rows);
        results.append(button("Queue selected downloads", () => act(async () => {
            const selected = choices.filter(item => item.box.checked);
            if (!selected.length) return notify("Select at least one file", true);
            const items = selected.map(item => ({ url: item.file.url, root: destination.value, folder: folder.value.trim(),
                filename: item.filename.value.trim(), model_type: info.model_type, model_name: info.model_name,
                sha256: item.file.sha256 }));
            const response = await request("/download/queue", { items });
            queueNotice(response);
            await drawQueue();
        }), "rfm-primary"));
    }
    async function drawQueue() {
        const data = await request("/tasks");
        const downloads = data.tasks.filter(item => item.kind === "download");
        list.replaceChildren();
        if (!downloads.length) { empty(list, "No downloads yet."); return; }
        for (const task of downloads) list.append(taskRow(task));
    }
    state.drawDownloadQueue = drawQueue;
    drawSource();
    act(drawQueue);
}

async function renderHistory() {
    const main = $(".rfm-main"); main.replaceChildren();
    const title = element("div", "rfm-titlebar");
    title.append(element("h1", "", "Transfer History"), button("Refresh", () => act(renderHistory)));
    main.append(title, element("p", "rfm-muted", "Saved downloads, uploads, and browser download links. History remains after clearing the task queue or restarting ComfyUI."));
    const toolbar = element("div", "rfm-toolbar rfm-wrap");
    const search = input("search", "Search filename, link, or folder", state.transferSearch || ""); search.classList.add("rfm-grow");
    const filter = select([["all", "All transfers"], ["download", "Server downloads"], ["upload", "Uploads"], ["export", "Browser downloads"]], state.transferFilter || "all");
    toolbar.append(search, filter); main.append(toolbar);
    const list = element("div", "rfm-transfer-list"); main.append(list);
    empty(list, "Loading saved transfers…");
    const data = await request("/transfers");
    if (state.page !== "History") return;
    function draw() {
        list.replaceChildren();
        const visible = data.transfers.filter(item => (filter.value === "all" || item.kind === filter.value) &&
            `${item.name} ${item.url || ""} ${item.root}/${item.path}`.toLowerCase().includes(search.value.toLowerCase()));
        if (!visible.length) { empty(list, "No transfers match this view."); return; }
        for (const item of visible) {
            const card = element("div", "rfm-task-row");
            const top = element("div", "rfm-task-top");
            top.append(element("strong", "", item.name), element("span", "rfm-muted", `${item.kind} · ${item.status}`));
            card.append(top, element("div", "rfm-task-detail", `${date(item.created)} · ${bytes(item.size)} · ${item.root}/${item.path}`));
            if (item.url) {
                const link = element("a", "rfm-transfer-link", item.url);
                link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
            }
            const actions = element("div", "rfm-actions");
            actions.append(button("Open folder", () => act(async () => {
                showPage("Files"); await navigate(item.root, item.path.split("/").slice(0, -1).join("/"));
            })), button("Download saved file", () => window.open(url("/files/download", { root: item.root, path: item.path }), "_blank", "noopener")));
            if (item.reusable) actions.append(button("Re-download…", () => act(() => pickFolder(item.root, item.path.split("/").slice(0, -1).join("/"), (root, folder) => act(async () => {
                const response = await request("/download/queue", { items: [{ ...item.metadata, url: item.url, filename: item.name, root, folder }] });
                queueNotice(response); await renderHistory();
            }))), "rfm-primary"));
            else if (item.url) card.append(element("p", "rfm-muted", "This source used a temporary or signed link. Paste a fresh link in Downloads to fetch it again."));
            card.append(actions); list.append(card);
        }
    }
    search.oninput = () => { state.transferSearch = search.value; draw(); };
    filter.onchange = () => { state.transferFilter = filter.value; draw(); };
    draw();
}

async function renderModels() {
    const main = $(".rfm-main"); main.replaceChildren();
    const title = element("div", "rfm-titlebar"); title.append(element("h1", "", "Model Library"), button("Refresh", () => act(renderModels)));
    main.append(title);
    const tabs = element("div", "rfm-toolbar rfm-wrap");
    for (const name of ["Installed", "Missing", "Duplicates", "Sources", "Downloads"]) {
        tabs.append(button(name, () => { state.modelTab = name; act(renderModels); }, name === state.modelTab ? "rfm-primary" : ""));
    }
    main.append(tabs);
    const search = input("search", "Filter models by name or path"); search.classList.add("rfm-grow");
    main.append(search);
    const list = element("div", "rfm-table"); main.append(list);
    empty(list, "Scanning model information…");
    if (state.modelTab === "Downloads") {
        const data = await request("/tasks");
        list.replaceChildren();
        for (const task of data.tasks.filter(item => item.kind === "download")) list.append(taskRow(task));
        if (!list.children.length) empty(list, "No model downloads yet.");
        return;
    }
    const data = await request(state.modelTab === "Missing" ? "/models/missing" : "/models");
    if (state.page !== "Models") return;
    let models = data.models;
    if (state.modelTab === "Sources") models = models.filter(model => model.source_url);
    if (state.modelTab === "Duplicates") {
        const counts = new Map();
        for (const model of models) {
            const key = model.sha256 || model.name.toLowerCase();
            counts.set(key, (counts.get(key) || 0) + 1);
        }
        models = models.filter(model => counts.get(model.sha256 || model.name.toLowerCase()) > 1);
    }
    title.append(element("span", "rfm-muted", `${models.length} model(s) · ${bytes(models.reduce((sum, model) => sum + (model.size || 0), 0))}`));
    function draw() {
        list.replaceChildren();
        const visible = models.filter(model => `${model.name} ${model.path || ""} ${model.type || ""}`.toLowerCase().includes(search.value.toLowerCase()));
        if (!visible.length) { empty(list, state.modelTab === "Missing" ? "No missing models found in saved workflows." : "No models match this view."); return; }
        const header = element("div", "rfm-model-row header");
        for (const label of ["Model", "Type", state.modelTab === "Missing" ? "Used by" : "Size", "Hash / Source", "Actions"]) header.append(element("span", "", label));
        list.append(header);
        for (const model of visible) {
            const row = element("div", "rfm-model-row");
            const name = element("div"); name.append(element("strong", "", model.name));
            if (model.path) name.append(element("small", "", `${model.root}/${model.path}`));
            row.append(name, element("span", "", model.type),
                element("span", "", model.used_by_workflows ? `${model.used_by_workflows.length} workflow(s)` : bytes(model.size)));
            const source = element("div");
            source.append(element("small", "", model.sha256 ? `SHA256 ${model.sha256.slice(0, 16)}…` : "Hash not calculated"));
            source.append(element("small", "", model.source_url || model.sources?.[0]?.url || "Source unknown"));
            row.append(source);
            const actions = element("div", "rfm-actions");
            if (state.modelTab === "Missing") {
                actions.append(button("Find source", () => act(async () => {
                    const found = await request("/source/search", { filename: model.name });
                    const candidates = found.results.map(item => `${item.confidence}: ${item.url}`).join("\n") || "No candidates found";
                    const chosen = prompt(`${model.name}\n${candidates}\n\nEnter a source URL to inspect`, model.sources?.[0]?.url || "");
                    if (!chosen) return;
                    state.source = await request("/source/info", { value: chosen }); showPage("Downloads");
                })));
            } else {
                const parent = model.path.split("/").slice(0, -1).join("/");
                actions.append(button("Open folder", () => act(async () => {
                    showPage("Files");
                    await navigate(model.root, parent);
                })));
                actions.append(button("Move", () => act(() => pickFolder(model.root, parent, (root, path) => act(async () => {
                    if (root === model.root && path === parent) return notify("Choose a different destination folder", true);
                    const result = await request("/files/action", { action: "move", sources: [{ root: model.root, path: model.path }],
                        destination: { root, path } });
                    notify(`Move queued in Tasks: ${result.task.slice(0, 8)}`);
                })))));
                actions.append(button("Rename", () => act(async () => {
                    const name = prompt("New model filename", model.name);
                    if (!name || name === model.name) return;
                    const result = await request("/files/action", { action: "rename", sources: [{ root: model.root, path: model.path }],
                        destination: { root: model.root, path: parent }, name });
                    notify(`Rename queued in Tasks: ${result.task.slice(0, 8)}`);
                })));
                actions.append(button("Delete", () => act(async () => {
                    const permanent = await chooseDeletion(model.name);
                    if (permanent === null) return;
                    const result = await request("/files/action", { action: "delete", sources: [{ root: model.root, path: model.path }], permanent });
                    notify(`Delete queued in Tasks: ${result.task.slice(0, 8)}`);
                }), "rfm-danger"));
                actions.append(button("Hash", () => act(async () => { await request("/models/hash", { root: model.root, path: model.path }); notify("Hash queued"); })));
                actions.append(button("Set source", () => act(async () => {
                    const url = prompt("Public source URL", model.source_url || "");
                    if (!url) return;
                    const platform = /huggingface\.co/.test(url) ? "huggingface" : /civitai\.com/.test(url) ? "civitai" : "other";
                    await request("/models/source", { root: model.root, path: model.path, platform, url, model_type: model.type });
                    notify("Source saved"); await renderModels();
                })));
            }
            row.append(actions); list.append(row);
        }
    }
    search.oninput = draw;
    draw();
}

async function renderWorkflows() {
    const main = $(".rfm-main"); main.replaceChildren();
    main.append(element("h1", "", "Workflow Dependencies"));
    const layout = element("div", "rfm-columns");
    const files = section("Saved workflows");
    const report = section("Dependency report");
    layout.append(files, report); main.append(layout);
    empty(files, "Finding workflows…");
    empty(report, "Choose a workflow to analyze.");
    const result = await request("/workflows");
    if (state.page !== "Workflows") return;
    files.replaceChildren(element("h2", "", `Saved workflows (${result.workflows.length})`));
    for (const entry of result.workflows) {
        const row = element("div", "rfm-workflow-row");
        row.append(element("span", "", entry.path));
        row.append(button("Analyze", () => act(async () => {
            const data = await request("/workflows/analyze", { root: entry.root, path: entry.path });
            state.currentWorkflow = { entry, data };
            drawReport(report, entry, data);
        })));
        row.append(button("Download", () => window.open(url("/files/download", { root: entry.root, path: entry.path }), "_blank", "noopener")));
        files.append(row);
    }
    if (!result.workflows.length) empty(files, "No saved workflow JSON files found.");
}

function drawReport(container, entry, data) {
    container.replaceChildren(element("h2", "", entry.name));
    container.append(element("h3", "", `Models (${data.models.length})`));
    if (!data.models.length) container.append(element("p", "rfm-muted", "No model filenames were found in this workflow."));
    for (const model of data.models) {
        const row = element("div", "rfm-dependency-row");
        row.append(element("strong", "", model.name), element("span", model.status === "missing" ? "rfm-bad" : "rfm-good", model.status));
        if (model.locations.length) row.append(element("small", "", model.locations.map(item => `${item.root}/${item.path}`).join(", ")));
        if (model.status === "missing") row.append(button("Find source", () => act(async () => {
            const found = await request("/source/search", { filename: model.name });
            const dialog = section(`Source candidates for ${model.name}`);
            for (const source of found.results) {
                const candidate = element("div", "rfm-workflow-row");
                candidate.append(element("span", "", `${source.confidence}: ${source.url}`));
                candidate.append(button("Inspect", () => { state.source = null; showPage("Downloads"); const inputNode = $(".rfm-source-results").previousSibling.querySelector("input"); inputNode.value = source.url; inputNode.nextSibling.click(); }));
                dialog.append(candidate);
            }
            if (!found.results.length) dialog.append(element("p", "rfm-muted", "No source found. Add a source manually when known."));
            container.append(dialog);
        })));
        container.append(row);
    }
    container.append(element("h3", "", `Custom nodes (${data.custom_nodes.length})`));
    for (const node of data.custom_nodes) {
        const row = element("div", "rfm-dependency-row");
        row.append(element("strong", "", node.node), element("span", node.status === "missing" ? "rfm-bad" : "rfm-good", node.status));
        if (node.package) row.append(element("small", "", node.package));
        container.append(row);
    }
}

function taskRow(task) {
    const row = element("div", "rfm-task-row");
    const top = element("div", "rfm-task-top");
    top.append(element("strong", "", task.name), element("span", `rfm-status-${task.status}`, task.status));
    row.append(top);
    const progress = element("progress", "rfm-progress");
    if (task.total > 0) { progress.max = task.total; progress.value = task.bytes || 0; }
    else if (["completed", "skipped", "failed", "cancelled"].includes(task.status)) { progress.max = 1; progress.value = ["completed", "skipped"].includes(task.status) ? 1 : 0; }
    row.append(progress);
    const detail = element("div", "rfm-task-detail");
    detail.append(element("span", "", `${task.kind} · ${bytes(task.bytes)}${task.total != null ? ` / ${bytes(task.total)}` : ""} · ${bytes(task.speed)}/s`));
    if (task.total && task.speed) detail.append(element("span", "", `ETA ${Math.max(0, Math.ceil((task.total - task.bytes) / task.speed))}s`));
    row.append(detail);
    if (task.error) row.append(element("p", "rfm-bad", task.error));
    if (task.kind === "install-node" && task.result) {
        if (task.result.stage) row.append(element("p", "rfm-muted", task.result.stage));
        if (task.result.installed) row.append(element("p", "rfm-muted", `${task.result.package} installed.${task.result.requirements_installed ? " Requirements installed." : " No Python packages installed."} ${task.result.dependency_check === true ? "Dependency check passed." : task.result.dependency_check === false ? "The Python environment has dependency conflicts; review the output before restarting." : ""}`));
        if (task.result.output) row.append(element("pre", "rfm-terminal-output", task.result.output));
    }
    if (task.result?.skipped) row.append(element("p", "rfm-muted", task.result.skipped));
    if (task.status === "completed" && task.result?.size != null && task.kind === "size")
        row.append(element("p", "rfm-muted", `${bytes(task.result.size)} in ${task.result.files} file(s)`));
    if (task.status === "completed" && task.kind === "restore" && task.result)
        row.append(element("p", "rfm-muted", `${task.result.restored} restored, ${task.result.skipped} skipped, ${task.result.requirements_installed || 0} requirements installed. Restart ComfyUI if custom nodes were restored.`));
    const actions = element("div", "rfm-actions");
    const control = async action => { await request("/tasks/control", { id: task.id, action }); await refreshTasks(true); };
    if (task.status === "running" || task.status === "waiting") {
        if (task.kind === "download") actions.append(button("Pause", () => act(() => control("pause"))));
        if (["download", "copy", "duplicate", "zip", "extract", "hash", "size", "backup", "restore", "upload", "requirements", "install-node"].includes(task.kind))
            actions.append(button("Cancel", () => act(() => control("cancel"))));
    }
    if (task.retryable && ["paused", "failed", "cancelled"].includes(task.status)) actions.append(button(task.status === "paused" ? "Resume" : "Retry", () => act(() => control(task.status === "paused" ? "resume" : "retry"))));
    if (task.status === "completed" && task.result?.export_id) actions.append(button("Download ZIP", () => window.open(api.apiURL(`/robot/backup/download/${task.result.export_id}`), "_blank", "noopener"), "rfm-primary"));
    if (["completed", "failed", "cancelled", "paused", "skipped"].includes(task.status)) actions.append(button("Remove", () => act(() => control("remove"))));
    row.append(actions);
    return row;
}

async function refreshTasks(draw = false) {
    const data = await request("/tasks");
    const previous = new Map(state.tasks.map(task => [task.id, task.status]));
    const filesChanged = data.tasks.some(task => ["download", "upload", "delete", "move", "rename", "copy", "duplicate", "extract", "restore", "install-node"].includes(task.kind) &&
        ["completed", "failed", "cancelled"].includes(task.status) && previous.get(task.id) !== task.status);
    state.tasks = data.tasks;
    if (filesChanged) {
        state.treeFolders.clear();
        await refreshResources(true);
        if (state.page === "Files" || state.page === "Outputs") await navigate(state.root, state.path, false);
        else if (state.page === "Models") await renderModels();
        else if (state.page === "History") await renderHistory();
    }
    const active = data.tasks.filter(item => ["waiting", "running", "pausing", "cancelling"].includes(item.status)).length;
    if (shell) $(".rfm-status").textContent = active ? `${active} active task${active > 1 ? "s" : ""}` : "Ready";
    const install = data.tasks.find(task => task.id === state.installTask);
    const installed = await completeNodeInstall(install);
    if (installed && state.page === "Node Installer" && state.drawInstallerHistory) await state.drawInstallerHistory();
    if (state.page === "Node Installer" && state.drawInstallerStatus) await state.drawInstallerStatus(data.tasks);
    if (!draw || state.page !== "Tasks") return;
    const main = $(".rfm-main"); const previousScroll = main.scrollTop; main.replaceChildren();
    main.append(element("h1", "", "Tasks & Logs"));
    const filter = select([["all", "All tasks"], ["download", "Downloads"], ["backup", "Backups"], ["restore", "Restores"], ["failed", "Failures"]], state.taskFilter);
    main.append(filter);
    const list = element("div", "rfm-task-list"); main.append(list);
    function drawList() {
        list.replaceChildren();
        const shown = data.tasks.filter(item => filter.value === "all" || item.kind === filter.value || filter.value === "failed" && item.status === "failed");
        if (!shown.length) { empty(list, "No tasks match this filter."); return; }
        for (const task of shown) list.append(taskRow(task));
    }
    filter.onchange = () => { state.taskFilter = filter.value; drawList(); };
    drawList();
    const events = await request("/logs");
    const activity = section("Activity log");
    const logFilter = select([["all", "All activity"], ["error", "Errors"], ["source", "Source lookups"],
        ["models", "Model scans"], ["workflow", "Workflows"], ["backup", "Backups"], ["restore", "Restores"]], state.logFilter);
    const logList = element("div", "rfm-log-list");
    activity.append(logFilter, logList);
    main.append(activity);
    function drawLogs() {
        logList.replaceChildren();
        const selected = events.events.filter(item => logFilter.value === "all" || item.kind === logFilter.value);
        if (!selected.length) { empty(logList, "No activity matches this filter."); return; }
        for (const item of selected) {
            const row = element("div", "rfm-log-row");
            row.append(element("small", "rfm-muted", new Date(item.time).toLocaleString()),
                element("strong", item.kind === "error" ? "rfm-bad" : "", item.kind), element("span", "", item.message));
            logList.append(row);
        }
    }
    logFilter.onchange = () => { state.logFilter = logFilter.value; drawLogs(); };
    drawLogs();
    main.scrollTop = previousScroll;
}

async function restartComfy(confirm = true) {
    if (confirm && !confirmAction("Restart ComfyUI now? The page will reconnect and reload after startup.")) return;
    const result = await request("/system/restart", { confirm_restart: true });
    localStorage.setItem("Robot.FileManager.pendingInstall", "1");
    state.restarting = true;
    $(".rfm-status").textContent = "Restarting ComfyUI…";
    notify("Restarting ComfyUI. This page will reload when the server is ready.");
    let attempts = 0;
    const timer = setInterval(async () => {
        try {
            const ready = await request("/custom-nodes/install-history");
            if (ready.started !== result.started) { clearInterval(timer); window.location.reload(); return; }
        } catch { /* The server is offline during restart. */ }
        if (++attempts >= 180) {
            clearInterval(timer); state.restarting = false;
            notify("ComfyUI has not reconnected yet. Check the server console and refresh this page when it is ready.", true);
        }
    }, 2000);
}

async function completeNodeInstall(task) {
    if (task && ["failed", "cancelled"].includes(task.status)) { state.installHandled = task.id; return false; }
    if (!task || task.status !== "completed" || state.installHandled === task.id) return false;
    state.installHandled = task.id; state.nodeSummary = null;
    if (state.installAutoRestart && task.result?.installed && task.result.dependency_check !== false) await restartComfy(false);
    return true;
}

async function renderInstaller() {
    const main = $(".rfm-main"); main.replaceChildren();
    const title = element("div", "rfm-titlebar");
    title.append(element("h1", "", "Custom Node Installer"), button("Restart ComfyUI", () => act(() => restartComfy())));
    main.append(title, element("p", "rfm-muted", "Choose a ZIP, a custom node folder, or a single Python node from your PC. Review the package, install its requirements into ComfyUI's Python environment, and restart to load it."));
    const upload = section("Upload from PC");
    const file = input("file"); file.accept = ".zip,.py,application/zip"; file.hidden = true;
    const folder = input("file"); folder.multiple = true; folder.setAttribute("webkitdirectory", ""); folder.hidden = true;
    const selected = element("p", "rfm-muted");
    const choose = files => { state.nodeFiles = [...files]; state.nodeSummary = null; state.installTask = null; selected.textContent = `${state.nodeFiles.length} file(s) · ${bytes(state.nodeFiles.reduce((sum, item) => sum + item.size, 0))}`; };
    file.onchange = () => choose(file.files); folder.onchange = () => choose(folder.files);
    const controls = element("div", "rfm-toolbar rfm-wrap");
    const inspect = button("Upload & inspect", () => act(async () => {
        if (!state.nodeFiles?.length) return notify("Choose a ZIP, Python file, or folder first", true);
        inspect.disabled = true; selected.textContent = "Uploading and checking package…";
        try {
            const form = new FormData();
            for (const item of state.nodeFiles) form.append("file", item, item.webkitRelativePath || item.name);
            const response = await api.fetchApi("/robot/custom-nodes/upload", { method: "POST", body: form });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "Node upload failed");
            state.nodeSummary = data.package; state.nodeFiles = []; await renderInstaller();
        } finally { inspect.disabled = false; }
    }), "rfm-primary");
    controls.append(button("Choose ZIP or .py file", () => file.click()), button("Choose folder", () => folder.click()), inspect, file, folder);
    upload.append(controls, selected); main.append(upload);
    if (state.nodeFiles?.length) selected.textContent = `${state.nodeFiles.length} file(s) selected`;
    const summary = state.nodeSummary;
    if (summary && !state.installTask) {
        const preview = section("Review installation");
        preview.append(element("p", "rfm-muted", `${summary.files} file(s) · ${bytes(summary.size)} · ${summary.kind === "file" ? "Single Python node" : "Custom node package"}`));
        const name = input("text", "Destination name in custom_nodes", summary.name); name.classList.add("rfm-grow");
        name.setAttribute("aria-label", "Custom node destination name");
        const destination = element("p", "rfm-muted", `Destination: custom_nodes/${name.value}`);
        preview.append(name, destination);
        if (summary.exists) preview.append(element("p", "rfm-bad", "This node already exists. Existing files are protected; choose another destination name to install this upload."));
        const requirements = check("Install requirements.txt in ComfyUI's Python environment", summary.requirements !== null);
        requirements[1].disabled = summary.requirements === null;
        const restart = check("Restart ComfyUI after a successful installation and dependency check", true);
        preview.append(requirements[0]);
        if (summary.requirements !== null) preview.append(element("pre", "rfm-requirements-preview", summary.requirements || "requirements.txt is empty."));
        else preview.append(element("p", "rfm-muted", "No requirements.txt found. The node files will be installed without installing Python packages."));
        preview.append(restart[0]);
        const install = button("Install custom node", () => act(async () => {
            const response = await request("/custom-nodes/install", { id: summary.id, name: name.value.trim(), requirements: requirements[1].checked });
            state.installTask = response.task; state.installAutoRestart = restart[1].checked; state.installHandled = null;
            notify("Custom node installation started"); await renderInstaller();
        }), "rfm-primary");
        install.disabled = summary.exists;
        name.oninput = () => { destination.textContent = `Destination: custom_nodes/${name.value}`; install.disabled = summary.exists && name.value === summary.name; };
        preview.append(install); main.append(preview);
    }
    const status = element("div", "rfm-install-status"); main.append(status);
    async function drawStatus(tasks) {
        if (state.page !== "Node Installer" || !status.isConnected) return;
        const task = tasks.find(item => item.id === state.installTask);
        status.replaceChildren();
        if (!task) return;
        status.append(taskRow(task));
        if (["failed", "cancelled"].includes(task.status)) status.append(button("Review upload and try again", () => { state.installTask = null; act(renderInstaller); }));
        if (task.status === "completed") {
            status.append(button("Restart ComfyUI", () => act(() => restartComfy())));
            if (await completeNodeInstall(task)) await drawHistory();
        }
    }
    state.drawInstallerStatus = drawStatus;
    const history = section("Installation status"); main.append(history);
    async function drawHistory() {
        const data = await request("/custom-nodes/install-history");
        if (!history.isConnected) return;
        history.replaceChildren(element("h2", "", "Installation status"));
        if (!data.installs.length) { history.append(element("p", "rfm-muted", "Uploaded node installations appear here. After restart, this view checks whether ComfyUI registered the node classes.")); return; }
        for (const item of data.installs) {
            const card = element("div", "rfm-install-record");
            card.append(element("strong", "", item.name), element("p", "rfm-muted", item.loaded_nodes.length ?
                `Loaded in ComfyUI · ${item.loaded_nodes.length} registered node(s): ${item.loaded_nodes.join(", ")}` : item.exists ?
                    "Installed on disk. No registered node classes detected; restart ComfyUI or check its console for import errors." : "Node files are no longer present."));
            if (item.requirements_installed) card.append(element("small", "rfm-muted", item.dependency_check ? "Requirements installed · Python dependency check passed" : "Requirements installed · Python environment has dependency conflicts"));
            card.append(button("Open folder", () => act(async () => { showPage("Files"); await navigate("custom_nodes", item.name.endsWith(".py") ? "" : item.name); })));
            history.append(card);
        }
    }
    state.drawInstallerHistory = drawHistory;
    const tasks = await request("/tasks");
    await drawStatus(tasks.tasks);
    await drawHistory();
}

async function renderTerminal() {
    const main = $(".rfm-main"); main.replaceChildren();
    main.append(element("h1", "", "Linux Server Terminal"));
    main.append(element("p", "rfm-muted", "Run commands with ComfyUI's permissions in the selected working folder. Output streams below; Stop ends the command and its child processes."));
    const form = section("Command");
    const controls = element("div", "rfm-toolbar rfm-wrap");
    const root = select(rootOptions(), state.terminalRoot || state.root);
    const folder = input("text", "Working folder within root", state.terminalPath ?? state.path); folder.classList.add("rfm-grow");
        const saveFolder = () => { state.terminalRoot = root.value; state.terminalPath = folder.value; };
    root.onchange = () => { folder.value = ""; saveFolder(); }; folder.oninput = saveFolder;
    controls.append(element("span", "rfm-muted", "Working folder"), root, folder, button("Browse…", () => act(() => pickFolder(root.value, folder.value,
        (chosenRoot, chosenPath) => { root.value = chosenRoot; folder.value = chosenPath; saveFolder(); }))));
    const command = element("textarea", "rfm-input rfm-command-input");
    command.placeholder = "ls -lah"; command.value = state.terminalInput || "";
    command.setAttribute("aria-label", "Linux command");
    command.oninput = () => { state.terminalInput = command.value; };
    const run = button("Run command", () => act(async () => {
        if (!command.value.trim()) return;
        saveFolder();
        await request("/terminal/run", { command: command.value, root: root.value, path: folder.value.trim() });
        await drawCommands();
    }), "rfm-primary");
    run.disabled = true;
    command.onkeydown = event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); run.click(); } };
    form.append(controls, command, element("p", "rfm-muted", "Ctrl+Enter to run. Each command starts in this folder; use cd folder && command when needed. Interactive input is unavailable."), run);
    main.append(form);
    const list = element("div", "rfm-terminal-list"); main.append(list);
    const cards = new Map();
    async function drawCommands() {
        const data = await request("/terminal");
        if (state.page !== "Terminal") return;
        run.disabled = !data.available; command.disabled = !data.available;
        if (!data.available) { empty(list, "The Terminal tab is available when the ComfyUI server runs on Linux. This server is running another operating system."); return; }
        if (!data.commands.length) { empty(list, "No commands run in this server session."); return; }
        list.querySelector(".rfm-empty")?.remove();
        for (const item of [...data.commands].reverse()) {
            let card = cards.get(item.id);
            if (!card) {
                const panel = section("");
                panel.querySelector("h2").textContent = `$ ${item.command}`;
                panel.append(element("small", "rfm-muted", `${item.root}/${item.path} · ${date(item.created)}`));
                const output = element("pre", "rfm-terminal-output"); output.tabIndex = 0;
                const status = element("span", "rfm-muted");
                const stop = button("Stop", () => act(async () => { await request("/terminal/stop", { id: item.id }); await drawCommands(); }), "rfm-danger");
                const actions = element("div", "rfm-toolbar rfm-wrap");
                actions.append(status, stop, button("Copy output", () => act(async () => { await navigator.clipboard.writeText(output.textContent); notify("Output copied"); })));
                panel.append(output, actions); list.prepend(panel);
                card = { panel, output, status, stop }; cards.set(item.id, card);
            }
            const follow = card.output.scrollHeight - card.output.scrollTop - card.output.clientHeight < 30;
            const text = `${item.truncated ? "[Earlier output trimmed; showing the last 1,000,000 characters]\n" : ""}${item.output || ""}`;
            if (card.output.textContent !== text) card.output.textContent = text;
            if (follow) card.output.scrollTop = card.output.scrollHeight;
            card.status.textContent = `${item.status}${item.code !== null ? ` · Exit ${item.code}` : ""}`;
            card.stop.disabled = item.status !== "running";
        }
        const live = new Set(data.commands.map(item => item.id));
        for (const [id, card] of cards) if (!live.has(id)) { card.panel.remove(); cards.delete(id); }
    }
    state.drawTerminal = drawCommands;
    await drawCommands();
}

async function renderTrash() {
    const main = $(".rfm-main"); main.replaceChildren();
    const title = element("div", "rfm-titlebar");
    title.append(element("h1", "", "Trash"), button("Empty Trash", () => act(async () => {
        if (!confirmAction("Permanently delete everything in Trash?")) return;
        await request("/files/trash", { action: "empty" }); await renderTrash();
    }), "rfm-danger"));
    main.append(title);
    const data = await request("/files/trash");
    if (state.page !== "Trash") return;
    if (!data.entries.length) { main.append(element("p", "rfm-empty", "Trash is empty.")); return; }
    for (const item of data.entries) {
        const row = element("div", "rfm-workflow-row");
        row.append(element("span", "", `${item.root}/${item.path}`), element("small", "rfm-muted", date(item.deleted)));
        row.append(button("Restore", () => act(async () => { await request("/files/trash", { id: item.id, action: "restore" }); await renderTrash(); })));
        row.append(button("Delete forever", () => act(async () => { if (!confirmAction(`Permanently delete ${item.name}?`)) return; await request("/files/trash", { id: item.id, action: "delete" }); await renderTrash(); }), "rfm-danger"));
        main.append(row);
    }
}

async function renderSettings() {
    const main = $(".rfm-main"); main.replaceChildren();
    main.append(element("h1", "", "Settings"));
    const result = await request("/settings");
    const general = section("File manager");
    const ttl = input("number", "Hours", String(result.settings.export_ttl_hours)); ttl.min = "1"; ttl.max = "168";
    general.append(element("p", "rfm-muted", "Deleting files always asks whether to send them to Trash or delete permanently."),
        element("p", "rfm-muted", "Temporary export and import ZIP retention (hours)"), ttl);
    general.append(button("Save settings", () => act(async () => {
        await request("/settings", { export_ttl_hours: Number(ttl.value) }); notify("Settings saved");
    }), "rfm-primary"));
    main.append(general);
    const credentials = section("Provider credentials");
    credentials.append(element("p", "rfm-muted", "Tokens stay in the server's private user directory and are never added to backup ZIPs."));
    for (const [platform, label] of [["huggingface", "Hugging Face"], ["civitai", "Civitai"]]) {
        const row = element("div", "rfm-toolbar");
        const token = input("password", `${label} token`); token.autocomplete = "off";
        row.append(element("strong", "", label), token,
            button("Save", () => act(async () => { await request("/settings/credential", { platform, token: token.value }); token.value = ""; notify(`${label} token saved`); })),
            button("Clear", () => act(async () => { await request("/settings/credential", { platform, token: "" }); token.value = ""; notify(`${label} token cleared`); })));
        row.append(element("span", "rfm-muted", result.credentials[platform] ? "Configured" : "Not configured"));
        credentials.append(row);
    }
    main.append(credentials);
    const paths = section("Allowed roots");
    paths.append(element("p", "rfm-muted", "The server owner can approve additional paths in user/.robot_file_manager/allowed_roots.json. On Linux, approving / adds the whole filesystem to Files. Secure ComfyUI access before enabling it."));
    for (const root of state.roots) paths.append(element("div", "rfm-root-row", `${root.id} — ${root.path}`));
    main.append(paths);
}

function renderBackup() {
    const main = $(".rfm-main"); main.replaceChildren();
    main.append(element("h1", "", "Backup & Restore"));
    const columns = element("div", "rfm-columns");
    const exportCard = section("Export workspace");
    const workflow = check("Workflows", true);
    const models = check("Model manifest (no model binaries)", true);
    const sources = check("Known model source links", true);
    const custom = check("Custom node manifest", true);
    const plugin = check("Plugin settings", true);
    const comfy = check("Supported ComfyUI UI preferences", false);
    for (const item of [workflow, models, sources, custom, plugin, comfy]) exportCard.append(item[0]);
    exportCard.append(element("h3", "", "Keep selected custom nodes in the backup"));
    exportCard.append(element("p", "rfm-muted", "Selected folders are copied without model files, caches, or common credential files. Review the folders before sharing the ZIP."));
    const nodeChoices = [];
    const nodeList = element("div", "rfm-node-picker");
    empty(nodeList, "Finding installed custom nodes…");
    exportCard.append(nodeList);
    act(async () => {
        const found = await request("/custom-nodes");
        nodeList.replaceChildren();
        for (const item of found.packages) {
            const entry = check(`${item.package}${item.requirements ? " · requirements.txt" : ""}`);
            entry[1].onchange = () => { if (entry[1].checked) custom[1].checked = true; };
            nodeList.append(entry[0]);
            nodeChoices.push({ name: item.package, box: entry[1] });
        }
        if (!found.packages.length) empty(nodeList, "No custom node folders found.");
    });
    exportCard.append(element("h3", "", "Outputs"));
    const outputMode = select([["skip", "Skip outputs"], ["all", "Entire output folder"], ["folder", "Specific folder"], ["selected", "Selected paths"]], "skip");
    const outputFolder = input("text", "Output subfolder, or comma separated selected paths");
    const outputBrowse = button("Browse output folder", () => act(() => pickFolder("output", outputFolder.value, (_root, path) => { outputFolder.value = path; })));
    const after = input("date"); after.title = "Outputs modified on or after";
    const before = input("date"); before.title = "Outputs modified on or before";
    const extensions = input("text", "Extensions, e.g. png,mp4");
    exportCard.append(outputMode, outputFolder, outputBrowse, element("p", "rfm-muted", "Optional output filters"), after, before, extensions);
    const exportPreview = element("div", "rfm-summary");
    const options = () => ({ workflows: workflow[1].checked, models: models[1].checked, sources: sources[1].checked,
        custom_nodes: custom[1].checked, plugin_settings: plugin[1].checked, comfy_settings: comfy[1].checked,
        bundled_custom_nodes: nodeChoices.filter(item => item.box.checked).map(item => item.name),
        outputs: outputMode.value, output_folder: outputFolder.value.trim(), after: after.value || null, before: before.value || null,
        extensions: extensions.value.split(",").map(value => value.trim()).filter(Boolean),
        selected_outputs: outputMode.value === "selected" ? outputFolder.value.split(",").map(value => value.trim()).filter(Boolean) : [] });
    exportCard.append(button("Analyze backup", () => act(async () => {
        const summary = await request("/backup/preview", options());
        exportPreview.replaceChildren();
        for (const [label, value] of [["Workflows", summary.workflows], ["Models", summary.models],
            ["Custom nodes", summary.custom_nodes], ["Folders kept in ZIP", summary.bundled_custom_nodes],
            ["Custom node files", bytes(summary.custom_node_size)], ["Outputs", summary.outputs],
            ["Output size", bytes(summary.output_size)], ["Estimated ZIP size", bytes(summary.estimated_zip_size)],
            ["Free space", bytes(summary.free_space)],
            ["Hugging Face links", summary.sources.huggingface], ["Civitai links", summary.sources.civitai],
            ["Unknown model sources", summary.sources.unknown]]) {
            const line = element("div", "rfm-summary-line"); line.append(element("span", "", label), element("strong", "", String(value))); exportPreview.append(line);
        }
    })), exportPreview,
    button("Create export ZIP", () => act(async () => {
        if (!confirmAction("Create this workspace backup ZIP?")) return;
        const task = await request("/backup/create", options());
        notify(`Backup task ${task.task.slice(0, 8)} queued. Download it from Tasks.`);
    }), "rfm-primary"));
    columns.append(exportCard);

    const restoreCard = section("Import & restore");
    restoreCard.append(element("p", "rfm-muted", "Select a Robot File Manager backup ZIP. The server validates paths and compares installed models before restore."));
    const archive = input("file"); archive.accept = ".zip,application/zip";
    restoreCard.append(archive);
    const summary = element("div", "rfm-summary");
    restoreCard.append(button("Analyze archive", () => act(async () => {
        if (!archive.files.length) return notify("Choose a ZIP archive", true);
        const data = new FormData(); data.append("file", archive.files[0], archive.files[0].name);
        const response = await api.fetchApi("/robot/backup/import", { method: "POST", body: data });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Import failed");
        state.importId = result.import_id;
        state.importSummary = result.summary;
        drawImportSummary(summary, result.summary);
    }), "rfm-primary"), summary);
    columns.append(restoreCard); main.append(columns);
    if (state.importSummary) drawImportSummary(summary, state.importSummary);
}

function drawImportSummary(container, data) {
    container.replaceChildren();
    container.append(element("h3", "", "Archive comparison"));
    for (const [label, value] of [["Workflows", data.workflows], ["Outputs", data.outputs],
        ["Models", data.models.length], ["Missing models", data.models.filter(item => item.status === "Missing").length],
        ["Missing custom nodes", data.custom_nodes.filter(item => item.status === "Missing").length],
        ["Custom node folders in ZIP", data.manifest.bundled_custom_nodes?.length || 0],
        ["Uncompressed size", bytes(data.uncompressed_size)], ["Free space", bytes(data.free_space)]]) {
        const line = element("div", "rfm-summary-line"); line.append(element("span", "", label), element("strong", "", String(value))); container.append(line);
    }
    const restoreWorkflow = check("Restore workflows", true);
    const restoreOutputs = check("Restore outputs", false);
    const overwrite = check("Overwrite existing files", false);
    const restoreSettings = check("Restore ComfyUI settings", false);
    const restorePlugin = check("Restore File Manager settings", false);
    const installRequirements = check("Install requirements for selected custom nodes first", false);
    for (const item of [restoreWorkflow, restoreOutputs, overwrite, restoreSettings, restorePlugin, installRequirements]) container.append(item[0]);
    container.append(element("p", "rfm-muted", "Restored custom node code runs after a ComfyUI restart. Requirements installation changes the ComfyUI Python environment."));
    const selectedNodes = [];
    container.append(button("Restore selected content", () => act(async () => {
        const customNodes = selectedNodes.filter(item => item.box.checked).map(item => item.name);
        if (!confirmAction(`Restore selected files${customNodes.length ? ` and ${customNodes.length} custom node folder(s)` : ""}${installRequirements[1].checked ? " and install their requirements" : ""}?`)) return;
        const result = await request("/backup/restore", { import_id: state.importId, workflows: restoreWorkflow[1].checked,
            outputs: restoreOutputs[1].checked, overwrite: overwrite[1].checked, comfy_settings: restoreSettings[1].checked,
            plugin_settings: restorePlugin[1].checked, custom_nodes: customNodes,
            install_requirements: installRequirements[1].checked });
        notify(`Restore task ${result.task.slice(0, 8)} queued`);
    }), "rfm-primary"));
    container.append(element("h3", "", "Models to restore"));
    const selected = [];
    async function queueModels(rows, automatic = false) {
        const items = [];
        const unresolved = [];
        for (const row of rows) {
            if (!row.source.value || (automatic && Object.keys(row.model.sources || {}).length !== 1)) {
                unresolved.push(row.model.name); continue;
            }
            let info;
            try { info = await request("/source/info", { value: row.source.value }); }
            catch { unresolved.push(row.model.name); continue; }
            const matches = info.files.filter(item => item.name?.toLowerCase() === row.model.name.toLowerCase());
            const verified = matches.filter(item => row.model.sha256 && item.sha256?.toLowerCase() === row.model.sha256.toLowerCase());
            const file = verified.length === 1 ? verified[0] : matches.length === 1 ? matches[0] : null;
            if (!file || (row.model.sha256 && file.sha256 && file.sha256.toLowerCase() !== row.model.sha256.toLowerCase())) {
                unresolved.push(row.model.name); continue;
            }
            items.push({ url: file.url, root: row.root.value, folder: row.folder.value,
                filename: row.model.name, model_type: row.model.model_type, sha256: row.model.sha256 || file.sha256 });
        }
        if (!items.length) return notify(unresolved.length ? `${unresolved.length} model(s) need manual source/file selection` : "Select missing models first", true);
        let queued = 0, errors = 0, skipped = 0;
        for (let index = 0; index < items.length; index += 100) {
            const result = await request("/download/queue", { items: items.slice(index, index + 100) });
            queued += result.tasks.length;
            errors += result.errors.length;
            skipped += result.skipped.length;
        }
        notify(`${queued} model download(s) queued${skipped ? `; ${skipped} already present or queued` : ""}${unresolved.length ? `; ${unresolved.length} need manual file selection` : ""}${errors ? `; ${errors} rejected` : ""}`,
            !!errors);
    }
    for (const model of data.models) {
        const row = element("div", "rfm-restore-row");
        const needsDownload = ["Missing", "Hash Mismatch"].includes(model.status);
        const box = input("checkbox"); box.disabled = !needsDownload;
        const name = element("div"); name.append(element("strong", "", model.name), element("small", "", model.model_type || "Unknown type"));
        const status = element("span", model.status === "Missing" ? "rfm-bad" : "rfm-good", model.status);
        const sources = model.sources || {};
        const source = select(Object.entries(sources).map(([platform, value]) => [value, platform]), Object.values(sources)[0]);
        if (!source.options.length) {
            const option = element("option", "", "Source unknown"); option.value = ""; source.append(option);
        }
        source.title = "Known source. Inspect uncertain links before downloading.";
        const expected = model.expected_folder?.replace(/^models\/?/, "") || "";
        const folder = input("text", "Destination folder", expected);
        const root = select(rootOptions(), "models");
        const folderCell = element("div", "rfm-folder-cell");
        folderCell.append(folder, button("Browse", () => act(() => pickFolder(root.value, folder.value, (chosenRoot, chosenPath) => {
            root.value = chosenRoot; folder.value = chosenPath;
        }))));
        row.append(box, name, status, source, root, folderCell);
        const actions = element("div", "rfm-actions");
        if (needsDownload) actions.append(button("Use URL", () => {
            const value = prompt("Direct file URL, Hugging Face file/repository, or Civitai model URL");
            if (!value) return;
            const option = element("option", "", "Custom URL"); option.value = value.trim(); source.append(option); source.value = option.value;
        }));
        if (needsDownload) actions.append(button("Inspect source", () => act(async () => {
            if (!source.value) return notify("No source URL is known", true);
            const result = await request("/source/info", { value: source.value });
            state.source = result; showPage("Downloads");
        })));
        if (needsDownload) actions.append(button("Locate existing", () => act(async () => {
            const chosenRoot = prompt("Allowed root ID containing the file", "models");
            if (!chosenRoot) return;
            const chosenPath = prompt("Path to existing model file within that root", model.name);
            if (!chosenPath) return;
            const found = await request("/models/locate", { root: chosenRoot, path: chosenPath });
            if (model.sha256 && found.sha256 && model.sha256.toLowerCase() !== found.sha256.toLowerCase()) {
                return notify("The located file's saved SHA256 does not match the backup", true);
            }
            status.textContent = model.sha256 && found.sha256 ? "Hash Match" : "Located (unverified)";
            status.className = model.sha256 && found.sha256 ? "rfm-good" : "rfm-muted";
            name.append(element("small", "", `${found.root}/${found.path}`));
            box.checked = false; box.disabled = true;
            notify(`Located ${found.name}`);
        })));
        row.append(actions);
        container.append(row);
        const selection = { model, box, source, root, folder };
        if (needsDownload) actions.append(button("Download this model", () => act(() => queueModels([selection]))));
        selected.push(selection);
    }
    container.append(button("Queue selected missing models", () => act(() => queueModels(selected.filter(item => item.box.checked))), "rfm-primary"));
    container.append(button("Queue all with one known source", () => act(() => queueModels(selected.filter(item => !item.box.disabled), true))));
    container.append(element("h3", "", "Custom nodes"));
    for (const node of data.custom_nodes) {
        const row = element("div", "rfm-workflow-row");
        if (node.bundled && !node.files_present) {
            const box = input("checkbox");
            row.append(box);
            selectedNodes.push({ name: node.package, box });
        }
        row.append(element("span", "", node.package || node.nodes?.join(", ") || "Unknown package"),
            element("span", node.status === "Missing" ? "rfm-bad" : "rfm-good", node.status));
        if (node.bundled) row.append(element("small", "rfm-muted", node.files_present ? "Files already present" : "Included in ZIP"));
        if (node.requirements_present && node.status !== "Installed") {
            row.append(button("Install requirements", () => act(async () => {
                if (!confirmAction(`Install ${node.package} requirements into the ComfyUI Python environment?`)) return;
                await request("/custom-nodes/install-requirements", { package: node.package });
                notify("Requirements installation queued; restart ComfyUI after it completes");
            })));
        }
        if (node.repository) {
            const link = element("a", "", "Repository / install guidance");
            link.href = node.repository; link.target = "_blank"; link.rel = "noopener noreferrer";
            row.append(link);
        }
        container.append(row);
    }
}

app.registerExtension({
    name: "Robot.FileManager",
    async setup() {
        const style = document.createElement("link");
        style.rel = "stylesheet";
        style.href = new URL("./manager.css", import.meta.url).href;
        document.head.append(style);
        try { await init(); }
        catch (error) { console.error("Robot File Manager failed to initialize", error); }
    },
});
