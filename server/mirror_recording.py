"""
Record the live H.264 mirror stream (and optionally the matching audio
stream) to a single MP4 file.

Design
------
Both ``phone_capture`` and ``audio_capture`` already expose a clean
fan-out API: ``add_subscriber`` hands you an ``asyncio.Queue`` that
receives whole frames / chunks. The recorder is just another
subscriber — it pulls frames off the queue and writes them to disk.
This avoids any changes to the capture pipelines and means recording
piggybacks on the same H.264 stream the browser viewer is already
decoding (zero extra cost on the phone).

Why not pipe straight into ffmpeg?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
ffmpeg with two real-time inputs from named pipes is fiddly: if audio
is missing or stops mid-recording, the muxer blocks waiting on its
input and never finalises the MP4. The post-mux approach below is
simpler and dramatically more robust:

1. While recording, write the raw H.264 elementary stream to a
   ``.h264`` file and (when audio is on) the raw PCM to a ``.pcm`` file.
   Both files are append-only and crash-safe — even a hard kill leaves
   recoverable raw streams behind.
2. On stop, run ffmpeg once to remux ``.h264`` (and ``.pcm``, when
   present) into a single ``.mp4`` with ``-c:v copy -c:a aac``. Source
   files are deleted on success.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import audio_capture, phone_capture

STATE_DIR = Path.home() / ".phone-bridge"
RECORDINGS_DIR = STATE_DIR / "recordings"

MUX_TIMEOUT_SEC = 60.0           # ffmpeg post-mux upper bound
PUMP_JOIN_TIMEOUT_SEC = 2.5      # how long stop() waits for tasks to drain
MAX_RECORDING_SECONDS = 4 * 3600  # hard ceiling on the per-recording cap (4 h)


def _safe_serial_for_path(serial: str) -> str:
    """Filesystem-safe form of an adb serial (e.g. ``192.168.1.42:5555``
    becomes ``192.168.1.42_5555``)."""
    out = []
    for ch in serial:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "device"


class Recording:
    """One in-flight recording bound to a device serial."""

    def __init__(self, serial: str, max_seconds: Optional[int] = None) -> None:
        self.serial = serial
        self.started_at = time.time()
        self.stopped_at: float = 0.0
        ts = int(self.started_at)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        stem = RECORDINGS_DIR / f"phone-{_safe_serial_for_path(serial)}-{ts}"
        self._h264_path: Path = stem.with_suffix(".h264")
        self._pcm_path: Optional[Path] = None
        self._mp4_path: Path = stem.with_suffix(".mp4")
        self._stop = asyncio.Event()
        self._video_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._video_q: Optional["asyncio.Queue[bytes]"] = None
        self._audio_q: Optional["asyncio.Queue[bytes]"] = None
        self._video_sess: Optional[phone_capture.CaptureSession] = None
        self._audio_sess: Optional[audio_capture.AudioCaptureSession] = None
        self.bytes_video: int = 0
        self.bytes_audio: int = 0
        self.last_error: str = ""
        self.finalized: bool = False
        self.mp4_size: int = 0
        # Optional max-length safety net. ``max_seconds`` of 0/None
        # means unlimited; anything positive arms a watchdog task that
        # auto-stops the recording when the deadline elapses. The
        # deadline can be pushed back at runtime via :meth:`extend`.
        if max_seconds is not None and max_seconds > 0:
            self.max_seconds: Optional[int] = min(int(max_seconds),
                                                  MAX_RECORDING_SECONDS)
            self.deadline_at: Optional[float] = (self.started_at
                                                 + self.max_seconds)
        else:
            self.max_seconds = None
            self.deadline_at = None
        self.auto_stopped: bool = False
        self._deadline_changed = asyncio.Event()
        self._watchdog_task: Optional[asyncio.Task] = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        v_sess = phone_capture.get_session(self.serial)
        if v_sess is None or not v_sess.is_running():
            raise RuntimeError(
                "Mirror capture is not running. Start the browser viewer first."
            )
        if v_sess.mode != phone_capture.MODE_H264:
            raise RuntimeError(
                "Recording requires the H.264 mirror mode. Open the mirror "
                "viewer in a WebCodecs-capable browser (Chrome/Edge/Safari)."
            )
        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "ffmpeg is not installed (try: sudo apt install ffmpeg)."
            )
        self._video_q = await v_sess.add_subscriber()
        if self._video_q is None:
            raise RuntimeError(
                f"No mirror viewer slot free (max {phone_capture.MAX_SUBSCRIBERS}). "
                "Close a browser viewer and try again."
            )
        self._video_sess = v_sess

        # Audio is best-effort: we attach to the audio session if it's
        # already running, otherwise the recording is video-only. We
        # deliberately don't auto-start the audio pipeline — it has its
        # own permission/UX implications and the user toggled it
        # explicitly when they wanted it.
        a_sess = audio_capture.get_session(self.serial)
        if a_sess is not None and a_sess.is_running():
            q = await a_sess.add_subscriber()
            if q is not None:
                self._audio_q = q
                self._audio_sess = a_sess
                self._pcm_path = self._h264_path.with_suffix(".pcm")

        self._video_task = asyncio.create_task(
            self._pump_video(), name=f"rec-v:{self.serial}",
        )
        if self._audio_q is not None:
            self._audio_task = asyncio.create_task(
                self._pump_audio(), name=f"rec-a:{self.serial}",
            )
        if self.deadline_at is not None:
            self._watchdog_task = asyncio.create_task(
                self._watchdog(), name=f"rec-wd:{self.serial}",
            )

    async def _watchdog(self) -> None:
        """Auto-stop the recording when the configured cap elapses.

        Sleeps until the deadline. ``extend()`` sets
        ``_deadline_changed`` to wake the loop early so a new (later)
        deadline takes effect without waiting for the original timer
        to fire."""
        try:
            while not self._stop.is_set():
                if self.deadline_at is None:  # extended → unlimited
                    return
                remaining = self.deadline_at - time.time()
                if remaining <= 0:
                    self.auto_stopped = True
                    # Schedule stop in its own task so the watchdog
                    # itself can be awaited by stop()'s pump-join loop
                    # without deadlocking on its own cancellation.
                    asyncio.create_task(
                        self._auto_finalize(),
                        name=f"rec-cap:{self.serial}",
                    )
                    return
                self._deadline_changed.clear()
                try:
                    await asyncio.wait_for(
                        self._deadline_changed.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    # Deadline reached — loop around to fire the
                    # auto-stop branch above.
                    continue
        except asyncio.CancelledError:
            return

    def extend(self, extra_seconds: int) -> Dict[str, Any]:
        """Push the auto-stop deadline back by ``extra_seconds``.

        Passing 0 or a negative value clears the cap (the recording
        becomes unlimited). The extension is clamped so the total
        recording length never exceeds :data:`MAX_RECORDING_SECONDS`."""
        if self.finalized:
            raise RuntimeError("Recording is already finished.")
        if extra_seconds <= 0:
            self.deadline_at = None
            self.max_seconds = None
        else:
            now = time.time()
            base = max(self.deadline_at or now, now)
            new_deadline = base + int(extra_seconds)
            hard_cap = self.started_at + MAX_RECORDING_SECONDS
            self.deadline_at = min(new_deadline, hard_cap)
            self.max_seconds = int(self.deadline_at - self.started_at)
        # Wake the watchdog so it picks up the new deadline immediately.
        self._deadline_changed.set()
        return self.status()

    async def _pump_video(self) -> None:
        ended_externally = False
        try:
            with open(self._h264_path, "wb") as f:
                while not self._stop.is_set():
                    try:
                        au = await self._video_q.get()
                    except asyncio.CancelledError:
                        return
                    if not au:
                        # Capture session ended underneath us — flag so
                        # we can auto-finalise after this finally block.
                        ended_externally = True
                        return
                    f.write(au)
                    self.bytes_video += len(au)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"video write: {e}"[:300]
        finally:
            try:
                if self._video_sess is not None and self._video_q is not None:
                    await self._video_sess.remove_subscriber(self._video_q)
            except Exception:  # noqa: BLE001
                pass
            # Auto-finalise when the upstream capture stops on its own
            # (phone unplugged, viewer page closed and session torn
            # down, etc.). Without this the .h264 file would sit
            # around un-muxed until someone clicked Stop, and the UI
            # would show a stuck "REC ..." button.
            if ended_externally and not self.finalized and not self._stop.is_set():
                # Schedule the auto-stop in its own task so we don't
                # await ourselves from inside our own finally.
                asyncio.create_task(
                    self._auto_finalize(),
                    name=f"rec-autofin:{self.serial}",
                )

    async def _auto_finalize(self) -> None:
        # Best-effort wrapper around stop() — used by the video pump
        # when the upstream capture session ends unexpectedly.
        try:
            await self.stop()
        except Exception:  # noqa: BLE001
            pass

    async def _pump_audio(self) -> None:
        if self._pcm_path is None or self._audio_q is None:
            return
        try:
            with open(self._pcm_path, "wb") as f:
                while not self._stop.is_set():
                    try:
                        chunk = await self._audio_q.get()
                    except asyncio.CancelledError:
                        return
                    if not chunk:
                        return
                    f.write(chunk)
                    self.bytes_audio += len(chunk)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"audio write: {e}"[:300]
        finally:
            try:
                if self._audio_sess is not None and self._audio_q is not None:
                    await self._audio_sess.remove_subscriber(self._audio_q)
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> Dict[str, Any]:
        if self.finalized:
            return self.status()
        self._stop.set()
        # Wake the watchdog so it exits its sleep promptly.
        self._deadline_changed.set()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Drain pumps. We can't just cancel: we want their `with open()`
        # blocks to finish flushing to disk before ffmpeg reads them.
        for t in (self._video_task, self._audio_task):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=PUMP_JOIN_TIMEOUT_SEC)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self.stopped_at = time.time()
        await self._mux()
        return self.status()

    async def _mux(self) -> None:
        """Remux the raw streams into a single fragmented-friendly MP4."""
        if self.bytes_video == 0:
            self.last_error = (self.last_error
                               or "no video data was captured")
            return
        cmd: List[str] = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "h264", "-i", str(self._h264_path),
        ]
        has_audio = (self._pcm_path is not None
                     and self._pcm_path.exists()
                     and self.bytes_audio > 0)
        if has_audio:
            cmd += [
                "-f", "s16le",
                "-ar", str(audio_capture.SAMPLE_RATE),
                "-ac", str(audio_capture.CHANNELS),
                "-i", str(self._pcm_path),
                "-c:v", "copy", "-c:a", "aac", "-shortest",
            ]
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-movflags", "+faststart", str(self._mp4_path)]
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=MUX_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self.last_error = "ffmpeg mux timed out"
                return
            if proc.returncode != 0:
                msg = stderr.decode("utf-8", errors="replace").strip()
                self.last_error = (msg or "ffmpeg failed")[:300]
                return
            try:
                self.mp4_size = self._mp4_path.stat().st_size
            except OSError:
                self.mp4_size = 0
            self.finalized = True
        except FileNotFoundError:
            self.last_error = "ffmpeg disappeared between probe and mux"
        except Exception as e:  # noqa: BLE001
            self.last_error = f"mux: {e}"[:300]
        finally:
            if self.finalized:
                # Drop the intermediate raw files — the MP4 is the
                # canonical artifact from here on. If the mux failed
                # we keep them around so the user can recover by hand.
                for p in (self._h264_path, self._pcm_path):
                    if p is None:
                        continue
                    try:
                        p.unlink()
                    except OSError:
                        pass

    # ---------- status ----------

    def duration_sec(self) -> float:
        end = self.stopped_at or time.time()
        return max(0.0, end - self.started_at)

    def status(self) -> Dict[str, Any]:
        seconds_remaining: Optional[float] = None
        if self.deadline_at is not None and not self.finalized:
            seconds_remaining = max(0.0, self.deadline_at - time.time())
        return {
            "serial": self.serial,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at or None,
            "duration_sec": round(self.duration_sec(), 2),
            "bytes_video": self.bytes_video,
            "bytes_audio": self.bytes_audio,
            "bytes_total": self.bytes_video + self.bytes_audio,
            "has_audio": self._audio_q is not None,
            "finalized": self.finalized,
            "mp4_path": str(self._mp4_path) if self.finalized else None,
            "mp4_size": self.mp4_size,
            "last_error": self.last_error,
            "max_seconds": self.max_seconds,
            "deadline_at": self.deadline_at,
            "seconds_remaining": (round(seconds_remaining, 2)
                                  if seconds_remaining is not None else None),
            "auto_stopped": self.auto_stopped,
        }


# ---------- Module-level registry ----------

_recordings: Dict[str, Recording] = {}
_lock = asyncio.Lock()


async def start_recording(serial: str,
                           max_seconds: Optional[int] = None) -> Recording:
    # Reserve the slot under the lock so two concurrent clients can't
    # both think they won the "no recording running" check and end up
    # with two parallel recorders fighting over subscriber slots.
    async with _lock:
        existing = _recordings.get(serial)
        if existing is not None and not existing.finalized:
            raise RuntimeError(
                "A recording is already in progress for this device."
            )
        rec = Recording(serial, max_seconds=max_seconds)
        _recordings[serial] = rec
    try:
        # start() may raise (no capture, wrong mode, no ffmpeg, no
        # subscriber slot) — do it OUTSIDE the lock since it awaits
        # adb / queue plumbing, but on failure we have to clean up
        # the placeholder we just inserted so the next attempt isn't
        # falsely told a recording is already running.
        await rec.start()
    except Exception:
        async with _lock:
            # Only drop our entry if it's still the one we put in
            # (a racing manual stop_all could have removed it).
            if _recordings.get(serial) is rec:
                del _recordings[serial]
        raise
    return rec


async def stop_recording(serial: str) -> Optional[Dict[str, Any]]:
    async with _lock:
        rec = _recordings.get(serial)
    if rec is None:
        return None
    return await rec.stop()


def get_recording(serial: str) -> Optional[Recording]:
    return _recordings.get(serial)


async def extend_recording(serial: str,
                            extra_seconds: int) -> Optional[Dict[str, Any]]:
    rec = _recordings.get(serial)
    if rec is None or rec.finalized:
        return None
    return rec.extend(extra_seconds)


def list_recordings() -> List[Dict[str, Any]]:
    return [r.status() for r in _recordings.values()]


async def stop_all() -> None:
    async with _lock:
        active = [r for r in _recordings.values() if not r.finalized]
    for r in active:
        try:
            await r.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------- Saved-file library ----------
#
# Helpers for the laptop-side "Recordings" card: list every finalised
# MP4 in RECORDINGS_DIR, resolve a user-supplied name to a path inside
# that directory (rejecting traversal), and delete one file by name.

# Cache for ffprobe-derived durations so list_saved() doesn't re-probe
# every file on every page load. Keyed by (path, size, mtime_ns) so any
# real change to the file invalidates the entry automatically.
_duration_cache: Dict[tuple, Optional[float]] = {}


async def _probe_duration(path: Path) -> Optional[float]:
    """Best-effort MP4 duration in seconds via ffprobe. None if unknown."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in _duration_cache:
        return _duration_cache[key]
    if not shutil.which("ffprobe"):
        _duration_cache[key] = None
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _duration_cache[key] = None
            return None
        if proc.returncode != 0:
            _duration_cache[key] = None
            return None
        text = stdout.decode("utf-8", errors="replace").strip()
        try:
            dur = float(text)
        except ValueError:
            dur = None
        _duration_cache[key] = dur
        return dur
    except Exception:  # noqa: BLE001
        _duration_cache[key] = None
        return None


