import { getToken } from "./auth.js";
import { showToast } from "./utils.js";
import { apiUrl, currentTargetOnline } from "./target.js";

let interval = null;
let pairInited = false;
let pairIp = null;

function fmtBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1048576) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1073741824) return `${(b / 1048576).toFixed(1)} MB`;
  return `${(b / 1073741824).toFixed(2)} GB`;
}

function barClass(pct) {
  if (pct > 85) return "danger";
  if (pct > 65) return "warn";
  return "";
}

function setBar(id, pct) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.width = `${Math.min(100, pct)}%`;
  el.className = `stat-bar-fill ${barClass(pct)}`;
}

async function fetchStats() {
  // Skip when the selected peer is offline — the proxy would 503
  // every 3 s, spamming logs and the toast surface. The picker's
  // offline-toast tells the user to switch machines; until they do,
  // pause the polling for this tab.
  if (!currentTargetOnline("stats")) return;
  const token = getToken();
  try {
    const res = await fetch(apiUrl("/api/stats", "stats"), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return;
    const d = await res.json();

    const cpuPct = d.cpu.percent;
    document.getElementById("cpu-val").textContent = `${cpuPct.toFixed(0)}%`;
    document.getElementById("cpu-sub").textContent = `${d.cpu.count} cores`;
    setBar("cpu-bar", cpuPct);

    const ramPct = d.ram.percent;
    document.getElementById("ram-val").textContent = `${ramPct.toFixed(0)}%`;
    document.getElementById("ram-sub").textContent = `${fmtBytes(d.ram.used)} / ${fmtBytes(d.ram.total)}`;
    setBar("ram-bar", ramPct);

    const diskPct = d.disk.percent;
    document.getElementById("disk-val").textContent = `${diskPct.toFixed(0)}%`;
    document.getElementById("disk-sub").textContent = `${fmtBytes(d.disk.used)} / ${fmtBytes(d.disk.total)}`;
    setBar("disk-bar", diskPct);

    if (d.battery) {
      const batPct = d.battery.percent;
      document.getElementById("bat-val").textContent = `${batPct}%`;
      const status = d.battery.charging ? "Charging" : (d.battery.time_left ? `${Math.floor(d.battery.time_left / 60)}m left` : "Discharging");
      document.getElementById("bat-sub").textContent = status;
      setBar("bat-bar", batPct);
    } else {
      document.getElementById("bat-val").textContent = "N/A";
      document.getElementById("bat-sub").textContent = "No battery";
    }

    document.getElementById("net-sent").textContent = fmtBytes(d.network.bytes_sent);
    document.getElementById("net-recv").textContent = fmtBytes(d.network.bytes_recv);
  } catch {
    // ignore
  }
}

async function fetchPairInfo() {
  const token = getToken();
  const ipEl = document.getElementById("pair-ip");
  const hintEl = document.getElementById("pair-hint");
  const copyBtn = document.getElementById("pair-copy-btn");
  const qrToggle = document.getElementById("pair-qr-toggle");
  if (!ipEl) return;

  try {
    const res = await fetch("/api/stats/pair-info", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (!d.ip) {
      ipEl.textContent = "Unavailable";
      hintEl.textContent = "Couldn't detect this phone's IP address.";
      return;
    }
    pairIp = d.ip;
    ipEl.textContent = d.ip;
    copyBtn.disabled = false;
    if (qrToggle) qrToggle.style.display = "";
    if (d.looks_local) {
      hintEl.textContent = "Open the laptop's /mirror-setup page, then paste this IP into the manual pairing fields.";
    } else {
      hintEl.textContent = "This doesn't look like a private Wi-Fi address — make sure the phone and laptop are on the same network.";
    }
  } catch {
    ipEl.textContent = "Unavailable";
    hintEl.textContent = "Couldn't reach the bridge to detect the phone's IP.";
  }
}

async function copyToClipboard(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

function renderPairQr() {
  const canvas = document.getElementById("pair-qr-canvas");
  if (!canvas || !pairIp || typeof QRCode === "undefined") return;
  const port = (document.getElementById("pair-qr-port")?.value || "").trim();
  const code = (document.getElementById("pair-qr-code")?.value || "").trim();
  // Encode as a self-describing payload that the laptop can parse.
  // Falls back to just the IP when port/code aren't supplied yet.
  let payload;
  if (port && code) {
    payload = `phonebridge-pair:host=${pairIp};port=${port};code=${code}`;
  } else if (port) {
    payload = `phonebridge-pair:host=${pairIp};port=${port}`;
  } else {
    payload = `phonebridge-pair:host=${pairIp}`;
  }
  QRCode.toCanvas(canvas, payload, { width: 220, margin: 1 }, () => { /* ignore */ });
}

function initPairCard() {
  if (pairInited) return;
  pairInited = true;
  const copyBtn = document.getElementById("pair-copy-btn");
  const qrToggle = document.getElementById("pair-qr-toggle");
  const qrArea = document.getElementById("pair-qr-area");
  const portEl = document.getElementById("pair-qr-port");
  const codeEl = document.getElementById("pair-qr-code");

  copyBtn?.addEventListener("click", async () => {
    if (!pairIp) return;
    const ok = await copyToClipboard(pairIp);
    showToast(ok ? `Copied ${pairIp}` : "Copy failed — long-press to copy", ok ? "" : "error");
  });

  qrToggle?.addEventListener("click", () => {
    if (!qrArea) return;
    const showing = qrArea.style.display !== "none";
    qrArea.style.display = showing ? "none" : "";
    qrToggle.textContent = showing ? "Show pairing QR" : "Hide pairing QR";
    if (!showing) renderPairQr();
  });

  portEl?.addEventListener("input", renderPairQr);
  codeEl?.addEventListener("input", renderPairQr);

  fetchPairInfo();
}

/** One-shot stats refresh used by the picker so retargeting feels
 * instant instead of waiting for the next 3-second tick. Safe to
 * call before ``startStats()`` (it just no-ops if the target is
 * offline). */
export function refreshStatsNow() {
  fetchStats();
}

export function startStats() {
  if (interval) return;
  initPairCard();
  fetchStats();
  interval = setInterval(fetchStats, 3000);
}

export function stopStats() {
  if (interval) {
    clearInterval(interval);
    interval = null;
  }
}
