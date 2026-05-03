import { getToken } from "./auth.js";
import { showToast } from "./utils.js";
import { apiUrl } from "./target.js";

export function initClipboard() {
  const fetchBtn = document.getElementById("clip-fetch-btn");
  const copyBtn = document.getElementById("clip-copy-btn");
  const pushBtn = document.getElementById("clip-push-btn");
  const textEl = document.getElementById("clip-text");
  const inputEl = document.getElementById("clip-input");

  if (!fetchBtn) return;

  fetchBtn.addEventListener("click", async () => {
    const token = getToken();
    try {
      const res = await fetch(apiUrl("/api/clipboard", "control"), {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) { showToast("Failed to read clipboard", "error"); return; }
      const { text } = await res.json();
      textEl.textContent = text || "(empty)";
      showToast("Clipboard fetched", "success");
    } catch {
      showToast("Connection error", "error");
    }
  });

  copyBtn.addEventListener("click", async () => {
    const text = textEl.textContent;
    if (!text || text === "(empty)") return;
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied to phone clipboard", "success");
    } catch {
      showToast("Copy failed — try selecting text manually", "error");
    }
  });

  pushBtn.addEventListener("click", async () => {
    const text = inputEl.value.trim();
    if (!text) { showToast("Nothing to push", "error"); return; }
    const token = getToken();
    try {
      const res = await fetch(apiUrl("/api/clipboard", "control"), {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ text }),
      });
      if (res.ok) {
        showToast("Pushed to laptop clipboard", "success");
        inputEl.value = "";
      } else {
        showToast("Push failed", "error");
      }
    } catch {
      showToast("Connection error", "error");
    }
  });
}
