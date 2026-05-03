/**
 * Browser-based phone-mirror viewer.
 *
 * Connects to /ws/mirror?serial=...&token=... and renders incoming PNG
 * frames into a <canvas>. Falls back gracefully if the capture session
 * isn't running, the device hasn't been picked, or the server returns an
 * "ended" sentinel after the user stops the mirror from any client.
 */
import { initLogin, getToken, checkAuth, setOnSessionLost } from "./auth.js";
import { showToast } from "./utils.js";

const $ = (id) => document.getElementById(id);

let ws = null;
let reconnectTimer = null;
let frameCount = 0;
let fpsTimer = null;
let lastFpsTick = 0;
let currentSerial = null;
let lastInputErrorAt = 0;

// ---- Audio (browser-side phone-audio playback) ----
//
// Audio capture is opt-in (the user clicks the speaker button). Once
// enabled, we POST /api/scrcpy/audio-viewer/start, then open a WS to
// /ws/mirror-audio. The server sends a JSON `hello` describing the PCM
// format, then a stream of binary 16-bit signed little-endian PCM chunks
// (48 kHz stereo). We decode each chunk into a Float32 AudioBuffer and
// schedule it on a GainNode so we can mute when the tab backgrounds.
let audioWs = null;
let audioCtx = null;
let audioGain = null;
let audioFormat = null;             // {sample_rate, channels, bit_depth}
let audioNextStart = 0;             // next absolute AudioContext time to schedule
let audioReconnectTimer = null;
let audioEnabled = false;           // user-intended state (toggle button)
let audioPreMuteGain = 1.0;         // remember gain before tab-blur mute
let audioToggleBusy = false;        // re-entrancy guard for toggleAudio()

function setStatus(text, cls) {
  const pill = $("mv-status");
  pill.textContent = text;
  pill.classList.remove("ok", "warn", "danger");
  if (cls) pill.classList.add(cls);
}

function showEmpty(msg) {
  const e = $("mv-empty");
  if (msg) $("mv-empty-msg").innerHTML = msg;
  e.classList.remove("hidden");
}
function hideEmpty() { $("mv-empty").classList.add("hidden"); }

function paramSerial() {
  const u = new URL(window.location.href);
  return (u.searchParams.get("serial") || "").trim();
}

async function pickSerial() {
  // 1. ?serial= wins if present.
  const fromUrl = paramSerial();
  if (fromUrl) return fromUrl;
  // 2. Otherwise ask the server which capture session is running. We use
  //    that instead of /scrcpy/recent so the viewer works even on a
  //    second laptop that's never opened the setup page.
  try {
    const res = await fetch("/api/scrcpy/viewer/status", {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) return "";
    const body = await res.json();
    const running = (body.sessions || []).filter((s) => s.running);
    return running.length ? running[0].serial : "";
  } catch {
    return "";
  }
}

// Latest values pulled from the server's stats reply on the WS. The
// FPS pill is composed from these so adaptive bit-rate changes are
// visible at a glance.
let lastBitrateBps = null;        // current applied --bit-rate (server-side)
let lastBitrateChanges = 0;       // monotonic counter of adapt events
let lastMeasuredFps = 0;          // most-recent measured FPS, for re-rendering

// Quality pin (Auto / Low / Med / High). Saved per-serial in
// localStorage so a reconnect within the same tab keeps the user's
// choice — including across the WS reconnect that follows a
// PNG/h264 mode flip.
const QUALITY_LS_PREFIX = "mirror.quality.";
function loadQualityPref(serial) {
  if (!serial) return "auto";
  try {
    return window.localStorage.getItem(QUALITY_LS_PREFIX + serial) || "auto";
  } catch { return "auto"; }
}
function saveQualityPref(serial, value) {
  if (!serial) return;
  try { window.localStorage.setItem(QUALITY_LS_PREFIX + serial, value); }
  catch { /* private mode etc. — non-fatal */ }
}

async function applyQualityPin(value) {
  // value is "auto" or a stringified bps integer (e.g. "4000000").
  if (!currentSerial) return;
  const bitrate = (value === "auto" || !value) ? null : Number.parseInt(value, 10);
  try {
    const r = await fetch("/api/scrcpy/viewer/bitrate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial, bitrate }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({ detail: r.statusText }));
      // 404 just means the capture isn't running yet (the page loaded
      // before we hit Start). Don't badger the user with a toast for
      // that — we'll re-apply on the next hello.
      if (r.status !== 404) {
        showToast(`Quality: ${detail.detail || `HTTP ${r.status}`}`);
      }
      return;
    }
  } catch (e) {
    showToast(`Quality error: ${e.message}`);
  }
}

function fmtBitrate(bps) {
  if (!bps || bps <= 0) return "";
  const mbps = bps / 1_000_000;
  return mbps >= 10 ? `${mbps.toFixed(0)} Mbps` : `${mbps.toFixed(1)} Mbps`;
}

// Show/hide the "🔒 PINNED" mini-pill next to the FPS readout based
// on the current dropdown value. Called whenever the user changes the
// dropdown and when the WS hello arrives (which can re-enable or
// disable pinning depending on h264 vs PNG mode).
function updatePinnedIndicator() {
  const pill = $("mv-pinned");
  const sel = $("mv-quality");
  if (!pill) return;
  const pinned = !!(sel && !sel.disabled
                    && sel.value && sel.value !== "auto");
  pill.classList.toggle("show", pinned);
}

function renderFpsPill() {
  const el = $("mv-fps");
  if (!el) return;
  const fpsText = `${lastMeasuredFps.toFixed(1)} fps`;
  if (videoMode === "h264") {
    const br = fmtBitrate(lastBitrateBps);
    el.textContent = br ? `h264 · ${fpsText} · ${br}` : `h264 · ${fpsText}`;
  } else if (videoMode === "png") {
    el.textContent = fpsText;
  } else {
    el.textContent = fpsText;
  }
}

let bitrateFlashTimer = null;
function flashBitrateChange(direction) {
  const el = $("mv-fps");
  if (!el) return;
  const cls = direction === "up" ? "flash-up" : "flash-down";
  // Cancel any in-flight clear so a rapid second change can't have
  // its newer flash wiped early by the previous timer.
  if (bitrateFlashTimer) { clearTimeout(bitrateFlashTimer); bitrateFlashTimer = null; }
  el.classList.remove("flash-up", "flash-down");
  // Force a reflow so re-adding the same class restarts the animation.
  void el.offsetWidth;
  el.classList.add(cls);
  bitrateFlashTimer = setTimeout(() => {
    el.classList.remove(cls);
    bitrateFlashTimer = null;
  }, 3100);
}

function tickFps() {
  const now = performance.now();
  if (!lastFpsTick) { lastFpsTick = now; return; }
  const dt = (now - lastFpsTick) / 1000;
  if (dt < 1.0) return;
  lastMeasuredFps = frameCount / dt;
  renderFpsPill();
  frameCount = 0;
  lastFpsTick = now;
}

function drawFrame(blob) {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    const canvas = $("mv-canvas");
    if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
    }
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    frameCount += 1;
    if (frameCount === 1) hideEmpty();
  };
  img.onerror = () => URL.revokeObjectURL(url);
  img.src = url;
}

