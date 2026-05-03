import { initLogin, getToken, checkAuth, setOnSessionLost } from "./auth.js";
import { showToast } from "./utils.js";

const $ = (id) => document.getElementById(id);

let statusTimer = null;
let qrTimer = null;
let recentTimer = null;
const serialNames = new Map(); // serial -> friendly name (from /devices)

function authHeaders() {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
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

function setPill(el, text, cls) {
  el.textContent = text;
  el.classList.remove("ok", "warn", "danger");
  if (cls) el.classList.add(cls);
}

async function loadDeps() {
  const sPill = $("dep-scrcpy");
  const aPill = $("dep-adb");
  const ndPill = $("dep-sndcpy");
  const vPill = $("dep-vlc");
  setPill(sPill, "scrcpy: …");
  setPill(aPill, "adb: …");
  setPill(ndPill, "sndcpy: …");
  setPill(vPill, "vlc: …");
  try {
    const r = await api("/api/scrcpy/deps");
    if (r.scrcpy.installed) {
      setPill(sPill, `scrcpy ✓ ${r.scrcpy.version || ""}`.trim(), "ok");
    } else {
      setPill(sPill, "scrcpy: missing", "danger");
    }
    if (r.adb.installed) {
      setPill(aPill, `adb ✓ ${r.adb.version ? r.adb.version.replace(/Android Debug Bridge version /, "v") : ""}`.trim(), "ok");
    } else {
      setPill(aPill, "adb: missing", "danger");
    }
    // sndcpy + vlc are optional — show as a neutral "missing" pill (warn,
    // not danger) so it's clear they're only needed for audio forwarding.
    if (r.sndcpy && r.sndcpy.installed) {
      setPill(ndPill, "sndcpy ✓", "ok");
    } else {
      setPill(ndPill, "sndcpy: missing (optional)", "warn");
    }
    if (r.vlc && r.vlc.installed) {
      setPill(vPill, "vlc ✓", "ok");
    } else {
      setPill(vPill, "vlc: missing (optional)", "warn");
    }
    const missing = !r.scrcpy.installed || !r.adb.installed;
    $("dep-install").style.display = missing ? "block" : "none";
    const audioMissing = !(r.sndcpy && r.sndcpy.installed) || !(r.vlc && r.vlc.installed);
    $("dep-audio-install").style.display = audioMissing ? "block" : "none";
    // Render the backend-supplied install hint so the guidance stays in
    // one place (server) and we don't duplicate apt/download instructions.
    const hintEl = $("audio-install-hint");
    if (hintEl) hintEl.textContent = r.audio_install_hint || "";
    // Cache the audio-deps state so the per-device audio toggle can disable
    // itself with a hint instead of failing silently after launch.
    audioDepsOk = !audioMissing;
    paintAudioPill();
    return r;
  } catch (e) {
    setPill(sPill, "scrcpy: error", "danger");
    setPill(aPill, "adb: error", "danger");
    setPill(ndPill, "sndcpy: error", "danger");
    setPill(vPill, "vlc: error", "danger");
    showToast(`Dependency check failed: ${e.message}`);
    return null;
  }
}

// Tracks whether sndcpy + vlc are both installed; updated by loadDeps().
let audioDepsOk = false;
// Last seen sndcpy state from /api/scrcpy/status (running, serial), so the
// dedicated audio pill can stay accurate even between dep rechecks.
let audioRunState = { running: false, serial: null };

function paintAudioPill() {
  const pill = $("audio-pill");
  if (!pill) return;
  if (!audioDepsOk) {
    setPill(pill, "Audio: install sndcpy + VLC", "warn");
    return;
  }
  if (audioRunState.running) {
    const who = audioRunState.serial ? ` · ${audioRunState.serial}` : "";
    setPill(pill, `Audio: on${who}`, "ok");
  } else {
    setPill(pill, "Audio: off");
  }
}

function devTagFor(state) {
  const s = (state || "").toLowerCase();
  if (s === "device") return { cls: "", text: "ready" };
  if (s === "unauthorized") return { cls: "unauth", text: "unauthorized" };
  if (s === "offline") return { cls: "offline", text: "offline" };
  return { cls: "offline", text: state || "?" };
}

function renderDevices(devices) {
  const list = $("dev-list");
  const select = $("launch-serial");
  list.innerHTML = "";
  select.innerHTML = "";
  if (!devices.length) {
    list.innerHTML = `<div class="ms-empty">No devices found. Plug in over USB (with USB debugging on) or pair wirelessly below.</div>`;
    select.innerHTML = `<option value="">(no devices)</option>`;
    return;
  }
  for (const d of devices) {
    const tag = devTagFor(d.state);
    const row = document.createElement("div");
    row.className = "ms-dev";
    row.innerHTML = `
      <div class="ms-dev-info">
        <div class="ms-dev-name">${escapeHtml(d.name)} ${d.wireless ? "📶" : "🔌"}</div>
        <div class="ms-dev-meta">${escapeHtml(d.serial)}</div>
      </div>
      <span class="ms-dev-tag ${tag.cls}">${tag.text}</span>
    `;
    list.appendChild(row);

    if (d.state === "device") {
      const opt = document.createElement("option");
      opt.value = d.serial;
      opt.textContent = `${d.name} — ${d.serial}`;
      opt.dataset.name = d.name;
      select.appendChild(opt);
      serialNames.set(d.serial, d.name);
    }
  }
  if (!select.options.length) {
    select.innerHTML = `<option value="">(no ready devices — accept the USB-debug prompt on the phone)</option>`;
  }
}

async function detectDevices(showSpinner = true) {
  const btn = $("dev-refresh-btn");
  const errEl = $("dev-error");
  errEl.textContent = "";
  if (showSpinner) btn.disabled = true;
  try {
    const r = await api("/api/scrcpy/devices");
    renderDevices(r.devices || []);
  } catch (e) {
    errEl.textContent = `Could not list devices: ${e.message}`;
    renderDevices([]);
  } finally {
    btn.disabled = false;
  }
}

function fmtAttempt(ts) {
  if (!ts) return "never";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
}

function renderWatcher(w) {
  const pill = $("watcher-pill");
  const line = $("watcher-line");
  if (!w) { pill.textContent = "Watcher: unknown"; line.textContent = ""; return; }
  if (w.auto_count > 0) {
    pill.textContent = `Watcher: on · ${w.auto_count} device${w.auto_count === 1 ? "" : "s"}`;
    pill.classList.remove("warn", "danger"); pill.classList.add("ok");
  } else {
    pill.textContent = "Watcher: idle";
    pill.classList.remove("ok", "warn", "danger");
  }
  const parts = [];
  parts.push(`every ${Math.round(w.interval || 15)}s`);
  if (w.last_scan) parts.push(`last scan ${fmtAttempt(w.last_scan)}`);
  if (w.last_action) parts.push(w.last_action);
  line.textContent = parts.join(" · ");
}

function scheduleRecentRefresh() {
  if (recentTimer) clearTimeout(recentTimer);
  // Cheap endpoint; 10 s keeps the watcher pill + per-device "last attempt"
  // labels live without spamming the server.
  recentTimer = setTimeout(loadRecent, 10000);
}

async function loadRecent() {
  const list = $("recent-list");
  try {
    const r = await api("/api/scrcpy/recent");
    renderWatcher(r.watcher);
    scheduleRecentRefresh();
    const items = r.recent || [];
    if (!items.length) {
      list.innerHTML = `<div class="ms-empty">No remembered devices yet — they'll appear here after the first launch.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const d of items) {
      const row = document.createElement("div");
      row.className = "ms-dev";
      const when = d.last_used ? new Date(d.last_used * 1000).toLocaleString() : "";
      const presetOpts = ["default", "low-bitrate", "record"]
        .map((p) => `<option value="${p}"${(d.auto_preset || "default") === p ? " selected" : ""}>${p}</option>`)
        .join("");
      const lastAttempt = d.last_attempt
        ? `last attempt ${fmtAttempt(d.last_attempt)} — ${escapeHtml(d.last_outcome || "unknown")}`
        : "never auto-launched";
      const qOverride = d.quality_override || "auto";
      const qOpts = [
        ["auto", "Auto-tune"],
        ["high", "Force high"],
        ["medium", "Force medium"],
        ["low", "Force low"],
      ].map(([v, label]) =>
        `<option value="${v}"${qOverride === v ? " selected" : ""}>${label}</option>`
      ).join("");
      const lq = d.last_quality;
      let tunePill = "";
      if (lq && lq.profile) {
        const cls = lq.profile === "high" ? "ok"
          : lq.profile === "medium" ? "" : "warn";
        let detail;
        if (lq.override) {
          detail = "forced";
        } else if (lq.rtt_ms != null) {
          detail = `RTT ${Math.round(lq.rtt_ms)}ms`;
        } else {
          detail = lq.source || "n/a";
        }
        tunePill = `<span class="ms-pill ${cls}" style="margin-left:6px"
          title="Last tuning: ${escapeHtml(JSON.stringify(lq))}">Tuned · ${escapeHtml(lq.profile)} · ${escapeHtml(detail)}</span>`;
      }
      const audioTitle = audioDepsOk
        ? "Routes phone audio to the laptop via sndcpy + VLC when the mirror launches."
        : "Install sndcpy + VLC (see Dependencies above) to enable.";
      row.innerHTML = `
        <div class="ms-dev-info">
          <div class="ms-dev-name">${escapeHtml(d.name || d.serial)}</div>
          <div class="ms-dev-meta">${escapeHtml(d.serial)} · last used ${escapeHtml(when)}</div>
          <div class="ms-dev-meta">
            <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
              <input type="checkbox" data-action="auto"${d.auto ? " checked" : ""} />
              Auto-mirror when this device comes online
            </label>
            <select data-action="auto-preset" class="ms-input" style="max-width:140px;margin-left:8px">${presetOpts}</select>
            <select data-action="quality" class="ms-input" style="max-width:140px;margin-left:8px"
                    title="Auto-tune picks low/medium/high based on a quick link probe before each launch. Force a profile to skip the probe.">${qOpts}</select>
            ${tunePill}
            <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;margin-left:12px"
                   title="${escapeHtml(audioTitle)}">
              <input type="checkbox" data-action="audio"${d.audio ? " checked" : ""}${audioDepsOk ? "" : " disabled"} />
              🔊 Play audio on laptop
            </label>
            <div style="margin-top:2px;font-size:11px;color:#94a3b8">${lastAttempt}</div>
          </div>
        </div>
        <button class="ms-btn primary" data-action="reconnect">Reconnect &amp; launch</button>
        <button class="ms-btn" data-action="ring" data-serial="${escapeHtml(d.serial)}">📢 Ring</button>
        <button class="ms-btn" data-action="forget">Forget</button>
      `;
      const cb = row.querySelector('[data-action="auto"]');
      const sel = row.querySelector('[data-action="auto-preset"]');
      cb.addEventListener("change", () => setAuto(d.serial, cb.checked, sel.value));
      sel.addEventListener("change", () => {
        // Only persist preset when auto is enabled (otherwise just remember choice locally).
        if (cb.checked) setAuto(d.serial, true, sel.value);
      });
      const audioCb = row.querySelector('[data-action="audio"]');
      if (audioCb) {
        audioCb.addEventListener("change", () => setAudio(d.serial, audioCb.checked));
      }
      const qSel = row.querySelector('[data-action="quality"]');
      if (qSel) {
        qSel.addEventListener("change", () => setQuality(d.serial, qSel.value));
      }
      row.querySelector('[data-action="reconnect"]').addEventListener("click", () =>
        reconnectAndLaunch(d.serial));
      row.querySelector('[data-action="ring"]').addEventListener("click", (e) =>
        toggleRing(d.serial, e.currentTarget));
      row.querySelector('[data-action="forget"]').addEventListener("click", () =>
        forgetDevice(d.serial));
      list.appendChild(row);
    }
    // Sync per-row Ring buttons with server state (live countdown if any).
    refreshRingButtons();
  } catch (e) {
    list.innerHTML = `<div class="ms-error">Could not load recents: ${escapeHtml(e.message)}</div>`;
  }
}

async function loadQuietHours() {
  try {
    const r = await api("/api/scrcpy/quiet-hours");
    renderQuietHours(r);
  } catch (e) {
    $("quiet-error").textContent = `Could not load quiet hours: ${e.message}`;
  }
}

function renderQuietHours(q) {
  $("quiet-enabled").checked = !!q.enabled;
  $("quiet-start").value = q.start || "22:00";
  $("quiet-end").value = q.end || "07:00";
  const pill = $("quiet-pill");
  pill.classList.remove("ok", "warn", "danger");
  if (!q.enabled) {
    pill.textContent = "Off";
  } else if (q.now_quiet) {
    pill.textContent = `Quiet now · ${q.start}–${q.end}`;
    pill.classList.add("warn");
  } else {
    pill.textContent = `On · ${q.start}–${q.end}`;
    pill.classList.add("ok");
  }
  const hint = $("quiet-hint");
  if (q.enabled && q.start === q.end) {
    hint.textContent = "Start and end are equal — quiet window is empty.";
  } else if (q.enabled) {
    const wrap = q.start > q.end ? " (wraps past midnight)" : "";
    hint.textContent = `Auto-launch is suppressed between ${q.start} and ${q.end}${wrap}.`;
  } else {
    hint.textContent = "";
  }
}

async function saveQuietHours() {
  const enabled = $("quiet-enabled").checked;
  const start = $("quiet-start").value;
  const end = $("quiet-end").value;
  $("quiet-error").textContent = "";
  $("quiet-save-btn").disabled = true;
  try {
    const r = await api("/api/scrcpy/quiet-hours", {
      method: "POST",
      body: JSON.stringify({ enabled, start, end }),
    });
    renderQuietHours(r);
    showToast(enabled ? "Quiet hours saved" : "Quiet hours disabled");
    // Refresh the watcher line so the user sees the new "skipping" reason promptly.
    loadRecent();
  } catch (e) {
    $("quiet-error").textContent = `Save failed: ${e.message}`;
  } finally {
    $("quiet-save-btn").disabled = false;
  }
}

async function setQuality(serial, override) {
  try {
    await api("/api/scrcpy/quality", {
      method: "POST",
      body: JSON.stringify({ serial, override }),
    });
    showToast(override === "auto"
      ? "Auto-tune re-enabled — link will be probed before each launch"
      : `Quality forced to ${override} for this device`);
  } catch (e) {
    showToast(`Could not set quality: ${e.message}`);
    loadRecent();
  }
}

async function setAudio(serial, enabled) {
  try {
    await api("/api/scrcpy/audio", {
      method: "POST",
      body: JSON.stringify({ serial, enabled }),
    });
    showToast(enabled
      ? "Phone audio will play on this laptop on the next launch"
      : "Phone audio routing turned off");
  } catch (e) {
    showToast(`Could not update audio: ${e.message}`);
    loadRecent();
  }
}

async function setAuto(serial, enabled, preset) {
  try {
    const r = await api("/api/scrcpy/auto", {
      method: "POST",
      body: JSON.stringify({ serial, enabled, preset }),
    });
    renderWatcher(r.watcher);
    showToast(enabled ? "Auto-mirror on" : "Auto-mirror off");
  } catch (e) {
    showToast(`Could not update: ${e.message}`);
    loadRecent();  // resync UI from server truth
  }
}

// Per-row ring state. We poll /api/scrcpy/ring/status (whole-bridge view)
// at 1 s intervals while at least one row is active, so each button shows
// a live countdown and reverts cleanly when the server's auto-stop fires.
let ringPollTimer = null;

function paintRingButton(btn, ringState) {
  // ringState: null/undefined => idle; { active:true, remaining:N } => ringing.
  if (ringState && ringState.active) {
    const r = Math.max(0, Math.round(ringState.remaining || 0));
    btn.textContent = `⏹ Stop (${r}s)`;
    btn.classList.add("danger");
    btn.dataset.ringing = "1";
  } else {
    btn.textContent = "📢 Ring";
    btn.classList.remove("danger");
    delete btn.dataset.ringing;
  }
}

async function refreshRingButtons() {
  const buttons = document.querySelectorAll('#recent-list [data-action="ring"]');
  if (!buttons.length) {
    if (ringPollTimer) { clearInterval(ringPollTimer); ringPollTimer = null; }
    return;
  }
  let bySerial = new Map();
  try {
    const r = await api("/api/scrcpy/ring/status");
    for (const ring of (r.rings || [])) bySerial.set(ring.serial, ring);
  } catch { /* leave existing labels alone on transient errors */ return; }

  let anyActive = false;
  for (const btn of buttons) {
    const serial = btn.dataset.serial;
    if (!serial) continue;
    const ring = bySerial.get(serial);
    paintRingButton(btn, ring);
    if (ring && ring.active) anyActive = true;
  }
  if (anyActive && !ringPollTimer) {
    ringPollTimer = setInterval(refreshRingButtons, 1000);
  } else if (!anyActive && ringPollTimer) {
    clearInterval(ringPollTimer); ringPollTimer = null;
  }
}

async function toggleRing(serial, btn) {
  const ringing = btn.dataset.ringing === "1";
  btn.disabled = true;
  try {
    if (ringing) {
      await api("/api/scrcpy/ring/stop", {
        method: "POST", body: JSON.stringify({ serial }),
      });
      showToast("Ringing stopped");
      paintRingButton(btn, null);
    } else {
      const r = await api("/api/scrcpy/ring/start", {
        method: "POST", body: JSON.stringify({ serial }),
      });
      showToast(r.extended ? "Ring extended" : "Phone is ringing");
      paintRingButton(btn, { active: true, remaining: r.remaining });
      if (!ringPollTimer) ringPollTimer = setInterval(refreshRingButtons, 1000);
    }
  } catch (e) {
    showToast(`Ring failed: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
}

async function forgetDevice(serial) {
  try {
    await api("/api/scrcpy/forget", { method: "POST", body: JSON.stringify({ serial }) });
    showToast("Forgotten");
    loadRecent();
  } catch (e) {
    showToast(`Forget failed: ${e.message}`);
  }
}

async function reconnectAndLaunch(serial) {
  // Wireless serials look like "ip:port" — re-issue adb connect first.
  if (serial.includes(":") && !serial.startsWith("emulator-")) {
    const [host, port] = serial.split(":");
    try {
      await api("/api/scrcpy/connect", {
        method: "POST",
        body: JSON.stringify({ host, port: Number(port) }),
      });
    } catch (e) {
      showToast(`Connect failed: ${e.message}`);
      return;
    }
  }
  await detectDevices(false);
  // Pick the matching option in the launch dropdown
  const select = $("launch-serial");
  for (const opt of select.options) {
    if (opt.value === serial) { select.value = serial; break; }
  }
  await launchScrcpy();
}

// ---------- Webcam QR scanner (consumes phonebridge-pair: payloads) ----------

let scanStream = null;
let scanRaf = null;
let scanDetector = null;  // BarcodeDetector instance, or null

const SCAN_CAMERA_KEY = "phonebridge.scanCameraId";

function getSavedCameraId() {
  try { return localStorage.getItem(SCAN_CAMERA_KEY) || ""; } catch { return ""; }
}

function saveCameraId(id) {
  try {
    if (id) localStorage.setItem(SCAN_CAMERA_KEY, id);
    else localStorage.removeItem(SCAN_CAMERA_KEY);
  } catch { /* ignore */ }
}

async function refreshCameraList() {
  const sel = $("scan-camera");
  if (!sel || !navigator.mediaDevices?.enumerateDevices) return;
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch { return; }
  const cams = devices.filter((d) => d.kind === "videoinput");
  const saved = getSavedCameraId();
  const current = sel.value || saved;
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = "";
  def.textContent = cams.length ? "Default camera" : "No cameras detected";
  sel.appendChild(def);
  cams.forEach((c, i) => {
    const opt = document.createElement("option");
    opt.value = c.deviceId;
    opt.textContent = c.label || `Camera ${i + 1}`;
    sel.appendChild(opt);
  });
  // Restore selection if the device is still present.
  if (current && cams.some((c) => c.deviceId === current)) {
    sel.value = current;
  }
}

function setScanStatus(text, kind) {
  const el = $("scan-status");
  el.textContent = text;
  el.style.color =
    kind === "ok" ? "#86efac" : kind === "err" ? "#fda4af" : "";
}

function parsePairPayload(raw) {
  // Accept "phonebridge-pair:host=..;port=..;code=.." (semicolon-separated
  // k=v pairs). Returns {host, port?, code?} or null.
  if (typeof raw !== "string") return null;
  const m = /^phonebridge-pair:(.+)$/i.exec(raw.trim());
  if (!m) return null;
  const out = {};
  for (const part of m[1].split(";")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    out[part.slice(0, eq).trim().toLowerCase()] = part.slice(eq + 1).trim();
  }
  if (!out.host) return null;
  return out;
}

async function stopScan() {
  if (scanRaf) { cancelAnimationFrame(scanRaf); scanRaf = null; }
  if (scanStream) {
    for (const t of scanStream.getTracks()) {
      try { t.stop(); } catch { /* ignore */ }
    }
    scanStream = null;
  }
  // Detach the stream from the <video> for cleaner teardown across browsers.
  const v = document.getElementById("scan-video");
  if (v) v.srcObject = null;
  $("scan-area").style.display = "none";
  $("scan-stop-btn").disabled = true;
  $("scan-start-btn").disabled = false;
}

function flashReticle() {
  const r = document.querySelector("#scan-overlay .scan-reticle");
  if (!r) return;
  r.classList.add("flash");
  setTimeout(() => r.classList.remove("flash"), 350);
}

function applyScannedPayload(payload) {
  const data = parsePairPayload(payload);
  if (!data) return false;
  $("pair-host").value = data.host;
  if (data.port) $("pair-port").value = data.port;
  if (data.code) $("pair-code").value = data.code;
  // Pre-fill the connect host too, since the QR carries the LAN IP.
  if (!$("conn-host").value) $("conn-host").value = data.host;
  setScanStatus(`Scanned ${data.host}${data.port ? ":" + data.port : ""}`, "ok");
  flashReticle();
  showToast("QR code scanned");
  return true;
}

// Map the actually-rendered reticle rectangle (in CSS pixels, relative to
// the <video> element) into source-video pixel coordinates. This makes the
// decode region exactly match what the user sees in the aiming overlay,
// regardless of camera aspect ratio or how the browser letterboxes the feed.
function getCropRect(video) {
  const reticle = document.querySelector("#scan-overlay .scan-reticle");
  const sw = video.videoWidth;
  const sh = video.videoHeight;
  // Fallback: centered 70% square in source pixels.
  const fallback = () => {
    const side = Math.floor(Math.min(sw, sh) * 0.70);
    return {
      x: Math.floor((sw - side) / 2),
      y: Math.floor((sh - side) / 2),
      w: side,
      h: side,
    };
  };
  if (!reticle) return fallback();
  const vRect = video.getBoundingClientRect();
  const rRect = reticle.getBoundingClientRect();
  if (vRect.width <= 0 || vRect.height <= 0) return fallback();
  // The video uses default object-fit (which for a <video> with intrinsic
  // size is "contain"-like via internal letterboxing). Compute the actually
  // displayed video box inside the element so we map CSS px -> source px
  // through the displayed area, not the (possibly larger) element box.
  const elemAR = vRect.width / vRect.height;
  const srcAR = sw / sh;
  let dispW, dispH, offX, offY;
  if (srcAR > elemAR) {
    dispW = vRect.width;
    dispH = vRect.width / srcAR;
    offX = 0;
    offY = (vRect.height - dispH) / 2;
  } else {
    dispH = vRect.height;
    dispW = vRect.height * srcAR;
    offY = 0;
    offX = (vRect.width - dispW) / 2;
  }
  // Reticle position relative to the displayed video box.
  const rx = rRect.left - vRect.left - offX;
  const ry = rRect.top - vRect.top - offY;
  const scaleX = sw / dispW;
  const scaleY = sh / dispH;
  let x = Math.round(rx * scaleX);
  let y = Math.round(ry * scaleY);
  let w = Math.round(rRect.width * scaleX);
  let h = Math.round(rRect.height * scaleY);
  // Clamp to source bounds.
  x = Math.max(0, Math.min(sw - 1, x));
  y = Math.max(0, Math.min(sh - 1, y));
  w = Math.max(1, Math.min(sw - x, w));
  h = Math.max(1, Math.min(sh - y, h));
  return { x, y, w, h };
}

async function _scanLoop() {
  if (!scanStream) return;
  const video = $("scan-video");
  if (video.readyState < 2 || video.videoWidth === 0) {
    scanRaf = requestAnimationFrame(_scanLoop);
    return;
  }
  let payload = null;
  // Crop the frame to the aiming-overlay region — feeding only those pixels
  // to the decoder is faster (smaller image) and lets jsQR succeed from
  // further away because the QR fills more of the analyzed area.
  const crop = getCropRect(video);
  const canvas = $("scan-canvas");
  canvas.width = crop.w;
  canvas.height = crop.h;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) {
    scanRaf = requestAnimationFrame(_scanLoop);
    return;
  }
  try {
    ctx.drawImage(video, crop.x, crop.y, crop.w, crop.h, 0, 0, crop.w, crop.h);
  } catch {
    scanRaf = requestAnimationFrame(_scanLoop);
    return;
  }
  try {
    if (scanDetector) {
      const codes = await scanDetector.detect(canvas);
      if (codes && codes.length) payload = codes[0].rawValue;
    } else if (typeof jsQR === "function") {
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const result = jsQR(img.data, img.width, img.height, {
        inversionAttempts: "dontInvert",
      });
      if (result && result.data) payload = result.data;
    }
  } catch { /* keep scanning on transient errors */ }

  if (payload) {
    if (applyScannedPayload(payload)) {
      // Hold the camera + overlay open briefly so the success flash
      // animation (~350ms) is actually visible before teardown.
      await new Promise((r) => setTimeout(r, 380));
      await stopScan();
      // Auto-pair if all three fields are now present.
      if ($("pair-host").value && $("pair-port").value && $("pair-code").value) {
        pair();
      }
      return;
    }
    // We saw a QR but it wasn't ours — surface a hint so the user knows
    // what to look for, but keep scanning. Throttle by only updating on
    // change to avoid status flicker.
    setScanStatus(
      "That QR isn't a Phone Bridge pairing code — show the “Pair this phone” QR.",
      "err",
    );
  }
  scanRaf = requestAnimationFrame(_scanLoop);
}

async function startScan() {
  setScanStatus("");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setScanStatus("This browser doesn't support camera access.", "err");
    return;
  }
  if (typeof jsQR !== "function" && !("BarcodeDetector" in window)) {
    setScanStatus("No QR decoder available in this browser.", "err");
    return;
  }
  $("scan-start-btn").disabled = true;
  // Build a video constraint that prefers the user-picked camera, falling
  // back to the rear-facing one when nothing is selected.
  const sel = $("scan-camera");
  const chosenId = (sel && sel.value) || getSavedCameraId();
  const videoConstraint = { width: { ideal: 1280 } };
  if (chosenId) {
    videoConstraint.deviceId = { exact: chosenId };
  } else {
    videoConstraint.facingMode = "environment";
  }
  let lastErr = null;
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({
      video: videoConstraint,
      audio: false,
    });
  } catch (e) {
    lastErr = e;
    // If the saved deviceId is gone (camera unplugged), retry with defaults.
    if (chosenId && e && (e.name === "OverconstrainedError" || e.name === "NotFoundError")) {
      try {
        scanStream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment", width: { ideal: 1280 } },
          audio: false,
        });
        saveCameraId("");
        if (sel) sel.value = "";
        lastErr = null;
      } catch (e2) { lastErr = e2; }
    }
  }
  if (!scanStream) {
    setScanStatus(
      lastErr && lastErr.name === "NotAllowedError"
        ? "Camera permission denied."
        : `Camera error: ${(lastErr && (lastErr.message || lastErr.name)) || "unknown"}`,
      "err",
    );
    $("scan-start-btn").disabled = false;
    return;
  }
  // Now that the user has granted permission, device labels are available —
  // refresh the dropdown so the names show up instead of "Camera 1/2/3".
  refreshCameraList();
  // Prefer the native BarcodeDetector when available — it's faster and
  // handles inversion / perspective better than jsQR.
  scanDetector = null;
  if ("BarcodeDetector" in window) {
    try {
      const formats = await window.BarcodeDetector.getSupportedFormats();
      if (formats.includes("qr_code")) {
        scanDetector = new window.BarcodeDetector({ formats: ["qr_code"] });
      }
    } catch { scanDetector = null; }
  }
  $("scan-video").srcObject = scanStream;
  $("scan-area").style.display = "block";
  $("scan-stop-btn").disabled = false;
  setScanStatus("Looking for the phone's pairing QR…");
  scanRaf = requestAnimationFrame(_scanLoop);
}

