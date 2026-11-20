// Phone "machine picker" — per-tab target selection.
//
// Each tab (Files, Terminal, Stats, Clipboard) can independently
// point at the main laptop or any linked peer laptop. The pill at
// the top of the screen shows the active tab's current target and
// opens a sheet to switch.
//
// URL rewriting:
//   apiUrl("/api/files", "files") ->
//     - "/api/files"                            (target = main)
//     - "/api/peers/<id>/files"                 (target = peer)
//   wsPath("/ws/terminal", "terminal") ->
//     - "/ws/terminal"                         (callers append qs)
//     - "/ws/peers/<id>/terminal"
//
// The peer's secret never reaches the phone — the main bridge
// rewrites the request to the peer using the secret it already
// holds in its registry.

import { getToken } from "./auth.js";

const STORAGE_PREFIX = "phone-bridge:target:";
const MAIN = { kind: "main", id: null, hostname: "this laptop" };

// In-memory cache of /api/peers — refreshed every 5 s while the
// picker sheet is open, and once at startup.
let peersCache = [];
let peersFetchedAt = 0;
let peersTimer = null;

const subs = new Map(); // tabKey -> Set<cb>
let activeTab = null;

function storageKey(tabKey) {
  return `${STORAGE_PREFIX}${tabKey}`;
}

export function targetFor(tabKey) {
  if (!tabKey) return MAIN;
  let raw;
  try { raw = localStorage.getItem(storageKey(tabKey)); } catch { raw = null; }
  if (!raw) return MAIN;
  try {
    const v = JSON.parse(raw);
    if (v && v.kind === "peer" && typeof v.id === "string") {
      // Hydrate hostname from cache so the pill shows a friendly name
      // even before the next /api/peers refresh completes.
      const hit = peersCache.find((p) => p.id === v.id);
      return { kind: "peer", id: v.id,
               hostname: hit?.hostname || v.hostname || v.id };
    }
  } catch { /* fall through */ }
  return MAIN;
}

// ``opts.auto`` (default false): set true for system-driven
// retargets (capability-shrink fallback, stale-id reconcile) that
// should NOT count as the user re-engaging with their target
// choice. User-initiated calls (picker click, regain toast action)
// leave it false so any prior "no thanks" memory is wiped and
// future regain prompts can fire normally again.
export function setTargetFor(tabKey, target, opts = {}) {
  const key = storageKey(tabKey);
  try {
    if (!target || target.kind === "main") {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify({
        kind: "peer", id: target.id, hostname: target.hostname,
      }));
    }
  } catch { /* localStorage disabled — accept the silent no-op */ }
  if (!opts.auto) _clearRegainSuppressionForTab(tabKey);
  // Any explicit user choice (chip tap, picker pick, regain-toast
  // "switch back", manual main pick) supersedes a stale auto-fallback
  // hint — drop it so the picker doesn't keep nagging. The shrink
  // branch that auto-falls back also calls through here (with
  // opts.auto=true) and immediately re-sets a fresh fallback record.
  _clearFallback(tabKey);
  notify(tabKey);
}

export function subscribe(tabKey, cb) {
  if (!subs.has(tabKey)) subs.set(tabKey, new Set());
  subs.get(tabKey).add(cb);
  return () => subs.get(tabKey)?.delete(cb);
}

function notify(tabKey) {
  const set = subs.get(tabKey);
  if (!set) return;
  const t = targetFor(tabKey);
  for (const cb of set) {
    try { cb(t); } catch { /* don't let one bad subscriber kill the rest */ }
  }
}

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

export function apiUrl(path, tabKey) {
  const t = targetFor(tabKey);
  if (t.kind !== "peer") return path;
  // path is "/api/<rest>[?qs]" — rewrite to /api/peers/<id>/<rest>
  if (!path.startsWith("/api/")) return path;
  const tail = path.slice("/api/".length);
  return `/api/peers/${t.id}/${tail}`;
}

export function wsPath(path, tabKey) {
  const t = targetFor(tabKey);
  if (t.kind !== "peer") return path;
  if (!path.startsWith("/ws/")) return path;
  const tail = path.slice("/ws/".length);
  return `/ws/peers/${t.id}/${tail}`;
}

// ---------------------------------------------------------------------------
// Peer list polling
// ---------------------------------------------------------------------------

