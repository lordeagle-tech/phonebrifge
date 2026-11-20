/**
 * Browser-side controller for the "control a peer laptop" page.
 *
 * Talks only to the main bridge: an MJPEG <img> for the screen, a
 * WebSocket for forwarding mouse/keyboard events. Both are proxied
 * through to the chosen peer by the main bridge — the browser never
 * sees the peer's IP, port or secret.
 */
import {
  initLogin, getToken, checkAuth, setOnSessionLost,
  ticketedUrl, openTicketedWebSocket,
} from "./auth.js";

const $ = (id) => document.getElementById(id);

let peers = [];
let currentPeer = null;       // id of the peer we're streaming
let inputWs = null;
let inputWsClosing = false;
let reconnectTimer = null;
let frameCount = 0;
let fpsTimer = null;
let lastRtt = null;
let pingTimer = null;
let outstandingPing = null;   // ts of the last unanswered ping
let peerPollTimer = null;

function setStatus(text, cls) {
  const pill = $("pc-status");
  pill.textContent = text;
  pill.classList.remove("ok", "warn", "danger");
  if (cls) pill.classList.add(cls);
}

function setPeerLabel(name) {
  // Persistent "Controlling: <hostname>" indicator next to the peer
  // dropdown so the active session is obvious even when the status
  // pill flips through transient states.
  const el = $("pc-peer-name");
  if (!el) return;
  if (name) {
    el.textContent = `Controlling: ${name}`;
    el.style.display = "";
  } else {
    el.textContent = "";
    el.style.display = "none";
  }
}

function showOverlay(title, msg, dim) {
  const o = $("pc-overlay");
  $("pc-overlay-title").textContent = title;
  $("pc-overlay-msg").textContent = msg || "";
  o.classList.remove("hidden");
  o.classList.toggle("dimmed", !!dim);
}
function hideOverlay() { $("pc-overlay").classList.add("hidden"); }

async function loadPeers() {
  const tok = getToken();
  if (!tok) return;
  let data = { peers: [] };
  try {
    const res = await fetch("/api/peers", {
      headers: { Authorization: `Bearer ${tok}` },
    });
    if (res.ok) data = await res.json();
  } catch (_) {
    // Network blip — just leave whatever we last had in the dropdown
    // so the user isn't kicked back to the empty state on a flaky LAN.
    return;
  }
  peers = data.peers || [];
  renderPeerOptions();
  // If our selected peer just went offline, fall through to the
  // "peer disconnected" UX so the user can see why the stream froze.
  if (currentPeer) {
    const me = peers.find((p) => p.id === currentPeer);
    if (!me || !me.online || !me.controllable) {
      handlePeerWentAway(me);
    } else if ($("pc-overlay").classList.contains("dimmed")) {
      // Peer came back — restart the full pipeline (stream + input WS
      // + FPS meter), not just the image. handlePeerWentAway() tore
      // those down so a stream-only reload would leave us with no
      // mouse/keyboard control.
      resumeAfterReconnect();
    }
  }
}

function renderPeerOptions() {
  const sel = $("pc-peer");
  const prev = sel.value;
  sel.innerHTML = "";
  if (!peers.length) {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "No peers paired";
    sel.appendChild(opt);
    return;
  }
  for (const p of peers) {
    const opt = document.createElement("option");
    opt.value = p.id;
    const onlineMark = p.online ? "🟢" : "⚫";
    const ctrlMark = p.controllable ? "" : " (no remote control)";
    opt.textContent = `${onlineMark} ${p.hostname}${ctrlMark}`;
    opt.disabled = !p.online || !p.controllable;
    sel.appendChild(opt);
  }
  sel.value = prev || (peers.find((p) => p.online && p.controllable)?.id ?? "");
}

function streamUrl(peerId) {
  return `/api/peers/${peerId}/screen.mjpeg?fps=15&quality=65`;
}

async function reloadStream() {
  if (!currentPeer) return;
  const peerId = currentPeer;
  const img = $("pc-img");
  // Cache-bust so the browser actually re-issues the MJPEG GET; without
  // this the stale stream stays in place if the URL didn't change.
  try {
    const url = await ticketedUrl(streamUrl(peerId) + `&t=${Date.now()}`);
    if (currentPeer !== peerId) return;
    img.src = url;
  } catch (_) {
    setStatus("stream authorization failed", "danger");
    return;
  }
  img.classList.add("live");
  hideOverlay();
}