async function pair() {
  const host = $("pair-host").value.trim();
  const port = Number($("pair-port").value.trim());
  const code = $("pair-code").value.trim();
  const status = $("pair-status");
  status.textContent = "";
  if (!host || !port || !code) {
    status.textContent = "Need host, port, and pairing code.";
    return;
  }
  $("pair-btn").disabled = true;
  try {
    const r = await api("/api/scrcpy/pair", {
      method: "POST",
      body: JSON.stringify({ host, port, code }),
    });
    status.textContent = `Paired ✓  ${r.output || ""}`;
    status.style.color = "#86efac";
    // Pre-fill the connect host so the next step is one tap
    if (!$("conn-host").value) $("conn-host").value = host;
    showToast("Paired with phone");
  } catch (e) {
    status.style.color = "";
    status.textContent = `Pair failed: ${e.message}`;
  } finally {
    $("pair-btn").disabled = false;
  }
}

async function connect() {
  const host = $("conn-host").value.trim();
  const port = Number($("conn-port").value.trim());
  const status = $("pair-status");
  status.textContent = "";
  if (!host || !port) {
    status.textContent = "Need host and port for Connect.";
    return;
  }
  $("conn-btn").disabled = true;
  try {
    const r = await api("/api/scrcpy/connect", {
      method: "POST",
      body: JSON.stringify({ host, port }),
    });
    status.style.color = "#86efac";
    status.textContent = `Connected ✓  ${r.output || ""}`;
    showToast("Connected — detecting devices");
    await detectDevices(false);
  } catch (e) {
    status.style.color = "";
    status.textContent = `Connect failed: ${e.message}`;
  } finally {
    $("conn-btn").disabled = false;
  }
}