// ---------- H.264 / WebCodecs path ----------
//
// Modern browsers (Chrome, Edge, Safari 17+) expose VideoDecoder and
// can decode raw H.264 access units directly. This is dramatically
// smoother and lighter on the wire than the PNG screencap loop, so we
// prefer it whenever available and fall back transparently to PNG.
let videoMode = "png";          // negotiated from /scrcpy/viewer/start
let videoDecoder = null;
let videoDecoderCodec = "";
let videoSeenKeyframe = false;
let videoTimestampUs = 0;
let videoIntentionalClose = false;  // set when we close the WS to switch modes
// Cached parameter-set NALs from the server's hello payload. Prepended
// to the first decoded AU so a viewer attaching mid-stream (between
// keyframes) can bootstrap the decoder without waiting for the next
// SPS/PPS to arrive in-band.
let videoBootstrapNals = null;  // Uint8Array | null
const VIDEO_FRAME_INTERVAL_US = 16_000;  // ~60fps virtual clock
// Adaptive-bitrate heartbeat: every STATS_HEARTBEAT_MS we send the WS's
// `bufferedAmount` (bytes the browser hasn't been able to flush yet) to
// the server. The capture session uses that — alongside its own
// per-subscriber queue depth — to step the screenrecord --bit-rate up
// (clean link, more headroom) or down (frames piling up). PNG mode
// skips this; the server only reacts to it for h264 sessions.
let videoStatsTimer = null;
const STATS_HEARTBEAT_MS = 2000;

function webCodecsSupported() {
  return typeof window.VideoDecoder === "function"
      && typeof window.EncodedVideoChunk === "function";
}

async function probeH264Support() {
  if (!webCodecsSupported()) return false;
  try {
    // We don't know the exact profile yet; baseline 3.0 is the
    // lowest-common-denominator screenrecord output and is universally
    // supported when WebCodecs is available at all.
    const sup = await window.VideoDecoder.isConfigSupported({
      codec: "avc1.42E01E",
      optimizeForLatency: true,
    });
    return !!(sup && sup.supported);
  } catch {
    return false;
  }
}

function findNalInAu(auBytes, type) {
  // auBytes is a Uint8Array containing Annex-B NAL units. Find the
  // first NAL whose header byte's low 5 bits == type.
  const data = auBytes instanceof Uint8Array ? auBytes : new Uint8Array(auBytes);
  let i = 0;
  while (i < data.length - 3) {
    let payloadStart;
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1) {
      payloadStart = i + 3;
    } else if (data[i] === 0 && data[i + 1] === 0
               && data[i + 2] === 0 && data[i + 3] === 1) {
      payloadStart = i + 4;
    } else { i += 1; continue; }
    if (payloadStart < data.length
        && (data[payloadStart] & 0x1f) === type) {
      // Find the end (next start code, or end of buffer).
      let j = payloadStart + 1;
      while (j < data.length - 2) {
        if (data[j] === 0 && data[j + 1] === 0
            && (data[j + 2] === 1
                || (data[j + 2] === 0 && j + 3 < data.length
                    && data[j + 3] === 1))) {
          break;
        }
        j += 1;
      }
      return data.subarray(payloadStart, j);
    }
    i = payloadStart;
  }
  return null;
}

function auContainsNal(auBytes, type) {
  return findNalInAu(auBytes, type) !== null;
}

function codecStringFromSps(sps) {
  if (!sps || sps.length < 4) return "avc1.42E01E";
  const hex = (b) => b.toString(16).padStart(2, "0").toUpperCase();
  return `avc1.${hex(sps[1])}${hex(sps[2])}${hex(sps[3])}`;
}

function ensureVideoDecoder(codec) {
  if (videoDecoder && videoDecoderCodec === codec) return videoDecoder;
  // Reconfigure on codec change (e.g. SPS updated after screenrecord
  // restart) — VideoDecoder.configure() can be called repeatedly.
  if (!videoDecoder) {
    const canvas = $("mv-canvas");
    const ctx = canvas.getContext("2d");
    videoDecoder = new VideoDecoder({
      output: (frame) => {
        try {
          if (canvas.width !== frame.displayWidth
              || canvas.height !== frame.displayHeight) {
            canvas.width = frame.displayWidth;
            canvas.height = frame.displayHeight;
          }
          ctx.drawImage(frame, 0, 0);
          frameCount += 1;
          if (frameCount === 1) hideEmpty();
        } finally {
          frame.close();
        }
      },
      error: (e) => {
        // Decoder went south — most often a missed keyframe after a
        // stream restart. Reset and wait for the next IDR.
        // eslint-disable-next-line no-console
        console.warn("VideoDecoder error", e);
        try { videoDecoder.close(); } catch { /* ignore */ }
        videoDecoder = null;
        videoDecoderCodec = "";
        videoSeenKeyframe = false;
      },
    });
  }
  try {
    videoDecoder.configure({ codec, optimizeForLatency: true });
    videoDecoderCodec = codec;
  } catch (e) {
    showToast(`Video decoder configure failed: ${e.message}`);
    try { videoDecoder.close(); } catch { /* ignore */ }
    videoDecoder = null;
    videoDecoderCodec = "";
    return null;
  }
  return videoDecoder;
}

