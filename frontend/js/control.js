import { getToken } from "./auth.js";
import { showToast } from "./utils.js";
import { wsPath } from "./target.js";

let ws = null;

function getWsBase() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function connectWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const token = getToken();
  ws = new WebSocket(`${getWsBase()}${wsPath("/ws/input", "control")}?token=${encodeURIComponent(token)}`);
  ws.onopen = () => {};
  ws.onclose = () => {};
  ws.onerror = () => {};
}

// ── Touchpad ─────────────────────────────────────────────────────────

const CLICK_MAX_MS = 200;
const SENSITIVITY = 1.8;

function initTouchpad() {
  const pad = document.getElementById("touchpad");
  if (!pad) return;

  let touches = {};
  let startTime = 0;
  let moved = false;
  let lastPos = {};
  let lastTapTime = 0;

  function getTouches(e) {
    return Array.from(e.touches).slice(0, 2);
  }

  pad.addEventListener("touchstart", (e) => {
    e.preventDefault();
    const ts = getTouches(e);
    startTime = Date.now();
    moved = false;
    touches = {};
    ts.forEach((t) => {
      touches[t.identifier] = { x: t.clientX, y: t.clientY };
      lastPos[t.identifier] = { x: t.clientX, y: t.clientY };
    });
  }, { passive: false });

  pad.addEventListener("touchmove", (e) => {
    e.preventDefault();
    const ts = getTouches(e);

    if (ts.length === 1) {
      const t = ts[0];
      const prev = lastPos[t.identifier];
      if (prev) {
        const dx = (t.clientX - prev.x) * SENSITIVITY;
        const dy = (t.clientY - prev.y) * SENSITIVITY;
        if (Math.abs(dx) > 0.1 || Math.abs(dy) > 0.1) {
          send({ type: "move", dx, dy });
          moved = true;
        }
      }
      lastPos[t.identifier] = { x: t.clientX, y: t.clientY };
    } else if (ts.length === 2) {
      const t0 = ts[0], t1 = ts[1];
      const prev0 = lastPos[t0.identifier];
      const prev1 = lastPos[t1.identifier];
      if (prev0 && prev1) {
        const avgDy = ((t0.clientY - prev0.y) + (t1.clientY - prev1.y)) / 2;
        const avgDx = ((t0.clientX - prev0.x) + (t1.clientX - prev1.x)) / 2;
        if (Math.abs(avgDy) > 0.5 || Math.abs(avgDx) > 0.5) {
          send({ type: "scroll", dx: avgDx * 0.5, dy: avgDy * 0.5 });
          moved = true;
        }
      }
      lastPos[t0.identifier] = { x: t0.clientX, y: t0.clientY };
      lastPos[t1.identifier] = { x: t1.clientX, y: t1.clientY };
    }
  }, { passive: false });

  pad.addEventListener("touchend", (e) => {
    e.preventDefault();
    const numTouches = Object.keys(touches).length;
    const elapsed = Date.now() - startTime;

    if (!moved && elapsed < CLICK_MAX_MS) {
      if (numTouches === 1) {
        const now = Date.now();
        if (now - lastTapTime < 300) {
          send({ type: "click", button: "left" });
          send({ type: "click", button: "left" });
        } else {
          send({ type: "click", button: "left" });
        }
        lastTapTime = now;
      } else if (numTouches === 2) {
        send({ type: "click", button: "right" });
      }
    }
    touches = {};
  }, { passive: false });
}

// ── Keyboard ─────────────────────────────────────────────────────────

function initKeyboard() {
  const input = document.getElementById("kb-input");
  const sendBtn = document.getElementById("kb-send-btn");
  const clearBtn = document.getElementById("kb-clear-btn");

  if (!input) return;

  let prevVal = "";

  input.addEventListener("input", () => {
    const cur = input.value;
    if (cur.length > prevVal.length) {
      const added = cur.slice(prevVal.length);
      send({ type: "type", text: added });
    } else if (cur.length < prevVal.length) {
      const removed = prevVal.length - cur.length;
      for (let i = 0; i < removed; i++) send({ type: "key", key: "Backspace" });
    }
    prevVal = cur;
  });

  input.addEventListener("keydown", (e) => {
    const specialKeys = ["Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Delete"];
    if (specialKeys.includes(e.key)) {
      e.preventDefault();
      send({ type: "key", key: e.key });
    }
  });

  sendBtn.addEventListener("click", () => {
    send({ type: "key", key: "Enter" });
  });

  clearBtn.addEventListener("click", () => {
    input.value = "";
    prevVal = "";
    input.focus();
  });

  document.querySelectorAll(".special-key").forEach((btn) => {
    btn.addEventListener("click", () => send({ type: "key", key: btn.dataset.key }));
  });
}

// ── Tab switching ─────────────────────────────────────────────────────

function initControlTabs() {
  document.querySelectorAll(".ctrl-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".ctrl-tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".ctrl-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      const panelId = tab.dataset.panel;
      document.getElementById(panelId)?.classList.add("active");

      if (panelId === "kb-panel") {
        setTimeout(() => document.getElementById("kb-input")?.focus(), 100);
      }
    });
  });
}

export function initControl() {
  connectWs();
  initControlTabs();
  initTouchpad();
  initKeyboard();

  document.getElementById("pad-left-btn")?.addEventListener("click", () => send({ type: "click", button: "left" }));
  document.getElementById("pad-right-btn")?.addEventListener("click", () => send({ type: "click", button: "right" }));

  document.getElementById("ctrl-reconnect-btn")?.addEventListener("click", () => {
    // Explicitly close the prior socket before dropping the
    // reference — without this, frequent retargeting (each picker
    // switch fires a click on this button) would leak orphaned
    // connections that stay half-open until the browser GCs them.
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    connectWs();
    showToast("Reconnecting...");
  });
}