async function launchScrcpy() {
  const select = $("launch-serial");
  const serial = select.value;
  const preset = $("launch-preset").value;
  const errEl = $("run-error");
  errEl.textContent = "";
  if (!serial) {
    errEl.textContent = "Pick a device first.";
    return;
  }
  // Pull the friendly name from the selected option's dataset (set when we
  // rendered the device list) so the recents file remembers "Pixel 8"
  // instead of just "192.168.1.42:5555".
  const opt = select.selectedOptions[0];
  const name = (opt && opt.dataset.name) || serialNames.get(serial) || "";
  $("launch-btn").disabled = true;
  try {
    const launchRes = await api("/api/scrcpy/launch", {
      method: "POST",
      body: JSON.stringify({ serial, preset, name }),
    });
    // Immediate feedback for the auto-tuner: show the chosen profile and
    // measured RTT so the user knows why the mirror started at this
    // quality, without waiting for the recents-row pill to refresh.
    const t = launchRes && launchRes.tuning;
    if (t && t.profile) {
      let detail;
      if (t.override) {
        detail = "forced";
      } else if (t.rtt_ms != null) {
        detail = `RTT ${Math.round(t.rtt_ms)}ms`;
      } else {
        detail = t.source || "n/a";
      }
      showToast(`Launching scrcpy · tuned ${t.profile} (${detail})`);
    } else {
      showToast("Launching scrcpy…");
    }
    pollStatus();
    loadRecent();
  } catch (e) {
    if (e.status === 409) {
      errEl.textContent = "scrcpy is already running. Stop it first.";
    } else {
      errEl.textContent = `Launch failed: ${e.message}`;
    }
  } finally {
    $("launch-btn").disabled = false;
  }
}

