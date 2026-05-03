"""
Phone-side notification mirroring via ADB.

Polls the connected phone for active notifications using
``adb shell dumpsys notification --noredact`` (with ``cmd notification list``
as a sanity check on which keys are still active). Parses the output into
a stable list of ``{key, package, app, title, text, when, subtext}`` records
and pushes diffs to subscribed WebSocket clients.

Dismiss / open-on-phone helpers run additional adb commands and audit-log
each action.

Designed to fail-quietly: if no device is connected, the snapshot is just
``{"connected": False, "items": []}`` and subscribers see an empty list.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Set

from . import scrcpy_state

POLL_INTERVAL = 2.0       # seconds between adb polls
ADB_TIMEOUT = 6.0         # per adb invocation
DUMPSYS_TIMEOUT = 8.0     # dumpsys can be a bit slower

_state: Dict[str, Any] = {
    "running": False,
    "connected": False,
    "serial": None,
    "items": [],          # list of notification dicts, newest first by `when`
    "last_poll": None,
    "last_error": None,
}
_state_lock = asyncio.Lock()
_subscribers: Set[asyncio.Queue] = set()
_subscribers_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


# ---------- adb helper ----------

async def _adb(args: List[str], *, timeout: float = ADB_TIMEOUT) -> Dict[str, Any]:
    if not shutil.which("adb"):
        return {"ok": False, "stdout": "", "stderr": "adb not installed", "code": -1}
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "stdout": "", "stderr": f"adb timed out ({timeout:.0f}s)",
                    "code": -1}
        return {
            "ok": proc.returncode == 0,
            "stdout": out_b.decode(errors="replace"),
            "stderr": err_b.decode(errors="replace"),
            "code": proc.returncode,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": str(e), "code": -1}


async def _ready_serials() -> List[str]:
    """Return adb serials currently in the `device` (ready) state."""
    r = await _adb(["devices", "-l"])
    serials: List[str] = []
    if not r["ok"]:
        return serials
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


async def _pick_serial() -> Optional[str]:
    """Pick the device to query. Prefer the one currently being mirrored."""
    cur = scrcpy_state.current_serial()
    ready = await _ready_serials()
    if cur and cur in ready:
        return cur
    if ready:
        return ready[0]
    return None


# ---------- dumpsys parser ----------

# A NotificationRecord block starts with a line like:
#   NotificationRecord(0xab12c3d4: pkg=com.example user=UserHandle{0} ...
# We use the `key=...` and `pkg=...` fields plus the indented `extras={...}`
# block that follows for title/text. The exact format varies a little
# between Android versions, so the parser is intentionally lenient.
_NREC_RE = re.compile(r"NotificationRecord\([^)]*\)")
_KEY_RE = re.compile(r"\bkey=(\S+)")
_PKG_RE = re.compile(r"\bpkg=(\S+)")
_WHEN_RE = re.compile(r"\bwhen=(\d{10,})")
_TITLE_RE = re.compile(r"android\.title=(?:String )?\(?([^\)\n]+)\)?")
_TEXT_RE = re.compile(r"android\.text=(?:String )?\(?([^\)\n]+)\)?")
_BIGTEXT_RE = re.compile(r"android\.bigText=(?:String )?\(?([^\)\n]+)\)?")
_SUBTEXT_RE = re.compile(r"android\.subText=(?:String )?\(?([^\)\n]+)\)?")
_TICKER_RE = re.compile(r"\btickerText=([^\n]+)")


def _strip_quoted(s: str) -> str:
    s = s.strip().strip('"').strip("'")
    # dumpsys sometimes prints `String (Title text)` or `String "Title"`
    if s.endswith(")"):
        s = s.rstrip(")")
    return s.strip()


def _parse_notifications_dump(text: str) -> List[Dict[str, Any]]:
    """Best-effort parse of ``dumpsys notification --noredact`` output.

    Splits the text on NotificationRecord(...) headers and pulls out the
    key fields from each block. Anything we can't parse is silently
    omitted from that record.
    """
    items: List[Dict[str, Any]] = []
    if not text:
        return items
    # Find all NotificationRecord block start positions and slice between them.
    headers = list(_NREC_RE.finditer(text))
    if not headers:
        return items
    spans: List[tuple[int, int]] = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        spans.append((start, end))

    seen_keys: Set[str] = set()
    for start, end in spans:
        block = text[start:end]
        key_m = _KEY_RE.search(block)
        if not key_m:
            continue
        key = key_m.group(1)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        pkg_m = _PKG_RE.search(block)
        when_m = _WHEN_RE.search(block)
        title_m = _TITLE_RE.search(block)
        text_m = _TEXT_RE.search(block)
        big_m = _BIGTEXT_RE.search(block)
        sub_m = _SUBTEXT_RE.search(block)
        ticker_m = _TICKER_RE.search(block)

        title = _strip_quoted(title_m.group(1)) if title_m else ""
        body = _strip_quoted((big_m or text_m).group(1)) if (big_m or text_m) else ""
        if not title and ticker_m:
            title = _strip_quoted(ticker_m.group(1))

        items.append({
            "key": key,
            "package": pkg_m.group(1) if pkg_m else "",
            "title": title,
            "text": body,
            "subtext": _strip_quoted(sub_m.group(1)) if sub_m else "",
            "when": int(when_m.group(1)) if when_m else 0,
        })

    items.sort(key=lambda r: r["when"], reverse=True)
    return items


def _items_signature(items: List[Dict[str, Any]]) -> tuple:
    """A stable tuple summarising the snapshot — used to detect changes.

    Includes every user-visible field so progress-bar style updates that only
    change `subtext` (or `when`) still trigger a broadcast to clients.
    """
    return tuple(
        (i.get("key", ""), i.get("package", ""), i.get("title", ""),
         i.get("text", ""), i.get("subtext", ""), i.get("when", 0))
        for i in items
    )


# ---------- snapshot + subscribers ----------

async def _broadcast(snapshot: Dict[str, Any]) -> None:
    """Push the snapshot to every subscribed websocket queue (drop slow ones)."""
    async with _subscribers_lock:
        dead: List[asyncio.Queue] = []
        for q in _subscribers:
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    async with _subscribers_lock:
        _subscribers.add(q)
    # Seed the new subscriber with the current snapshot so it doesn't have
    # to wait for the next change to render anything.
    snap = await get_snapshot()
    try:
        q.put_nowait(snap)
    except asyncio.QueueFull:
        pass
    return q


async def unsubscribe(q: asyncio.Queue) -> None:
    async with _subscribers_lock:
        _subscribers.discard(q)


async def get_snapshot() -> Dict[str, Any]:
    async with _state_lock:
        return {
            "connected": _state["connected"],
            "serial": _state["serial"],
            "items": list(_state["items"]),
            "last_poll": _state["last_poll"],
            "last_error": _state["last_error"],
        }


# ---------- poller ----------

async def _poll_once() -> None:
    if not shutil.which("adb"):
        async with _state_lock:
            changed = _state["last_error"] != "adb not installed"
            _state["connected"] = False
            _state["serial"] = None
            _state["items"] = []
            _state["last_poll"] = int(time.time())
            _state["last_error"] = "adb not installed"
        if changed:
            await _broadcast(await get_snapshot())
        return

    serial = await _pick_serial()
    if not serial:
        async with _state_lock:
            changed = (
                _state["connected"] is True
                or bool(_state["items"])
                or _state["serial"] is not None
                or _state["last_error"] != "no phone connected"
            )
            _state["connected"] = False
            _state["serial"] = None
            _state["items"] = []
            _state["last_poll"] = int(time.time())
            _state["last_error"] = "no phone connected"
        if changed:
            await _broadcast(await get_snapshot())
        return

    r = await _adb(
        ["-s", serial, "shell", "dumpsys", "notification", "--noredact"],
        timeout=DUMPSYS_TIMEOUT,
    )
    if not r["ok"]:
        async with _state_lock:
            _state["connected"] = True
            _state["serial"] = serial
            _state["last_poll"] = int(time.time())
            _state["last_error"] = (r.get("stderr") or "dumpsys failed").strip()[:200]
        await _broadcast(await get_snapshot())
        return

    items = _parse_notifications_dump(r["stdout"])
    async with _state_lock:
        prev_sig = _items_signature(_state["items"])
        new_sig = _items_signature(items)
        changed = (
            prev_sig != new_sig
            or _state["connected"] is False
            or _state["serial"] != serial
        )
        _state["connected"] = True
        _state["serial"] = serial
        _state["items"] = items
        _state["last_poll"] = int(time.time())
        _state["last_error"] = None

    if changed:
        await _broadcast(await get_snapshot())


async def _loop() -> None:
    _state["running"] = True
    try:
        while True:
            try:
                await _poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - never let one bad poll kill the loop
                async with _state_lock:
                    _state["last_error"] = f"{type(e).__name__}: {e}"
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        _state["running"] = False


def start() -> None:
    """Start the notification poller. Safe to call multiple times."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="notification-poller")


