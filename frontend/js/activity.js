import { getToken } from "./auth.js";

let timer = null;
let currentCat = "all";

const CATEGORY_ICONS = {
  auth: "🔑",
  files: "📁",
  clipboard: "📋",
  control: "🖱️",
  screen: "🖥️",
  sms: "💬",
  system: "⚙️",
};

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function outcomeClass(outcome) {
  if (outcome === "ok") return "ok";
  if (outcome === "locked" || outcome === "blocked") return "danger";
  if (outcome === "bad_pin") return "warn";
  if (outcome.startsWith("err_")) return "danger";
  return "muted";
}

function detailText(ev) {
  const d = ev.detail || {};
  if (ev.category === "auth" && ev.action === "login") {
    if (ev.outcome === "bad_pin") return `wrong PIN · ${d.attempts_left ?? "?"} attempts left`;
    if (ev.outcome === "locked") return `IP locked for ${Math.ceil((d.retry_after_s || 0) / 60)} min`;
    return "";
  }
  if (ev.category === "auth" && ev.action === "lockout") return "IP locked for 15 min";
  if (ev.category === "system" && ev.action === "rate_limit")
    return `rate limit hit on ${d.bucket || "?"}`;
  return d.path || "";
}

function render(events) {
  const list = document.getElementById("activity-list");
  if (!list) return;
  if (!events.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">📋</div>No activity yet</div>`;
    return;
  }
  list.innerHTML = events.map((ev) => `
    <div class="activity-item">
      <div class="activity-icon">${CATEGORY_ICONS[ev.category] || "•"}</div>
      <div class="activity-body">
        <div class="activity-title">
          <span class="activity-action">${ev.category} · ${ev.action}</span>
          <span class="activity-outcome ${outcomeClass(ev.outcome)}">${ev.outcome}</span>
        </div>
        <div class="activity-meta">
          <span>${fmtTime(ev.ts)}</span>
          <span>${ev.ip}</span>
          ${detailText(ev) ? `<span class="activity-detail">${detailText(ev)}</span>` : ""}
        </div>
      </div>
    </div>
  `).join("");
}

async function refresh() {
  const token = getToken();
  if (!token) return;
  const status = document.getElementById("activity-status");
  try {
    const url = `/api/activity?limit=50${currentCat !== "all" ? `&category=${currentCat}` : ""}`;
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) {
      if (status) status.textContent = "error";
      return;
    }
    const data = await res.json();
    render(data.events || []);
    if (status) status.textContent = `${(data.events || []).length} events`;
  } catch {
    if (status) status.textContent = "offline";
  }
}

export function startActivity() {
  refresh();
  if (timer) clearInterval(timer);
  timer = setInterval(refresh, 5000);
}

export function stopActivity() {
  if (timer) { clearInterval(timer); timer = null; }
}

export function initActivity() {
  document.querySelectorAll("#activity-filters .chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#activity-filters .chip").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentCat = btn.dataset.cat;
      refresh();
    });
  });
}