// ---------- QR pairing ----------

function setQrPill(text, cls) {
  const el = $("qr-status-pill");
  el.textContent = text;
  el.classList.remove("ok", "warn", "danger");
  if (cls) el.classList.add(cls);
}

function stopQrPolling() {
  if (qrTimer) { clearTimeout(qrTimer); qrTimer = null; }
}

async function startQrPair() {
  $("qr-error").textContent = "";
  $("qr-message").textContent = "";
  $("qr-start-btn").disabled = true;
  try {
    const r = await api("/api/scrcpy/pair-qr/start", { method: "POST" });
    // Render the QR onto the canvas
    const canvas = $("qr-canvas");
    if (typeof QRCode === "undefined" || !QRCode.toCanvas) {
      throw new Error("QR library failed to load");
    }
    await QRCode.toCanvas(canvas, r.qr_payload, { width: 240, margin: 1 });
    $("qr-area").style.display = "block";
    $("qr-fallback-code").textContent = `service: ${r.service}  ·  code: ${r.code}`;
    $("qr-cancel-btn").disabled = false;
    setQrPill("Waiting for scan", "warn");
    pollQrStatus();
  } catch (e) {
    $("qr-error").textContent = `Could not start QR pairing: ${e.message}`;
    $("qr-start-btn").disabled = false;
    setQrPill("Idle");
  }
}

