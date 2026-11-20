import { getToken, ticketedUrl } from "./auth.js";
import { showToast } from "./utils.js";
import { apiUrl } from "./target.js";

let currentPath = null;
let selectMode = false;
const selected = new Map(); // path -> {name, is_dir}

function getFileIcon(entry) {
  if (entry.is_dir) return "📁";
  const ext = entry.name.split(".").pop().toLowerCase();
  const icons = {
    pdf: "📄", doc: "📝", docx: "📝", txt: "📃", md: "📃",
    jpg: "🖼", jpeg: "🖼", png: "🖼", gif: "🖼", svg: "🖼", webp: "🖼",
    mp4: "🎬", mkv: "🎬", avi: "🎬", mov: "🎬", webm: "🎬",
    mp3: "🎵", flac: "🎵", wav: "🎵", ogg: "🎵",
    zip: "📦", tar: "📦", gz: "📦", rar: "📦", "7z": "📦",
    py: "🐍", js: "📜", ts: "📜", html: "🌐", css: "🎨",
    sh: "⚙️", bash: "⚙️", json: "📋", xml: "📋", yaml: "📋",
  };
  return icons[ext] || "📄";
}

function formatDate(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function buildBreadcrumb(path, home) {
  const el = document.getElementById("breadcrumb");
  el.innerHTML = "";
  const homeParts = home.split("/").filter(Boolean);
  const parts = path.split("/").filter(Boolean);
  const segments = [];

  for (let i = 0; i < parts.length; i++) {
    const fullPath = "/" + parts.slice(0, i + 1).join("/");
    const isHome = i === homeParts.length - 1 && fullPath === home;
    segments.push({ label: isHome ? "~" : parts[i], path: fullPath });
  }

  segments.forEach((seg, i) => {
    const span = document.createElement("span");
    span.className = "breadcrumb-item";
    span.textContent = seg.label;
    if (i < segments.length - 1) {
      span.addEventListener("click", () => loadFiles(seg.path));
    }
    el.appendChild(span);
    if (i < segments.length - 1) {
      const sep = document.createElement("span");
      sep.className = "breadcrumb-sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }
  });
}

async function deleteEntry(path, name) {
  if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
  const token = getToken();
  try {
    const res = await fetch(apiUrl(`/api/files?path=${encodeURIComponent(path)}`, "files"), {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) {
      showToast(`Deleted ${name}`, "success");
      loadFiles(currentPath);
    } else {
      showToast("Delete failed", "error");
    }
  } catch {
    showToast("Connection error", "error");
  }
}

async function downloadFile(path, name) {
  try {
    const url = await ticketedUrl(apiUrl(`/api/files/download?path=${encodeURIComponent(path)}`, "files"));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  } catch {
    showToast("Could not authorize download", "error");
  }
}

let activeDownload = null;

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function showDownloadProgress(label, onCancel) {
  const overlay = document.createElement("div");
  overlay.className = "dl-overlay";
  overlay.innerHTML = `
    <div class="dl-card">
      <div class="dl-spinner"></div>
      <div class="dl-title" data-role="title"></div>
      <div class="dl-status" data-role="status">Preparing zip on server…</div>
      <div class="dl-bar"><div class="dl-bar-fill" data-role="bar" style="width:0%"></div></div>
      <div class="dl-meta" data-role="meta">Elapsed 0s</div>
      <button class="btn danger" data-role="cancel">Cancel</button>
    </div>
  `;
  overlay.querySelector('[data-role="title"]').textContent = label;
  document.body.appendChild(overlay);
  overlay.querySelector('[data-role="cancel"]').addEventListener("click", () => {
    overlay.querySelector('[data-role="status"]').textContent = "Cancelling…";
    onCancel();
  });
  return overlay;
}

function updateDownloadProgress(overlay, { status, bar, meta }) {
  if (status != null) overlay.querySelector('[data-role="status"]').textContent = status;
  if (bar != null) {
    const fill = overlay.querySelector('[data-role="bar"]');
    fill.style.width = `${Math.max(0, Math.min(100, bar))}%`;
    fill.classList.toggle("indeterminate", bar < 0);
  }
  if (meta != null) overlay.querySelector('[data-role="meta"]').textContent = meta;
}

function closeDownloadProgress(overlay) {
  if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

async function performZipDownload({ label, filename, fetchInit, url }) {
  if (activeDownload) {
    showToast("A download is already in progress", "error");
    return;
  }
  const controller = new AbortController();
  fetchInit.signal = controller.signal;
  const startedAt = Date.now();
  let stage = "preparing";
  const overlay = showDownloadProgress(label, () => controller.abort());
  activeDownload = controller;

  const ticker = setInterval(() => {
    const elapsed = fmtElapsed(Date.now() - startedAt);
    if (stage === "preparing") {
      updateDownloadProgress(overlay, { meta: `Elapsed ${elapsed}` });
    }
  }, 250);

  try {
    const res = await fetch(url, fetchInit);
    if (!res.ok) {
      showToast("Download failed", "error");
      return;
    }
    stage = "downloading";
    const total = parseInt(res.headers.get("Content-Length") || "0", 10);
    updateDownloadProgress(overlay, {
      status: total ? "Downloading zip…" : "Downloading zip…",
      bar: total ? 0 : -1,
    });

    const reader = res.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      const elapsed = fmtElapsed(Date.now() - startedAt);
      if (total) {
        updateDownloadProgress(overlay, {
          bar: (received / total) * 100,
          meta: `${fmtBytes(received)} / ${fmtBytes(total)} · ${elapsed}`,
        });
      } else {
        updateDownloadProgress(overlay, {
          meta: `${fmtBytes(received)} · ${elapsed}`,
        });
      }
    }
    saveBlob(new Blob(chunks), filename);
    showToast("Download ready", "success");
  } catch (e) {
    if (e && e.name === "AbortError") {
      showToast("Download cancelled");
    } else {
      showToast("Connection error", "error");
    }
  } finally {
    clearInterval(ticker);
    closeDownloadProgress(overlay);
    activeDownload = null;
  }
}

function downloadFolder(path, name) {
  const token = getToken();
  const safeName = name || "folder";
  performZipDownload({
    label: `Zipping ${safeName}`,
    filename: `${safeName}.zip`,
    url: apiUrl(`/api/files/download-folder?path=${encodeURIComponent(path)}`, "files"),
    fetchInit: {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    },
  });
}

async function downloadSelectedZip() {
  if (selected.size === 0) {
    showToast("Nothing selected", "error");
    return;
  }
  const token = getToken();
  const paths = Array.from(selected.keys());
  let filename = "files.zip";
  let label = `Zipping ${paths.length} items`;
  if (paths.length === 1) {
    const only = selected.get(paths[0]);
    filename = `${only.name}.zip`;
    label = `Zipping ${only.name}`;
  }
  await performZipDownload({
    label,
    filename,
    url: apiUrl("/api/files/download-zip", "files"),
    fetchInit: {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ paths }),
    },
  });
}

function updateSelectionBar() {
  const bar = document.getElementById("selection-bar");
  const countEl = document.getElementById("selection-count");
  bar.style.display = selectMode ? "flex" : "none";
  countEl.textContent = `${selected.size} selected`;
}

function toggleSelectMode(force) {
  selectMode = typeof force === "boolean" ? force : !selectMode;
  if (!selectMode) selected.clear();
  const btn = document.getElementById("select-toggle-btn");
  const label = document.getElementById("select-toggle-label");
  btn.classList.toggle("active", selectMode);
  label.textContent = selectMode ? "Done" : "Select";
  updateSelectionBar();
  loadFiles(currentPath);
}

async function uploadFiles(fileList) {
  if (!fileList || fileList.length === 0) return;
  const token = getToken();
  const files = Array.from(fileList);
  let ok = 0;
  let fail = 0;
  for (const file of files) {
    showToast(`Uploading ${file.name} (${ok + fail + 1}/${files.length})...`);
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch(apiUrl(`/api/files/upload?path=${encodeURIComponent(currentPath || "")}`, "files"), {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      if (res.ok) ok++;
      else fail++;
    } catch {
      fail++;
    }
  }
  if (fail === 0) {
    showToast(`Uploaded ${ok} file${ok > 1 ? "s" : ""}`, "success");
  } else if (ok === 0) {
    showToast(`Upload failed (${fail})`, "error");
  } else {
    showToast(`Uploaded ${ok}, ${fail} failed`, "error");
  }
  loadFiles(currentPath);
}

export async function loadFiles(path) {
  const token = getToken();
  const listEl = document.getElementById("file-list");
  listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div>Loading...</div>';

  try {
    const url = apiUrl(path ? `/api/files?path=${encodeURIComponent(path)}` : "/api/files", "files");
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div>Failed to load files</div>';
      return;
    }
    const data = await res.json();
    if (data.path !== currentPath) {
      // Changing folders clears selection
      selected.clear();
      updateSelectionBar();
    }
    currentPath = data.path;
    listEl.dataset.entries = "[]";

    buildBreadcrumb(data.path, data.home);

    if (data.entries.length === 0) {
      listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div>Empty folder</div>';
      return;
    }

    listEl.innerHTML = "";

    if (data.parent && !selectMode) {
      const item = document.createElement("div");
      item.className = "file-item";
      item.innerHTML = `<div class="file-icon">⬆️</div><div class="file-info"><div class="file-name">..</div><div class="file-meta">Parent folder</div></div>`;
      item.addEventListener("click", () => loadFiles(data.parent));
      listEl.appendChild(item);
    }

    data.entries.forEach((entry) => {
      const item = document.createElement("div");
      item.className = "file-item";
      const isSelected = selected.has(entry.path);
      if (isSelected) item.classList.add("selected");

      let actions;
      if (selectMode) {
        actions = "";
      } else if (entry.is_dir) {
        actions = `<div class="file-actions"><button class="icon-btn dlf" title="Download folder as zip">⬇️</button><button class="icon-btn del" title="Delete">🗑</button></div>`;
      } else {
        actions = `<div class="file-actions"><button class="icon-btn dl" title="Download">⬇️</button><button class="icon-btn del" title="Delete">🗑</button></div>`;
      }

      const checkbox = selectMode
        ? `<div class="file-checkbox">✓</div>`
        : "";

      item.innerHTML = `
        ${checkbox}
        <div class="file-icon"></div>
        <div class="file-info">
          <div class="file-name"></div>
          <div class="file-meta"></div>
        </div>
        ${actions}
      `;
      item.querySelector(".file-icon").textContent = getFileIcon(entry);
      item.querySelector(".file-name").textContent = entry.name;
      item.querySelector(".file-meta").textContent = `${entry.is_dir ? "Folder" : entry.size_fmt} · ${formatDate(entry.modified)}`;

      if (selectMode) {
        item.addEventListener("click", () => {
          if (selected.has(entry.path)) {
            selected.delete(entry.path);
            item.classList.remove("selected");
          } else {
            selected.set(entry.path, { name: entry.name, is_dir: entry.is_dir });
            item.classList.add("selected");
          }
          updateSelectionBar();
        });
      } else {
        if (entry.is_dir) {
          item.addEventListener("click", (e) => {
            if (!e.target.closest(".file-actions")) loadFiles(entry.path);
          });
        }

        const dlBtn = item.querySelector(".dl");
        if (dlBtn) dlBtn.addEventListener("click", (e) => { e.stopPropagation(); downloadFile(entry.path, entry.name); });

        const dlfBtn = item.querySelector(".dlf");
        if (dlfBtn) dlfBtn.addEventListener("click", (e) => { e.stopPropagation(); downloadFolder(entry.path, entry.name); });

        item.querySelector(".del").addEventListener("click", (e) => {
          e.stopPropagation();
          deleteEntry(entry.path, entry.name);
        });
      }

      listEl.appendChild(item);
    });

    // Cache entries for "select all"
    listEl.dataset.entries = JSON.stringify(data.entries.map((e) => ({ path: e.path, name: e.name, is_dir: e.is_dir })));
  } catch {
    listEl.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div>Connection error</div>';
  }
}

export function initFiles() {
  document.getElementById("refresh-files-btn").addEventListener("click", () => loadFiles(currentPath));

  document.getElementById("select-toggle-btn").addEventListener("click", () => toggleSelectMode());

  document.getElementById("select-all-btn").addEventListener("click", () => {
    const listEl = document.getElementById("file-list");
    const entries = JSON.parse(listEl.dataset.entries || "[]");
    entries.forEach((e) => selected.set(e.path, { name: e.name, is_dir: e.is_dir }));
    updateSelectionBar();
    loadFiles(currentPath);
  });

  document.getElementById("select-clear-btn").addEventListener("click", () => {
    selected.clear();
    updateSelectionBar();
    loadFiles(currentPath);
  });

  document.getElementById("download-selected-btn").addEventListener("click", downloadSelectedZip);

  const uploadInput = document.getElementById("upload-input");
  uploadInput.addEventListener("change", async () => {
    const files = uploadInput.files;
    if (!files || files.length === 0) return;
    await uploadFiles(files);
    uploadInput.value = "";
  });

  initDragAndDrop();
}

// ─── Drag-and-drop upload ────────────────────────────────────────────
function initDragAndDrop() {
  const page = document.getElementById("files-page");
  if (!page) return;

  // Block the browser's default "open file" behavior for drops anywhere
  // on the page so an accidental miss doesn't navigate away.
  ["dragover", "drop"].forEach((evt) => {
    window.addEventListener(evt, (e) => {
      if (page.classList.contains("active")) e.preventDefault();
    });
  });

  let depth = 0;

  page.addEventListener("dragenter", (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    depth++;
    page.classList.add("drag-active");
  });

  page.addEventListener("dragover", (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });

  page.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) page.classList.remove("drag-active");
  });

  page.addEventListener("drop", async (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    depth = 0;
    page.classList.remove("drag-active");

    const files = collectDroppedFiles(e.dataTransfer);
    if (files.length === 0) {
      // Folder drops land here too — spec is flat-files only.
      showToast("No valid files to upload (folders aren't supported)", "error");
      return;
    }
    await uploadFiles(files);
  });
}

function hasFiles(dt) {
  if (!dt) return false;
  if (dt.types) {
    for (const t of dt.types) {
      if (t === "Files" || t === "application/x-moz-file") return true;
    }
  }
  return false;
}

// Spec: flat file list only — folders are out of scope. Use webkitGetAsEntry
// when available so we can SKIP directories explicitly (otherwise dt.files
// would silently include the directory as a zero-byte File on some browsers).
function collectDroppedFiles(dt) {
  const out = [];
  const items = dt.items;
  if (items && items.length && typeof items[0].webkitGetAsEntry === "function") {
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind !== "file") continue;
      const entry = items[i].webkitGetAsEntry?.();
      if (entry && entry.isDirectory) continue; // skip folders
      const f = items[i].getAsFile();
      if (f) out.push(f);
    }
    return out;
  }
  // Fallback: plain file drop (older browsers without the entries API).
  if (dt.files) {
    for (let i = 0; i < dt.files.length; i++) out.push(dt.files[i]);
  }
  return out;
}
