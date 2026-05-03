import { getToken } from "./auth.js";
import { wsPath } from "./target.js";

let term = null;
let fitAddon = null;
let ws = null;
let mode = "laptop";

function getWsBase() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

function setStatus(connected) {
  const el = document.getElementById("term-status");
  el.textContent = connected ? "Connected" : "Disconnected";
  el.className = connected ? "connected" : "disconnected";
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    ws.close();
  }

  const token = getToken();
  ws = new WebSocket(
    `${getWsBase()}${wsPath("/ws/terminal", "terminal")}` +
    `?token=${encodeURIComponent(token)}&mode=${encodeURIComponent(mode)}`
  );
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    setStatus(true);
    sendResize();
  };

  ws.onmessage = (e) => {
    if (!term) return;
    const data = e.data instanceof ArrayBuffer ? new Uint8Array(e.data) : e.data;
    term.write(data);
  };

  ws.onclose = () => {
    setStatus(false);
  };

  ws.onerror = () => {
    setStatus(false);
  };
}

function sendResize() {
  if (!ws || ws.readyState !== WebSocket.OPEN || !term) return;
  ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
}

export function initTerminal() {
  const container = document.getElementById("terminal-container");

  term = new Terminal({
    theme: {
      background: "#0a0a0a",
      foreground: "#e2e8f0",
      cursor: "#a78bfa",
      selectionBackground: "rgba(124,58,237,0.4)",
      black: "#0a0a0a",
      brightBlack: "#475569",
      red: "#ef4444",
      brightRed: "#f87171",
      green: "#22c55e",
      brightGreen: "#4ade80",
      yellow: "#f59e0b",
      brightYellow: "#fbbf24",
      blue: "#7c3aed",
      brightBlue: "#a78bfa",
      magenta: "#d946ef",
      brightMagenta: "#e879f9",
      cyan: "#06b6d4",
      brightCyan: "#22d3ee",
      white: "#e2e8f0",
      brightWhite: "#f8fafc",
    },
    fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", monospace',
    fontSize: 13,
    lineHeight: 1.4,
    cursorBlink: true,
    scrollback: 2000,
  });

  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  fitAddon.fit();

  term.onData((data) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "input", data }));
  });

  term.onResize(() => sendResize());

  window.addEventListener("resize", () => {
    if (fitAddon) fitAddon.fit();
  });

  document.getElementById("term-reconnect-btn").addEventListener("click", connect);

  document.querySelectorAll(".term-mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const newMode = btn.dataset.mode;
      if (newMode === mode) return;
      mode = newMode;
      document.querySelectorAll(".term-mode-btn").forEach((b) => {
        b.classList.toggle("active", b.dataset.mode === mode);
      });
      if (term) {
        term.clear();
        term.write(`\x1b[2m[Switching to ${mode}…]\x1b[0m\r\n`);
      }
      connect();
    });
  });

  connect();
}

export function fitTerminal() {
  if (fitAddon) fitAddon.fit();
}
