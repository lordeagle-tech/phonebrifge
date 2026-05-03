// Auto-import phone photos: home-page card showing the watcher's settings,
// status, and a list of the most recent imports.
import { getToken } from "./auth.js";
import { showToast } from "./utils.js";

let pollTimer = null;
let inited = false;
let lastSettings = { enabled: false, destination: "" };

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const token = getToken();
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(opts.headers || {}),
    },
  });
  let body = null;
  try { body = await res.json(); } catch { /* not json */ }
  if (!res.ok) {
    const msg = (body && (body.detail || body.message)) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function setStatus(text, kind) {
  const el = $("photos-status");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
}

function setHint(text) {
  const el = $("photos-hint");
  if (el) el.textContent = text;
}

function renderStatus(s) {
  if (!s.enabled) {
    setStatus("off");
    return;
  }
  if (s.last_error) {
    setStatus("error", "err");
  } else if (s.last_import_at) {
    setStatus(`watching · last copy ${fmtAgo(s.last_import_at)}`, "ok");
  } else {
    setStatus("watching", "ok");
  }
  if (s.last_action) setHint(s.last_action);
}

function renderRecent(items) {
  const list = $("photos-recent");
  if (!list) return;
  if (!items || items.length === 0) {
    list.innerHTML = '<div class="notif-empty">No photos imported yet.</div>';
    return;
  }
  list.innerHTML = "";
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "notif-item";
    row.innerHTML = `
      <div class="notif-title">${escapeHtml(it.name)}</div>
      <div class="notif-body">${fmtBytes(it.size)} · ${fmtAgo(it.ts)}</div>
    `;
    list.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function refresh() {
  try {
    const [s, r] = await Promise.all([
      api("/api/photos/settings"),
      api("/api/photos/recent"),
    ]);
    lastSettings = { enabled: !!s.enabled, destination: s.destination || "" };
    const enabledEl = $("photos-enabled");
    const destEl = $("photos-dest");
    if (enabledEl && document.activeElement !== enabledEl) enabledEl.checked = lastSettings.enabled;
    if (destEl && document.activeElement !== destEl) destEl.value = lastSettings.destination;
    renderStatus(s);
    renderRecent(r.recent || []);
  } catch {
    setStatus("error", "err");
  }
}

async function save() {
  const enabled = !!$("photos-enabled").checked;
  const destination = $("photos-dest").value.trim() || lastSettings.destination;
  try {
    await api("/api/photos/settings", {
      method: "POST",
      body: JSON.stringify({ enabled, destination }),
    });
    showToast(enabled ? "Auto-import on" : "Auto-import off", "success");
    await refresh();
  } catch (e) {
    showToast(`Save failed: ${e.message}`, "error");
  }
}

export async function initPhotos() {
  if (inited) return;
  inited = true;
  const saveBtn = $("photos-save-btn");
  const enabledEl = $("photos-enabled");
  if (saveBtn) saveBtn.addEventListener("click", save);
  if (enabledEl) enabledEl.addEventListener("change", save);
  await refresh();
  if (!pollTimer) pollTimer = setInterval(refresh, 5000);
}

export function teardownPhotos() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  // Symmetrical with initPhotos so re-login doesn't stack listeners and
  // amplify save requests.
  const saveBtn = $("photos-save-btn");
  const enabledEl = $("photos-enabled");
  if (saveBtn) saveBtn.removeEventListener("click", save);
  if (enabledEl) enabledEl.removeEventListener("change", save);
  inited = false;
}