async function refreshPeers() {
  const token = getToken();
  if (!token) return;
  try {
    const res = await fetch("/api/peers", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return;
    const d = await res.json();
    peersCache = Array.isArray(d.peers) ? d.peers : [];
    peersFetchedAt = Date.now();
    detectOfflineTransitions();
    detectCapabilityShrinks();
    reconcileSavedTargets();
    renderActivePill();
    renderSheet();
  } catch { /* network blip — try again on the next tick */ }
}

// Tracks whether the picker *wants* to be polling. We split intent
// from the actual timer so visibility-driven pause/resume can stop
// the interval without forgetting that polling should resume when
// the tab is focused again.
let pollingWanted = false;

function startPolling() {
  if (pollingWanted) return;
  pollingWanted = true;
  // Visibility listener is attached lazily on the first start so we
  // never bind it during module import (when document state may not
  // be settled) and only ever bind it once per page.
  if (!_visibilityHooked) {
    document.addEventListener("visibilitychange", _onVisibilityChange);
    _visibilityHooked = true;
  }
  // If the phone is already asleep / tab hidden when the user signs
  // in, skip the immediate fetch — _onVisibilityChange will pick it
  // up the moment the screen comes back.
  if (document.hidden) return;
  refreshPeers();
  peersTimer = setInterval(refreshPeers, 5000);
}

function stopPolling() {
  pollingWanted = false;
  if (peersTimer) { clearInterval(peersTimer); peersTimer = null; }
}

let _visibilityHooked = false;

function _onVisibilityChange() {
  if (!pollingWanted) return;
  if (document.hidden) {
    // Phone screen off / tab backgrounded — pause the interval to
    // save battery + LAN traffic. The picker is irrelevant while
    // the user can't see it.
    if (peersTimer) { clearInterval(peersTimer); peersTimer = null; }
  } else {
    // Tab is visible again. Refresh immediately so the pill state
    // is current before the user can tap it, then restore the 5 s
    // cadence.
    if (!peersTimer) {
      refreshPeers();
      peersTimer = setInterval(refreshPeers, 5000);
    }
  }
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

function renderActivePill() {
  const pill = document.getElementById("target-pill");
  if (!pill) return;
  const tab = activeTab;
  if (!tab) {
    pill.style.display = "none";
    return;
  }
  // Hide for tabs that aren't peer-targetable (e.g. Activity, Share,
  // Photos) — caller opts a tab in by registering it.
  if (!TARGETABLE_TABS.has(tab)) {
    pill.style.display = "none";
    return;
  }
  const t = targetFor(tab);
  pill.style.display = "";
  const label = pill.querySelector(".target-pill-label");
  const dot = pill.querySelector(".target-pill-dot");
  if (label) label.textContent = t.kind === "peer"
    ? `→ ${t.hostname}`
    : "→ this laptop";
  if (dot) {
    if (t.kind === "main") {
      dot.className = "target-pill-dot online";
    } else {
      const live = peersCache.find((p) => p.id === t.id);
      dot.className = "target-pill-dot " + (live?.online ? "online" : "offline");
    }
  }
}

function renderSheet() {
  const list = document.getElementById("target-sheet-list");
  if (!list) return;
  const tab = activeTab;
  const current = tab ? targetFor(tab) : MAIN;
  list.innerHTML = "";

  const rows = [
    { kind: "main", id: null, hostname: "this laptop", online: true,
      sub: location.hostname },
    ...peersCache.map((p) => ({
      kind: "peer", id: p.id, hostname: p.hostname || p.id,
      online: !!p.online, controllable: !!p.controllable,
      sub: p.local_ip || "no IP",
    })),
  ];

  for (const r of rows) {
    const btn = document.createElement("button");
    btn.className = "target-sheet-row";
    const isCurrent = (r.kind === "main" && current.kind === "main")
      || (r.kind === "peer" && current.kind === "peer" && current.id === r.id);
    if (isCurrent) btn.classList.add("current");
    const offline = r.kind === "peer" && (!r.online || !r.controllable);
    // A peer can be online + controllable but still unable to serve
    // the active tab if its peer-server build predates that feature
    // mount. We surface that as a separate "unsupported" disabled
    // state so the user understands they need to upgrade the peer
    // agent rather than wait for it to come back online.
    const livePeer = r.kind === "peer"
      ? peersCache.find((p) => p.id === r.id)
      : null;
    const tabLabel = TAB_LABEL[tab] || tab;
    const unsupported = !offline && r.kind === "peer" && tab
      && !peerSupportsTab(livePeer, tab);
    if (offline) btn.classList.add("offline");
    if (unsupported) {
      btn.classList.add("offline");
      btn.classList.add("unsupported");
      btn.title = `This laptop's bridge is too old to share ${tabLabel}.`;
    }

    const dot = document.createElement("span");
    dot.className = "target-sheet-dot " + (r.online ? "online" : "offline");
    btn.appendChild(dot);

    const main = document.createElement("div");
    main.className = "target-sheet-main";
    const name = document.createElement("div");
    name.className = "target-sheet-name";
    name.textContent = r.hostname;
    const sub = document.createElement("div");
    sub.className = "target-sheet-sub";
    if (unsupported) {
      sub.textContent = `bridge too old for ${tabLabel}`;
    } else if (offline) {
      sub.textContent = r.online ? "needs newer peer agent" : "offline";
    } else {
      sub.textContent = r.sub;
    }
    main.appendChild(name);
    main.appendChild(sub);
    btn.appendChild(main);

    if (isCurrent) {
      const tick = document.createElement("span");
      tick.className = "target-sheet-tick";
      tick.textContent = "✓";
      btn.appendChild(tick);
    }

    btn.addEventListener("click", () => {
      if (offline || unsupported) return;
      // setTargetFor (default opts.auto=false) re-arms any prior
      // "no thanks" suppression for this tab — the user just made
      // an explicit pick, so a future shrink/regain cycle should
      // be allowed to prompt again.
      setTargetFor(tab, r.kind === "peer"
        ? { kind: "peer", id: r.id, hostname: r.hostname }
        : { kind: "main" });
      renderActivePill();
      renderSheet();
      closeSheet();
    });
    list.appendChild(btn);

    // Right under the "this laptop" row, surface a durable
    // "switch back to <hostname>" chip whenever the active tab
    // was auto-fallen back here and the prior peer is reachable
    // again. The regain toast already offered this once; the chip
    // is the persistent affordance for users who missed it.
    if (r.kind === "main") {
      const sb = pendingSwitchBack(tab);
      if (sb) {
        // Wrap so the dismiss "×" can be a sibling button without
        // nesting (button-in-button is invalid HTML and breaks tap
        // handling on iOS Safari).
        const wrap = document.createElement("div");
        wrap.className = "target-sheet-switchback-wrap";

        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "target-sheet-switchback";
        chip.textContent = `↩ Switch back to ${sb.hostname}`;
        chip.title = `${sb.tabLabel} fell back to this laptop when `
          + `${sb.hostname} couldn't share it. Tap to retarget now `
          + `that it's back.`;
        chip.addEventListener("click", (ev) => {
          ev.stopPropagation();
          setTargetFor(tab, {
            kind: "peer", id: sb.peerId, hostname: sb.hostname,
          });
          renderActivePill();
          renderSheet();
          closeSheet();
        });
        wrap.appendChild(chip);

        // Dismiss "×": clears the fallback record (memory +
        // localStorage) so the chip vanishes for this tab. A future
        // shrink+regain on the same peer will repopulate it via the
        // capability detection path. We deliberately do NOT call
        // suppressRegain here — that's the regain-toast's "I let it
        // expire" semantics, which is a separate user signal.
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "target-sheet-switchback-dismiss";
        dismiss.textContent = "×";
        dismiss.setAttribute("aria-label",
          `Dismiss switch-back to ${sb.hostname}`);
        dismiss.title = "Dismiss this offer";
        dismiss.addEventListener("click", (ev) => {
          ev.stopPropagation();
          _clearFallback(tab);
          renderSheet();
        });
        wrap.appendChild(dismiss);

        list.appendChild(wrap);
      }
    }
  }

  if (peersCache.length === 0) {
    const hint = document.createElement("div");
    hint.className = "target-sheet-empty";
    hint.textContent = "No linked peer laptops yet. Pair one with " +
      "`python run.py peer --main … --pair-code …` from another laptop.";
    list.appendChild(hint);
  }
}

function openSheet() {
  const sheet = document.getElementById("target-sheet");
  if (!sheet) return;
  sheet.classList.add("open");
  refreshPeers();
}

function closeSheet() {
  document.getElementById("target-sheet")?.classList.remove("open");
}

// Tabs that have peer-side support. Other tabs are silently
// excluded from the picker (the pill just hides). The "control" key
// covers the entire control page (touchpad, keyboard, clipboard) so
// retargeting it switches every interaction on that page in one go.
const TARGETABLE_TABS = new Set([
  "files", "stats", "terminal", "screen", "control",
]);

// Maps each targetable tab key to the capability string the peer
// must advertise to serve it. Kept in sync with peer_server.py's
// ``capabilities()``. Tabs absent from this map require no
// capability (always-allowed).
const TAB_CAPABILITY = {
  files: "files",
  stats: "stats",
  terminal: "terminal",
  screen: "screen",
  control: "control",
};

// Human label for tooltips when a peer can't serve a tab. Avoids
// leaking the raw capability key into the UI.
const TAB_LABEL = {
  files: "Files",
  stats: "Stats",
  terminal: "Terminal",
  screen: "Screen",
  control: "Control",
};

/** True if the peer record's advertised capabilities cover the
 * given tab. Unknown/legacy peers (capabilities === null) get the
 * benefit of the doubt so an old agent isn't silently locked out
 * of features it actually does serve — the worst case there is
 * the original "selectable but 404s" behaviour, which we can only
 * fix once the peer upgrades and starts advertising. */
function peerSupportsTab(peer, tabKey) {
  const cap = TAB_CAPABILITY[tabKey];
  if (!cap) return true;
  if (!peer || !Array.isArray(peer.capabilities)) return true;
  return peer.capabilities.includes(cap);
}

// ---------------------------------------------------------------------------
// Offline-of-selected-target detection
//
// When the peer that a tab is currently pointed at transitions to
// offline (or its agent is too old to control), we fire one-shot
// callbacks so the UI can warn the user + pause polling on that tab.
// We only fire on the "online -> offline" edge so the toast doesn't
// repeat every 5 s.
// ---------------------------------------------------------------------------
const offlineCbs = new Set(); // Set<(tabKey, hostname) => void>
const _lastSeenOnline = new Map(); // peerId -> bool

export function onTargetOffline(cb) {
  offlineCbs.add(cb);
  return () => offlineCbs.delete(cb);
}

/** True if the active target for ``tabKey`` is reachable. Main is
 * always considered reachable; peers must be in the cache + online +
 * controllable. Used by polling tabs (Stats) to skip fetches that
 * would just 503. */
export function currentTargetOnline(tabKey) {
  const t = targetFor(tabKey);
  if (t.kind !== "peer") return true;
  const live = peersCache.find((p) => p.id === t.id);
  if (!(live && live.online && live.controllable)) return false;
  // A peer that's "online" but doesn't advertise this tab's
  // capability would 404 on every poll — treat it as unreachable
  // so polling tabs (Stats) don't hammer it pointlessly.
  return peerSupportsTab(live, tabKey);
}

// ---------------------------------------------------------------------------
// Capability-shrink detection
//
// A peer can come back online after the user uninstalled (or its
// build dropped) one of the optional modules — its advertised
// capability list shrinks. The picker already disables newly
// unsupported tabs, but a tab the user had ALREADY pointed at the
// peer would just silently start failing. Mirror the offline
// pattern: on the edge where a peer's caps stop covering a tab the
// user is currently targeting at it, fire a one-shot toast and
// auto-fall back to the main bridge.
// ---------------------------------------------------------------------------
const capLostCbs = new Set(); // Set<({tabKey, hostname, tabLabel}) => void>
const _lastSeenCaps = new Map(); // peerId -> capabilities | null (only set
                                 // after the first refresh that observed
                                 // the peer, so the very first poll never
                                 // looks like a "shrink").

export function onTargetCapabilityLost(cb) {
  capLostCbs.add(cb);
  return () => capLostCbs.delete(cb);
}

// Tabs we force-fell off a peer because that peer dropped the
// required capability. Keyed by peer id so the entry survives the
// peer briefly leaving the cache and returning. The inner Map's
// presence is what arms the regain detector below — once a regain
// fires (or the user manually moves the tab to a different target),
// the entry is dropped so we don't keep nagging.
const _pendingRegain = new Map(); // peerId -> Map<tabKey, {hostname, tabLabel}>

const capRegainCbs = new Set(); // Set<({tabKey, peerId, hostname, tabLabel}) => void>

export function onTargetCapabilityRegained(cb) {
  capRegainCbs.add(cb);
  return () => capRegainCbs.delete(cb);
}

// ---------------------------------------------------------------------------
// Regain-toast suppression
//
// If the user lets a "X can share Y again" toast expire without
// tapping it, we treat that as a deliberate "no, I'm staying on
// main" — re-prompting them every time the peer's module restarts
// would be nagging. Suppression is keyed by (peerId, tabKey),
// persisted to localStorage so it survives reloads, and cleared on
// any explicit picker selection for that tab.
// ---------------------------------------------------------------------------
const SUPPRESS_KEY = "phone-bridge:regain-suppress";

function _readSuppress() {
  let raw;
  try { raw = localStorage.getItem(SUPPRESS_KEY); } catch { return {}; }
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return (v && typeof v === "object") ? v : {};
  } catch { return {}; }
}

function _writeSuppress(obj) {
  try { localStorage.setItem(SUPPRESS_KEY, JSON.stringify(obj)); }
  catch { /* localStorage disabled — silent no-op */ }
}

function _isSuppressed(peerId, tabKey) {
  const s = _readSuppress();
  return Array.isArray(s[peerId]) && s[peerId].includes(tabKey);
}

/** Record "user dismissed the regain offer for this peer+tab" so
 * future regain edges for the same pair stay quiet. Called from
 * the toast's onDismiss in app.js. */
export function suppressRegain(peerId, tabKey) {
  if (!peerId || !tabKey) return;
  const s = _readSuppress();
  const list = Array.isArray(s[peerId]) ? s[peerId] : [];
  if (!list.includes(tabKey)) list.push(tabKey);
  s[peerId] = list;
  _writeSuppress(s);
}

/** Drop the "user said no" memory for this tab across ALL peers.
 * Called from the picker click handler so any explicit retarget of
 * a tab re-arms future regain prompts for that tab. */
function _clearRegainSuppressionForTab(tabKey) {
  const s = _readSuppress();
  let changed = false;
  for (const pid of Object.keys(s)) {
    const list = Array.isArray(s[pid]) ? s[pid] : [];
    const filtered = list.filter((t) => t !== tabKey);
    if (filtered.length !== list.length) {
      changed = true;
      if (filtered.length === 0) delete s[pid];
      else s[pid] = filtered;
    }
  }
  if (changed) _writeSuppress(s);
}

// ---------------------------------------------------------------------------
// Durable "switch back" hint
//
// The regain toast above is a one-shot affordance — easy to miss
// (phone screen off, away from the device, swiped away). To make
// the offer durable, every time we auto-fall a tab back to main we
// also stash a per-tab record of the peer it used to live on. The
// picker sheet reads this via ``pendingSwitchBack()`` and renders a
// small chip under "this laptop" that retargets the tab in one tap.
//
// The record is persisted to localStorage so it survives reload /
// brief logout cycles. It's cleared the moment the user takes any
// explicit action on this tab (picking a target manually, tapping
// the chip, accepting the regain toast — all routed through
// setTargetFor).
// ---------------------------------------------------------------------------
const FALLBACK_STORAGE_PREFIX = "phone-bridge:fallback:";

// tabKey -> {peerId, hostname, tabLabel}
const _recentFallback = new Map();

function _fallbackStorageKey(tabKey) {
  return `${FALLBACK_STORAGE_PREFIX}${tabKey}`;
}

function _persistFallback(tabKey) {
  const key = _fallbackStorageKey(tabKey);
  const v = _recentFallback.get(tabKey);
  try {
    if (!v) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(v));
  } catch { /* localStorage disabled — accept silent no-op */ }
}

