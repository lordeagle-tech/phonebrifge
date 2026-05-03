import { getToken } from "./auth.js";
import { showToast } from "./utils.js";
import { targetFor, wsPath } from "./target.js";

let ws = null;
let active = false;
let mjpegMode = false;
let imgEl = null;
let viewEl = null;
let currentFps = 10;
let currentQuality = 60;
let objectUrl = null;

/* ── Pinch-to-zoom state ── */
let scale = 1;
let panX = 0;
let panY = 0;
let lastPinchDist = null;
let lastTap = 0;
let dragStart = null;
let panAtDragStart = null;

const MIN_SCALE = 1;
const MAX_SCALE = 5;

/* ── Click-through state ── */
let inputWs = null;
let tapStartPos = null;
let tapStartTime = 0;
let tapMoved = false;
let longPressTimer = null;
let longPressFired = false;
let suppressTap = false;

const TAP_MOVE_THRESHOLD = 10;
const TAP_MAX_MS = 250;
const LONG_PRESS_MS = 500;

let pendingInput = null; // single queued message while WS is connecting

function flushPending() {
  if (pendingInput && inputWs && inputWs.readyState === WebSocket.OPEN) {
    try { inputWs.send(JSON.stringify(pendingInput)); } catch {}
  }
  pendingInput = null;
}

function connectInputWs() {
  if (inputWs && (inputWs.readyState === WebSocket.OPEN || inputWs.readyState === WebSocket.CONNECTING)) return;
  const token = getToken();
  inputWs = new WebSocket(`${getWsBase()}${wsPath("/ws/input", "screen")}?token=${encodeURIComponent(token)}`);
  inputWs.onopen = flushPending;
  inputWs.onclose = () => { inputWs = null; pendingInput = null; };
  inputWs.onerror = () => {};
}

function sendInput(msg) {
  if (inputWs && inputWs.readyState === WebSocket.OPEN) {
    inputWs.send(JSON.stringify(msg));
    return;
  }
  // Queue this single message and (re)connect — avoids dropping the first tap.
  pendingInput = msg;
  connectInputWs();
}

function sendClickAt(clientX, clientY, button) {
  if (!imgEl) return;
  const rect = imgEl.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return;
  const nx = (clientX - rect.left) / rect.width;
  const ny = (clientY - rect.top) / rect.height;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
  sendInput({ type: "click_at", x: nx, y: ny, button });
}

function cancelLongPress() {
  if (longPressTimer) {
    clearTimeout(longPressTimer);
    longPressTimer = null;
  }
}

function getWsBase() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

function setStatus(text, cls = "") {
  const el = document.getElementById("screen-status");
  if (el) { el.textContent = text; el.className = cls; }
}

function setOverlay(state) {
  const empty = document.getElementById("screen-empty");
  const loading = document.getElementById("screen-loading");
  const img = document.getElementById("screen-img");
  if (!empty || !loading || !img) return;
  empty.classList.toggle("hidden", state !== "empty");
  loading.classList.toggle("hidden", state !== "loading");
  img.classList.toggle("hidden", state !== "live");
}

function revoke() {
  if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
}

/* ── Zoom helpers ── */
function applyTransform() {
  if (!imgEl) return;
  imgEl.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
}

function clampPan() {
  if (!viewEl || !imgEl) return;
  const vw = viewEl.clientWidth;
  const vh = viewEl.clientHeight;
  const iw = imgEl.naturalWidth || imgEl.clientWidth;
  const ih = imgEl.naturalHeight || imgEl.clientHeight;
  const scaledW = iw * scale;
  const scaledH = ih * scale;

  const maxX = 0;
  const minX = Math.min(0, vw - scaledW);
  const maxY = 0;
  const minY = Math.min(0, vh - scaledH);

  panX = Math.min(maxX, Math.max(minX, panX));
  panY = Math.min(maxY, Math.max(minY, panY));
}

function resetZoom() {
  scale = 1; panX = 0; panY = 0;
  applyTransform();
  updateResetBtn();
}

function updateResetBtn() {
  const btn = document.getElementById("screen-reset-zoom");
  if (btn) btn.style.display = scale > 1 ? "" : "none";
}

function dist(t1, t2) {
  const dx = t1.clientX - t2.clientX;
  const dy = t1.clientY - t2.clientY;
  return Math.sqrt(dx * dx + dy * dy);
}

function midpoint(t1, t2) {
  return { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 };
}

