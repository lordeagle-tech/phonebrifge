import { checkAuth, initLogin, clearToken, setOnSessionLost, renderBridgeQR } from "./auth.js";
import { startStats, stopStats, refreshStatsNow } from "./stats.js";
import { initFiles, loadFiles } from "./files.js";
import { initTerminal, fitTerminal } from "./terminal.js";
import { initScreen, stopScreen } from "./screen.js";
import { initControl } from "./control.js";
import { initClipboard } from "./clipboard.js";
import { initActivity, startActivity, stopActivity } from "./activity.js";
import { initCall, teardownCall } from "./call.js";
import { initNotifications, teardownNotifications } from "./notifications.js";
import { initSms, teardownSms } from "./sms.js";
import { initRing, teardownRing } from "./ring.js";
import { initPhotos, teardownPhotos } from "./photos.js";
import { showToast } from "./utils.js";
import {
  initTargetPicker, teardownTargetPicker, setActiveTab,
  subscribe as subscribeTarget, onTargetOffline, onTargetStale,
  onTargetCapabilityLost, onTargetCapabilityRegained, suppressRegain,
  setTargetFor, targetFor,
} from "./target.js";
import { retargetScreen } from "./screen.js";

export { showToast };

// Map page id -> tab key the target.js picker uses. Pages omitted
// from this map don't show the picker (they're not peer-targetable).
// "control" covers the entire Control page (touchpad, keyboard,
// clipboard) so retargeting once swaps every interaction over.
const PAGE_TO_TAB = {
  "home-page": "stats",
  "files-page": "files",
  "terminal-page": "terminal",
  "screen-page": "screen",
  "control-page": "control",
};

let currentPage = null;
let terminalInited = false;
let filesInited = false;
let screenInited = false;
let controlInited = false;
let activityInited = false;

function showPage(pageId) {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
  document.getElementById(pageId).classList.add("active");
  document.querySelector(`.nav-btn[data-page="${pageId}"]`)?.classList.add("active");

  const prev = currentPage;
  currentPage = pageId;

  // Tell the picker which tab is active so the pill + sheet show
  // the right target. Pages not in PAGE_TO_TAB hide the pill.
  setActiveTab(PAGE_TO_TAB[pageId] || null);

  if (prev === "screen-page" && pageId !== "screen-page") {
    stopScreen();
  }

  if (prev === "activity-page" && pageId !== "activity-page") {
    stopActivity();
  }

  if (pageId === "home-page") {
    startStats();
    renderBridgeQR();
  } else {
    stopStats();
  }

  if (pageId === "files-page" && !filesInited) {
    filesInited = true;
    initFiles();
    loadFiles(null);
  }

  if (pageId === "terminal-page") {
    if (!terminalInited) {
      terminalInited = true;
      initTerminal();
    }
    setTimeout(() => fitTerminal(), 50);
  }

  if (pageId === "screen-page" && !screenInited) {
    screenInited = true;
    initScreen();
  }

  if (pageId === "control-page" && !controlInited) {
    controlInited = true;
    initControl();
    initClipboard();
  }

  if (pageId === "activity-page") {
    if (!activityInited) {
      activityInited = true;
      initActivity();
    }
    startActivity();
  }
}