function startStreaming(peerId) {
  stopStreaming();
  currentPeer = peerId;
  const peer = peers.find((p) => p.id === peerId);
  if (!peer) return;
  setPeerLabel(peer.hostname);
  setStatus(`connecting to ${peer.hostname}…`, "warn");
  reloadStream();
  startInput(peerId);
  startFpsMeter();
}

function stopStreaming() {
  const img = $("pc-img");
  img.classList.remove("live");
  img.removeAttribute("src");
  stopInput();
  stopFpsMeter();
  $("pc-fps").textContent = "— fps";
  $("pc-rtt").textContent = "— ms";
  lastRtt = null;
}

function disconnect() {
  currentPeer = null;
  stopStreaming();
  setPeerLabel(null);
  setStatus("idle");
  showOverlay("Disconnected", "Pick a peer above to start controlling it.");
}

function handlePeerWentAway(peer) {
  setPeerLabel(peer ? peer.hostname : null);
  setStatus("peer offline", "danger");
  // Keep the last rendered frame on screen (don't clear img.src) so
  // the user sees the desktop frozen behind the dimmed overlay
  // instead of a black void while we wait for the peer to come back.
  showOverlay(
    "Peer disconnected — reconnecting…",
    peer ? `Waiting for ${peer.hostname} to come back online.` : "Waiting for the peer to come back."
  , true);
  stopInput();
  stopFpsMeter();
}

function resumeAfterReconnect() {
  if (!currentPeer) return;
  const peer = peers.find((p) => p.id === currentPeer);
  if (!peer || !peer.online || !peer.controllable) return;
  setStatus(`reconnecting to ${peer.hostname}…`, "warn");
  reloadStream();
  startInput(currentPeer);
  startFpsMeter();
}

// ---- FPS counter -----------------------------------------------------
function startFpsMeter() {
  frameCount = 0;
  const img = $("pc-img");
  const onFrame = () => { frameCount += 1; };
  // MJPEG triggers `load` once per part on most browsers, but not all
  // of them — rather than relying on it, sample the image's
  // currentSrc-byte changes via a 1-Hz tick and the load event when
  // we can.
  img.addEventListener("load", onFrame);
  img._pcFrameHandler = onFrame;
  fpsTimer = setInterval(() => {
    $("pc-fps").textContent = `${frameCount} fps`;
    frameCount = 0;
  }, 1000);
}
function stopFpsMeter() {
  if (fpsTimer) { clearInterval(fpsTimer); fpsTimer = null; }
  const img = $("pc-img");
  if (img._pcFrameHandler) {
    img.removeEventListener("load", img._pcFrameHandler);
    delete img._pcFrameHandler;
  }
}