async function cancelQrPair() {
  $("qr-cancel-btn").disabled = true;
  try {
    await api("/api/scrcpy/pair-qr/cancel", { method: "POST" });
  } catch { /* ignore */ }
  stopQrPolling();
  $("qr-area").style.display = "none";
  $("qr-message").textContent = "";
  $("qr-start-btn").disabled = false;
  setQrPill("Idle");
}

async function pollQrStatus() {
  stopQrPolling();
  let s;
  try {
    s = await api("/api/scrcpy/pair-qr/status");
  } catch {
    qrTimer = setTimeout(pollQrStatus, 2000);
    return;
  }
  $("qr-message").textContent = s.message || "";
  if (s.status === "waiting_scan") {
    setQrPill("Waiting for scan", "warn");
  } else if (s.status === "pairing") {
    setQrPill("Pairing…", "warn");
  } else if (s.status === "connecting") {
    setQrPill("Connecting…", "warn");
  } else if (s.status === "paired") {
    setQrPill("Paired ✓", "ok");
    $("qr-area").style.display = "none";
    $("qr-start-btn").disabled = false;
    $("qr-cancel-btn").disabled = true;
    $("qr-message").textContent = `Connected as ${s.paired_serial}. Detecting devices…`;
    showToast("Phone paired and connected");
    await detectDevices(false);
    await loadRecent();
    return;  // stop polling
  } else if (s.status === "error") {
    setQrPill("Failed", "danger");
    $("qr-error").textContent = s.error || "Pairing failed";
    $("qr-area").style.display = "none";
    $("qr-start-btn").disabled = false;
    $("qr-cancel-btn").disabled = true;
    return;
  } else if (s.status === "cancelled") {
    setQrPill("Cancelled");
    $("qr-area").style.display = "none";
    $("qr-start-btn").disabled = false;
    $("qr-cancel-btn").disabled = true;
    return;
  } else {
    setQrPill("Idle");
    return;
  }
  qrTimer = setTimeout(pollQrStatus, 1500);
}