function initPinchZoom() {
  if (!viewEl) return;

  viewEl.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      lastPinchDist = dist(e.touches[0], e.touches[1]);
      // Second finger arrived — cancel any pending tap/long-press
      cancelLongPress();
      tapMoved = true;
      suppressTap = true;
    } else if (e.touches.length === 1) {
      const now = Date.now();
      const t = e.touches[0];

      tapStartPos = { x: t.clientX, y: t.clientY };
      tapStartTime = now;
      tapMoved = false;
      longPressFired = false;
      suppressTap = false;

      if (now - lastTap < 300) {
        resetZoom();
        suppressTap = true; // double-tap is a zoom gesture, not a click
      }
      lastTap = now;

      if (scale > 1) {
        dragStart = { x: t.clientX, y: t.clientY };
        panAtDragStart = { x: panX, y: panY };
      }

      cancelLongPress();
      const startX = t.clientX;
      const startY = t.clientY;
      longPressTimer = setTimeout(() => {
        longPressTimer = null;
        if (!tapMoved && !suppressTap) {
          longPressFired = true;
          sendClickAt(startX, startY, "right");
        }
      }, LONG_PRESS_MS);
    }
  }, { passive: false });

  viewEl.addEventListener("touchmove", (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      cancelLongPress();
      tapMoved = true;
      const d = dist(e.touches[0], e.touches[1]);
      if (lastPinchDist === null) { lastPinchDist = d; return; }
      const factor = d / lastPinchDist;
      lastPinchDist = d;

      const mid = midpoint(e.touches[0], e.touches[1]);
      const rect = viewEl.getBoundingClientRect();
      const mx = mid.x - rect.left;
      const my = mid.y - rect.top;

      const newScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale * factor));
      const ds = newScale / scale;
      panX = mx - ds * (mx - panX);
      panY = my - ds * (my - panY);
      scale = newScale;
      clampPan();
      applyTransform();
      updateResetBtn();
    } else if (e.touches.length === 1) {
      const t = e.touches[0];
      if (tapStartPos) {
        const dx = t.clientX - tapStartPos.x;
        const dy = t.clientY - tapStartPos.y;
        if (!tapMoved && Math.hypot(dx, dy) > TAP_MOVE_THRESHOLD) {
          tapMoved = true;
          cancelLongPress();
        }
      }
      if (scale > 1 && dragStart) {
        e.preventDefault();
        panX = panAtDragStart.x + (t.clientX - dragStart.x);
        panY = panAtDragStart.y + (t.clientY - dragStart.y);
        clampPan();
        applyTransform();
      }
    }
  }, { passive: false });

  viewEl.addEventListener("touchend", (e) => {
    if (e.touches.length < 2) lastPinchDist = null;
    if (e.touches.length === 0) {
      cancelLongPress();
      const elapsed = Date.now() - tapStartTime;
      if (
        tapStartPos &&
        !tapMoved &&
        !suppressTap &&
        !longPressFired &&
        elapsed < TAP_MAX_MS
      ) {
        sendClickAt(tapStartPos.x, tapStartPos.y, "left");
      }
      dragStart = null;
      panAtDragStart = null;
      tapStartPos = null;
    }
  });

  viewEl.addEventListener("touchcancel", () => {
    cancelLongPress();
    tapStartPos = null;
    dragStart = null;
    panAtDragStart = null;
    lastPinchDist = null;
  });

  // Mouse fallback (desktop preview / testing)
  viewEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    tapStartPos = { x: e.clientX, y: e.clientY };
    tapStartTime = Date.now();
    tapMoved = false;
    longPressFired = false;
    suppressTap = false;
    cancelLongPress();
    const startX = e.clientX;
    const startY = e.clientY;
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      if (!tapMoved) {
        longPressFired = true;
        sendClickAt(startX, startY, "right");
      }
    }, LONG_PRESS_MS);
  });

  viewEl.addEventListener("mousemove", (e) => {
    if (!tapStartPos) return;
    const dx = e.clientX - tapStartPos.x;
    const dy = e.clientY - tapStartPos.y;
    if (!tapMoved && Math.hypot(dx, dy) > TAP_MOVE_THRESHOLD) {
      tapMoved = true;
      cancelLongPress();
    }
  });

  viewEl.addEventListener("mouseup", (e) => {
    if (e.button !== 0 || !tapStartPos) return;
    cancelLongPress();
    const elapsed = Date.now() - tapStartTime;
    if (!tapMoved && !longPressFired && elapsed < TAP_MAX_MS) {
      sendClickAt(e.clientX, e.clientY, "left");
    }
    tapStartPos = null;
  });

  viewEl.addEventListener("mouseleave", () => {
    cancelLongPress();
    tapStartPos = null;
  });

  // Suppress browser context menu so long-press feels native
  viewEl.addEventListener("contextmenu", (e) => e.preventDefault());
}

/* ── Stream (WS for main, MJPEG for peers) ── */
//
// The main bridge serves binary JPEG frames over /ws/screen, but
// peer agents only expose /screen.mjpeg over plain HTTP (no WS).
// When the user retargets the screen tab to a peer we therefore
// fall back to a simple ``<img src=mjpeg-url>`` — the browser
// renders multipart/x-mixed-replace natively, so we don't have to
// touch the canvas pipeline. Switching back to main re-opens the
// WS as before.

