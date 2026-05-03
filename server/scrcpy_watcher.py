"""Background poller that auto-launches scrcpy when an opted-in remembered
device comes back online (e.g. when you walk in the door and your phone
joins the home Wi-Fi).

The watcher is intentionally tiny:
  - Every POLL_INTERVAL seconds, look at the recent-devices file.
  - For each entry whose `auto` flag is True:
      * If something is already mirroring, skip.
      * If the serial looks like host:port, try `adb connect` first.
      * Then run `adb devices -l` and check whether the serial is in
        the `device` (i.e. ready) state.
      * If yes, call scrcpy_state.launch() with the device's saved preset.
  - Always record the outcome on the device entry so the UI can show
    "last attempted at HH:MM — connection refused" etc.

It runs as a single asyncio task started from the FastAPI lifespan and
cancelled on shutdown. There is no separate thread.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from . import scrcpy_state

POLL_INTERVAL = 15.0      # seconds between scans
ADB_CMD_TIMEOUT = 6.0     # per adb invocation
BACKOFF_BASE = 15.0       # seconds; first failure waits this long before retry
BACKOFF_MAX = 300.0       # cap exponential backoff at 5 minutes


def _backoff_seconds(failures: int) -> float:
    """Exponential backoff per the task spec: ``15s * 2**failures``,
    capped at 5 min. So failures=1 → 30s, failures=2 → 60s, …, hitting
    the 300s cap at failures=5."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE * (2 ** failures), BACKOFF_MAX)


def _in_backoff(entry: Dict[str, Any], now: float) -> Optional[float]:
    """Return seconds remaining in backoff, or None if the device may retry."""
    failures = int(entry.get("failures", 0) or 0)
    if failures <= 0:
        return None
    last = entry.get("last_attempt")
    if not last:
        return None
    wait = _backoff_seconds(failures)
    remaining = (last + wait) - now
    return remaining if remaining > 0 else None

# Wireless serials always look like "<host>:<port>". USB serials don't.
_WIRELESS_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}:\d{1,5}$")

_state: Dict[str, Any] = {
    "running": False,
    "interval": POLL_INTERVAL,
    "last_scan": None,        # unix ts of the last completed scan
    "last_action": None,      # human-readable description of the most recent action
    "auto_count": 0,          # how many remembered devices are currently watched
    "quiet_hours": {"quiet": False, "enabled": False, "start": "22:00", "end": "07:00"},
}
_task: Optional[asyncio.Task] = None


def status() -> Dict[str, Any]:
    return dict(_state)


async def _adb(args: List[str], input_text: Optional[str] = None) -> Dict[str, Any]:
    if not shutil.which("adb"):
        return {"ok": False, "stdout": "", "stderr": "adb not installed", "code": -1}
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin = input_text.encode() if input_text is not None else None
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=ADB_CMD_TIMEOUT
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": out_b.decode(errors="replace"),
            "stderr": err_b.decode(errors="replace"),
            "code": proc.returncode,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "stdout": "", "stderr": "adb timed out", "code": -1}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": str(e), "code": -1}


async def _device_is_ready(serial: str) -> bool:
    r = await _adb(["devices", "-l"])
    if not r["ok"]:
        return False
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == serial and parts[1] == "device":
            return True
    return False


async def _try_one(entry: Dict[str, Any]) -> str:
    serial = entry["serial"]
    preset = entry.get("auto_preset") or "default"
    name = entry.get("name") or ""

    # Wireless devices may have rotated to a new TLS port — try connect first.
    if _WIRELESS_RE.match(serial):
        await _adb(["connect", serial])

    if not await _device_is_ready(serial):
        return "not reachable"

    # Re-check the running flag right before launching to avoid a race
    # where two enabled devices come online at the same scan.
    if scrcpy_state.is_running():
        return "skipped (mirror already running)"

    try:
        # launch() now runs a short link probe (ping/TCP) before spawning
        # scrcpy; push it off the event loop so the watcher tick doesn't
        # block other coroutines for ~1-2s on a degraded link.
        await asyncio.to_thread(
            scrcpy_state.launch, serial, preset, None, name or None,
        )
    except RuntimeError as e:
        return f"launch failed: {e}"
    return f"launched ({preset})"


async def _loop() -> None:
    _state["running"] = True
    try:
        while True:
            try:
                recent = scrcpy_state.list_recent()
            except Exception as e:  # noqa: BLE001
                recent = []
                _state["last_action"] = f"recents read failed: {e}"

            enabled = [r for r in recent if r.get("auto")]
            _state["auto_count"] = len(enabled)

            # Quiet hours: don't even try adb when the user has asked us to
            # stay silent (e.g. overnight). We still record the reason so the
            # UI shows why nothing is happening.
            quiet = scrcpy_state.in_quiet_hours()
            _state["quiet_hours"] = quiet
            if enabled and quiet.get("quiet"):
                _state["last_action"] = (
                    f"quiet hours active ({quiet['start']}–{quiet['end']}); "
                    f"skipping {len(enabled)} watched device"
                    f"{'' if len(enabled) == 1 else 's'}"
                )
                _state["last_scan"] = int(time.time())
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if enabled and not scrcpy_state.is_running():
                now = time.time()
                last_entry = None
                last_outcome = None
                for entry in enabled:
                    # Skip devices that are in their per-device backoff window.
                    # We don't update_attempt here on purpose — that would spam
                    # the audit/log files and reset the cooldown timestamp.
                    remaining = _in_backoff(entry, now)
                    if remaining is not None:
                        continue
                    try:
                        outcome = await _try_one(entry)
                    except Exception as e:  # noqa: BLE001 - never let one bad device kill the loop
                        outcome = f"error: {type(e).__name__}: {e}"
                    success = outcome.startswith("launched")
                    scrcpy_state.update_attempt(entry["serial"], outcome, success=success)
                    last_entry, last_outcome = entry, outcome
                    if success:
                        break  # only one mirror at a time
                if last_entry is not None:
                    _state["last_action"] = (
                        f"{last_entry.get('name') or last_entry['serial']} → "
                        f"{last_outcome}"
                    )
                else:
                    # Every enabled device is currently cooling off — surface
                    # the soonest retry so the UI doesn't look stuck.
                    pairs = [(_in_backoff(e, now) or 0.0, e) for e in enabled]
                    secs, dev = min(pairs, key=lambda p: p[0])
                    if secs > 0:
                        _state["last_action"] = (
                            f"{dev.get('name') or dev['serial']} cooling off "
                            f"for {int(secs)}s after {int(dev.get('failures', 0))} "
                            f"failed attempts"
                        )

            _state["last_scan"] = int(time.time())
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        _state["running"] = False


def start() -> None:
    """Start the watcher task. Safe to call multiple times."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="scrcpy-watcher")


async def stop() -> None:
    """Cancel the watcher task. Used from the FastAPI shutdown hook."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