async def stop() -> None:
    """Cancel the poller. Used from the FastAPI shutdown hook."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


# ---------- actions ----------

async def dismiss(key: str) -> Dict[str, Any]:
    """Cancel a notification by its dumpsys key.

    Tries the modern ``cmd notification cancel <key>`` form first; if that
    isn't supported on the phone's Android version, falls back to
    ``cmd notification cancel_all <package>`` derived from the key.
    """
    serial = await _pick_serial()
    if not serial:
        return {"ok": False, "error": "no phone connected"}

    r = await _adb(["-s", serial, "shell", "cmd", "notification", "cancel", key])
    if r["ok"]:
        await _poll_once()
        return {"ok": True, "method": "cancel"}

    # Fallback: derive package from the key (format: "user|pkg|id|tag|uid")
    parts = key.split("|")
    pkg = parts[1] if len(parts) >= 2 else ""
    if pkg:
        r2 = await _adb(["-s", serial, "shell", "cmd", "notification",
                         "cancel_all", pkg])
        if r2["ok"]:
            await _poll_once()
            return {"ok": True, "method": "cancel_all", "package": pkg}
        err = (r2.get("stderr") or r2.get("stdout") or "cancel_all failed").strip()
    else:
        err = (r.get("stderr") or r.get("stdout") or "cancel failed").strip()
    return {"ok": False, "error": err[:200]}


async def open_on_phone(package: str) -> Dict[str, Any]:
    """Wake the phone screen and bring the originating app to the foreground.

    We don't have access to the notification's PendingIntent over ADB, so we
    do the next-best thing: launch the app's main activity with `monkey`,
    which works for any installed package without knowing its activity name.
    """
    if not package:
        return {"ok": False, "error": "package required"}
    serial = await _pick_serial()
    if not serial:
        return {"ok": False, "error": "no phone connected"}

    # Wake + unlock-attempt (no-op if no lockscreen).
    await _adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    await _adb(["-s", serial, "shell", "wm", "dismiss-keyguard"])

    r = await _adb([
        "-s", serial, "shell", "monkey",
        "-p", package, "-c", "android.intent.category.LAUNCHER", "1",
    ])
    if r["ok"]:
        return {"ok": True}
    return {"ok": False, "error": (r.get("stderr") or r.get("stdout")
                                   or "launch failed").strip()[:200]}