function handleAccessUnit(auBytes) {
  const data = auBytes instanceof Uint8Array
    ? auBytes : new Uint8Array(auBytes);
  // If we don't have a decoder yet, look for an SPS (NAL type 7) to
  // derive the exact codec string. Until then, we can't decode.
  let codec = videoDecoderCodec;
  if (!codec) {
    const sps = findNalInAu(data, 7);
    if (!sps) {
      // No SPS in this AU and we don't know the codec — drop and wait.
      return;
    }
    codec = codecStringFromSps(sps);
  } else {
    // If a new SPS appeared with a different codec string, reconfigure.
    const sps = findNalInAu(data, 7);
    if (sps) {
      const newCodec = codecStringFromSps(sps);
      if (newCodec !== codec) codec = newCodec;
    }
  }
  const dec = ensureVideoDecoder(codec);
  if (!dec) return;
  const isKey = auContainsNal(data, 5);
  if (!videoSeenKeyframe) {
    if (!isKey) return;            // wait for first IDR before decoding
    videoSeenKeyframe = true;
  }
  videoTimestampUs += VIDEO_FRAME_INTERVAL_US;
  // If the AU is missing SPS/PPS but we cached them from the server's
  // hello, prepend them so the decoder can configure on this very
  // chunk. Drop the cache once consumed — subsequent IDRs include
  // SPS/PPS in-band again.
  let chunkData = data;
  if (videoBootstrapNals
      && (!findNalInAu(data, 7) || !findNalInAu(data, 8))) {
    const merged = new Uint8Array(videoBootstrapNals.length + data.length);
    merged.set(videoBootstrapNals, 0);
    merged.set(data, videoBootstrapNals.length);
    chunkData = merged;
  }
  videoBootstrapNals = null;
  try {
    dec.decode(new EncodedVideoChunk({
      type: isKey ? "key" : "delta",
      timestamp: videoTimestampUs,
      data: chunkData,
    }));
  } catch (e) {
    // Bad chunk — reset and wait for the next keyframe.
    // eslint-disable-next-line no-console
    console.warn("decode() threw", e);
    try { dec.close(); } catch { /* ignore */ }
    videoDecoder = null;
    videoDecoderCodec = "";
    videoSeenKeyframe = false;
  }
}

function resetVideoDecoder() {
  if (videoDecoder) {
    try { videoDecoder.close(); } catch { /* ignore */ }
    videoDecoder = null;
  }
  videoDecoderCodec = "";
  videoSeenKeyframe = false;
  videoTimestampUs = 0;
}

function stopStatsHeartbeat() {
  if (videoStatsTimer) {
    clearInterval(videoStatsTimer);
    videoStatsTimer = null;
  }
}

function startStatsHeartbeat() {
  // Only meaningful for h264 — the PNG path uses a single-slot fan-out
  // queue and the server doesn't act on stats there. Restart any
  // existing timer so a reconnect doesn't double up.
  stopStatsHeartbeat();
  videoStatsTimer = setInterval(() => {
    if (!ws || ws.readyState !== 1) return;
    if (videoMode !== "h264") return;
    // ws.bufferedAmount is bytes queued by the browser but not yet
    // sent to the server — a direct read on local back-pressure that
    // doesn't need any RTT measurement.
    let buffered = 0;
    try { buffered = ws.bufferedAmount || 0; } catch { /* ignore */ }
    try {
      ws.send(JSON.stringify({ type: "stats", bufferedAmount: buffered }));
    } catch { /* ignore */ }
  }, STATS_HEARTBEAT_MS);
}

function b64ToBytes(b64) {
  if (!b64) return null;
  try {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return null;
  }
}

function configureDecoderFromHello(codecHint) {
  if (!codecHint || !codecHint.codec) return;
  // Pre-configure with the codec string from the server. The first AU
  // we receive will still carry SPS+PPS in-band so the decoder can
  // initialise even if this hint is slightly off.
  ensureVideoDecoder(codecHint.codec);
  // Cache the parameter-set NALs so we can prepend them to the first
  // AU we feed to the decoder. Without this, a subscriber that attaches
  // between keyframes (and whose primed cached AU is itself an
  // SPS-less delta slice) would have to wait up to ~2 s for the next
  // IDR before any picture appeared.
  const sc = new Uint8Array([0, 0, 0, 1]);
  const sps = b64ToBytes(codecHint.sps_b64);
  const pps = b64ToBytes(codecHint.pps_b64);
  if (!sps && !pps) {
    videoBootstrapNals = null;
    return;
  }
  const parts = [];
  if (sps) { parts.push(sc); parts.push(sps); }
  if (pps) { parts.push(sc); parts.push(pps); }
  let total = 0;
  for (const p of parts) total += p.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  videoBootstrapNals = out;
}