function showApp() {
  document.getElementById("login-page").classList.remove("active");
  document.getElementById("nav").style.display = "flex";
  showPage("home-page");
  initCall();
  initNotifications();
  initSms();
  initRing();
  initPhotos();
  initTargetPicker();

  // When the user retargets the active tab, re-run its loader so the
  // new machine's data shows up immediately rather than on the next
  // poll/interaction.
  subscribeTarget("files",   () => { if (filesInited) loadFiles(null); });
  subscribeTarget("stats",   () => { refreshStatsNow(); });
  subscribeTarget("control", () => {
    // The Control page hosts touchpad+keyboard+clipboard. The
    // input WS reconnects via the page's own Reconnect button;
    // clipboard fetches retarget naturally on next click.
    document.getElementById("ctrl-reconnect-btn")?.click();
  });
  subscribeTarget("terminal",  () => {
    if (terminalInited) {
      // The terminal module's reconnect button does the right thing
      // (closes old WS, opens new one against the now-current target).
      document.getElementById("term-reconnect-btn")?.click();
    }
  });
  subscribeTarget("screen", () => {
    // Tear down the active screen + input streams and re-open
    // against the new target; if nothing is running, the next
    // Start handles it. ``retargetScreen`` switches between the
    // main bridge's WS pipeline and a peer's MJPEG <img> stream.
    retargetScreen();
  });

  // When the SELECTED peer for a tab transitions to offline, warn the
  // user once and let them open the picker to switch. Polling tabs
  // (Stats) check ``currentTargetOnline`` themselves and pause silently
  // until the user picks a new machine.
  onTargetOffline((tabKey, hostname) => {
    showToast(`${hostname} went offline — tap the menu to switch machine`,
              "error");
  });

  // When the SELECTED peer reconnects with a smaller capability set
  // (e.g. an optional module was uninstalled on that laptop), target.js
  // auto-falls the affected tab back to the main bridge and fires this
  // so we can tell the user why their tab just jumped.
  onTargetCapabilityLost(({ hostname, tabLabel }) => {
    showToast(
      `${hostname} can no longer share ${tabLabel} — switched back to this laptop`,
      "error");
  });

  // Mirror image of the cap-lost toast: when that same peer gains
  // the capability back, offer one tap to retarget the tab to it.
  // We deliberately don't auto-rebind — the user is on the main
  // bridge now and may be in the middle of something; explicit
  // consent is the safer default. Dismissing (letting the toast
  // expire) keeps them on main.
  onTargetCapabilityRegained(({ tabKey, peerId, hostname, tabLabel }) => {
    showToast(
      `${hostname} can share ${tabLabel} again`,
      "success",
      {
        action: {
          label: `Switch back to ${hostname}`,
          onClick: () => {
            setTargetFor(tabKey, {
              kind: "peer", id: peerId, hostname,
            });
            showToast(`${tabLabel} → ${hostname}`, "success");
          },
        },
        // If the user lets this offer expire without tapping it,
        // remember "no thanks for this peer+tab" so the next
        // shrink/regain cycle on the same module restart doesn't
        // re-prompt. Cleared the moment they touch the picker for
        // this tab again.
        //
        // Race guard: if the user *did* explicitly retarget the tab
        // (via picker or elsewhere) before this toast timed out,
        // setTargetFor already cleared their suppression — we
        // mustn't re-add it here. Only suppress if the tab is still
        // sitting on the main bridge, which is the state the offer
        // was made against.
        onDismiss: () => {
          const cur = targetFor(tabKey);
          if (cur.kind === "main") suppressRegain(peerId, tabKey);
        },
      });
  });

  // When a saved peer disappears from /api/peers (unpaired or its id
  // rotated by a re-pair), target.js auto-reconciles localStorage and
  // fires this callback so we can tell the user why their tab just
  // jumped to a different machine.
  onTargetStale(({ hostname, action, newHostname }) => {
    if (action === "rebound") {
      showToast(
        `${hostname} was re-paired — reconnected to ${newHostname}`,
        "info");
    } else {
      showToast(
        `${hostname} is gone — switched back to this laptop`,
        "error");
    }
  });

  try {
    document.getElementById("home-hostname").textContent = location.hostname;
  } catch { /* ignore */ }
}

function showLogin() {
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  document.getElementById("login-page").classList.add("active");
  document.getElementById("nav").style.display = "none";
  stopStats();
  stopScreen();
  stopActivity();
  teardownCall();
  teardownNotifications();
  teardownSms();
  teardownRing();
  teardownPhotos();
  teardownTargetPicker();
}

let loginInited = false;
function ensureLoginInited() {
  if (loginInited) return;
  loginInited = true;
  initLogin(() => { showApp(); });
}

let sessionLostHandled = false;
function handleSessionLost() {
  if (sessionLostHandled) return;
  sessionLostHandled = true;
  showToast("Session expired — please sign in again", "error");
  showLogin();
  ensureLoginInited();
  // allow another session-lost cycle once user re-authenticates
  setTimeout(() => { sessionLostHandled = false; }, 1000);
}

async function boot() {
  // Defer registering the global session-lost handler until after the
  // initial auth check — otherwise a 401 from /api/auth/check during boot
  // would invoke handleSessionLost AND the boot fallback, double-binding
  // the login form.
  const ok = await checkAuth();
  setOnSessionLost(handleSessionLost);
  if (ok) {
    showApp();
  } else {
    clearToken();
    showLogin();
    ensureLoginInited();
  }
}

document.querySelectorAll(".nav-btn[data-page]").forEach((btn) => {
  btn.addEventListener("click", () => showPage(btn.dataset.page));
});

boot();
