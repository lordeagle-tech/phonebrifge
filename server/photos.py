"""
Auto-import new phone photos to a laptop folder.

Background poller that, when enabled, lists the phone's camera + Pictures
directories over `adb shell` every POLL_INTERVAL seconds, diffs against an
on-disk seen-index keyed by (path, size), and `adb pull`s anything new
into the user-chosen destination folder.

State is persisted at ~/.phone-bridge/photos.json so duplicates aren't
re-pulled across server restarts. A small in-memory + on-disk ring keeps
the most recent N imports for the UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import audit, scrcpy_state

STATE_DIR = Path.home() / ".phone-bridge"
STATE_FILE = STATE_DIR / "photos.json"

POLL_INTERVAL = 20.0
ADB_TIMEOUT = 12.0
PULL_TIMEOUT = 120.0
RECENT_KEEP = 10
SEEN_KEEP = 5000           # cap seen-index growth
COOLDOWN_BASE = 30.0       # seconds; first failure waits this long
COOLDOWN_MAX = 300.0       # cap exponential backoff at 5 min

DEFAULT_DEST = str(Path.home() / "Pictures" / "PhoneBridge")
HOME = Path.home().resolve()

REMOTE_DIRS = ["/sdcard/DCIM/Camera", "/sdcard/Pictures"]
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".dng", ".raw", ".bmp", ".tif", ".tiff",
    ".mp4", ".mov", ".mkv", ".webm", ".3gp",
}

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_state: Dict[str, Any] = {
    "watcher_running": False,
    "last_scan": None,
    "last_action": None,
    "last_error": None,
    "last_import_at": None,
}
# Per-serial backoff tracking. Lives in memory only — restarts give the
# user a fresh attempt, which is what they'd expect after restarting the
# server.
_device_cooldown: Dict[str, Dict[str, float]] = {}


def _cooldown_seconds(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(COOLDOWN_BASE * (2 ** (failures - 1)), COOLDOWN_MAX)


def _bump_failure(serial: str) -> float:
    info = _device_cooldown.get(serial) or {"failures": 0, "next_attempt": 0.0}
    info["failures"] = int(info.get("failures", 0)) + 1
    wait = _cooldown_seconds(info["failures"])
    info["next_attempt"] = time.time() + wait
    _device_cooldown[serial] = info
    return wait


def _clear_failure(serial: str) -> None:
    _device_cooldown.pop(serial, None)


def _cooldown_remaining(serial: str) -> float:
    info = _device_cooldown.get(serial)
    if not info:
        return 0.0
    rem = float(info.get("next_attempt", 0.0)) - time.time()
    return rem if rem > 0 else 0.0


_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()
# Serializes every read-modify-write cycle on photos.json so that
# concurrent settings changes (API) and watcher updates (recent/seen)
# can't clobber each other's fields.
_file_lock = threading.Lock()


# ---------- on-disk settings ----------

def _ensure_dir() -> None:
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass


def _load() -> Dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    _ensure_dir()
    # Atomic write: dump to a sibling temp file and rename so a crash mid-write
    # can't corrupt the on-disk state.
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(str(tmp), str(STATE_FILE))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _safe_dest(path: str) -> Path:
    """Resolve a destination path and ensure it sits inside $HOME."""
    if not path:
        raise ValueError("destination is required")
    resolved = Path(os.path.expanduser(path)).resolve()
    try:
        resolved.relative_to(HOME)
    except ValueError as e:
        raise ValueError("destination must be inside your home directory") from e
    return resolved


def get_settings() -> Dict[str, Any]:
    with _file_lock:
        data = _load()
    return {
        "enabled": bool(data.get("enabled", False)),
        "destination": data.get("destination") or DEFAULT_DEST,
    }


def set_settings(enabled: bool, destination: str) -> Dict[str, Any]:
    dest = _safe_dest(destination)
    with _file_lock:
        data = _load()
        data["enabled"] = bool(enabled)
        data["destination"] = str(dest)
        _save(data)
    return get_settings()


def _seen() -> Dict[str, int]:
    """Returns {remote_path: size}. Used to dedupe across rescans."""
    with _file_lock:
        data = _load()
    raw = data.get("seen") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _save_seen(seen: Dict[str, int]) -> None:
    # Cap growth: keep the most recent SEEN_KEEP entries (insertion order).
    if len(seen) > SEEN_KEEP:
        items = list(seen.items())[-SEEN_KEEP:]
        seen = dict(items)
    with _file_lock:
        data = _load()
        data["seen"] = seen
        _save(data)


def get_recent() -> List[Dict[str, Any]]:
    with _file_lock:
        data = _load()
    raw = data.get("recent") or []
    if not isinstance(raw, list):
        return []
    return raw[:RECENT_KEEP]


def _push_recent(entry: Dict[str, Any]) -> None:
    with _file_lock:
        data = _load()
        raw = data.get("recent") or []
        if not isinstance(raw, list):
            raw = []
        raw.insert(0, entry)
        data["recent"] = raw[:RECENT_KEEP]
        _save(data)


def status() -> Dict[str, Any]:
    settings = get_settings()
    cooldowns = {
        s: {
            "failures": int(info.get("failures", 0)),
            "remaining": max(0.0, float(info.get("next_attempt", 0.0)) - time.time()),
        }
        for s, info in _device_cooldown.items()
    }
    return {
        **settings,
        "watcher_running": _state["watcher_running"],
        "last_scan": _state["last_scan"],
        "last_action": _state["last_action"],
        "last_error": _state["last_error"],
        "last_import_at": _state["last_import_at"],
        "cooldowns": cooldowns,
    }


# ---------- adb helpers ----------

async def _adb(args: List[str], timeout: float = ADB_TIMEOUT) -> Dict[str, Any]:
    if not shutil.which("adb"):
        return {"ok": False, "stdout": "", "stderr": "adb not installed", "code": -1}
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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


async def _ready_serial() -> Optional[str]:
    """Pick a target serial: prefer a currently-mirroring one, else first
    'device'-state entry from `adb devices`."""
    s = scrcpy_state.current_serial()
    if s:
        return s
    r = await _adb(["devices"])
    if not r["ok"]:
        return None
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and SERIAL_RE.match(parts[0]):
            return parts[0]
    return None


async def _list_remote(serial: str) -> List[Tuple[str, int]]:
    """List candidate photo files on the phone as (remote_path, size)."""
    # `stat -c '%s|%n'` is widely available on Android (toybox). We collect
    # output across all configured directories in one shell call to keep
    # round-trips cheap.
    parts = []
    for d in REMOTE_DIRS:
        # Quote the path; suppress find errors so a missing directory
        # doesn't fail the whole scan.
        parts.append(
            f"find '{d}' -type f -exec stat -c '%s|%n' {{}} + 2>/dev/null"
        )
    cmd = " ; ".join(parts)
    r = await _adb(["-s", serial, "shell", cmd])
    if not r["ok"]:
        return []
    out: List[Tuple[str, int]] = []
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        size_s, _, path = line.partition("|")
        try:
            size = int(size_s)
        except ValueError:
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in PHOTO_EXTS:
            continue
        out.append((path, size))
    return out


def _unique_local_path(dest_dir: Path, name: str) -> Path:
    """Return dest_dir/name, suffixing ' (n)' if a file already exists."""
    target = dest_dir / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        cand = dest_dir / f"{stem} ({n}){ext}"
        if not cand.exists():
            return cand
        n += 1


async def _pull_one(serial: str, remote: str, dest_dir: Path) -> Optional[Dict[str, Any]]:
    name = os.path.basename(remote) or "photo"
    local = _unique_local_path(dest_dir, name)
    r = await _adb(["-s", serial, "pull", remote, str(local)], timeout=PULL_TIMEOUT)
    if not r["ok"]:
        _state["last_error"] = (r["stderr"] or "adb pull failed").strip()[:200]
        return None
    try:
        size = local.stat().st_size
    except OSError:
        size = 0
    return {
        "name": local.name,
        "remote": remote,
        "local": str(local),
        "size": size,
        "ts": int(time.time()),
    }


async def _scan_once() -> None:
    settings = get_settings()
    if not settings["enabled"]:
        _state["last_action"] = "disabled"
        return

    try:
        dest_dir = _safe_dest(settings["destination"])
    except ValueError as e:
        _state["last_error"] = str(e)
        _state["last_action"] = f"bad destination: {e}"
        return

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _state["last_error"] = f"cannot create destination: {e}"
        _state["last_action"] = _state["last_error"]
        return

    serial = await _ready_serial()
    if not serial:
        _state["last_action"] = "no phone connected"
        return

    cooldown = _cooldown_remaining(serial)
    if cooldown > 0:
        failures = int(_device_cooldown.get(serial, {}).get("failures", 0))
        _state["last_action"] = (
            f"{serial} cooling off for {int(cooldown)}s after "
            f"{failures} failed attempt{'' if failures == 1 else 's'}"
        )
        return

    remote = await _list_remote(serial)
    if not remote:
        # Treat an empty/error listing as a soft failure for backoff
        # purposes — but only bump the counter when adb itself is missing
        # or the directories truly don't exist won't change scan-to-scan.
        # We still bump on every empty listing; a working "no new photos"
        # path uses _list_remote returning the existing entries.
        wait = _bump_failure(serial)
        _state["last_action"] = (
            f"no files visible from {serial}; backing off {int(wait)}s"
        )
        return

    seen = _seen()
    new_items = [(p, sz) for (p, sz) in remote if seen.get(p) != sz]
    if not new_items:
        _state["last_action"] = f"up to date ({len(remote)} files seen)"
        # Refresh seen set so renamed/deleted entries don't grow forever.
        _save_seen({p: sz for (p, sz) in remote})
        return

    pulled = 0
    pull_failed = False
    for remote_path, size in new_items:
        entry = await _pull_one(serial, remote_path, dest_dir)
        if entry is None:
            # Don't mark as seen — we'll retry next scan.
            pull_failed = True
            continue
        seen[remote_path] = size
        _push_recent(entry)
        pulled += 1
        _state["last_import_at"] = entry["ts"]
        try:
            audit.log("files", "photo_imported", outcome="ok",
                      detail={"name": entry["name"], "size": size, "serial": serial})
        except Exception:  # noqa: BLE001
            pass
    _save_seen(seen)
    # Reset backoff on any forward progress; otherwise treat as failure.
    if pulled > 0:
        _clear_failure(serial)
    elif pull_failed:
        wait = _bump_failure(serial)
        _state["last_action"] = (
            f"{serial}: every pull failed; backing off {int(wait)}s"
        )
        return
    else:
        _clear_failure(serial)
    _state["last_action"] = (
        f"imported {pulled} new file{'' if pulled == 1 else 's'}"
        f" ({len(new_items) - pulled} pending)" if pulled < len(new_items)
        else f"imported {pulled} new file{'' if pulled == 1 else 's'}"
    )
    if pulled:
        _state["last_error"] = None


async def _loop() -> None:
    _state["watcher_running"] = True
    try:
        while True:
            try:
                async with _lock:
                    await _scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _state["last_error"] = f"{type(e).__name__}: {e}"
                _state["last_action"] = _state["last_error"]
            _state["last_scan"] = int(time.time())
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    finally:
        _state["watcher_running"] = False


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="photos-watcher")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