function connect() {
  if (!currentSerial) {
    setStatus("no device", "warn");
    showEmpty(
      `No browser viewer is running. Open the
       <a href="/mirror-setup" style="color:#a78bfa">mirror setup page</a>,
       pick a device, then click <em>Start browser viewer</em>.`,
    );
    return;
  }
  $("mv-serial").textContent = currentSerial;
  const token = getToken();
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${window.location.host}/ws/mirror`
    + `?serial=${encodeURIComponent(currentSerial)}`
    + `&token=${encodeURIComponent(token)}`;
  setStatus("connecting…");
  ws = new WebSocket(url);
  // ArrayBuffer is faster to inspect (we need to read the first byte
  // of each NAL) than Blob, so prefer it for h264. For PNG mode we
  // still convert to Blob below — Image() loads from blob URLs.
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    setStatus("connected", "ok");
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "hello") {
        const serverMode = msg.mode || "png";
        // Belt-and-braces: if the server reports h264 but we can't
        // decode it (no WebCodecs, or somebody else upgraded the
        // session under us), force the session back to PNG and
        // reconnect rather than dropping every binary frame on the
        // floor and showing a frozen canvas.
        if (serverMode === "h264" && !webCodecsSupported()) {
          setStatus("downgrading…", "warn");
          // Mark the close as intentional so onclose() doesn't also
          // schedule a reconnect — we'll do it ourselves once the
          // server has flipped the session to PNG.
          videoIntentionalClose = true;
          if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
          try { ws.close(); } catch { /* ignore */ }
          ws = null;
          // Force PNG and reconnect once the switch lands.
          (async () => {
            try {
              await fetch("/api/scrcpy/viewer/start", {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  Authorization: `Bearer ${getToken()}`,
                },
                body: JSON.stringify({ serial: currentSerial, mode: "png" }),
              });
            } catch (e) {
              // eslint-disable-next-line no-console
              console.warn("png fallback request failed", e);
            }
            videoMode = "png";
            resetVideoDecoder();
            connect();
          })();
          return;
        }
        videoMode = serverMode;
        // Reset cached bitrate so the pill doesn't show a stale
        // value from a previous session before the first stats reply
        // arrives.
        lastBitrateBps = null;
        lastBitrateChanges = 0;
        if (videoMode === "h264") {
          $("mv-fps").textContent = `h264 · ${msg.fps} fps target`;
          configureDecoderFromHello(msg.codec);
          // Begin reporting WS bufferedAmount so the server's adaptive
          // bit-rate loop has a client-side back-pressure signal.
          // The server now also replies on the same channel with the
          // current --bit-rate so the viewer can show it live.
          startStatsHeartbeat();
          // Re-apply the user's pinned quality (if any) — the server
          // resets to "auto" on every fresh capture session, so we
          // need to push the preference back after each (re)connect.
          const pref = loadQualityPref(currentSerial);
          if (pref && pref !== "auto") {
            applyQualityPin(pref);
          }
          const sel = $("mv-quality");
          if (sel) {
            sel.value = pref || "auto";
            // A previous PNG hello may have disabled the control;
            // make sure it comes back to life on the h264 upgrade.
            sel.disabled = false;
          }
          updatePinnedIndicator();
        } else {
          $("mv-fps").textContent = `target ${msg.fps} fps`;
          stopStatsHeartbeat();
          // Pinning is only meaningful in h264. The dropdown stays
          // visible (so the user can see why nothing happened) but
          // gets disabled until they upgrade.
          const sel = $("mv-quality");
          if (sel) sel.disabled = true;
          updatePinnedIndicator();
        }
        setStatus("live", "ok");
      } else if (msg.type === "stats") {
        // Server-side adaptive bit-rate reply. Update cache + pill,
        // and briefly flash up/down whenever the adapt counter
        // increments so the user can see "ah, the picture changed
        // because the link did".
        const newBr = (typeof msg.bitrate === "number") ? msg.bitrate : null;
        const changes = (typeof msg.bitrate_changes === "number")
          ? msg.bitrate_changes : lastBitrateChanges;
        if (changes > lastBitrateChanges && newBr != null
            && lastBitrateBps != null) {
          flashBitrateChange(newBr >= lastBitrateBps ? "up" : "down");
        }
        lastBitrateChanges = changes;
        lastBitrateBps = newBr;
        renderFpsPill();
      } else if (msg.type === "ended") {
        setStatus("stopped", "warn");
        showEmpty(
          `The browser viewer was stopped on the laptop. Open the
           <a href="/mirror-setup" style="color:#a78bfa">mirror setup page</a>
           and click <em>Start browser viewer</em> to resume.`,
        );
        try { ws.close(); } catch { /* ignore */ }
      } else if (msg.type === "error") {
        setStatus("error", "danger");
        showEmpty(msg.message || "Stream error.");
      } else if (msg.type === "input_error") {
        // Surface failures (offline phone, bad keycode…) without
        // spamming — one toast per second is plenty.
        const now = performance.now();
        if (now - lastInputErrorAt > 1000) {
          lastInputErrorAt = now;
          showToast(`Phone input failed: ${msg.error || "unknown error"}`);
        }
      }
      return;
    }
    if (videoMode === "h264") {
      handleAccessUnit(new Uint8Array(ev.data));
    } else {
      // PNG path expects a Blob. ArrayBuffer -> Blob is cheap.
      drawFrame(new Blob([ev.data], { type: "image/png" }));
    }
  };
  ws.onclose = (ev) => {
    if (ws) ws = null;
    stopStatsHeartbeat();
    // We initiated this close to switch modes — connect() will be
    // called explicitly by the downgrade handler. Don't schedule a
    // duplicate reconnect or report the close as a failure.
    if (videoIntentionalClose) {
      videoIntentionalClose = false;
      return;
    }
    // 4404 / 4429 / 4001 are "don't auto-retry" close codes from the
    // server (no session, too many viewers, auth failed). For anything
    // else, try once more after a short delay so a transient network
    // blip doesn't kick the viewer out permanently.
    if (ev.code === 4404 || ev.code === 4429 || ev.code === 4001
        || ev.code === 4400 || ev.code === 4401 || ev.code === 4029) {
      if (ev.code === 4404) {
        setStatus("not running", "warn");
      } else if (ev.code === 4429) {
        setStatus("too many viewers", "warn");
        showEmpty("Maximum number of browser viewers already connected.");
      } else if (ev.code === 4029) {
        setStatus("rate limited", "warn");
        showEmpty("Too many requests from this address. Wait a moment and refresh.");
      } else {
        setStatus("disconnected", "danger");
      }
      return;
    }
    setStatus("reconnecting…", "warn");
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => {
    setStatus("error", "danger");
  };
}

async function stopServer() {
  if (!currentSerial) return;
  try {
    const res = await fetch("/api/scrcpy/viewer/stop", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail || body.message || detail;
      } catch { /* not json */ }
      showToast(`Stop failed: ${detail}`);
      return;
    }
    showToast("Browser viewer stopped");
  } catch (e) {
    showToast(`Stop failed: ${e.message}`);
  }
}

// ---------- Audio pipeline ----------

function setAudioBtn(state) {
  // state: "off" | "loading" | "on" | "muted" | "error"
  const btn = $("mv-audio-btn");
  if (!btn) return;
  btn.classList.remove("on", "muted", "danger");
  if (state === "on") {
    btn.classList.add("on");
    btn.textContent = "🔊 Audio on";
    btn.title = "Click to stop streaming phone audio to this tab";
  } else if (state === "muted") {
    btn.classList.add("on", "muted");
    btn.textContent = "🔈 Muted (tab inactive)";
    btn.title = "Audio resumes when this tab is in the foreground";
  } else if (state === "loading") {
    btn.textContent = "⏳ Audio…";
    btn.title = "Starting audio capture on the phone";
  } else if (state === "error") {
    btn.classList.add("danger");
    btn.textContent = "🔇 Audio error";
  } else {
    btn.textContent = "🔇 Audio off";
    btn.title = "Hear phone audio in this tab";
  }
}

function ensureAudioCtx() {
  if (audioCtx) return audioCtx;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) {
    showToast("This browser doesn't support Web Audio.");
    return null;
  }
  audioCtx = new Ctx({ sampleRate: 48000 });
  audioGain = audioCtx.createGain();
  audioGain.gain.value = 1.0;
  audioGain.connect(audioCtx.destination);
  return audioCtx;
}

function pcm16ToFloat32(buffer, channels) {
  // buffer: ArrayBuffer of interleaved s16le samples
  const view = new DataView(buffer);
  const total = view.byteLength / 2;
  const perChannel = total / channels;
  const out = [];
  for (let c = 0; c < channels; c += 1) out.push(new Float32Array(perChannel));
  for (let i = 0; i < perChannel; i += 1) {
    for (let c = 0; c < channels; c += 1) {
      const sample = view.getInt16((i * channels + c) * 2, true);
      out[c][i] = sample / 32768;
    }
  }
  return out;
}

function scheduleChunk(buffer) {
  if (!audioCtx || !audioFormat) return;
  const channels = audioFormat.channels || 2;
  const sr = audioFormat.sample_rate || 48000;
  let planes;
  try {
    planes = pcm16ToFloat32(buffer, channels);
  } catch {
    return;
  }
  if (!planes[0] || !planes[0].length) return;
  const ab = audioCtx.createBuffer(channels, planes[0].length, sr);
  for (let c = 0; c < channels; c += 1) ab.copyToChannel(planes[c], c);
  const src = audioCtx.createBufferSource();
  src.buffer = ab;
  src.connect(audioGain);
  // Tiny jitter buffer: keep ~100 ms ahead of "now" to absorb network
  // hiccups, but don't let it drift more than ~500 ms behind real time.
  const now = audioCtx.currentTime;
  const targetAhead = 0.1;
  if (audioNextStart < now + 0.02) audioNextStart = now + targetAhead;
  if (audioNextStart > now + 0.5) audioNextStart = now + targetAhead;
  src.start(audioNextStart);
  audioNextStart += ab.duration;
}

function audioWsUrl(serial, token) {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/mirror-audio`
    + `?serial=${encodeURIComponent(serial)}`
    + `&token=${encodeURIComponent(token)}`;
}

