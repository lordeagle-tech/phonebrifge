"""
"Find My Phone" — make the phone ring loudly from the laptop.

Plays a system alarm tone on the phone over `adb shell` at full volume on
the *alarm* stream so the sound bypasses silent / vibrate mode. Restores
the prior ringer mode, DND state and alarm-stream volume when the user
hits stop or after the auto-stop timeout (30 s by default).

Per-device state is tracked so two phones can ring independently and
each stop call only affects its own device.

Caveats:
- We can't ship an Android app, so we drive the system audio from the
  shell. The sound itself is whatever alarm `.ogg` we can find under
  `/system/media/audio/alarms/` (or one of a few common vendor paths).
- Some hardened ROMs reject `cmd media_session volume --set` or the
  `settings put global zen_mode` write; the helper records each step's
  status and the API surfaces them so the UI can explain failures.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_DURATION = 30.0      # seconds of ringing on a single Start
RETRIGGER_INTERVAL = 7.0     # most stock alarm clips are ~5–8 s long
ADB_TIMEOUT = 8.0
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# Common locations vendors put their stock alarm tones in. We pick the
# first existing .ogg we find so the helper works on stock AOSP, Pixel,
# Samsung, Xiaomi, OnePlus, etc.
ALARM_DIRS = (
    "/system/media/audio/alarms",
    "/system/product/media/audio/alarms",
    "/product/media/audio/alarms",
    "/system/media/audio/notifications",
)


@dataclass
class _RingState:
    serial: str
    started_at: float
    ends_at: float
    sound_path: Optional[str] = None
    saved_alarm_vol: Optional[int] = None
    saved_alarm_max: Optional[int] = None
    saved_music_vol: Optional[int] = None
    saved_music_max: Optional[int] = None
    saved_zen_mode: Optional[str] = None
    saved_ringer_mode: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


_active: Dict[str, _RingState] = {}
_lock = asyncio.Lock()


# ---------- adb helpers ----------

async def _adb(serial: str, args: List[str], *, timeout: float = ADB_TIMEOUT
               ) -> Dict[str, Any]:
    if not shutil.which("adb"):
        return {"ok": False, "code": -1, "stdout": "", "stderr": "adb not installed"}
    cmd = ["adb", "-s", serial, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e)}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "code": -1, "stdout": "",
                "stderr": f"adb timed out after {timeout:.0f}s"}
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }


async def _shell(serial: str, line: str, *, timeout: float = ADB_TIMEOUT
                 ) -> Dict[str, Any]:
    return await _adb(serial, ["shell", line], timeout=timeout)


# ---------- discovery / state save & restore ----------

async def _find_alarm_sound(serial: str) -> Optional[str]:
    for d in ALARM_DIRS:
        r = await _shell(serial, f"ls {d} 2>/dev/null")
        if not r["ok"]:
            continue
        for line in r["stdout"].splitlines():
            name = line.strip()
            if not name:
                continue
            lower = name.lower()
            if lower.endswith((".ogg", ".mp3", ".m4a", ".wav")):
                return f"{d}/{name}"
    return None


async def _get_stream_volume(serial: str, stream: int
                             ) -> tuple[Optional[int], Optional[int]]:
    """Return (current_vol, max_vol) for an Android audio stream id."""
    r = await _shell(serial, f"cmd media_session volume --stream {stream} --get")
    if not r["ok"]:
        return None, None
    cur = max_v = None
    for tok in r["stdout"].split():
        if tok.startswith("volume=") or tok.startswith("vol="):
            try:
                cur = int(tok.split("=", 1)[1].rstrip(","))
            except ValueError:
                pass
        elif tok.startswith("max="):
            try:
                max_v = int(tok.split("=", 1)[1].rstrip(","))
            except ValueError:
                pass
    return cur, max_v


async def _set_stream_volume(serial: str, stream: int, vol: int) -> bool:
    r = await _shell(serial,
                     f"cmd media_session volume --stream {stream} --set {vol}")
    return r["ok"]


async def _get_zen_mode(serial: str) -> Optional[str]:
    r = await _shell(serial, "settings get global zen_mode")
    return r["stdout"].strip() if r["ok"] else None


async def _set_zen_mode(serial: str, value: str) -> bool:
    r = await _shell(serial, f"settings put global zen_mode {value}")
    return r["ok"]


async def _get_ringer_mode(serial: str) -> Optional[str]:
    r = await _shell(serial, "settings get system mode_ringer")
    return r["stdout"].strip() if r["ok"] else None


async def _set_ringer_mode(serial: str, value: str) -> bool:
    # mode_ringer values: 0=silent, 1=vibrate, 2=normal
    r = await _shell(serial, f"settings put system mode_ringer {value}")
    return r["ok"]


async def _play_sound(serial: str, sound_path: str) -> Dict[str, Any]:
    # `am start` with VIEW + audio mime opens the default audio player and
    # plays the file once. We re-issue it every RETRIGGER_INTERVAL to loop.
    # shlex.quote defends against ROM filenames that contain spaces or
    # quotes — `adb shell` runs the line through the device's /system/bin/sh.
    quoted = shlex.quote(f"file://{sound_path}")
    return await _shell(
        serial,
        f"am start -a android.intent.action.VIEW -d {quoted} -t 'audio/*' "
        "--activity-clear-task --activity-new-task",
    )


async def _silence_player(serial: str) -> None:
    # Best-effort: media stop + force-stop common audio player packages.
    await _shell(serial, "input keyevent 86")  # KEYCODE_MEDIA_STOP
    for pkg in (
        "com.android.music",
        "com.google.android.apps.youtube.music",
        "com.android.bips.media",
        "com.sec.android.app.music",
    ):
        await _shell(serial, f"am force-stop {pkg}")


# ---------- public API ----------

def _public_state(rs: _RingState) -> Dict[str, Any]:
    now = time.time()
    return {
        "serial": rs.serial,
        "active": True,
        "started_at": rs.started_at,
        "ends_at": rs.ends_at,
        "remaining": max(0.0, rs.ends_at - now),
        "sound_path": rs.sound_path,
        "notes": list(rs.notes),
    }


async def get_status(serial: Optional[str] = None) -> Dict[str, Any]:
    async with _lock:
        if serial:
            if not SERIAL_RE.match(serial):
                return {"active": False, "serial": serial}
            rs = _active.get(serial)
            return _public_state(rs) if rs else {"active": False, "serial": serial}
        # Whole-bridge view: list every active ring.
        return {
            "active": bool(_active),
            "rings": [_public_state(rs) for rs in _active.values()],
        }


async def stop(serial: str) -> Dict[str, Any]:
    if not SERIAL_RE.match(serial):
        return {"ok": False, "error": "invalid serial"}
    async with _lock:
        rs = _active.pop(serial, None)
    if not rs:
        return {"ok": True, "stopped": False, "note": "not ringing"}
    if rs.task and not rs.task.done():
        rs.task.cancel()
        try:
            await rs.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await _restore_state(rs)
    return {"ok": True, "stopped": True, "serial": serial}


async def _restore_state(rs: _RingState) -> None:
    await _silence_player(rs.serial)
    if rs.saved_alarm_vol is not None:
        await _set_stream_volume(rs.serial, 4, rs.saved_alarm_vol)
    if rs.saved_music_vol is not None:
        await _set_stream_volume(rs.serial, 3, rs.saved_music_vol)
    if rs.saved_zen_mode is not None and rs.saved_zen_mode.isdigit():
        await _set_zen_mode(rs.serial, rs.saved_zen_mode)
    if rs.saved_ringer_mode is not None and rs.saved_ringer_mode.isdigit():
        await _set_ringer_mode(rs.serial, rs.saved_ringer_mode)


async def _ring_loop(rs: _RingState) -> None:
    """Re-play the alarm tone every RETRIGGER_INTERVAL seconds until expiry."""
    try:
        while True:
            now = time.time()
            if now >= rs.ends_at:
                break
            if rs.sound_path:
                await _play_sound(rs.serial, rs.sound_path)
            # Sleep in short ticks so cancellation is responsive.
            slept = 0.0
            while slept < RETRIGGER_INTERVAL and time.time() < rs.ends_at:
                await asyncio.sleep(0.5)
                slept += 0.5
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        rs.notes.append(f"loop error: {type(e).__name__}: {e}")
    finally:
        # If the loop ended on its own (timeout), restore + drop state.
        # If it was cancelled by stop(), stop() already did the cleanup.
        # We keep the lock held across _restore_state so that a concurrent
        # start() for the same serial cannot install a fresh ring whose
        # boosted volume we then overwrite with the saved (low) value.
        async with _lock:
            if _active.get(rs.serial) is rs:
                _active.pop(rs.serial, None)
                try:
                    await _restore_state(rs)
                except Exception as e:  # noqa: BLE001
                    rs.notes.append(f"restore error: {type(e).__name__}: {e}")


async def start(serial: str, duration: float = DEFAULT_DURATION) -> Dict[str, Any]:
    if not SERIAL_RE.match(serial):
        return {"ok": False, "error": "invalid serial"}
    if not shutil.which("adb"):
        return {"ok": False, "error": "adb not installed"}

    duration = max(5.0, min(120.0, float(duration)))

    async with _lock:
        existing = _active.get(serial)
    if existing:
        # Idempotent: already ringing → just extend the deadline.
        existing.ends_at = max(existing.ends_at, time.time() + duration)
        return {"ok": True, "extended": True, **_public_state(existing)}

    # Sanity: device must be reachable.
    probe = await _shell(serial, "echo ok")
    if not probe["ok"] or "ok" not in probe["stdout"]:
        err = (probe.get("stderr") or "device unreachable").strip()
        return {"ok": False, "error": err[:200]}

    rs = _RingState(
        serial=serial,
        started_at=time.time(),
        ends_at=time.time() + duration,
    )

    rs.sound_path = await _find_alarm_sound(serial)
    if not rs.sound_path:
        rs.notes.append("no alarm tone found in /system/media/audio/alarms")

    # Save prior state so we can restore it on stop.
    a_cur, a_max = await _get_stream_volume(serial, 4)   # ALARM
    m_cur, m_max = await _get_stream_volume(serial, 3)   # MUSIC / MEDIA
    rs.saved_alarm_vol, rs.saved_alarm_max = a_cur, a_max
    rs.saved_music_vol, rs.saved_music_max = m_cur, m_max
    rs.saved_zen_mode = await _get_zen_mode(serial)
    rs.saved_ringer_mode = await _get_ringer_mode(serial)

    # Drop Do-Not-Disturb so the alarm stream isn't suppressed on Android 6+.
    if rs.saved_zen_mode and rs.saved_zen_mode.isdigit() and rs.saved_zen_mode != "0":
        if not await _set_zen_mode(serial, "0"):
            rs.notes.append("could not disable Do Not Disturb")
    # Force ringer to normal so the regular ring stream isn't stuck silent
    # for any vendor that ties alarm volume to ringer mode.
    if rs.saved_ringer_mode and rs.saved_ringer_mode.isdigit() and rs.saved_ringer_mode != "2":
        if not await _set_ringer_mode(serial, "2"):
            rs.notes.append("could not switch ringer to normal")
    # Crank both alarm AND music streams. The default audio player triggered
    # by `am start ... VIEW -t audio/*` routes its output through the music
    # stream on most ROMs, while a few vendor players use the alarm stream
    # directly — boosting both guarantees the sound is loud either way.
    a_target = a_max if a_max is not None else 15
    m_target = m_max if m_max is not None else 15
    if not await _set_stream_volume(serial, 4, a_target):
        rs.notes.append("could not set alarm volume")
    if not await _set_stream_volume(serial, 3, m_target):
        rs.notes.append("could not set media volume")

    if not rs.sound_path:
        # Nothing to play — restore state we just touched and bail.
        await _restore_state(rs)
        return {"ok": False, "error": "no alarm tone available on this phone"}

    async with _lock:
        _active[serial] = rs
        rs.task = asyncio.create_task(_ring_loop(rs), name=f"ring-{serial}")

    return {"ok": True, "started": True, **_public_state(rs)}


async def shutdown() -> None:
    """Cancel every active ring (best-effort) — used at server stop."""
    async with _lock:
        items = list(_active.items())
        _active.clear()
    for _, rs in items:
        if rs.task and not rs.task.done():
            rs.task.cancel()
            try:
                await rs.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await _restore_state(rs)