function _peerMjpegUrl() {
  const t = targetFor("screen");
  if (t.kind !== "peer") return null;
  const token = getToken();
  return `/api/peers/${t.id}/screen.mjpeg`
       + `?token=${encodeURIComponent(token)}`
       + `&fps=${currentFps}&quality=${currentQuality}`;
}

function connect() {
  if (ws) { ws.close(); ws = null; }
  if (!imgEl) return;
  setStatus("Connecting…");
  setOverlay("loading");
  active = true;

  const mjpeg = _peerMjpegUrl();
  if (mjpeg) {
    // Peer-targeted: just point the <img> at the proxied MJPEG
    // stream. There's no per-frame "open" event so we optimistically
    // call it Live; ``onerror`` will downgrade to Error if the peer
    // is unreachable.
    mjpegMode = true;
    revoke();
    imgEl.onerror = () => { setStatus("Error", "disconnected"); setOverlay("empty"); };
    imgEl.onload = () => { setStatus("Live (peer)", "connected"); setOverlay("live"); };
    imgEl.src = mjpeg;
    setStatus("Live (peer)", "connected");
    return;
  }

  mjpegMode = false;
  const token = getToken();
  ws = new WebSocket(`${getWsBase()}/ws/screen?token=${encodeURIComponent(token)}&fps=${currentFps}&quality=${currentQuality}`);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    setStatus("Live", "connected");
  };

  ws.onmessage = (e) => {
    if (!imgEl) return;
    revoke();
    const blob = new Blob([e.data], { type: "image/jpeg" });
    objectUrl = URL.createObjectURL(blob);
    imgEl.src = objectUrl;
    setOverlay("live");
  };

  ws.onclose = () => {
    setStatus("Stopped", "disconnected");
    active = false;
    setOverlay("empty");
  };

  ws.onerror = () => {
    setStatus("Error", "disconnected");
    setOverlay("empty");
  };
}

function disconnect() {
  if (ws) { ws.close(); ws = null; }
  revoke();
  if (imgEl) {
    imgEl.onerror = null;
    imgEl.onload = null;
    imgEl.src = "";
  }
  mjpegMode = false;
  setStatus("Stopped", "disconnected");
  active = false;
  setOverlay("empty");
}

function sendSettings() {
  if (mjpegMode) {
    // The MJPEG URL embeds fps + quality in its query string, so to
    // pick up a slider change we just rebuild the URL — the browser
    // will tear down the old multipart connection and start a new
    // one transparently.
    if (active && imgEl) imgEl.src = _peerMjpegUrl() || "";
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ fps: currentFps, quality: currentQuality }));
  }
}

/** Called by the picker when the user retargets the screen tab.
 * If a stream is currently running we tear it down and reconnect
 * against the new target so the user doesn't have to manually
 * stop+start. Reconnecting input is handled separately by
 * connectInputWs(). */
export function retargetScreen() {
  if (!active) {
    // Even when stopped, reconnect input WS so the next click
    // routes to the right machine.
    if (inputWs) { try { inputWs.close(); } catch {} inputWs = null; }
    return;
  }
  disconnect();
  if (inputWs) { try { inputWs.close(); } catch {} inputWs = null; }
  connect();
  const btn = document.getElementById("screen-start-btn");
  if (btn) btn.textContent = "Stop";
}

export function initScreen() {
  imgEl = document.getElementById("screen-img");
  viewEl = document.querySelector(".screen-view");
  const startBtn = document.getElementById("screen-start-btn");
  const fpsEl = document.getElementById("screen-fps");
  const qualityEl = document.getElementById("screen-quality");
  const fpsVal = document.getElementById("screen-fps-val");
  const qualityVal = document.getElementById("screen-quality-val");
  const resetZoomBtn = document.getElementById("screen-reset-zoom");

  fpsEl.addEventListener("input", () => {
    currentFps = parseInt(fpsEl.value, 10);
    fpsVal.textContent = currentFps;
    sendSettings();
  });

  qualityEl.addEventListener("input", () => {
    currentQuality = parseInt(qualityEl.value, 10);
    qualityVal.textContent = currentQuality;
    sendSettings();
  });

  function toggle() {
    if (active) {
      disconnect();
      startBtn.textContent = "Start";
    } else {
      connect();
      startBtn.textContent = "Stop";
    }
  }
  startBtn.addEventListener("click", toggle);
  document.getElementById("screen-empty-start")?.addEventListener("click", toggle);

  resetZoomBtn?.addEventListener("click", resetZoom);

  setOverlay("empty");
  connectInputWs();
  initPinchZoom();
}

export function stopScreen() {
  disconnect();
  resetZoom();
  const btn = document.getElementById("screen-start-btn");
  if (btn) btn.textContent = "Start";
}