async function stopScrcpy() {
  $("stop-btn").disabled = true;
  try {
    await api("/api/scrcpy/stop", { method: "POST" });
    showToast("Stopped scrcpy");
    pollStatus();
  } catch (e) {
    showToast(`Stop failed: ${e.message}`);
  }
}

async function pollStatus() {
  if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
  let s;
  try {
    s = await api("/api/scrcpy/status");
  } catch {
    statusTimer = setTimeout(pollStatus, 4000);
    return;
  }
  const pill = $("run-pill");
  if (s.running) {
    setPill(pill, `Running · ${s.serial}`, "ok");
    $("stop-btn").disabled = false;
    $("launch-btn").disabled = true;
  } else {
    setPill(pill, "Stopped");
    $("stop-btn").disabled = true;
    $("launch-btn").disabled = false;
  }
  // Mirror status carries the sndcpy snapshot under `audio` — use it to
  // keep the dedicated audio pill in sync on every poll without an extra
  // round-trip.
  if (s.audio) {
    audioRunState = {
      running: !!s.audio.running,
      serial: s.audio.serial || null,
    };
    paintAudioPill();
  }
  const log = $("run-log");
  log.textContent = (s.log_tail || []).join("\n") || "scrcpy log will appear here…";
  log.scrollTop = log.scrollHeight;
  // Poll faster while running so the user sees fresh output / exits quickly.
  statusTimer = setTimeout(pollStatus, s.running ? 1500 : 5000);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
    }
    showToast("Copied");
  } catch {
    showToast("Copy failed — long-press to copy manually");
  }
}

