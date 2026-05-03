import { initLogin, getToken, saveToken, checkAuth, setOnSessionLost } from "./auth.js";
import { showToast } from "./utils.js";
import { initViewerCall, teardownViewerCall } from "./viewer-call.js";

const ROOM = "main";

let pc = null;
let sock = null;

function setStatus(text, cls) {
  const el = document.getElementById("viewer-status");
  if (!el) return;
  el.textContent = text;
  el.className = "";
  if (cls) el.classList.add(cls);
}

function showEmpty(show) {
  const el = document.getElementById("viewer-empty");
  if (el) el.style.display = show ? "block" : "none";
}

async function getIceServers() {
  const token = getToken();
  const res = await fetch("/api/webrtc/config", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) return [];
  const data = await res.json();
  return data.iceServers || [];
}

async function startViewer() {
  document.getElementById("viewer-login").style.display = "none";
  document.getElementById("viewer-app").style.display = "flex";
  showEmpty(true);
  setStatus("waiting for sender…", "warn");
  initViewerCall();

  const iceServers = await getIceServers();
  pc = new RTCPeerConnection({ iceServers });
  const video = document.getElementById("viewer-video");

  pc.ontrack = (ev) => {
    if (ev.streams && ev.streams[0]) {
      video.srcObject = ev.streams[0];
      showEmpty(false);
    }
  };

  pc.onconnectionstatechange = () => {
    const st = pc?.connectionState;
    if (st === "connected") setStatus("connected", "ok");
    else if (st === "connecting") setStatus("connecting…", "warn");
    else if (st === "failed" || st === "disconnected") {
      setStatus("disconnected", "danger");
      showEmpty(true);
    }
  };

  const token = getToken();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  sock = new WebSocket(
    `${proto}//${location.host}/ws/signal?token=${encodeURIComponent(token)}&room=${ROOM}&role=viewer`
  );

  pc.onicecandidate = (e) => {
    if (e.candidate && sock?.readyState === 1) {
      sock.send(JSON.stringify({
        type: "signal",
        data: { kind: "ice", candidate: e.candidate.toJSON() },
      }));
    }
  };

  const pendingIce = [];
  async function flushIce() {
    while (pendingIce.length) {
      const c = pendingIce.shift();
      try { await pc.addIceCandidate(c); } catch { /* ignore */ }
    }
  }

  sock.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ready") return;
    if (msg.type === "peer-joined" && msg.role === "sender") {
      setStatus("sender connected", "warn");
    } else if (msg.type === "peer-left") {
      setStatus("sender disconnected", "danger");
      showEmpty(true);
    } else if (msg.type === "signal" && msg.data) {
      if (msg.data.kind === "sdp" && msg.data.sdp) {
        await pc.setRemoteDescription(msg.data.sdp);
        await flushIce();
        if (msg.data.sdp.type === "offer") {
          const answer = await pc.createAnswer();
          await pc.setLocalDescription(answer);
          sock.send(JSON.stringify({
            type: "signal",
            data: { kind: "sdp", sdp: pc.localDescription },
          }));
        }
      } else if (msg.data.kind === "ice" && msg.data.candidate) {
        if (!pc.remoteDescription) {
          pendingIce.push(msg.data.candidate);
        } else {
          try { await pc.addIceCandidate(msg.data.candidate); } catch { /* ignore */ }
        }
      }
    }
  };

  sock.onclose = () => setStatus("signaling closed", "danger");
}

function showLogin() {
  document.getElementById("viewer-login").style.display = "flex";
  document.getElementById("viewer-app").style.display = "none";
  initLogin(() => startViewer());
}

document.getElementById("viewer-fullscreen-btn")?.addEventListener("click", () => {
  const v = document.getElementById("viewer-video");
  if (v && v.requestFullscreen) v.requestFullscreen().catch(() => {});
});

(async function boot() {
  setOnSessionLost(() => {
    if (pc) { try { pc.close(); } catch { /* ignore */ } pc = null; }
    if (sock) { try { sock.close(); } catch { /* ignore */ } sock = null; }
    teardownViewerCall();
    showLogin();
    showToast("Session expired — please sign in again", "error");
  });
  const ok = await checkAuth();
  if (ok) startViewer();
  else showLogin();
})();