function _setFallback(tabKey, info) {
  _recentFallback.set(tabKey, {
    peerId: info.peerId,
    hostname: info.hostname,
    tabLabel: info.tabLabel,
  });
  _persistFallback(tabKey);
}

function _clearFallback(tabKey) {
  if (!_recentFallback.has(tabKey)) return;
  _recentFallback.delete(tabKey);
  _persistFallback(tabKey);
}

function _hydrateFallbacks() {
  for (const tab of TARGETABLE_TABS) {
    let raw;
    try { raw = localStorage.getItem(_fallbackStorageKey(tab)); }
    catch { raw = null; }
    if (!raw) continue;
    try {
      const v = JSON.parse(raw);
      if (v && typeof v.peerId === "string" && typeof v.hostname === "string") {
        _recentFallback.set(tab, {
          peerId: v.peerId,
          hostname: v.hostname,
          tabLabel: v.tabLabel || TAB_LABEL[tab] || tab,
        });
      }
    } catch { /* corrupt entry — ignore */ }
  }
}

/** Returns the "switch back" record for ``tabKey`` IFF the recorded
 * peer is currently online, controllable, and supports the tab — i.e.
 * a chip tap would actually succeed. Returns null when the tab is
 * already pointed somewhere other than main (no need to offer it),
 * when no fallback is on file, or when the peer isn't currently
 * reachable. The chip should re-appear on its own once the peer
 * comes back. */
