"""
Send tap / swipe / text / key events back to the phone over `adb shell input`.

Used by the browser-based mirror viewer (`/ws/mirror`) so a watcher can
actually drive the device — taps become `input tap`, drags become
`input swipe`, mouse-wheel ticks become short vertical swipes, typed
characters become `input text`, and named keys (Enter, Backspace, arrows…)
become `input keyevent`.

Each call spawns one short-lived `adb` process. We coalesce concurrent
calls per-serial behind an asyncio.Lock so we never queue two `input`
commands at once for the same device — Android's input subsystem can get
confused if a swipe and a tap arrive on overlapping millisecond windows.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional

ADB_INPUT_TIMEOUT = 4.0
SWIPE_MIN_DURATION_MS = 30
SWIPE_MAX_DURATION_MS = 5000
MAX_TEXT_CHUNK = 500
COORD_MAX = 20000  # any sane phone is well below this
_ROTATION_STATE_FILE = Path.home() / ".phone-bridge" / "rotation-restore.json"
_rotation_state_lock = threading.Lock()

# adb `input text` interprets a handful of characters specially. The
# safest path is to: (a) reject control bytes, (b) shell-escape the
# argument with single quotes, (c) replace spaces with %s (Android's
# documented placeholder), and (d) escape characters the shell would
# otherwise expand inside single quotes (just `'`).
_PRINTABLE_RE = re.compile(r"^[\x20-\x7E]*$")

# Map browser KeyboardEvent.key values to Android keycodes.
# https://developer.android.com/reference/android/view/KeyEvent
_KEYCODES: Dict[str, int] = {
    "Enter": 66,
    "Backspace": 67,
    "Tab": 61,
    "Escape": 4,           # treat Esc as BACK — most useful on Android
    "Delete": 112,
    "ArrowUp": 19,
    "ArrowDown": 20,
    "ArrowLeft": 21,
    "ArrowRight": 22,
    "Home": 3,             # KEYCODE_HOME
    "End": 123,
    "PageUp": 92,
    "PageDown": 93,
    "AudioVolumeUp": 24,
    "AudioVolumeDown": 25,
    "AudioVolumeMute": 164,
    # Aliases the browser sometimes emits.
    "Esc": 4,
    "Del": 112,
    "Up": 19,
    "Down": 20,
    "Left": 21,
    "Right": 22,
    # Convenience names the frontend can send for hardware-style buttons.
    "AndroidBack": 4,
    "AndroidHome": 3,
    "AndroidRecents": 187,
    "AndroidPower": 26,
}


_locks: Dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for(serial: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(serial)
        if lock is None:
            lock = asyncio.Lock()
            _locks[serial] = lock
        return lock


async def _run_adb(serial: str, *args: str) -> Dict[str, object]:
    if not shutil.which("adb"):
        return {"ok": False, "error": "adb is not installed"}
    cmd = ["adb", "-s", serial, "shell", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=ADB_INPUT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"ok": False, "error": "adb input timed out"}
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        return {"ok": False, "error": (msg or "adb input failed")[:200],
                "stdout": stdout.decode("utf-8", errors="replace").strip()}
    return {"ok": True, "stdout": stdout.decode("utf-8", errors="replace").strip()}


def _clamp_coord(v) -> Optional[int]:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    if n < 0 or n > COORD_MAX:
        return None
    return n


async def tap(serial: str, x, y) -> Dict[str, object]:
    cx, cy = _clamp_coord(x), _clamp_coord(y)
    if cx is None or cy is None:
        return {"ok": False, "error": "invalid coordinates"}
    lock = await _lock_for(serial)
    async with lock:
        return await _run_adb(serial, "input", "tap", str(cx), str(cy))


async def swipe(serial: str, x1, y1, x2, y2,
                duration_ms: int = 200) -> Dict[str, object]:
    a, b, c, d = (_clamp_coord(x1), _clamp_coord(y1),
                  _clamp_coord(x2), _clamp_coord(y2))
    if None in (a, b, c, d):
        return {"ok": False, "error": "invalid coordinates"}
    try:
        dur = int(duration_ms)
    except (TypeError, ValueError):
        dur = 200
    dur = max(SWIPE_MIN_DURATION_MS, min(SWIPE_MAX_DURATION_MS, dur))
    lock = await _lock_for(serial)
    async with lock:
        return await _run_adb(
            serial, "input", "swipe",
            str(a), str(b), str(c), str(d), str(dur),
        )


async def keyevent(serial: str, key: str) -> Dict[str, object]:
    code = _KEYCODES.get(key)
    if code is None:
        # Allow numeric passthrough for advanced callers but never accept
        # arbitrary strings — they'd land in the shell.
        try:
            code = int(key)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"unsupported key: {key!r}"}
        if code < 0 or code > 999:
            return {"ok": False, "error": "keycode out of range"}
    lock = await _lock_for(serial)
    async with lock:
        return await _run_adb(serial, "input", "keyevent", str(code))


def _load_rotation_baselines() -> Dict[str, Dict[str, int]]:
    try:
        raw = json.loads(_ROTATION_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        serial: value for serial, value in raw.items()
        if isinstance(serial, str) and isinstance(value, dict)
        and value.get("accelerometer_rotation") in (0, 1)
        and value.get("user_rotation") in (0, 1, 2, 3)
    }


def _get_rotation_baseline(serial: str) -> Optional[Dict[str, int]]:
    with _rotation_state_lock:
        return _load_rotation_baselines().get(serial)


def _set_rotation_baseline(serial: str, baseline: Dict[str, int]) -> bool:
    with _rotation_state_lock:
        states = _load_rotation_baselines()
        states[serial] = baseline
        parent = _ROTATION_STATE_FILE.parent
        temp_path = None
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix="rotation-restore-", suffix=".tmp", dir=parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(states, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, _ROTATION_STATE_FILE)
            temp_path = None
            return True
        except OSError:
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


def _clear_rotation_baseline(serial: str) -> bool:
    with _rotation_state_lock:
        states = _load_rotation_baselines()
        if serial not in states:
            return True
        del states[serial]
        try:
            if not states:
                _ROTATION_STATE_FILE.unlink(missing_ok=True)
                return True
            parent = _ROTATION_STATE_FILE.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix="rotation-restore-", suffix=".tmp", dir=parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(states, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, _ROTATION_STATE_FILE)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            return True
        except OSError:
            return False


async def device_action(serial: str, action: str) -> Dict[str, object]:
    """Run one of the fixed, non-text Android controls used by the mirror UI."""
    if action in {"home", "back", "recents"}:
        codes = {"home": 3, "back": 4, "recents": 187}
        return await keyevent(serial, str(codes[action]))

    if action == "rotate":
        lock = await _lock_for(serial)
        async with lock:
            current_rotation = await _run_adb(
                serial, "settings", "get", "system", "user_rotation",
            )
            if not current_rotation.get("ok"):
                return current_rotation
            try:
                rotation = int(str(current_rotation.get("stdout", "")).strip())
            except ValueError:
                return {"ok": False, "error": "Could not read the phone's current rotation setting"}
            if rotation not in (0, 1, 2, 3):
                return {"ok": False, "error": "Phone returned an unsupported rotation setting"}

            baseline = _get_rotation_baseline(serial)
            if baseline is None:
                current_auto = await _run_adb(
                    serial, "settings", "get", "system", "accelerometer_rotation",
                )
                if not current_auto.get("ok"):
                    return current_auto
                try:
                    accelerometer_rotation = int(
                        str(current_auto.get("stdout", "")).strip(),
                    )
                except ValueError:
                    return {"ok": False, "error": "Could not read the phone's Auto-rotate setting"}
                if accelerometer_rotation not in (0, 1):
                    return {"ok": False, "error": "Phone returned an unsupported Auto-rotate setting"}
                baseline = {
                    "accelerometer_rotation": accelerometer_rotation,
                    "user_rotation": rotation,
                }
                if not _set_rotation_baseline(serial, baseline):
                    return {"ok": False, "error": "Could not save the original phone rotation setting; no change was made"}

            # Alternate portrait/landscape and lock the choice. Android's
            # accelerometer can otherwise immediately undo the requested turn.
            target = 1 if rotation in (0, 2) else 0
            disabled = await _run_adb(
                serial, "settings", "put", "system", "accelerometer_rotation", "0",
            )
            if not disabled.get("ok"):
                return disabled
            result = await _run_adb(
                serial, "settings", "put", "system", "user_rotation", str(target),
            )
            if result.get("ok"):
                result["rotation"] = target
                result["orientation"] = "landscape" if target == 1 else "portrait"
                result["restore_available"] = True
            return result

    if action == "rotate_restore":
        lock = await _lock_for(serial)
        async with lock:
            baseline = _get_rotation_baseline(serial)
            if baseline is None:
                return {"ok": True, "restored": False, "message": "No saved rotation setting to restore"}
            restored_rotation = await _run_adb(
                serial, "settings", "put", "system", "user_rotation",
                str(baseline["user_rotation"]),
            )
            if not restored_rotation.get("ok"):
                return restored_rotation
            restored_auto = await _run_adb(
                serial, "settings", "put", "system", "accelerometer_rotation",
                str(baseline["accelerometer_rotation"]),
            )
            if not restored_auto.get("ok"):
                return restored_auto
            if not _clear_rotation_baseline(serial):
                return {"ok": False, "error": "Rotation was restored but the saved baseline could not be cleared"}
            return {
                "ok": True,
                "restored": True,
                "rotation": baseline["user_rotation"],
                "auto_rotate": bool(baseline["accelerometer_rotation"]),
            }

    if action in {"awake_on", "awake_off"}:
        # Android honours this while the phone is connected to power. This
        # avoids changing the user's screen timeout or altering awake-on-battery.
        return await _run_adb(
            serial, "svc", "power", "stayon",
            "true" if action == "awake_on" else "false",
        )

    return {"ok": False, "error": "unsupported device action"}


# `adb shell <argv...>` joins argv with spaces and ships the result to
# the device as one shell command line. So everything we hand `adb shell`
# is parsed twice: once by `adb` (just whitespace-joined) and once by
# the device's POSIX shell (mksh on Android). We therefore have to
# pre-quote the value so it survives the device-shell round-trip and
# arrives at `input text` as a single token.
#
# Android's `input text` parses the token itself, replaces "%s" with a
# space and "%%" with a literal %. We do the space substitution and use
# shlex.quote() (POSIX-compatible, matches the device shell) to escape
# everything else. Result: typing  `it's a "test" & ok`  goes over the
# wire as something like  input text 'it'"'"'s%sa%s"test"%s&%sok' , and
# arrives at the input service as exactly that string.
_INPUT_TEXT_FORBIDDEN = ("\n", "\r", "\t", "\x00")


def _quote_for_input_text(text: str) -> str:
    safe = text.replace(" ", "%s")
    return shlex.quote(safe)


async def type_text(serial: str, text: str) -> Dict[str, object]:
    if not text:
        return {"ok": True, "skipped": "empty"}
    if any(ch in text for ch in _INPUT_TEXT_FORBIDDEN):
        return {"ok": False, "error": "control characters not allowed"}
    if not _PRINTABLE_RE.match(text):
        return {"ok": False, "error": "non-printable characters in text"}
    if len(text) > MAX_TEXT_CHUNK:
        text = text[:MAX_TEXT_CHUNK]
    lock = await _lock_for(serial)
    async with lock:
        return await _run_adb(serial, "input", "text",
                              _quote_for_input_text(text))
