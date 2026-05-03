// "Find My Phone" — fires the loud alarm on the chosen phone.
// Lists every ready device in a selector when more than one is connected,
// then streams the live countdown from /api/scrcpy/ring/status while a
// ring is active.
import { getToken } from "./auth.js";
import { showToast } from "./utils.js";

let pollTimer = null;
let inited = false;
let devices = [];   // [{serial, name}]
let activeSerial = null;  // serial currently ringing (server-confirmed)

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

function selectedSerial() {
  const sel = $("ring-device");
  if (sel && sel.value) return sel.value;
  return devices.length === 1 ? devices[0].serial : null;
}

function setBtn(label, danger = false, disabled = false) {
  const btn = $("ring-btn");
  if (!btn) return;
  btn.textContent = label;
  btn.classList.toggle("danger", !!danger);
  btn.disabled = !!disabled;
}

function setHint(text) {
  const el = $("ring-hint");
  if (el) el.textContent = text;
}

function renderDeviceSelector() {
  const sel = $("ring-device");
  if (!sel) return;
  // Hide selector when only 0 or 1 devices are present.
  if (devices.length <= 1) {
    sel.style.display = "none";
    sel.innerHTML = "";
    return;
  }
  const prev = sel.value;
  sel.innerHTML = "";
  for (const d of devices) {
    const opt = document.createElement("option");
    opt.value = d.serial;
    opt.textContent = `${d.name || d.serial} — ${d.serial}`;
    sel.appendChild(opt);
  }
  if (prev && devices.some((d) => d.serial === prev)) sel.value = prev;
  sel.style.display = "block";
}

async function loadDevices() {
  try {
    const r = await api("/api/scrcpy/devices");
    devices = (r.devices || [])
      .filter((d) => d.state === "device")
      .map((d) => ({ serial: d.serial, name: d.name || d.serial }));
  } catch {
    devices = [];
  }
  renderDeviceSelector();
}

function renderIdle() {
  activeSerial = null;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (!devices.length) {
    setBtn("📢  Ring phone", false, true);
    setHint("Connect a phone over ADB to enable ring.");
    return;
  }
  setBtn("📢  Ring phone", false, false);
  const target = selectedSerial();
  if (devices.length > 1 && !target) {
    setHint("Pick a device above, then press Ring.");
  } else {
    const dev = devices.find((d) => d.serial === target) || devices[0];
    setHint(`Will ring ${dev.name} at full volume for ~30 s.`);
  }
}

function renderActive(state) {
  activeSerial = state.serial;
  const remaining = Math.max(0, Math.round(state.remaining || 0));
  setBtn(`⏹  Stop ringing (${remaining}s)`, true, false);
  const notes = (state.notes || []).filter(Boolean);
  setHint(notes.length
    ? `Ringing ${state.serial} · ${notes.join(" · ")}`
    : `Ringing ${state.serial}…`);
}

async function refreshStatus() {
  // Look across all serials so a ring started from another tab / page
  // (e.g. mirror-setup) is reflected here too.
  try {
    const r = await api("/api/scrcpy/ring/status");
    const rings = r.rings || [];
    if (rings.length) {
      // Prefer the ring matching the selector; otherwise the first.
      const sel = selectedSerial();
      const match = (sel && rings.find((x) => x.serial === sel)) || rings[0];
      renderActive(match);
      if (!pollTimer) pollTimer = setInterval(refreshStatus, 1000);
      return;
    }
  } catch { /* ignore */ }
  renderIdle();
}

async function startRing() {
  const serial = selectedSerial();
  if (!serial) {
    showToast("No phone connected over ADB", "error");
    return;
  }
  setBtn("Ringing…", true, true);
  try {
    const r = await api("/api/scrcpy/ring/start", {
      method: "POST",
      body: JSON.stringify({ serial }),
    });
    showToast(r.extended ? "Ring extended" : "Phone is ringing");
    renderActive(r);
    if (!pollTimer) pollTimer = setInterval(refreshStatus, 1000);
  } catch (e) {
    showToast(`Ring failed: ${e.message}`, "error");
    renderIdle();
  }
}

async function stopRing() {
  const serial = activeSerial;
  if (!serial) { renderIdle(); return; }
  try {
    await api("/api/scrcpy/ring/stop", {
      method: "POST",
      body: JSON.stringify({ serial }),
    });
    showToast("Ringing stopped");
  } catch (e) {
    showToast(`Stop failed: ${e.message}`, "error");
  } finally {
    renderIdle();
  }
}

async function onClick() {
  const btn = $("ring-btn");
  if (!btn || btn.disabled) return;
  if (btn.classList.contains("danger")) await stopRing();
  else await startRing();
}

function onSelectChange() {
  // If we're idle, just refresh the hint to reflect the new pick. If a
  // ring is active we leave it alone — the active row keeps its target.
  if (!activeSerial) renderIdle();
}

export async function initRing() {
  if (inited) return;
  inited = true;
  const btn = $("ring-btn");
  const sel = $("ring-device");
  if (btn) btn.addEventListener("click", onClick);
  if (sel) sel.addEventListener("change", onSelectChange);
  await loadDevices();
  await refreshStatus();
}

export function teardownRing() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const btn = $("ring-btn");
  const sel = $("ring-device");
  if (btn) btn.removeEventListener("click", onClick);
  if (sel) sel.removeEventListener("change", onSelectChange);
  inited = false;
  devices = [];
  activeSerial = null;
}