export function pendingSwitchBack(tabKey) {
  if (!tabKey) return null;
  const fb = _recentFallback.get(tabKey);
  if (!fb) return null;
  const t = targetFor(tabKey);
  if (t.kind !== "main") return null;
  const live = peersCache.find((p) => p.id === fb.peerId);
  if (!live || !live.online || !live.controllable) return null;
  if (!peerSupportsTab(live, tabKey)) return null;
  return {
    peerId: fb.peerId,
    hostname: live.hostname || fb.hostname,
    tabLabel: fb.tabLabel,
  };
}

function detectCapabilityShrinks() {
  for (const p of peersCache) {
    const hadCapsRecord = _lastSeenCaps.has(p.id);
    const priorCaps = _lastSeenCaps.get(p.id);
    // Always update the snapshot first so an offline blip (peer
    // missing from the cache and then returning) doesn't replay
    // shrink detection against a stale baseline.
    _lastSeenCaps.set(p.id, Array.isArray(p.capabilities)
      ? p.capabilities.slice()
      : null);
    if (!hadCapsRecord) continue; // first sighting — nothing to compare
    // Strict "shrink" semantics: only compare when we previously had
    // an explicit capability list. A legacy peer that advertised
    // nothing (priorCaps === null, treated as "trust them") and then
    // upgrades to publish a real list isn't actually losing support —
    // it's gaining metadata — and shouldn't fire a fallback toast.
    if (!Array.isArray(priorCaps)) continue;

    for (const tab of TARGETABLE_TABS) {
      const t = targetFor(tab);
      const wasSupported = peerSupportsTab(
        { capabilities: priorCaps }, tab);
      const isSupported = peerSupportsTab(p, tab);

      // Shrink edge: a tab still pointed at this peer just lost
      // the cap it relied on. Fall back to main and remember that
      // we did so, so a later regain can offer a one-tap retarget.
      if (t.kind === "peer" && t.id === p.id
          && wasSupported && !isSupported) {
        // Fall back to main BEFORE notifying so subscribers that
        // re-read targetFor() in their toast handler see the new
        // state (and the broken tab stops issuing peer requests).
        // auto=true: system-driven fallback, NOT a user re-engage,
        // so do not clear regain-suppression for this tab.
        setTargetFor(tab, { kind: "main" }, { auto: true });
        const tabLabel = TAB_LABEL[tab] || tab;
        if (!_pendingRegain.has(p.id)) _pendingRegain.set(p.id, new Map());
        _pendingRegain.get(p.id).set(tab, {
          hostname: p.hostname || p.id, tabLabel,
        });
        // Stash a durable "switch back" hint for the picker chip.
        // setTargetFor() above just cleared any previous fallback,
        // so it's safe to set the new one unconditionally here.
        _setFallback(tab, {
          peerId: p.id,
          hostname: p.hostname || p.id,
          tabLabel,
        });
        for (const cb of capLostCbs) {
          try { cb({ tabKey: tab, hostname: p.hostname || p.id, tabLabel }); }
          catch { /* keep going */ }
        }
        continue;
      }

      // Regain edge: a tab we previously force-fell off this peer
      // has its capability back. Offer the user a one-tap "switch
      // back to <hostname>" — but only while the tab is still on
      // main (if they manually moved it elsewhere, drop the
      // pending entry silently and don't pester them).
      const pending = _pendingRegain.get(p.id);
      const pendingInfo = pending?.get(tab);
      if (pendingInfo && !wasSupported && isSupported) {
        if (t.kind !== "main") {
          pending.delete(tab);
          if (pending.size === 0) _pendingRegain.delete(p.id);
          continue;
        }
        // Drop the pending entry now: this is a one-shot offer.
        // If the user dismisses and the peer's caps shrink+regain
        // again, the shrink branch above re-arms it.
        pending.delete(tab);
        if (pending.size === 0) _pendingRegain.delete(p.id);
        // Honor a previously-recorded "user said no" for this
        // (peer, tab) pair — they dismissed last time, don't ask
        // again until they re-target the tab manually.
        if (_isSuppressed(p.id, tab)) continue;
        const hostname = p.hostname || p.id;
        const tabLabel = pendingInfo.tabLabel || TAB_LABEL[tab] || tab;
        for (const cb of capRegainCbs) {
          try {
            cb({
              tabKey: tab, peerId: p.id, hostname, tabLabel,
            });
          } catch { /* keep going */ }
        }
      }
    }
  }
}

