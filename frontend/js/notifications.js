// Mirrors the phone's active notifications inside the dashboard card.
// Uses a WebSocket for live updates, with a one-shot REST fetch as the
// initial render so the card is never blank while the WS handshake runs.
import { getToken, openTicketedWebSocket } from "./auth.js";
import { showToast } from "./utils.js";

let ws = null;
let reconnectTimer = null;
let lastSnapshot = null;
let inited = false;

const FRIENDLY_APPS = {
  "com.android.systemui": "System",
  "com.google.android.gm": "Gmail",
  "com.google.android.apps.messaging": "Messages",
  "com.whatsapp": "WhatsApp",
  "com.slack": "Slack",
  "com.discord": "Discord",
  "com.spotify.music": "Spotify",
};

function appLabel(pkg) {
  if (!pkg) return "Unknown";
  if (FRIENDLY_APPS[pkg]) return FRIENDLY_APPS[pkg];
  // Fallback: last dotted segment, prettified.
  const tail = pkg.split(".").pop() || pkg;
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

function timeAgo(whenMs) {
  if (!whenMs) return "";
  const diff = Math.max(0, Date.now() - whenMs);
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function setStatus(text, kind = "") {
  const el = document.getElementById("notif-status");
  if (!el) return;
  el.textContent = text;
  el.className = `muted-pill ${kind}`;
}

function render(snapshot) {
  lastSnapshot = snapshot;
  const list = document.getElementById("notif-list");
  if (!list) return;

  if (!snapshot.connected) {
    const err = snapshot.last_error || "";
    if (err === "adb not installed") {
      setStatus("adb missing");
      list.innerHTML = `<div class="notif-empty">ADB isn't installed on the laptop. Run <code>sudo apt install adb</code> to enable notification mirroring.</div>`;
    } else {
      setStatus("no phone");
      list.innerHTML = `<div class="notif-empty">Connect your phone over ADB to see notifications here.</div>`;
    }
    return;
  }

  const items = snapshot.items || [];
  if (!items.length) {
    setStatus(`${snapshot.serial || "phone"} · clear`, "ok");
    list.innerHTML = `<div class="notif-empty">No active notifications. ✨</div>`;
    return;
  }

  setStatus(`${items.length} active`, "ok");
  list.innerHTML = items.map((n) => {
    const title = escapeHtml(n.title || appLabel(n.package));
    const text = escapeHtml(n.text || n.subtext || "");
    const app = escapeHtml(appLabel(n.package));
    const ago = escapeHtml(timeAgo(n.when));
    const key = escapeHtml(n.key);
    const pkg = escapeHtml(n.package || "");
    return `
      <div class="notif-item" data-key="${key}" data-pkg="${pkg}">
        <div class="notif-body">
          <div class="notif-meta">
            <span class="notif-app">${app}</span>
            <span class="notif-time">${ago}</span>
          </div>
          <div class="notif-title">${title}</div>
          ${text ? `<div class="notif-text">${text}</div>` : ""}
        </div>
        <div class="notif-actions">
          <button class="notif-btn open" data-act="open" ${pkg ? "" : "disabled"}>Open</button>
          <button class="notif-btn dismiss" data-act="dismiss">Dismiss</button>
        </div>
      </div>`;
  }).join("");
}

async function postJson(url, body) {
  const token = getToken();
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${txt || res.statusText}`);
  }
  return res.json();
}

async function handleClick(e) {
  const btn = e.target.closest(".notif-btn");
  if (!btn) return;
  const item = btn.closest(".notif-item");
  if (!item) return;
  const act = btn.dataset.act;
  const key = item.dataset.key;
  const pkg = item.dataset.pkg;

  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "…";
  try {
    if (act === "dismiss") {
      const r = await postJson("/api/notifications/dismiss", { key });
      if (r.ok) {
        item.style.opacity = "0.4";
        showToast("Dismissed on phone");
      } else {
        showToast(r.error || "Dismiss failed", "error");
      }
    } else if (act === "open") {
      const r = await postJson("/api/notifications/open", { package: pkg });
      if (r.ok) {
        showToast(`Opened ${appLabel(pkg)} on phone`);
      } else {
        showToast(r.error || "Open failed", "error");
      }
    }
  } catch (err) {
    showToast(`Action failed: ${err.message}`, "error");
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

async function fetchOnce() {
  try {
    const token = getToken();
    const res = await fetch("/api/notifications", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) render(await res.json());
  } catch { /* ignore — WS will fill in */ }
}

async function connect() {
  const token = getToken();
  if (!token) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/notifications`;
  try {
    ws = await openTicketedWebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch { /* ignore bad frames */ }
  };
  ws.onclose = () => { ws = null; scheduleReconnect(); };
  ws.onerror = () => { try { ws && ws.close(); } catch {} };
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  setStatus("reconnecting…");
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 3000);
}

export function initNotifications() {
  if (inited) return;
  inited = true;
  const list = document.getElementById("notif-list");
  if (list) list.addEventListener("click", handleClick);
  fetchOnce();
  connect();
}

export function teardownNotifications() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (ws) { try { ws.close(); } catch {} ws = null; }
  inited = false;
  lastSnapshot = null;
}
