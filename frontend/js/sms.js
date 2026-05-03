// Mirrors the phone's SMS inbox and (optionally) auto-copies OTP codes
// to the laptop clipboard. Live updates over /ws/sms; one-shot REST
// fetch on init so the panel renders immediately.
import { getToken } from "./auth.js";
import { showToast } from "./utils.js";

let ws = null;
let reconnectTimer = null;
let inited = false;
let active = false;        // false ⇒ no reconnect, ignore late events
let settings = { auto_copy: true };
let listClickHandler = null;
let toggleChangeHandler = null;

function setStatus(text, kind = "") {
  const el = document.getElementById("sms-status");
  if (!el) return;
  el.textContent = text;
  el.className = `muted-pill ${kind}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
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

function renderToggle() {
  const cb = document.getElementById("sms-autocopy");
  if (cb) cb.checked = !!settings.auto_copy;
}

function render(snapshot) {
  if (snapshot.settings) {
    settings = snapshot.settings;
    renderToggle();
  }
  const list = document.getElementById("sms-list");
  if (!list) return;

  if (!snapshot.connected) {
    const err = snapshot.last_error || "";
    if (err === "adb not installed") {
      setStatus("adb missing");
      list.innerHTML = `<div class="notif-empty">ADB isn't installed on the laptop. Run <code>sudo apt install adb</code> to enable SMS mirroring.</div>`;
    } else if (err.includes("permission")) {
      setStatus("permission");
      list.innerHTML = `<div class="notif-empty">${escapeHtml(err)}</div>`;
    } else {
      setStatus("no phone");
      list.innerHTML = `<div class="notif-empty">Connect your phone over ADB to read SMS here.</div>`;
    }
    return;
  }

  const items = snapshot.messages || [];
  if (!items.length) {
    setStatus(`${snapshot.serial || "phone"} · empty`, "ok");
    list.innerHTML = `<div class="notif-empty">No messages in your inbox.</div>`;
    return;
  }

  setStatus(`${items.length} recent`, "ok");
  list.innerHTML = items.map((m) => {
    const addr = escapeHtml(m.address || "Unknown");
    const body = escapeHtml(m.body || "");
    const ago = escapeHtml(timeAgo(m.date));
    const code = m.otp;
    const codeBadge = code
      ? `<button class="notif-btn open sms-copy-btn" data-code="${escapeHtml(code)}">Copy ${escapeHtml(code)}</button>`
      : "";
    return `
      <div class="notif-item sms-item">
        <div class="notif-body">
          <div class="notif-meta">
            <span class="notif-app">${addr}</span>
            <span class="notif-time">${ago}</span>
          </div>
          <div class="notif-text sms-body">${body}</div>
        </div>
        ${codeBadge ? `<div class="notif-actions">${codeBadge}</div>` : ""}
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
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

async function copyCode(code) {
  // Push to laptop clipboard via the existing /api/clipboard endpoint
  // so behaviour matches what the auto-copy path does on the server.
  try {
    await postJson("/api/clipboard", { text: code });
    showToast(`OTP ${code} copied to laptop clipboard`);
  } catch {
    showToast("Copy failed", "error");
  }
}

function handleClick(e) {
  const btn = e.target.closest(".sms-copy-btn");
  if (!btn) return;
  const code = btn.dataset.code;
  if (code) copyCode(code);
}

async function handleToggle(e) {
  const enabled = !!e.target.checked;
  try {
    const res = await postJson("/api/sms/settings", { auto_copy: enabled });
    settings = res;
    renderToggle();
    showToast(enabled ? "Auto-copy ON" : "Auto-copy OFF");
  } catch {
    showToast("Failed to save setting", "error");
    e.target.checked = !enabled;
  }
}

async function fetchOnce() {
  try {
    const token = getToken();
    const res = await fetch("/api/sms/recent", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.ok) render(await res.json());
  } catch { /* ignore — WS will fill in */ }
}

function handleFrame(payload) {
  if (!payload || !payload.type) return;
  if (payload.type === "snapshot") {
    render(payload);
  } else if (payload.type === "otp") {
    if (payload.copied) {
      showToast(`OTP ${payload.code} copied to clipboard`);
    } else if (!payload.auto_copy_enabled) {
      // Auto-copy is off — just hint that there's a code waiting.
      showToast(`OTP ${payload.code} received`);
    } else {
      showToast(`Got OTP ${payload.code} (clipboard write failed)`, "error");
    }
  }
}

function connect() {
  if (!active) return;
  const token = getToken();
  if (!token) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/sms?token=${encodeURIComponent(token)}`;
  try {
    ws = new WebSocket(url);
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };
  ws.onmessage = (ev) => {
    if (!active) return;
    try { handleFrame(JSON.parse(ev.data)); } catch { /* ignore bad frames */ }
  };
  ws.onclose = () => { ws = null; if (active) scheduleReconnect(); };
  ws.onerror = () => { try { ws && ws.close(); } catch {} };
}

function scheduleReconnect() {
  if (!active || reconnectTimer) return;
  setStatus("reconnecting…");
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (active) connect();
  }, 3000);
}

export function initSms() {
  if (inited) return;
  inited = true;
  active = true;
  const list = document.getElementById("sms-list");
  if (list) {
    listClickHandler = handleClick;
    list.addEventListener("click", listClickHandler);
  }
  const cb = document.getElementById("sms-autocopy");
  if (cb) {
    toggleChangeHandler = handleToggle;
    cb.addEventListener("change", toggleChangeHandler);
  }
  fetchOnce();
  connect();
}

export function teardownSms() {
  active = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (ws) { try { ws.close(); } catch {} ws = null; }
  const list = document.getElementById("sms-list");
  if (list && listClickHandler) list.removeEventListener("click", listClickHandler);
  listClickHandler = null;
  const cb = document.getElementById("sms-autocopy");
  if (cb && toggleChangeHandler) cb.removeEventListener("change", toggleChangeHandler);
  toggleChangeHandler = null;
  inited = false;
}