function detectOfflineTransitions() {
  // For every peer in the cache, compare the new online state against
  // the last one we recorded; on a true->false flip we fire ONCE per
  // peer (not once per tab that happens to target it) so the user
  // doesn't see N stacked toasts when multiple tabs share the same
  // machine. The callback receives the active tab if any of the
  // user's tabs were pointing there, otherwise null.
  for (const p of peersCache) {
    const wasOnline = _lastSeenOnline.get(p.id);
    const isOnline = !!(p.online && p.controllable);
    if (wasOnline === true && !isOnline) {
      let affectedTab = null;
      for (const tab of TARGETABLE_TABS) {
        const t = targetFor(tab);
        if (t.kind === "peer" && t.id === p.id) {
          affectedTab = tab;
          break;
        }
      }
      if (affectedTab !== null) {
        for (const cb of offlineCbs) {
          try { cb(affectedTab, p.hostname || p.id); } catch { /* keep going */ }
        }
      }
    }
    _lastSeenOnline.set(p.id, isOnline);
  }
}

// ---------------------------------------------------------------------------
// Stale-target reconciliation
//
// When a peer disappears from /api/peers (unpaired, or its id was
// rotated by a re-pair), tabs that were pointed at it would silently
// fall back to the main bridge — the user's Files tab suddenly shows
// a different machine with no explanation. Worse, if the same
// hostname comes back with a fresh id, the saved id stays stale
// forever and the tab never re-binds.
//
// On every successful peers refresh we walk the per-tab saved
// targets and:
//   - if the saved id is still in the cache, do nothing.
//   - if a peer with the same hostname exists under a different id,
//     silently rewrite the saved target to the new id and notify the
//     user via a one-shot toast (auto-rebind is the right default —
//     the user already chose this hostname).
//   - otherwise, drop the target back to main and toast "X is gone".
//
// We only reconcile after the FIRST successful poll has populated
// peersCache — without that guard, every saved peer would look
// "stale" on cold boot before /api/peers returned.
// ---------------------------------------------------------------------------
const staleCbs = new Set(); // Set<({tabKey, hostname, action, newHostname?}) => void>