// ---- Input forwarding -----------------------------------------------
async function startInput(peerId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/peers/${peerId}/input`;
  inputWsClosing = false;
  try {
    const socket = await openTicketedWebSocket(url);
    if (currentPeer !== peerId || inputWsClosing) {
      socket.close();
      return;
    }
    inputWs = socket;
  } catch (_) {
    setStatus("input failed", "danger");
    return;
  }
  inputWs.onopen = () => {
    setStatus("connected", "ok");
    hideOverlay();
    // Start RTT pings every 1 s so the latency pill stays fresh.
    pingTimer = setInterval(sendPing, 1000);
  };
  inputWs.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg && msg.type === "pong" && msg.ts && outstandingPing === msg.ts) {
      lastRtt = Math.round(performance.now() - msg.ts);
      $("pc-rtt").textContent = `${lastRtt} ms`;
      outstandingPing = null;
    }
  };
  inputWs.onclose = () => {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (inputWsClosing) return;
    // Was an unsolicited drop — let the peer-poll loop figure out
    // whether this is a temporary blip or the peer really left.
    setStatus("input dropped", "warn");
  };
  inputWs.onerror = () => setStatus("input error", "danger");
}

function stopInput() {
  inputWsClosing = true;
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  if (inputWs) {
    try { inputWs.close(); } catch (_) {}
    inputWs = null;
  }
  outstandingPing = null;
}

function sendInput(obj) {
  if (!inputWs || inputWs.readyState !== 1) return;
  try { inputWs.send(JSON.stringify(obj)); } catch (_) {}
}

function sendPing() {
  if (!inputWs || inputWs.readyState !== 1) return;
  // Skip if a previous ping never came back — keeps the RTT honest
  // when the link is congested instead of artificially inflating it.
  if (outstandingPing !== null) return;
  outstandingPing = performance.now();
  sendInput({ type: "ping", ts: outstandingPing });
}

// ---- Pointer + keyboard capture --------------------------------------
// Throttle mousemove forwarding to ~50 Hz so we don't flood the input
// WS — the peer cursor still feels live, and the proxy stays calm on
// flaky links.
const MOVE_INTERVAL_MS = 20;
let lastMoveSentAt = 0;

function attachPointerHandlers() {
  const img = $("pc-img");
  img.addEventListener("contextmenu", (e) => e.preventDefault());

  img.addEventListener("mousemove", (e) => {
    if (!currentPeer) return;
    const now = performance.now();
    if (now - lastMoveSentAt < MOVE_INTERVAL_MS) return;
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    lastMoveSentAt = now;
    sendInput({ type: "move_to", x, y });
  });

  img.addEventListener("click", (e) => {
    if (!currentPeer) return;
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    sendInput({ type: "click_at", x, y, button: "left" });
    // Keep keyboard focus so subsequent typing reaches the peer.
    $("pc-keys").focus();
  });

  img.addEventListener("auxclick", (e) => {
    if (!currentPeer || e.button !== 2) return;
    const rect = img.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    sendInput({ type: "click_at", x, y, button: "right" });
  });

  img.addEventListener("wheel", (e) => {
    if (!currentPeer) return;
    e.preventDefault();
    // Normalise to ~1 step per ~100 pixels so pynput doesn't get a
    // firehose; the peer multiplies it back by its OS scroll setting.
    const dy = Math.max(-3, Math.min(3, Math.round(e.deltaY / 100)));
    const dx = Math.max(-3, Math.min(3, Math.round(e.deltaX / 100)));
    if (dx || dy) sendInput({ type: "scroll", dx, dy });
  }, { passive: false });
}

function attachKeyboardHandlers() {
  const keys = $("pc-keys");
  // Single-character keys -> "type"; named keys -> "key".
  keys.addEventListener("keydown", (e) => {
    if (!currentPeer) return;
    const isPrintable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (isPrintable) {
      sendInput({ type: "type", text: e.key });
    } else {
      sendInput({ type: "key", key: e.key });
    }
    e.preventDefault();
  });
  // Re-grab focus whenever the user clicks anywhere in the stage so
  // typing keeps working even after they tab away momentarily.
  $("pc-stage").addEventListener("mousedown", () => keys.focus(), true);
}

// ---- Bootstrap -------------------------------------------------------
async function showApp() {
  $("login-screen").classList.add("hidden");
  $("pc-app").classList.remove("hidden");
  attachPointerHandlers();
  attachKeyboardHandlers();
  $("pc-peer").addEventListener("change", (e) => {
    const id = e.target.value;
    if (!id) { disconnect(); return; }
    startStreaming(id);
  });
  $("pc-disconnect").addEventListener("click", disconnect);
  $("pc-reload").addEventListener("click", () => {
    if (currentPeer) reloadStream();
  });

  await loadPeers();
  showOverlay("No peer selected", "Pick a paired peer above to start.");
  // Refresh the peer list every 3 s so online/offline transitions
  // surface promptly without any ping-storm.
  peerPollTimer = setInterval(loadPeers, 3000);

  // Auto-pick the first online + controllable peer to save a click
  // when there's only one.
  const auto = peers.find((p) => p.online && p.controllable);
  if (auto && peers.filter((p) => p.online && p.controllable).length === 1) {
    $("pc-peer").value = auto.id;
    startStreaming(auto.id);
  }
}

function showLogin() {
  $("login-screen").classList.remove("hidden");
  $("pc-app").classList.add("hidden");
  initLogin(showApp);
}

setOnSessionLost(showLogin);

(async () => {
  if (await checkAuth()) showApp();
  else showLogin();
})();
