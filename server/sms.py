"""
SMS inbox mirroring + OTP auto-copy.

Polls the connected phone for inbox messages with
``adb shell content query --uri content://sms/inbox`` every few seconds,
parses the rows, keeps a rolling cache of the most recent N entries, and
pushes diffs to subscribed WebSocket clients.

When a *newly arrived* message contains an OTP code (see ``otp.py``) and
the user has auto-copy enabled, the code is written to the laptop
clipboard with ``pyperclip``. The detection is conservative; see
``otp.py`` for the rules.

Settings (currently just ``auto_copy``) are persisted to
``~/.phone-bridge/sms.json`` so they survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pyperclip

from . import scrcpy_state
from .otp import extract_otp

POLL_INTERVAL = 4.0       # seconds between adb polls
ADB_TIMEOUT = 8.0         # per adb invocation
MAX_KEEP = 30             # rolling cache size
SETTINGS_FILE = Path.home() / ".phone-bridge" / "sms.json"
DEFAULT_SETTINGS = {"auto_copy": True}

_state: Dict[str, Any] = {
    "running": False,
    "connected": False,
    "serial": None,
    "last_serial": None,     # last serial we baselined against
    "messages": [],          # list of {id, address, body, date, otp}
    "last_poll": None,
    "last_error": None,
    "settings": dict(DEFAULT_SETTINGS),
    "baseline_set": False,   # True once we've recorded the initial max id
    "max_seen_id": 0,        # highest _id we have ever seen on current serial
}
_state_lock = asyncio.Lock()
_subscribers: Set[asyncio.Queue] = set()
_subscribers_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


# ---------- settings persistence ----------

def _load_settings() -> Dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        return {
            "auto_copy": bool(raw.get("auto_copy", DEFAULT_SETTINGS["auto_copy"])),
        }
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: Dict[str, Any]) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(mode=0o700, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass


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
    cur = scrcpy_state.current_serial()
    ready = await _ready_serials()
    if cur and cur in ready:
        return cur
    if ready:
        return ready[0]
    return None


# ---------- content query parser ----------

# Each row from `content query` looks like:
#   Row: 0 _id=42, address=+15551234, body=Your code is 123456, date=1697654321000
# But a body may legitimately contain commas, equals signs, AND newlines
# (multi-line SMS). We accumulate lines into a single row buffer until
# the next "Row:" prefix, then anchor on the trailing `, date=<digits>`
# which is always the last field in a row.
_ROW_PREFIX_RE = re.compile(r"^Row:\s*\d+\s+(.*)", re.DOTALL)
_ROW_RE = re.compile(
    r"_id=(\d+),\s+address=(.*?),\s+body=(.*),\s+date=(\d+)\s*$",
    re.DOTALL,
)


def _try_parse_row(buf: str, out: List[Dict[str, Any]]) -> None:
    pm = _ROW_PREFIX_RE.match(buf)
    if not pm:
        return
    payload = pm.group(1).rstrip()
    m = _ROW_RE.match(payload)
    if not m:
        return
    try:
        sms_id = int(m.group(1))
        date_ms = int(m.group(4))
    except ValueError:
        return
    address = m.group(2).strip()
    body = m.group(3)
    # strip trailing whitespace from body but preserve internal newlines
    body = body.rstrip()
    if address.upper() == "NULL":
        address = ""
    if body.upper() == "NULL":
        body = ""
    out.append({
        "id": sms_id,
        "address": address,
        "body": body,
        "date": date_ms,
        "otp": extract_otp(body),
    })


def _parse_rows(text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not text:
        return items
    current: Optional[str] = None
    for line in text.splitlines():
        if line.lstrip().startswith("Row:"):
            if current is not None:
                _try_parse_row(current, items)
            current = line
        elif current is not None:
            current += "\n" + line
    if current is not None:
        _try_parse_row(current, items)
    items.sort(key=lambda r: r["id"], reverse=True)
    return items[:MAX_KEEP]


def _messages_signature(messages: List[Dict[str, Any]]) -> tuple:
    return tuple((m["id"], m.get("body", ""), m.get("address", "")) for m in messages)


# ---------- snapshot + subscribers ----------

async def _broadcast(payload: Dict[str, Any]) -> None:
    async with _subscribers_lock:
        dead: List[asyncio.Queue] = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


async def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    async with _subscribers_lock:
        _subscribers.add(q)
    snap = await get_snapshot()
    try:
        q.put_nowait({"type": "snapshot", **snap})
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
            "messages": list(_state["messages"]),
            "last_poll": _state["last_poll"],
            "last_error": _state["last_error"],
            "settings": dict(_state["settings"]),
        }


def get_settings() -> Dict[str, Any]:
    return dict(_state["settings"])


async def update_settings(auto_copy: Optional[bool] = None) -> Dict[str, Any]:
    async with _state_lock:
        if auto_copy is not None:
            _state["settings"]["auto_copy"] = bool(auto_copy)
        _save_settings(_state["settings"])
        snap = {
            "connected": _state["connected"],
            "serial": _state["serial"],
            "messages": list(_state["messages"]),
            "last_poll": _state["last_poll"],
            "last_error": _state["last_error"],
            "settings": dict(_state["settings"]),
        }
    await _broadcast({"type": "snapshot", **snap})
    return snap["settings"]


# ---------- poller ----------

def _copy_to_clipboard(text: str) -> bool:
    try:
        pyperclip.copy(text)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _poll_once() -> List[Dict[str, Any]]:
    """Poll once. Returns the list of *newly arrived* messages this poll."""
    if not shutil.which("adb"):
        async with _state_lock:
            changed = _state["last_error"] != "adb not installed"
            _state["connected"] = False
            _state["serial"] = None
            _state["messages"] = []
            _state["last_poll"] = int(time.time())
            _state["last_error"] = "adb not installed"
            # Force a re-baseline whenever the device next reappears.
            _state["baseline_set"] = False
            _state["last_serial"] = None
            _state["max_seen_id"] = 0
        if changed:
            await _broadcast({"type": "snapshot", **(await get_snapshot())})
        return []

    serial = await _pick_serial()
    if not serial:
        async with _state_lock:
            changed = (
                _state["connected"] is True
                or _state["serial"] is not None
                or _state["last_error"] != "no phone connected"
            )
            _state["connected"] = False
            _state["serial"] = None
            _state["messages"] = []
            _state["last_poll"] = int(time.time())
            _state["last_error"] = "no phone connected"
            _state["baseline_set"] = False
            _state["last_serial"] = None
            _state["max_seen_id"] = 0
        if changed:
            await _broadcast({"type": "snapshot", **(await get_snapshot())})
        return []

    r = await _adb([
        "-s", serial, "shell", "content", "query",
        "--uri", "content://sms/inbox",
        "--projection", "_id:address:body:date",
        "--sort", '"date DESC"',
    ])
    # Some Android builds ignore --sort or reject the --projection form; if
    # the query failed, retry without those flags before giving up.
    if not r["ok"] or not r["stdout"].strip():
        r = await _adb([
            "-s", serial, "shell", "content", "query",
            "--uri", "content://sms/inbox",
        ])
    if not r["ok"]:
        err = (r.get("stderr") or "content query failed").strip()
        # Permission denied is the most common failure: the user hasn't
        # granted SMS read access to the shell user. Make it actionable.
        if "permission" in err.lower() or "denied" in err.lower():
            err = ("permission denied — grant SMS access to ADB shell "
                   "(Settings → Apps → Special access → 'Modify system settings')")
        async with _state_lock:
            _state["connected"] = True
            _state["serial"] = serial
            _state["last_poll"] = int(time.time())
            _state["last_error"] = err[:240]
        await _broadcast({"type": "snapshot", **(await get_snapshot())})
        return []

    messages = _parse_rows(r["stdout"])

    async with _state_lock:
        prev_sig = _messages_signature(_state["messages"])
        new_sig = _messages_signature(messages)

        # Reset baseline whenever the active serial changes — a different
        # phone has its own _id space and the previous max is meaningless.
        # (Also covers the disconnected → connected transition.)
        if _state["last_serial"] != serial:
            _state["baseline_set"] = False
            _state["max_seen_id"] = 0
            _state["last_serial"] = serial

        prev_max = _state["max_seen_id"]
        baseline_set = _state["baseline_set"]

        new_arrivals: List[Dict[str, Any]] = []
        current_max = max((m["id"] for m in messages), default=0)
        if not baseline_set:
            # Establish baseline on the first SUCCESSFUL poll for this
            # serial, even if the inbox was empty. Anything that arrives
            # afterwards genuinely arrived "while we were watching".
            _state["max_seen_id"] = current_max
            _state["baseline_set"] = True
        elif current_max > prev_max:
            new_arrivals = [m for m in messages if m["id"] > prev_max]
            _state["max_seen_id"] = current_max

        changed = (
            prev_sig != new_sig
            or _state["connected"] is False
            or _state["serial"] != serial
            or _state["last_error"] is not None
        )
        _state["connected"] = True
        _state["serial"] = serial
        _state["messages"] = messages
        _state["last_poll"] = int(time.time())
        _state["last_error"] = None
        auto_copy = _state["settings"].get("auto_copy", True)

    if changed:
        await _broadcast({"type": "snapshot", **(await get_snapshot())})

    # Auto-copy + OTP toast for any *newly arrived* messages with a code.
    for m in sorted(new_arrivals, key=lambda x: x["id"]):
        code = m.get("otp")
        if not code:
            continue
        copied = False
        if auto_copy:
            copied = await asyncio.to_thread(_copy_to_clipboard, code)
        await _broadcast({
            "type": "otp",
            "id": m["id"],
            "code": code,
            "from": m.get("address", ""),
            "copied": copied,
            "auto_copy_enabled": auto_copy,
            "ts": int(time.time()),
        })

    return new_arrivals


async def _loop() -> None:
    _state["running"] = True
    # Load persisted settings on startup.
    _state["settings"] = _load_settings()
    try:
        while True:
            try:
                await _poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                async with _state_lock:
                    _state["last_error"] = f"{type(e).__name__}: {e}"
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        _state["running"] = False


def start() -> None:
    """Start the SMS poller. Safe to call multiple times."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="sms-poller")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