function connectAudioWs() {
  if (!currentSerial) return;
  const ctx = ensureAudioCtx();
  if (!ctx) return;
  // AudioContext starts suspended in many browsers — resume from the
  // user gesture that clicked the audio button.
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  audioNextStart = 0;
  const url = audioWsUrl(currentSerial, getToken());
  audioWs = new WebSocket(url);
  audioWs.binaryType = "arraybuffer";

  audioWs.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "hello") {
        audioFormat = {
          sample_rate: msg.sample_rate || 48000,
          channels: msg.channels || 2,
          bit_depth: msg.bit_depth || 16,
        };
        // Re-mute if the tab is hidden right now.
        applyVisibilityToAudio();
      } else if (msg.type === "ended") {
        showToast("Phone audio capture ended.");
        try { audioWs.close(); } catch { /* ignore */ }
      } else if (msg.type === "error") {
        showToast(msg.message || "Audio stream error.");
      }
      return;
    }
    if (audioEnabled) scheduleChunk(ev.data);
  };
  audioWs.onclose = (ev) => {
    audioWs = null;
    // Don't auto-retry on terminal close codes (auth, rate-limit, no
    // session, too many viewers). Otherwise try again so a brief
    // network blip doesn't drop audio.
    const terminal = (ev.code === 4001 || ev.code === 4029 || ev.code === 4400
                       || ev.code === 4404 || ev.code === 4429);
    if (!audioEnabled || terminal) {
      if (terminal && audioEnabled) {
        setAudioBtn("error");
        audioEnabled = false;
      }
      return;
    }
    if (audioReconnectTimer) clearTimeout(audioReconnectTimer);
    audioReconnectTimer = setTimeout(() => {
      if (audioEnabled) connectAudioWs();
    }, 1500);
  };
  audioWs.onerror = () => { /* close handler will react */ };
}

async function audioStartServer() {
  if (!currentSerial) {
    showToast("No device selected for audio.");
    return false;
  }
  setAudioBtn("loading");
  try {
    const res = await fetch("/api/scrcpy/audio-viewer/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail || body.message || detail;
      } catch { /* not json */ }
      showToast(`Audio start failed: ${detail}`);
      setAudioBtn("off");
      return false;
    }
  } catch (e) {
    showToast(`Audio start failed: ${e.message}`);
    setAudioBtn("off");
    return false;
  }
  return true;
}

async function audioStopServer() {
  if (!currentSerial) return;
  try {
    await fetch("/api/scrcpy/audio-viewer/stop", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial }),
    });
  } catch { /* best effort */ }
}

async function toggleAudio() {
  // Re-entrancy guard: a rapid double-click while a start or stop is
  // still mid-flight would otherwise race (e.g., user clicks "off"
  // before the first start finishes, but the start handler then turns
  // the WS on against the user's wishes). Drop extra clicks.
  if (audioToggleBusy) return;
  audioToggleBusy = true;
  // Snapshot the user's intent at the moment of the click so a
  // late-arriving response can't override a newer toggle.
  const wantOn = !audioEnabled;
  audioEnabled = wantOn;
  $("mv-audio-btn").disabled = true;
  try {
    if (!wantOn) {
      if (audioReconnectTimer) { clearTimeout(audioReconnectTimer); audioReconnectTimer = null; }
      if (audioWs) { try { audioWs.close(); } catch { /* ignore */ } audioWs = null; }
      setAudioBtn("off");
      await audioStopServer();
      return;
    }
    const ok = await audioStartServer();
    // The user may have clicked off again while start was pending.
    if (!audioEnabled) {
      // Roll back — don't open the WS, and stop the server we just started.
      if (ok) { audioStopServer(); }
      setAudioBtn("off");
      return;
    }
    if (!ok) { audioEnabled = false; return; }
    setAudioBtn("on");
    connectAudioWs();
    applyVisibilityToAudio();
  } finally {
    audioToggleBusy = false;
    $("mv-audio-btn").disabled = false;
  }
}

function applyVisibilityToAudio() {
  if (!audioEnabled || !audioGain) return;
  if (document.hidden) {
    audioPreMuteGain = audioGain.gain.value || 1.0;
    audioGain.gain.value = 0;
    setAudioBtn("muted");
  } else {
    audioGain.gain.value = audioPreMuteGain || 1.0;
    setAudioBtn("on");
  }
}

// ---------- Recording (live mirror -> MP4 on the laptop) ----------
//
// The server tees the H.264 access units we're already receiving into a
// raw .h264 file (and the audio PCM into a parallel .pcm file when the
// audio capture is on), then post-muxes everything with ffmpeg into a
// single .mp4 when we click stop. This widget just drives that
// endpoint and polls a quick status while it's running so the user can
// watch the file size grow.
let recording = false;            // we believe a session is active
let recordToggleBusy = false;     // re-entrancy guard, mirrors the audio toggle pattern
let recordStatusTimer = null;     // periodic /scrcpy/recorder/status poll
let recordWarnedAt80 = false;     // one-shot 80%-of-cap warning flag
let recordLastExtendBase = 0;     // remember initial cap so "Keep recording" extends by the same amount