// ---------- Browser-mirror viewer (section 6) ----------
//
// Independent of the native scrcpy launch above. Talks to
// /api/scrcpy/viewer/{start,stop,status} and POPULATES the same serial
// list the native launcher uses, so users don't have to re-pick a phone.

let mvStatusTimer = null;

function mvSyncSerials() {
  // Mirror the launch-serial dropdown into mv-serial-select so both stay
  // in sync with detected devices.
  const src = $("launch-serial");
  const dst = $("mv-serial-select");
  if (!src || !dst) return;
  const prev = dst.value;
  dst.innerHTML = "";
  for (const opt of src.options) {
    const c = document.createElement("option");
    c.value = opt.value;
    c.textContent = opt.textContent;
    dst.appendChild(c);
  }
  if (prev && [...dst.options].some((o) => o.value === prev)) {
    dst.value = prev;
  }
}

function mvPaintPill(running, fps) {
  const pill = $("mv-pill");
  pill.classList.remove("ok", "warn", "danger");
  if (running) {
    pill.textContent = `Live · ${fps} fps`;
    pill.classList.add("ok");
  } else {
    pill.textContent = "Off";
  }
  $("mv-stop-btn").disabled = !running;
  $("mv-open-btn").disabled = !running;
}

async function mvRefreshStatus() {
  try {
    const r = await api("/api/scrcpy/viewer/status");
    const running = (r.sessions || []).filter((s) => s.running);
    if (running.length) {
      const s = running[0];
      mvPaintPill(true, s.fps);
      // Pre-select the serial the server is already capturing so a Stop
      // press hits the right session.
      const sel = $("mv-serial-select");
      if (sel && [...sel.options].some((o) => o.value === s.serial)) {
        sel.value = s.serial;
      }
    } else {
      mvPaintPill(false, 0);
    }
  } catch {
    /* leave pill alone on transient errors */
  }
}

async function mvStart() {
  const serial = $("mv-serial-select").value;
  const fps = parseInt($("mv-fps-select").value, 10) || 6;
  $("mv-error").textContent = "";
  if (!serial) {
    $("mv-error").textContent = "Pick a connected device first.";
    return;
  }
  $("mv-start-btn").disabled = true;
  try {
    const r = await api("/api/scrcpy/viewer/start", {
      method: "POST",
      body: JSON.stringify({ serial, fps }),
    });
    mvPaintPill(true, r.session?.fps || fps);
    showToast(`Browser viewer live at /mirror-view (${r.session?.fps || fps} fps)`);
  } catch (e) {
    $("mv-error").textContent = `Could not start: ${e.message}`;
  } finally {
    $("mv-start-btn").disabled = false;
  }
}

async function mvStop() {
  const serial = $("mv-serial-select").value;
  if (!serial) return;
  $("mv-stop-btn").disabled = true;
  try {
    await api("/api/scrcpy/viewer/stop", {
      method: "POST",
      body: JSON.stringify({ serial }),
    });
    mvPaintPill(false, 0);
    showToast("Browser viewer stopped");
  } catch (e) {
    $("mv-error").textContent = `Stop failed: ${e.message}`;
  } finally {
    mvRefreshStatus();
  }
}

function mvOpenViewer() {
  const serial = $("mv-serial-select").value;
  const url = serial
    ? `/mirror-view?serial=${encodeURIComponent(serial)}`
    : "/mirror-view";
  window.open(url, "_blank", "noopener");
}

