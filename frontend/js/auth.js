const TOKEN_KEY = "pb_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function saveToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// Wrap fetch so any 401 from the server automatically logs the user out
// (e.g. session timed out or was invalidated). The original fetch is
// preserved so we can keep using it inside this module.
const _origFetch = window.fetch.bind(window);
let _onSessionLost = null;
export function setOnSessionLost(cb) { _onSessionLost = cb; }

window.fetch = async function (...args) {
  const res = await _origFetch(...args);
  if (res.status === 401) {
    const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
    if (!url.includes("/api/auth/login")) {
      clearToken();
      if (_onSessionLost) _onSessionLost();
    }
  }
  return res;
};

export async function checkAuth() {
  const token = getToken();
  if (!token) return false;
  try {
    const res = await fetch("/api/auth/check", {
      headers: { Authorization: `Bearer ${token}` },
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function initLogin(onSuccess) {
  let pin = "";
  const maxLen = 6;

  const dots = Array.from({ length: 6 }, (_, i) => document.getElementById(`d${i}`));
  const errorEl = document.getElementById("login-error");

  function updateDots() {
    dots.forEach((d, i) => d.classList.toggle("filled", i < pin.length));
  }

  function setError(msg) {
    errorEl.textContent = msg;
  }

  async function submit() {
    if (pin.length < 4) {
      setError("PIN must be at least 4 digits");
      return;
    }
    setError("");
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      if (res.ok) {
        const { token } = await res.json();
        saveToken(token);
        pin = "";
        updateDots();
        onSuccess();
      } else {
        setError("Incorrect PIN. Try again.");
        pin = "";
        updateDots();
        shakeDots();
      }
    } catch {
      setError("Connection error.");
    }
  }

  function shakeDots() {
    const display = document.getElementById("pin-display");
    display.style.animation = "none";
    display.offsetHeight;
    display.style.animation = "shake 0.3s ease";
  }

  document.querySelectorAll(".pin-key[data-digit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (pin.length < maxLen) {
        pin += btn.dataset.digit;
        updateDots();
        if (pin.length === maxLen) submit();
      }
    });
  });

  document.getElementById("pin-clear").addEventListener("click", () => {
    pin = pin.slice(0, -1);
    updateDots();
    setError("");
  });

  document.getElementById("pin-submit").addEventListener("click", submit);
}

let _bridgeQrRendered = false;
export function renderBridgeQR() {
  if (_bridgeQrRendered) return;
  try {
    const url = window.location.origin + "/";
    const urlEl = document.getElementById("bridge-qr-url");
    const canvas = document.getElementById("bridge-qr-canvas");
    const copyBtn = document.getElementById("bridge-qr-copy");
    if (urlEl) urlEl.textContent = url;
    if (canvas && typeof window.qrcode === "function") {
      const qr = window.qrcode(0, "M");
      qr.addData(url);
      qr.make();
      const count = qr.getModuleCount();
      const size = canvas.width;
      const cell = size / count;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#0d0d17";
      ctx.fillRect(0, 0, size, size);
      ctx.fillStyle = "#ffffff";
      for (let r = 0; r < count; r++) {
        for (let c = 0; c < count; c++) {
          if (qr.isDark(r, c)) {
            ctx.fillRect(c * cell, r * cell, cell + 0.5, cell + 0.5);
          }
        }
      }
      _bridgeQrRendered = true;
    }
    if (copyBtn && !copyBtn.dataset.bound) {
      copyBtn.dataset.bound = "1";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(url);
          copyBtn.textContent = "Copied!";
          setTimeout(() => (copyBtn.textContent = "Copy link"), 1500);
        } catch {
          copyBtn.textContent = "Select & copy manually";
        }
      });
    }
  } catch (e) {
    console.warn("QR render failed", e);
  }
}