function fmtBytes(n) {
  if (!n || n < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (!sec || sec < 0) return "0:00";
  const s = Math.floor(sec);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

function setRecordBtn(state, label) {
  const btn = $("mv-record-btn");
  if (!btn) return;
  btn.classList.remove("on", "danger");
  if (state === "on") {
    btn.classList.add("on", "danger");
    btn.textContent = label || "● REC";
    btn.title = "Click to stop recording and save the MP4";
  } else if (state === "loading") {
    btn.textContent = "⏳ Saving…";
    btn.title = "Finalising the MP4 with ffmpeg";
  } else {
    btn.textContent = "● Record";
    btn.title = "Save the live mirror to an MP4 file on the laptop";
  }
}

async function pollRecordStatus() {
  if (!recording || !currentSerial) return;
  try {
    const r = await fetch(
      `/api/scrcpy/recorder/status?serial=${encodeURIComponent(currentSerial)}`,
      { headers: { Authorization: `Bearer ${getToken()}` } },
    );
    if (!r.ok) return;
    const data = await r.json();
    const rec = data.recording;
    if (!rec || rec.finalized) {
      // Server-side completion (e.g. capture session ended under us
      // or the auto-stop watchdog fired). Show a toast in the
      // auto-stop case so the user knows where the MP4 went.
      const finalizedInfo = rec;
      recording = false;
      if (recordStatusTimer) { clearInterval(recordStatusTimer); recordStatusTimer = null; }
      setRecordBtn("off");
      $("mv-record-extend").classList.add("hidden");
      $("mv-record-limit").disabled = false;
      if (finalizedInfo && finalizedInfo.auto_stopped && finalizedInfo.mp4_path) {
        showToast(
          `Auto-stopped at ${fmtDuration(finalizedInfo.duration_sec)} — saved to ${finalizedInfo.mp4_path}`,
        );
      }
      return;
    }
    const audio = rec.has_audio ? " + 🔊" : "";
    let label = `● REC ${fmtDuration(rec.duration_sec)} · ${fmtBytes(rec.bytes_total)}${audio}`;
    // Append "(stops in 0:42)" once we cross the 80%-of-cap mark so
    // the user notices the time-pressure without needing the toast.
    if (rec.max_seconds && rec.seconds_remaining != null) {
      const used = rec.duration_sec / rec.max_seconds;
      if (used >= 0.8) {
        label += ` · stops in ${fmtDuration(rec.seconds_remaining)}`;
        const extBtn = $("mv-record-extend");
        extBtn.classList.remove("hidden");
        if (!recordWarnedAt80) {
          recordWarnedAt80 = true;
          showToast(
            `Recording will auto-stop in ${fmtDuration(rec.seconds_remaining)} — click "Keep recording" to extend.`,
          );
        }
      }
    }
    setRecordBtn("on", label);
  } catch { /* best effort */ }
}

async function extendRecording() {
  if (!currentSerial || !recording) return;
  // Extend by the same length the user originally chose — that's the
  // closest to "do another session of what I asked for".
  const extra = recordLastExtendBase || 600;
  try {
    const r = await fetch("/api/scrcpy/recorder/extend", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial, extra_seconds: extra }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({ detail: r.statusText }));
      showToast(`Extend: ${detail.detail || `HTTP ${r.status}`}`);
      return;
    }
    recordWarnedAt80 = false;
    $("mv-record-extend").classList.add("hidden");
    showToast(`Recording extended by ${fmtDuration(extra)}.`);
  } catch (e) {
    showToast(`Extend error: ${e.message}`);
  }
}

async function toggleRecord() {
  if (recordToggleBusy) return;
  if (!currentSerial) { showToast("No device selected"); return; }
  recordToggleBusy = true;
  const wantOn = !recording;
  const btn = $("mv-record-btn");
  btn.disabled = true;
  try {
    if (!wantOn) {
      setRecordBtn("loading");
      if (recordStatusTimer) { clearInterval(recordStatusTimer); recordStatusTimer = null; }
      const r = await fetch("/api/scrcpy/recorder/stop", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify({ serial: currentSerial }),
      });
      const data = await r.json().catch(() => ({}));
      recording = false;
      recordWarnedAt80 = false;
      $("mv-record-extend").classList.add("hidden");
      $("mv-record-limit").disabled = false;
      setRecordBtn("off");
      const info = data.recording;
      if (info && info.finalized) {
        showToast(
          `Saved ${fmtDuration(info.duration_sec)} (${fmtBytes(info.mp4_size)}) → ${info.mp4_path}`,
        );
      } else if (info && info.last_error) {
        showToast(`Save failed: ${info.last_error}`);
      }
      return;
    }
    if (videoMode !== "h264") {
      showToast("Recording needs the H.264 mirror — open this page in Chrome/Edge/Safari.");
      return;
    }
    const limitSel = $("mv-record-limit");
    const maxSeconds = Number.parseInt(limitSel.value, 10) || 0;
    const r = await fetch("/api/scrcpy/recorder/start", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ serial: currentSerial, max_seconds: maxSeconds }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({ detail: r.statusText }));
      showToast(`Recording: ${detail.detail || `HTTP ${r.status}`}`);
      return;
    }
    recording = true;
    recordWarnedAt80 = false;
    recordLastExtendBase = maxSeconds;
    limitSel.disabled = true;
    $("mv-record-extend").classList.add("hidden");
    setRecordBtn("on", "● REC 0:00 · 0 B");
    if (recordStatusTimer) clearInterval(recordStatusTimer);
    recordStatusTimer = setInterval(pollRecordStatus, 1000);
  } finally {
    recordToggleBusy = false;
    btn.disabled = false;
  }
}

// ---------- Input forwarding (tap / swipe / scroll / keyboard) ----------
//
// We send phone-pixel coordinates upstream. The canvas's intrinsic size
// (canvas.width/height) already matches the phone's screencap resolution
// because drawFrame() syncs it to img.naturalWidth/Height each frame, so
// scaling a CSS-space pointer event back to phone pixels is just a ratio
// of (canvas.width / cssWidth).
const TAP_MAX_MOVE_PX = 8;        // pointer travel under this = tap, not drag
const TAP_MAX_DURATION_MS = 250;  // ditto for time
const SWIPE_MIN_DURATION_MS = 60; // floor for ADB `input swipe`
const WHEEL_SWIPE_DURATION_MS = 80;
const WHEEL_SWIPE_FACTOR = 1.4;   // pixels of phone-swipe per pixel of wheel delta

let inputEnabled = true;
let activePointer = null;         // { id, startX, startY, startT, lastX, lastY, moved }
let activeTouches = new Map();    // touchId -> { x, y, ... } for multi-touch
let pinchStartDist = 0;

function sendInput(payload) {
  if (!inputEnabled) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try { ws.send(JSON.stringify(payload)); } catch { /* ignore */ }
}

function canvasPhoneCoords(clientX, clientY) {
  const canvas = $("mv-canvas");
  if (!canvas.width || !canvas.height) return null;
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const cx = clientX - rect.left;
  const cy = clientY - rect.top;
  if (cx < 0 || cy < 0 || cx > rect.width || cy > rect.height) return null;
  const x = Math.round(cx * canvas.width / rect.width);
  const y = Math.round(cy * canvas.height / rect.height);
  return {
    x: Math.max(0, Math.min(canvas.width - 1, x)),
    y: Math.max(0, Math.min(canvas.height - 1, y)),
  };
}