export function onTargetStale(cb) {
  staleCbs.add(cb);
  return () => staleCbs.delete(cb);
}

function _readRawTarget(tabKey) {
  // Bypass targetFor() so we get the original saved id+hostname even
  // when the peer is missing from the cache (targetFor falls back to
  // MAIN in that case which is exactly what we're trying to detect).
  let raw;
  try { raw = localStorage.getItem(storageKey(tabKey)); } catch { return null; }
  if (!raw) return null;
  try {
    const v = JSON.parse(raw);
    if (v && v.kind === "peer" && typeof v.id === "string") return v;
  } catch { /* corrupt — treat as no saved target */ }
  return null;
}

function _fireStale(payload) {
  for (const cb of staleCbs) {
    try { cb(payload); } catch { /* keep going */ }
  }
}

function reconcileSavedTargets() {
  // Dedupe stale notifications per hostname per refresh tick. If
  // three tabs all pointed at the same lost peer, we still rewrite
  // localStorage for all three but only fire the toast once — the
  // user doesn't need three identical "X is gone" pop-ups.
  const firedThisTick = new Set();
  for (const tab of TARGETABLE_TABS) {
    const saved = _readRawTarget(tab);
    if (!saved) continue;
    const hit = peersCache.find((p) => p.id === saved.id);
    if (hit) continue; // still valid

    // Saved id is gone. Try to find the same hostname under a new id
    // (re-pair rotation is the common case).
    const savedHost = saved.hostname || saved.id;
    const rebind = peersCache.find(
      (p) => (p.hostname || "") && p.hostname === saved.hostname,
    );
    if (rebind) {
      try {
        localStorage.setItem(storageKey(tab), JSON.stringify({
          kind: "peer", id: rebind.id, hostname: rebind.hostname,
        }));
      } catch { /* localStorage disabled — accept silent no-op */ }
      notify(tab);
      // Keep any "switch back" chip pointing at the new id so a tap
      // still works after a re-pair under the same hostname.
      const fb = _recentFallback.get(tab);
      if (fb && fb.hostname === rebind.hostname && fb.peerId !== rebind.id) {
        _setFallback(tab, {
          peerId: rebind.id, hostname: rebind.hostname, tabLabel: fb.tabLabel,
        });
      }
      const k = `rebound:${savedHost}`;
      if (!firedThisTick.has(k)) {
        firedThisTick.add(k);
        _fireStale({
          tabKey: tab, hostname: savedHost, action: "rebound",
          newHostname: rebind.hostname,
        });
      }
    } else {
      try { localStorage.removeItem(storageKey(tab)); } catch { /* ignore */ }
      notify(tab);
      const k = `lost:${savedHost}`;
      if (!firedThisTick.has(k)) {
        firedThisTick.add(k);
        _fireStale({ tabKey: tab, hostname: savedHost, action: "lost" });
      }
    }
  }
}

