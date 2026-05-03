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
import re
import shlex
import shutil
from typing import Dict, Optional

ADB_INPUT_TIMEOUT = 4.0
SWIPE_MIN_DURATION_MS = 30
SWIPE_MAX_DURATION_MS = 5000
MAX_TEXT_CHUNK = 500
COORD_MAX = 20000  # any sane phone is well below this

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
        _, stderr = await asyncio.wait_for(
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
        return {"ok": False, "error": (msg or "adb input failed")[:200]}
    return {"ok": True}


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
