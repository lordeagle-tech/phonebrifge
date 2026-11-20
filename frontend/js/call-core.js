// Shared bidirectional voice-call controller for phone and laptop viewer.
//
// The signaling server has a single sender/viewer slot per room, so we use
// two rooms — one per direction — and treat the side that initiates as
// `sender` in its outbound room. Each side also keeps a passive `viewer`
// listener on its inbound room so it can ring when the other side calls.
//
// Rooms:
//   "call-p2l"  — phone initiates → laptop receives
//   "call-l2p"  — laptop initiates → phone receives
//
// The same RTCPeerConnection / mic stream / overlay is shared between the
// two directions because only one call can be in progress at a time.

import { getToken, openTicketedWebSocket } from "./auth.js";
import { showToast } from "./utils.js";

const AUDIO_CONSTRAINTS = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
  video: false,
};

export function createCallController(cfg) {
  // cfg = { outRoom, inRoom, label, ids: {...}, statusPillId? }
  const ids = cfg.ids;
  const $ = (id) => document.getElementById(id);

  // ── State ─────────────────────────────────────────────────────────
  let pc = null;
  let localStream = null;
  let remoteAudioEl = null;
  let muted = false;
  let state = "idle"; // idle | calling | ringing | connecting | in-call
  let role = null;    // "caller" | "callee" while a call exists
  let callSock = null;

  // Always-on listener (inbound side, role=viewer).
  let listenerSock = null;
  let listenerActive = false;
  let listenerGen = 0;
  let reconnectTimer = null;
  let reconnectDelay = 2000;

  // Active-call timer.
  let timer = null;
  let connectStart = 0;
  const pendingIce = [];

  // Ringer (callee side).
  let ringer = null;

  // ── UI helpers ────────────────────────────────────────────────────
  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, "0")}`;
  }
  function setBtnLabel() {
    const b = $(ids.btn);
    if (!b) return;
    if (state === "idle") {
      b.textContent = `📞  Call ${cfg.peerLabel}`;
      b.disabled = false;
    } else {
      b.textContent = "📞  In call…";
      b.disabled = true;
    }
  }
  function setStatusPill(text) {
    if (!cfg.statusPillId) return;
    const el = $(cfg.statusPillId);
    if (el) el.textContent = text;
  }
  function setStatus(text) {
    const el = $(ids.status);
    if (el) el.textContent = text;
  }
  function showActive(show) { const el = $(ids.overlay); if (el) el.style.display = show ? "flex" : "none"; }
  function showRing(show) { const el = $(ids.ringOverlay); if (el) el.style.display = show ? "flex" : "none"; }
  function startTimer() {
    stopTimer();
    connectStart = Date.now();
    const t = $(ids.timer); if (t) t.textContent = "0:00";
    timer = setInterval(() => {
      const t2 = $(ids.timer); if (t2) t2.textContent = fmtElapsed(Date.now() - connectStart);
    }, 500);
  }
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
  function setMuteLabel() {
    const m = $(ids.muteBtn); if (m) m.textContent = muted ? "🔇  Unmute" : "🎙  Mute";
  }

  // ── Ringer (synthesized two-tone) ────────────────────────────────
  function startRinger() {
    stopRinger();
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const gain = ctx.createGain();
      gain.gain.value = 0;
      gain.connect(ctx.destination);
      const osc = ctx.createOscillator(); osc.type = "sine"; osc.frequency.value = 480;
      const osc2 = ctx.createOscillator(); osc2.type = "sine"; osc2.frequency.value = 620;
      osc.connect(gain); osc2.connect(gain); osc.start(); osc2.start();
      let on = false;
      const interval = setInterval(() => {
        on = !on;
        const t = ctx.currentTime;
        gain.gain.cancelScheduledValues(t);
        gain.gain.setValueAtTime(gain.gain.value, t);
        gain.gain.linearRampToValueAtTime(on ? 0.06 : 0, t + 0.05);
      }, 600);
      ringer = { ctx, interval, osc, osc2 };
    } catch { /* user gesture not yet given — visual ring still shows */ }
  }
  function stopRinger() {
    if (!ringer) return;
    try { clearInterval(ringer.interval); } catch { /* ignore */ }
    try { ringer.osc.stop(); } catch { /* ignore */ }
    try { ringer.osc2.stop(); } catch { /* ignore */ }
    try { ringer.ctx.close(); } catch { /* ignore */ }
    ringer = null;
  }

  // ── ICE config ────────────────────────────────────────────────────
  async function getIceServers() {
    try {
      const token = getToken();
      const res = await fetch("/api/webrtc/config", { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) return [];
      return (await res.json()).iceServers || [];
    } catch { return []; }
  }
  function ensureRemoteAudio() {
    if (!remoteAudioEl) {
      remoteAudioEl = document.createElement("audio");
      remoteAudioEl.autoplay = true;
      remoteAudioEl.playsInline = true;
      document.body.appendChild(remoteAudioEl);
    }
    return remoteAudioEl;
  }

  // ── Signaling helpers ─────────────────────────────────────────────
  function send(obj) {
    if (callSock && callSock.readyState === 1) {
      try { callSock.send(JSON.stringify(obj)); } catch { /* ignore */ }
    }
  }
  async function flushIce() {
    while (pendingIce.length) {
      const c = pendingIce.shift();
      try { await pc.addIceCandidate(c); } catch { /* ignore */ }
    }
  }
  async function negotiateAsCaller() {
    if (!pc || !callSock || callSock.readyState !== 1) return;
    const offer = await pc.createOffer({ offerToReceiveAudio: true });
    await pc.setLocalDescription(offer);
    send({ type: "signal", data: { kind: "sdp", sdp: pc.localDescription } });
  }
  async function handleSignal(msg) {
    if (!msg.data) return;
    if (msg.data.kind === "accept" && role === "caller") {
      setStatus("Connecting…");
      state = "connecting";
      await negotiateAsCaller();
      return;
    }
    if (msg.data.kind === "decline" && role === "caller") {
      showToast("Call declined", "error");
      endCall(false);
      return;
    }
    if (!pc) return;
    if (msg.data.kind === "sdp" && msg.data.sdp) {
      await pc.setRemoteDescription(msg.data.sdp);
      await flushIce();
      if (msg.data.sdp.type === "offer") {
        const ans = await pc.createAnswer();
        await pc.setLocalDescription(ans);
        send({ type: "signal", data: { kind: "sdp", sdp: pc.localDescription } });
      }
    } else if (msg.data.kind === "ice" && msg.data.candidate) {
      if (!pc.remoteDescription) pendingIce.push(msg.data.candidate);
      else { try { await pc.addIceCandidate(msg.data.candidate); } catch { /* ignore */ } }
    }
  }

  // ── Listener (inbound viewer role) ────────────────────────────────
  function scheduleReconnect(delay) {
    if (!listenerActive || reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (listenerActive && !listenerSock) openListener();
    }, delay);
  }
  async function openListener() {
    if (listenerSock) {
      try { listenerSock.close(); } catch { /* ignore */ }
      listenerSock = null;
    }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    listenerActive = true;
    const myGen = ++listenerGen;

    const token = getToken();
    if (!token) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/signal?room=${encodeURIComponent(cfg.inRoom)}&role=viewer`;
    let sock;
    try {
      sock = await openTicketedWebSocket(url);
    } catch {
      if (myGen === listenerGen && listenerActive) scheduleReconnect(reconnectDelay);
      return;
    }
    if (myGen !== listenerGen || !listenerActive) {
      sock.close();
      return;
    }
    listenerSock = sock;

    sock.onopen = () => { if (myGen === listenerGen) reconnectDelay = 2000; };

    sock.onmessage = async (ev) => {
      if (myGen !== listenerGen) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ready") return;
      if (msg.type === "peer-joined" && msg.role === "sender") {
        if (state !== "idle") return; // already busy
        role = "callee";
        callSock = sock;
        state = "ringing";
        setStatus(`Incoming call from ${cfg.peerLabel}`);
        setStatusPill("Incoming");
        showRing(true);
        startRinger();
      } else if (msg.type === "peer-left") {
        if (state === "ringing") {
          stopRinger(); showRing(false);
          state = "idle"; role = null;
          setStatus("Idle"); setStatusPill("Idle");
          showToast("Caller hung up", "error");
        } else if ((state === "in-call" || state === "connecting") && role === "callee") {
          showToast("Call ended", "success");
          endCall(false);
        }
      } else if (msg.type === "signal" && (role === "callee" || state === "ringing")) {
        await handleSignal(msg);
      } else if (msg.type === "error" && msg.message === "replaced") {
        listenerActive = false; // another tab took over
      }
    };

    sock.onclose = (ev) => {
      if (myGen !== listenerGen) return;
      listenerSock = null;
      if (callSock === sock) callSock = null;
      if (state === "ringing") {
        stopRinger(); showRing(false);
        state = "idle"; role = null;
        setStatus("Idle"); setStatusPill("Idle");
      }
      if (ev.code === 4409 || ev.code === 4001) { listenerActive = false; return; }
      if (!listenerActive) return;
      scheduleReconnect(reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
  }
  function closeListener() {
    listenerActive = false;
    listenerGen += 1;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (listenerSock) { try { listenerSock.close(); } catch { /* ignore */ } listenerSock = null; }
  }

  // ── Caller (outbound) ────────────────────────────────────────────
  async function startCall() {
    if (state !== "idle") return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showToast("Your browser doesn't support microphone capture", "error");
      return;
    }
    role = "caller";
    state = "calling";
    setBtnLabel();
    setStatusPill("Calling…");
    showActive(true);
    setStatus("Requesting microphone…");
    const t = $(ids.timer); if (t) t.textContent = "";
    muted = false; setMuteLabel();

    try {
      localStream = await navigator.mediaDevices.getUserMedia(AUDIO_CONSTRAINTS);
    } catch {
      showToast("Microphone permission denied", "error");
      setStatus("Microphone permission denied");
      setTimeout(() => endCall(false), 1500);
      return;
    }

    const iceServers = await getIceServers();
    pc = new RTCPeerConnection({ iceServers });
    for (const tr of localStream.getTracks()) pc.addTrack(tr, localStream);
    pc.ontrack = (ev) => {
      if (ev.streams && ev.streams[0]) ensureRemoteAudio().srcObject = ev.streams[0];
    };
    pc.onicecandidate = (e) => {
      if (e.candidate) send({ type: "signal", data: { kind: "ice", candidate: e.candidate.toJSON() } });
    };
    pc.onconnectionstatechange = () => {
      const st = pc?.connectionState;
      if (st === "connected") {
        state = "in-call"; setStatus("In call"); setStatusPill("In call"); startTimer();
      } else if (st === "failed" || st === "disconnected" || st === "closed") {
        if (state === "in-call" || state === "connecting") endCall(true);
      }
    };

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      callSock = await openTicketedWebSocket(
        `${proto}//${location.host}/ws/signal?room=${encodeURIComponent(cfg.outRoom)}&role=sender`
      );
    } catch {
      showToast("Could not authorize call", "error");
      endCall(false);
      return;
    }
    callSock.onopen = () => setStatus("Calling…");
    callSock.onclose = () => { if (state !== "idle" && role === "caller") endCall(true); };
    callSock.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ready") return;
      if (msg.type === "peer-joined" && msg.role === "viewer") {
        setStatus(`Ringing ${cfg.peerLabel}…`);
      } else if (msg.type === "peer-left") {
        if (state === "in-call") showToast("Call ended", "success");
        else showToast("Call unanswered", "error");
        endCall(false);
      } else if (msg.type === "signal") {
        await handleSignal(msg);
      } else if (msg.type === "error") {
        showToast("Call error: " + (msg.message || "unknown"), "error");
        endCall(false);
      }
    };
  }

  // ── Callee accept/decline ─────────────────────────────────────────
  async function acceptCall() {
    if (state !== "ringing" || role !== "callee") return;
    stopRinger();
    showRing(false);
    state = "connecting";
    setStatus("Connecting…");
    setStatusPill("Connecting…");
    const t = $(ids.timer); if (t) t.textContent = "";
    muted = false; setMuteLabel();
    showActive(true);
    setBtnLabel();

    try {
      localStream = await navigator.mediaDevices.getUserMedia(AUDIO_CONSTRAINTS);
    } catch {
      showToast("Microphone permission denied", "error");
      declineCall();
      return;
    }
    const iceServers = await getIceServers();
    pc = new RTCPeerConnection({ iceServers });
    for (const tr of localStream.getTracks()) pc.addTrack(tr, localStream);
    pc.ontrack = (ev) => {
      if (ev.streams && ev.streams[0]) ensureRemoteAudio().srcObject = ev.streams[0];
    };
    pc.onicecandidate = (e) => {
      if (e.candidate) send({ type: "signal", data: { kind: "ice", candidate: e.candidate.toJSON() } });
    };
    pc.onconnectionstatechange = () => {
      const st = pc?.connectionState;
      if (st === "connected") {
        state = "in-call"; setStatus("In call"); setStatusPill("In call"); startTimer();
      } else if (st === "failed" || st === "disconnected" || st === "closed") {
        if (state === "in-call" || state === "connecting") endCall(true);
      }
    };
    send({ type: "signal", data: { kind: "accept" } });
  }

  function declineCall() {
    if (state !== "ringing") return;
    stopRinger();
    showRing(false);
    send({ type: "signal", data: { kind: "decline" } });
    send({ type: "bye" });
    state = "idle"; role = null;
    setStatus("Idle"); setStatusPill("Idle");
    setBtnLabel();
    closeListener();
    openListener();
  }

  function endCall(notify = true) {
    if (state === "idle") return;
    const wasInCall = state === "in-call";
    const wasCaller = role === "caller";
    state = "idle";
    stopTimer();
    stopRinger();
    if (notify) send({ type: "bye" });
    if (callSock && (wasCaller || callSock !== listenerSock)) {
      try { callSock.close(); } catch { /* ignore */ }
    }
    callSock = null;
    if (pc) { try { pc.close(); } catch { /* ignore */ } pc = null; }
    if (localStream) {
      for (const tr of localStream.getTracks()) { try { tr.stop(); } catch { /* ignore */ } }
      localStream = null;
    }
    if (remoteAudioEl) { try { remoteAudioEl.srcObject = null; } catch { /* ignore */ } }
    pendingIce.length = 0;
    role = null;

    // Briefly show an explicit "Call ended" state in the active overlay
    // before tearing it down, so the user gets clear closure rather than
    // the overlay vanishing silently.
    if (wasInCall) {
      setStatus("Call ended");
      setStatusPill("Call ended");
      const t = $(ids.timer); if (t) t.textContent = "";
      setTimeout(() => {
        if (state !== "idle") return; // a new call already started
        showActive(false);
        showRing(false);
        setStatus("Idle"); setStatusPill("Idle");
      }, 1200);
    } else {
      showActive(false);
      showRing(false);
      setStatus("Idle"); setStatusPill("Idle");
    }
    setBtnLabel();
    // Rebuild a fresh inbound listener (the callee path may have used the
    // listener socket for signaling, which the bye just disrupted).
    closeListener();
    openListener();
  }

  function toggleMute() {
    if (!localStream) return;
    muted = !muted;
    for (const tr of localStream.getAudioTracks()) tr.enabled = !muted;
    setMuteLabel();
  }

  function init() {
    const bind = (id, fn) => {
      const el = $(id);
      if (el && !el.dataset.bound) { el.dataset.bound = "1"; el.addEventListener("click", fn); }
    };
    bind(ids.btn, startCall);
    bind(ids.endBtn, () => endCall(true));
    bind(ids.muteBtn, toggleMute);
    bind(ids.acceptBtn, acceptCall);
    bind(ids.declineBtn, declineCall);
    setStatus("Idle"); setStatusPill("Idle"); setBtnLabel();
    openListener();
  }

  function teardown() {
    if (state !== "idle") endCall(true);
    closeListener();
    stopRinger();
  }

  return { init, teardown, startCall, endCall };
}