function handlePointerDown(e) {
  if (!inputEnabled) return;
  if (e.pointerType === "mouse" && e.button !== 0) return;
  const coords = canvasPhoneCoords(e.clientX, e.clientY);
  if (!coords) return;
  // For touch we additionally track per-touch positions for pinch detection.
  if (e.pointerType === "touch") {
    activeTouches.set(e.pointerId, { ...coords, clientX: e.clientX, clientY: e.clientY });
    if (activeTouches.size === 2) {
      const pts = [...activeTouches.values()];
      pinchStartDist = Math.hypot(
        pts[0].clientX - pts[1].clientX,
        pts[0].clientY - pts[1].clientY,
      );
      // A second finger landed — abandon any single-finger gesture so we
      // don't accidentally also send a tap when the pinch lifts.
      activePointer = null;
      e.preventDefault();
      return;
    }
  }
  activePointer = {
    id: e.pointerId,
    pointerType: e.pointerType,
    startX: coords.x,
    startY: coords.y,
    startT: performance.now(),
    lastX: coords.x,
    lastY: coords.y,
    moved: false,
  };
  try { e.target.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  e.preventDefault();
}

function handlePointerMove(e) {
  if (e.pointerType === "touch" && activeTouches.has(e.pointerId)) {
    const coords = canvasPhoneCoords(e.clientX, e.clientY);
    if (coords) {
      activeTouches.set(e.pointerId, { ...coords, clientX: e.clientX, clientY: e.clientY });
    }
    if (activeTouches.size === 2) {
      const pts = [...activeTouches.values()];
      const dist = Math.hypot(
        pts[0].clientX - pts[1].clientX,
        pts[0].clientY - pts[1].clientY,
      );
      if (pinchStartDist > 0 && Math.abs(dist - pinchStartDist) > 40) {
        // Translate pinch into a synthetic two-step swipe along the line
        // between the two fingers — enough to trigger zoom-capable apps
        // since `adb input swipe` doesn't speak true multi-touch.
        const p1 = pts[0], p2 = pts[1];
        const cx = (p1.x + p2.x) / 2;
        const cy = (p1.y + p2.y) / 2;
        const grow = dist > pinchStartDist;
        const dx = (p2.x - p1.x) / 2;
        const dy = (p2.y - p1.y) / 2;
        if (grow) {
          sendInput({ type: "swipe", x1: Math.round(cx), y1: Math.round(cy),
                      x2: Math.round(cx + dx), y2: Math.round(cy + dy),
                      duration: 150 });
        } else {
          sendInput({ type: "swipe", x1: Math.round(cx + dx), y1: Math.round(cy + dy),
                      x2: Math.round(cx), y2: Math.round(cy), duration: 150 });
        }
        pinchStartDist = dist;
      }
      e.preventDefault();
      return;
    }
  }
  if (!activePointer || e.pointerId !== activePointer.id) return;
  const coords = canvasPhoneCoords(e.clientX, e.clientY);
  if (!coords) return;
  activePointer.lastX = coords.x;
  activePointer.lastY = coords.y;
  const dx = coords.x - activePointer.startX;
  const dy = coords.y - activePointer.startY;
  if (Math.hypot(dx, dy) > TAP_MAX_MOVE_PX) activePointer.moved = true;
  e.preventDefault();
}

function handlePointerUp(e) {
  if (e.pointerType === "touch") {
    activeTouches.delete(e.pointerId);
    if (activeTouches.size < 2) pinchStartDist = 0;
  }
  if (!activePointer || e.pointerId !== activePointer.id) return;
  const dt = performance.now() - activePointer.startT;
  const dx = activePointer.lastX - activePointer.startX;
  const dy = activePointer.lastY - activePointer.startY;
  const dist = Math.hypot(dx, dy);
  if (!activePointer.moved && dist <= TAP_MAX_MOVE_PX && dt <= TAP_MAX_DURATION_MS) {
    sendInput({ type: "tap", x: activePointer.startX, y: activePointer.startY });
  } else {
    sendInput({
      type: "swipe",
      x1: activePointer.startX, y1: activePointer.startY,
      x2: activePointer.lastX, y2: activePointer.lastY,
      duration: Math.max(SWIPE_MIN_DURATION_MS, Math.min(2000, Math.round(dt))),
    });
  }
  activePointer = null;
  e.preventDefault();
}

function handlePointerCancel(e) {
  if (e.pointerType === "touch") {
    activeTouches.delete(e.pointerId);
    if (activeTouches.size < 2) pinchStartDist = 0;
  }
  if (activePointer && e.pointerId === activePointer.id) activePointer = null;
}

let wheelAccumX = 0;
let wheelAccumY = 0;
let wheelTimer = null;
function handleWheel(e) {
  if (!inputEnabled) return;
  const coords = canvasPhoneCoords(e.clientX, e.clientY);
  if (!coords) return;
  e.preventDefault();
  // Convert deltaMode (lines/pages) into pixels first.
  let dx = e.deltaX;
  let dy = e.deltaY;
  if (e.deltaMode === 1) { dx *= 16; dy *= 16; }
  else if (e.deltaMode === 2) { dx *= 100; dy *= 100; }
  wheelAccumX += dx;
  wheelAccumY += dy;
  if (wheelTimer) return;
  wheelTimer = setTimeout(() => {
    const sx = wheelAccumX, sy = wheelAccumY;
    wheelAccumX = 0; wheelAccumY = 0; wheelTimer = null;
    if (Math.abs(sx) < 4 && Math.abs(sy) < 4) return;
    const canvas = $("mv-canvas");
    // Swipe in the *opposite* direction of the wheel (wheel down = page down
    // = content moves up = finger swipes up from the page).
    const ax = Math.round(coords.x);
    const ay = Math.round(coords.y);
    let bx = Math.round(ax - sx * WHEEL_SWIPE_FACTOR);
    let by = Math.round(ay - sy * WHEEL_SWIPE_FACTOR);
    bx = Math.max(0, Math.min(canvas.width - 1, bx));
    by = Math.max(0, Math.min(canvas.height - 1, by));
    sendInput({ type: "swipe", x1: ax, y1: ay, x2: bx, y2: by,
                duration: WHEEL_SWIPE_DURATION_MS });
  }, 60);
}

let typingBuffer = "";
let typingTimer = null;
function flushTyping() {
  if (typingBuffer) {
    sendInput({ type: "text", text: typingBuffer });
    typingBuffer = "";
  }
  typingTimer = null;
}

const KEYS_TO_FORWARD = new Set([
  "Enter", "Backspace", "Tab", "Escape", "Delete",
  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  "Home", "End", "PageUp", "PageDown",
]);

function viewerHasFocus() {
  // The viewer has focus when nothing else is focused, the canvas/stage
  // is, or the overall body is. We deliberately ignore keypresses that
  // arrive while the user is typing into our own login PIN page.
  if ($("mv-app").style.display === "none") return false;
  const ae = document.activeElement;
  if (!ae || ae === document.body) return true;
  if (ae.id === "mv-canvas" || ae.id === "mv-stage") return true;
  // Don't steal keystrokes from buttons / inputs the user is operating.
  return !ae.matches("input, textarea, select, button, [contenteditable='true']");
}

function handleKeyDown(e) {
  if (!inputEnabled || !viewerHasFocus()) return;
  // Ignore modifier-only presses and anything modified — the phone has no
  // analog of Ctrl+C / Cmd+Tab so passing those through would be noise.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "Shift" || e.key === "Control" || e.key === "Alt"
      || e.key === "Meta") return;

  if (KEYS_TO_FORWARD.has(e.key)) {
    if (typingTimer) { clearTimeout(typingTimer); flushTyping(); }
    sendInput({ type: "key", key: e.key });
    e.preventDefault();
    return;
  }
  // Single printable character — coalesce into one `input text` per
  // ~80 ms so a fast typist sends one shell call per word, not per key.
  if (e.key.length === 1) {
    typingBuffer += e.key;
    if (typingTimer) clearTimeout(typingTimer);
    typingTimer = setTimeout(flushTyping, 80);
    e.preventDefault();
  }
}