export function setActiveTab(tabKey) {
  activeTab = tabKey;
  renderActivePill();
}

// Bound DOM listeners are saved here so teardown can detach them on
// logout. Re-binding without removing them would stack handlers on
// every login cycle and fire each click N times.
let _boundListeners = null;

export function initTargetPicker() {
  const pill = document.getElementById("target-pill");
  const sheet = document.getElementById("target-sheet");
  if (!pill || !sheet) return;
  // Idempotent: detach any leftovers from a previous session before
  // re-binding so we never double-up.
  _detachListeners();
  const backdrop = sheet.querySelector(".target-sheet-backdrop");
  const closeBtn = sheet.querySelector(".target-sheet-close");
  pill.addEventListener("click", openSheet);
  backdrop?.addEventListener("click", closeSheet);
  closeBtn?.addEventListener("click", closeSheet);
  _boundListeners = { pill, backdrop, closeBtn };
  // Restore any persisted "switch back" hints before the first poll
  // so the chip can render the moment the user opens the picker.
  _hydrateFallbacks();
  startPolling();
  renderActivePill();
}

function _detachListeners() {
  if (!_boundListeners) return;
  const { pill, backdrop, closeBtn } = _boundListeners;
  pill?.removeEventListener("click", openSheet);
  backdrop?.removeEventListener("click", closeSheet);
  closeBtn?.removeEventListener("click", closeSheet);
  _boundListeners = null;
}

export function teardownTargetPicker() {
  stopPolling();
  // Hide the pill + sheet and forget the active tab so a fresh
  // login starts from a clean slate. Without this the previous
  // user's pill state can briefly flash on the login screen.
  activeTab = null;
  closeSheet();
  const pill = document.getElementById("target-pill");
  if (pill) pill.style.display = "none";
  // Drop ALL subscribers + offline callbacks. ``app.showApp()``
  // re-registers them on the next login; without this clear,
  // repeated logout/login cycles in one browser tab would stack
  // duplicate handlers and fire each toast/reconnect N times.
  subs.clear();
  offlineCbs.clear();
  staleCbs.clear();
  capLostCbs.clear();
  capRegainCbs.clear();
  _pendingRegain.clear();
  _lastSeenOnline.clear();
  _lastSeenCaps.clear();
  // Drop the in-memory fallback map but keep its localStorage twin
  // so the chip survives logout/login cycles. _hydrateFallbacks()
  // will repopulate it on the next initTargetPicker().
  _recentFallback.clear();
  _detachListeners();
}