function wireUI() {
  $("dep-refresh-btn").addEventListener("click", loadDeps);
  $("dev-refresh-btn").addEventListener("click", () => detectDevices(true));
  $("recent-refresh-btn").addEventListener("click", loadRecent);
  $("pair-btn").addEventListener("click", pair);
  $("conn-btn").addEventListener("click", connect);
  $("launch-btn").addEventListener("click", launchScrcpy);
  $("stop-btn").addEventListener("click", stopScrcpy);
  $("qr-start-btn").addEventListener("click", startQrPair);
  $("qr-cancel-btn").addEventListener("click", cancelQrPair);
  $("scan-start-btn").addEventListener("click", startScan);
  $("scan-stop-btn").addEventListener("click", stopScan);
  $("scan-camera").addEventListener("change", (e) => {
    saveCameraId(e.target.value);
  });
  // Browsers fire `devicechange` when a webcam is plugged in or unplugged —
  // keep the dropdown in sync without a page reload.
  if (navigator.mediaDevices?.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", refreshCameraList);
  }
  $("quiet-save-btn").addEventListener("click", saveQuietHours);
  $("mv-start-btn").addEventListener("click", mvStart);
  $("mv-stop-btn").addEventListener("click", mvStop);
  $("mv-open-btn").addEventListener("click", mvOpenViewer);
  $("rec-refresh-btn").addEventListener("click", loadRecordings);
  // Re-sync the viewer's serial dropdown whenever the launcher's
  // dropdown changes (i.e. after Detect devices repopulates it).
  const ls = $("launch-serial");
  if (ls) {
    const obs = new MutationObserver(mvSyncSerials);
    obs.observe(ls, { childList: true });
  }
  document.querySelectorAll("[data-copy]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.copy);
      if (target) copyText(target.textContent);
    }));
}

async function startApp() {
  $("ms-login").classList.add("hidden");
  $("ms-app").classList.remove("hidden");
  wireUI();
  await loadDeps();
  await detectDevices(false);
  await loadQuietHours();
  await loadRecent();
  // Initial camera enumeration. Labels usually only appear after the user
  // has granted webcam permission once before, but the deviceIds are still
  // useful for restoring a saved choice.
  refreshCameraList();
  pollStatus();
  mvSyncSerials();
  mvRefreshStatus();
  mvStatusTimer = setInterval(mvRefreshStatus, 5000);
  loadRecordings();
}

// ---------- Saved-recording library ----------

function fmtBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "—";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return `${m}:${String(r).padStart(2, "0")}`;
}

function recordingUrl(name) {
  const t = getToken();
  const q = t ? `?token=${encodeURIComponent(t)}` : "";
  return `/api/scrcpy/recordings/file/${encodeURIComponent(name)}${q}`;
}

function renderRecordings(items) {
  const list = $("rec-list");
  const pill = $("rec-count-pill");
  list.innerHTML = "";
  if (!items.length) {
    pill.textContent = "0 clips";
    list.innerHTML = `<div class="ms-empty">No recordings yet — start one from the browser viewer's REC button.</div>`;
    return;
  }
  pill.textContent = `${items.length} clip${items.length === 1 ? "" : "s"}`;
  for (const r of items) {
    const row = document.createElement("div");
    row.className = "ms-dev";
    row.style.flexDirection = "column";
    row.style.alignItems = "stretch";
    const when = r.mtime ? new Date(r.mtime * 1000).toLocaleString() : "";
    row.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;width:100%">
        <div class="ms-dev-info" style="cursor:pointer" data-action="play">
          <div class="ms-dev-name">${escapeHtml(r.name)}</div>
          <div class="ms-dev-meta">
            ${escapeHtml(fmtDuration(r.duration_sec))} ·
            ${escapeHtml(fmtBytes(r.size))} ·
            ${escapeHtml(when)}
          </div>
        </div>
        <button class="ms-btn" data-action="play">▶ Play</button>
        <button class="ms-btn" data-action="copy" title="Copy share-able URL">Copy URL</button>
        <button class="ms-btn danger" data-action="delete">Delete</button>
      </div>
      <div data-role="player" style="display:none;margin-top:10px"></div>
    `;
    const player = row.querySelector('[data-role="player"]');
    const togglePlay = () => {
      if (player.style.display === "none") {
        player.innerHTML = `
          <video controls autoplay preload="metadata"
                 style="width:100%;max-height:480px;border-radius:8px;background:#000"
                 src="${recordingUrl(r.name)}"></video>
        `;
        player.style.display = "block";
      } else {
        // Tear the <video> down so it stops fetching/decoding when collapsed.
        player.innerHTML = "";
        player.style.display = "none";
      }
    };
    row.querySelectorAll('[data-action="play"]').forEach((el) =>
      el.addEventListener("click", togglePlay));
    row.querySelector('[data-action="copy"]').addEventListener("click", () => {
      // Strip the token from the shared URL — recipients log in with the
      // PIN themselves; a leaked token would be a session-hijack handout.
      const path = `/api/scrcpy/recordings/file/${encodeURIComponent(r.name)}`;
      const abs = `${location.origin}${path}`;
      copyText(abs);
    });
    row.querySelector('[data-action="delete"]').addEventListener("click", () =>
      deleteRecording(r.name));
    list.appendChild(row);
  }
}

async function loadRecordings() {
  const errEl = $("rec-error");
  errEl.textContent = "";
  try {
    const r = await api("/api/scrcpy/recordings/list");
    renderRecordings(r.recordings || []);
  } catch (e) {
    errEl.textContent = `Could not load recordings: ${e.message}`;
    $("rec-list").innerHTML = "";
    $("rec-count-pill").textContent = "—";
  }
}

async function deleteRecording(name) {
  if (!confirm(`Delete "${name}"? This can't be undone.`)) return;
  try {
    await api(`/api/scrcpy/recordings/file/${encodeURIComponent(name)}`,
              { method: "DELETE" });
    showToast("Recording deleted");
    loadRecordings();
  } catch (e) {
    showToast(`Delete failed: ${e.message}`);
  }
}

setOnSessionLost(() => {
  if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
  if (recentTimer) { clearTimeout(recentTimer); recentTimer = null; }
  if (ringPollTimer) { clearInterval(ringPollTimer); ringPollTimer = null; }
  if (mvStatusTimer) { clearInterval(mvStatusTimer); mvStatusTimer = null; }
  stopQrPolling();
  stopScan();
  $("ms-app").classList.add("hidden");
  $("ms-login").classList.remove("hidden");
});

(async () => {
  if (await checkAuth()) {
    startApp();
  } else {
    initLogin(startApp);
  }
})();