def _is_safe_name(name: str) -> bool:
    """A library name is just a basename ending in .mp4 with no separators."""
    # Match list_saved()'s case-insensitive `.mp4` filter so an uppercase
    # `.MP4` listed in the UI can also be played and deleted.
    if not name or not name.lower().endswith(".mp4"):
        return False
    if "/" in name or "\\" in name or name in (".", ".."):
        return False
    # Reject anything that doesn't normalise to itself (catches sneaky
    # traversal like "foo/../bar.mp4" even though "/" is already blocked).
    return Path(name).name == name


def saved_path(name: str) -> Optional[Path]:
    """Resolve `name` to an MP4 inside RECORDINGS_DIR, or None if invalid."""
    if not _is_safe_name(name):
        return None
    candidate = (RECORDINGS_DIR / name).resolve()
    try:
        root = RECORDINGS_DIR.resolve()
    except OSError:
        return None
    # `is_relative_to` only exists on 3.9+; do the equivalent by hand for
    # broad compat. The earlier traversal check already rules out tricks,
    # this is a defense-in-depth assert.
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


async def list_saved() -> List[Dict[str, Any]]:
    """Return one entry per finalised MP4 in RECORDINGS_DIR, newest first."""
    if not RECORDINGS_DIR.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for path in RECORDINGS_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        duration = await _probe_duration(path)
        entries.append({
            "name": path.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "duration_sec": round(duration, 2) if duration is not None else None,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def delete_saved(name: str) -> bool:
    """Delete one MP4 from RECORDINGS_DIR. Returns True if it was removed."""
    target = saved_path(name)
    if target is None:
        return False
    try:
        target.unlink()
    except OSError:
        return False
    # Drop any stale duration-cache rows for the path so a re-uploaded
    # file with the same name doesn't pick up the old probe result.
    for k in [k for k in _duration_cache if k[0] == str(target)]:
        _duration_cache.pop(k, None)
    return True