function bindInput() {
  const stage = $("mv-stage");
  // Make the stage focusable so keyboard events land here once clicked.
  stage.setAttribute("tabindex", "0");
  stage.style.outline = "none";
  stage.style.touchAction = "none";

  stage.addEventListener("pointerdown", (e) => {
    handlePointerDown(e);
    stage.focus();
  });
  stage.addEventListener("pointermove", handlePointerMove);
  stage.addEventListener("pointerup", handlePointerUp);
  stage.addEventListener("pointercancel", handlePointerCancel);
  stage.addEventListener("lostpointercapture", handlePointerCancel);
  stage.addEventListener("wheel", handleWheel, { passive: false });
  // Suppress the browser context menu so right-click drags feel natural.
  stage.addEventListener("contextmenu", (e) => e.preventDefault());

  window.addEventListener("keydown", handleKeyDown);
  window.addEventListener("blur", () => {
    activePointer = null;
    activeTouches.clear();
    pinchStartDist = 0;
    if (typingTimer) { clearTimeout(typingTimer); flushTyping(); }
  });
}

function bindUi() {
  $("mv-stop-btn").addEventListener("click", () => {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) { try { ws.close(); } catch { /* ignore */ } ws = null; }
    stopServer();
    // Stopping the screen viewer also stops the audio stream — they
    // were started together by the user, tear them down together.
    if (audioEnabled) {
      audioEnabled = false;
      if (audioWs) { try { audioWs.close(); } catch { /* ignore */ } audioWs = null; }
      audioStopServer();
      setAudioBtn("off");
    }
  });
  $("mv-fullscreen-btn").addEventListener("click", () => {
    const el = $("mv-stage");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.requestFullscreen) el.requestFullscreen();
  });
  $("mv-audio-btn").addEventListener("click", () => {
    toggleAudio().catch((e) => showToast(`Audio error: ${e.message}`));
  });
  $("mv-record-btn").addEventListener("click", () => {
    toggleRecord().catch((e) => showToast(`Record error: ${e.message}`));
  });
  $("mv-record-extend").addEventListener("click", () => {
    extendRecording().catch((e) => showToast(`Extend error: ${e.message}`));
  });
  const qSel = $("mv-quality");
  if (qSel) {
    // Restore the user's last choice for this serial as soon as we
    // know the serial. The push to the server happens when the WS
    // hello lands (capture might not be running yet at this point).
    qSel.value = loadQualityPref(currentSerial);
    updatePinnedIndicator();
    qSel.addEventListener("change", () => {
      saveQualityPref(currentSerial, qSel.value);
      applyQualityPin(qSel.value);
      updatePinnedIndicator();
    });
  }
  document.addEventListener("visibilitychange", applyVisibilityToAudio);
  const inputBtn = $("mv-input-btn");
  if (inputBtn) {
    const refresh = () => {
      inputBtn.textContent = inputEnabled ? "🖱 Input: on" : "🖱 Input: off";
      inputBtn.classList.toggle("primary", inputEnabled);
    };
    inputBtn.addEventListener("click", () => {
      inputEnabled = !inputEnabled;
      if (!inputEnabled) {
        activePointer = null;
        activeTouches.clear();
        if (typingTimer) { clearTimeout(typingTimer); flushTyping(); }
      }
      refresh();
      showToast(inputEnabled ? "Remote control on" : "Remote control off");
    });
    refresh();
  }
  bindInput();
}

async function startViewer() {
  $("mv-login").classList.add("hidden");
  $("mv-app").style.display = "flex";
  bindUi();
  fpsTimer = setInterval(tickFps, 1000);
  setOnSessionLost(() => {
    if (ws) { try { ws.close(); } catch { /* ignore */ } ws = null; }
    if (audioWs) { try { audioWs.close(); } catch { /* ignore */ } audioWs = null; }
    audioEnabled = false;
    setStatus("session lost", "danger");
    setTimeout(() => window.location.reload(), 200);
  });
  currentSerial = await pickSerial();
  // Negotiate the capture mode deterministically. WebCodecs-capable
  // browsers upgrade to H.264 (smoother, lower bandwidth); older
  // browsers force the session back to PNG so they don't end up
  // staring at undecodable binary frames sent by a session another
  // (modern) viewer already upgraded.
  if (currentSerial) {
    const wantMode = (await probeH264Support()) ? "h264" : "png";
    try {
      const res = await fetch("/api/scrcpy/viewer/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${getToken()}`,
        },
        body: JSON.stringify({ serial: currentSerial, mode: wantMode }),
      });
      if (!res.ok) {
        // eslint-disable-next-line no-console
        console.warn(`viewer start (${wantMode}) failed: HTTP ${res.status}`);
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn(`viewer start (${wantMode}) error`, e);
    }
  }
  connect();
}

(async function main() {
  if (await checkAuth()) {
    startViewer();
  } else {
    initLogin(() => startViewer());
  }
})();
