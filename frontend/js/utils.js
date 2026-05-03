// ``opts.action`` (optional): { label, onClick } adds a tappable
// button after the message. Action toasts get pointer events back
// (the default toast disables them so it never intercepts taps),
// linger longer so the user has time to react, and dismiss
// themselves the moment the action is invoked.
//
// ``opts.duration`` (optional, ms): override the auto-dismiss
// timer. Defaults to 2500 ms (8000 ms when an action is present so
// the user can read + tap).
export function showToast(msg, type = "", opts = {}) {
  const el = document.getElementById("toast");
  const action = opts.action && typeof opts.action.onClick === "function"
    && opts.action.label ? opts.action : null;
  // Clear any prior content (text node + leftover button from a
  // previous actionable toast) before re-rendering.
  el.textContent = "";
  el.appendChild(document.createTextNode(msg));
  if (action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", () => {
      // Dismiss first so the action handler can show its own
      // toast without us instantly clobbering it on the timer.
      clearTimeout(el._timer);
      el.className = "";
      try { action.onClick(); } catch { /* swallow */ }
    });
    el.appendChild(btn);
  }
  el.className = `show ${type}${action ? " has-action" : ""}`.trim();
  clearTimeout(el._timer);
  const dur = opts.duration ?? (action ? 8000 : 2500);
  // ``opts.onDismiss`` (optional): invoked when the toast times out
  // *without* the action being tapped. Used by the regain flow to
  // record "user said no" suppression — we don't want to fire it on
  // action clicks (the action click handler clears the timer above
  // before this callback can run).
  el._timer = setTimeout(() => {
    el.className = "";
    if (typeof opts.onDismiss === "function") {
      try { opts.onDismiss(); } catch { /* swallow */ }
    }
  }, dur);
}
