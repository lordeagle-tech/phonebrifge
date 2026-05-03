"""
Headless phone-screen capture for the browser-based mirror viewer.

Two interchangeable modes share the same CaptureSession plumbing
(subscriber fan-out, registry, WS-friendly sentinels):

* ``MODE_PNG`` — periodic ``adb -s <serial> exec-out screencap -p``.
  Reliable everywhere, but capped around 6-15 fps and noticeably
  bandwidth-heavy. This is the legacy path the viewer started with.

* ``MODE_H264`` — ``adb -s <serial> exec-out screenrecord
  --output-format=h264 -``. Yields a raw H.264 elementary stream
  (Annex-B NAL units) that modern browsers can decode at 30-60 fps via
  the WebCodecs ``VideoDecoder`` API. Android caps each ``screenrecord``
  invocation at 180 s so we transparently restart it just below that
  ceiling and reuse the same subscriber queues.

The two modes are mutually exclusive per device (the viewer picks one
and the session is built accordingly). Subscribers receive whole frames
either way: PNG bytes in MODE_PNG, complete H.264 access units (NAL
units glued together with ``\\x00\\x00\\x00\\x01`` start codes) in
MODE_H264.
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import time
from typing import Dict, List, Optional, Set

DEFAULT_FPS = 6
MAX_FPS = 15
MIN_FPS = 1
MAX_SUBSCRIBERS = 4
MAX_FRAME_BYTES = 4 * 1024 * 1024  # 4 MB safety cap (PNG mode)
SCREENCAP_TIMEOUT = 4.0

# H.264 mode tunables ------------------------------------------------------
MODE_PNG = "png"
MODE_H264 = "h264"
VALID_MODES = (MODE_PNG, MODE_H264)

DEFAULT_H264_BITRATE = 4_000_000           # 4 Mbps — good 1080p quality
MIN_H264_BITRATE = 500_000
MAX_H264_BITRATE = 20_000_000
SCREENRECORD_RESTART_SECS = 175            # restart before Android's 180s cap
H264_READ_CHUNK = 64 * 1024
H264_STALL_TIMEOUT = 12.0                  # no bytes for this long => restart
NAL_START_CODE = b"\x00\x00\x00\x01"
MAX_AU_BYTES = 4 * 1024 * 1024
MAX_LEFTOVER_BYTES = 8 * 1024 * 1024

# Adaptive-bitrate tunables (H.264 only) -----------------------------------
# The capture session monitors per-subscriber queue depth and the WS
# `bufferedAmount` reported by clients in periodic stats heartbeats.
# When the link is consistently relaxed we step the bit-rate up toward
# H264_BITRATE_CEILING; when it backs up we drop toward H264_BITRATE_FLOOR.
# screenrecord is only restarted when the new target deviates by more than
# H264_BITRATE_RESTART_RATIO from the currently-applied value, so we don't
# churn the encoder on small adjustments.
H264_BITRATE_FLOOR = 1_000_000
H264_BITRATE_CEILING = 8_000_000
H264_BITRATE_RESTART_RATIO = 0.25          # >25% delta triggers restart
H264_ADJUST_INTERVAL = 2.0                 # seconds between samples
H264_PRESSURE_QUEUE = 5                    # of 8 — queue is backing up
H264_PRESSURE_BUFFERED = 2 * 1024 * 1024   # 2 MB WS buffer => congested
H264_HEADROOM_QUEUE = 1                    # at-or-below this == clean
H264_HEADROOM_BUFFERED = 256 * 1024        # 256 KB or less == clean
H264_PRESSURE_STREAK = 2                   # samples before stepping down
H264_HEADROOM_STREAK = 5                   # samples before stepping up
H264_STATS_FRESH_SECS = 8.0                # ignore stale client reports


# ---------- Annex-B helpers ------------------------------------------------

def _find_start_codes(data: bytes) -> List[tuple]:
    """Return ``[(sc_start, payload_start), ...]`` for every Annex-B
    start code (3- or 4-byte) in ``data``. Used to slice the encoder
    stdout into NAL units."""
    out: List[tuple] = []
    n = len(data)
    i = 0
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0:
            if data[i + 2] == 1:
                out.append((i, i + 3))
                i += 3
                continue
            if i + 3 < n and data[i + 2] == 0 and data[i + 3] == 1:
                out.append((i, i + 4))
                i += 4
                continue
        i += 1
    return out


def _split_annexb(data: bytes) -> tuple:
    """Slice ``data`` into NAL bodies. Returns ``(nals, leftover)``
    where ``leftover`` is the trailing partial NAL (from the last
    start code onwards, kept for the next read)."""
    scs = _find_start_codes(data)
    if not scs:
        # Defensive: drop runaway buffer with no start codes — corrupt
        # stream or non-Annex-B output. Shouldn't happen with screenrecord.
        if len(data) > MAX_LEFTOVER_BYTES:
            return [], b""
        return [], data
    nals: List[bytes] = []
    for k in range(len(scs) - 1):
        body = data[scs[k][1]:scs[k + 1][0]]
        if body:
            nals.append(bytes(body))
    leftover = bytes(data[scs[-1][0]:])
    return nals, leftover


def _nal_type(nal: bytes) -> int:
    if not nal:
        return 0
    return nal[0] & 0x1F


def _derive_codec_string(sps: bytes) -> str:
    """Build a WebCodecs-style codec string from an SPS NAL.

    Format: ``avc1.PPCCLL`` where PP=profile_idc, CC=constraint_set
    flags, LL=level_idc — all hex. SPS payload (after the 1-byte NAL
    header) starts with those three bytes."""
    if len(sps) < 4:
        return "avc1.42E01E"  # baseline 3.0 fallback
    return f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


# ---------- Capture session ------------------------------------------------

class CaptureSession:
    """One running capture loop bound to a single device serial.

    Mode-agnostic at the registry / subscriber boundary; the per-mode
    pump methods (``_run_png`` / ``_run_h264``) push frames or access
    units into the same fan-out queues.
    """

    def __init__(self, serial: str, fps: int = DEFAULT_FPS,
                 mode: str = MODE_PNG,
                 bitrate: int = DEFAULT_H264_BITRATE) -> None:
        self.serial = serial
        self.fps = max(MIN_FPS, min(MAX_FPS, int(fps)))
        self.mode = mode if mode in VALID_MODES else MODE_PNG
        self.bitrate = max(MIN_H264_BITRATE,
                           min(MAX_H264_BITRATE, int(bitrate)))
        self.started_at = time.time()
        self.last_frame_at: float = 0.0
        self.frame_count: int = 0
        self.error_count: int = 0
        self.last_error: str = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._subs: Set["asyncio.Queue[bytes]"] = set()
        self._sub_lock = asyncio.Lock()
        # Adaptive-bitrate state (h264 only). _sub_stats is keyed by
        # id(queue) so we can correlate client-reported WS bufferedAmount
        # with the per-subscriber fan-out queue depth without making the
        # queue object itself aware of the heartbeat. _current_proc lets
        # the adjust loop terminate the running screenrecord to apply a
        # new --bit-rate; the loop in _run_h264 then restarts it.
        self._sub_stats: Dict[int, Dict[str, float]] = {}
        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._target_bitrate: int = self.bitrate
        self._adjust_task: Optional[asyncio.Task] = None
        self._pressure_streak: int = 0
        self._headroom_streak: int = 0
        self._bitrate_changes: int = 0
        # When set, the adaptive loop is suppressed and the encoder
        # holds the user's chosen bit-rate exactly. Cleared by passing
        # ``None`` to :meth:`pin_bitrate`.
        self._pinned_bitrate: Optional[int] = None
        # PNG mode: latest single PNG to prime new subscribers.
        self._latest: Optional[bytes] = None
        # H.264 mode: cache of the last seen SPS, PPS, and IDR access
        # unit so newly-attaching subscribers can configure a decoder
        # and start playback without waiting for the next keyframe.
        self._sps: Optional[bytes] = None
        self._pps: Optional[bytes] = None
        self._codec_string: str = ""
        self._latest_keyframe_au: Optional[bytes] = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def codec_config(self) -> Optional[Dict]:
        """Return the H.264 decoder hints the WS handler should send to
        new viewers. ``None`` until we've parsed our first SPS."""
        if self.mode != MODE_H264 or not self._sps:
            return None
        return {
            "codec": self._codec_string or _derive_codec_string(self._sps),
            "sps_b64": base64.b64encode(self._sps).decode("ascii"),
            "pps_b64": (base64.b64encode(self._pps).decode("ascii")
                        if self._pps else ""),
        }

    def status(self) -> Dict:
        return {
            "running": self.is_running(),
            "serial": self.serial,
            "mode": self.mode,
            "fps": self.fps,
            "bitrate": self.bitrate if self.mode == MODE_H264 else None,
            "target_bitrate": (self._target_bitrate
                               if self.mode == MODE_H264 else None),
            "bitrate_changes": (self._bitrate_changes
                                if self.mode == MODE_H264 else None),
            "pinned_bitrate": (self._pinned_bitrate
                               if self.mode == MODE_H264 else None),
            "started_at": self.started_at,
            "last_frame_at": self.last_frame_at,
            "frame_count": self.frame_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "subscribers": self.subscriber_count,
        }

    async def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        target = self._run_h264 if self.mode == MODE_H264 else self._run_png
        self._task = asyncio.create_task(
            target(), name=f"phone-capture:{self.mode}:{self.serial}",
        )
        if self.mode == MODE_H264:
            self._adjust_task = asyncio.create_task(
                self._adjust_bitrate_loop(),
                name=f"phone-capture:adjust:{self.serial}",
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._adjust_task:
            try:
                await asyncio.wait_for(self._adjust_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._adjust_task.cancel()
            self._adjust_task = None
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        # Wake any blocked subscribers with an empty sentinel so the WS
        # handler can detect "stream ended" and close cleanly.
        for q in list(self._subs):
            # Drain a slot if needed so the sentinel always lands.
            while q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                q.put_nowait(b"")
            except asyncio.QueueFull:
                pass

    async def add_subscriber(self) -> Optional["asyncio.Queue[bytes]"]:
        async with self._sub_lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                return None
            # Bigger backlog for h264 since one access unit isn't
            # decodable on its own — the client needs key frames in
            # sequence and a small buffer absorbs network jitter.
            qsize = 8 if self.mode == MODE_H264 else 1
            q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=qsize)
            self._subs.add(q)
            self._sub_stats[id(q)] = {
                "buffered_amount": 0.0,
                "last_report": 0.0,
            }
        # Prime with the latest cached frame / keyframe so the new
        # viewer doesn't stare at a blank canvas.
        prime = (self._latest_keyframe_au if self.mode == MODE_H264
                 else self._latest)
        if prime:
            try:
                q.put_nowait(prime)
            except asyncio.QueueFull:
                pass
        return q

    async def remove_subscriber(self, q: "asyncio.Queue[bytes]") -> None:
        async with self._sub_lock:
            self._subs.discard(q)
            self._sub_stats.pop(id(q), None)

    def set_fps(self, fps: int) -> None:
        self.fps = max(MIN_FPS, min(MAX_FPS, int(fps)))

    def report_subscriber_stats(self, q: "asyncio.Queue[bytes]", *,
                                 buffered_amount: int) -> None:
        """Record a client-side WS ``bufferedAmount`` heartbeat.

        Called from the WebSocket handler when a viewer pushes a
        ``{"type":"stats"}`` frame. The adjust loop combines this signal
        with our own per-subscriber queue depth to decide whether the
        link can take more bits or needs to back off."""
        st = self._sub_stats.get(id(q))
        if st is None:
            return
        try:
            st["buffered_amount"] = max(0.0, float(buffered_amount))
        except (TypeError, ValueError):
            return
        st["last_report"] = time.time()

    # ------------- Adaptive bitrate (h264 only) -------------

    def adjust_bitrate(self, target: int) -> bool:
        """Set the desired H.264 bit-rate (bps) and, if it differs from
        the currently-applied value by more than the restart ratio,
        terminate the running ``screenrecord`` so the outer loop
        respawns it with the new ``--bit-rate=`` argument.

        Returns ``True`` when a restart was triggered, ``False`` otherwise
        (target outside the adaptive range, mode mismatch, or change too
        small to be worth the IDR gap that comes with a restart).
        """
        if self.mode != MODE_H264:
            return False
        clamped = max(H264_BITRATE_FLOOR,
                      min(H264_BITRATE_CEILING, int(target)))
        self._target_bitrate = clamped
        current = max(1, self.bitrate)
        if abs(clamped - current) / current <= H264_BITRATE_RESTART_RATIO:
            return False
        self.bitrate = clamped
        self._bitrate_changes += 1
        proc = self._current_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        return True

    def pin_bitrate(self, target: Optional[int]) -> Dict:
        """Pin the encoder to a fixed ``target`` bps (suppressing the
        adaptive loop) or, with ``target=None``, return to auto.

        The clamp / restart-ratio logic in :meth:`adjust_bitrate`
        applies; if the requested value equals the currently-applied
        bit-rate, no restart happens.
        """
        if self.mode != MODE_H264:
            return self.status()
        if target is None:
            self._pinned_bitrate = None
            # Reset decision streaks so the adaptive loop starts from a
            # neutral verdict rather than reacting to whatever load
            # built up while the user was holding it pinned.
            self._pressure_streak = 0
            self._headroom_streak = 0
            return self.status()
        clamped = max(H264_BITRATE_FLOOR,
                      min(H264_BITRATE_CEILING, int(target)))
        self._pinned_bitrate = clamped
        self._target_bitrate = clamped
        # Bypass adjust_bitrate()'s 25%-restart threshold: when the
        # user pins a value we want it applied exactly, even for a
        # small delta (e.g. 5 Mbps → 4 Mbps). The threshold exists to
        # avoid IDR-gap thrashing under the *adaptive* loop, which
        # this code path is overriding.
        if clamped != self.bitrate:
            self.bitrate = clamped
            self._bitrate_changes += 1
            proc = self._current_proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        return self.status()

    def _sample_link_pressure(self) -> Dict[str, float]:
        """Snapshot the worst-case queue depth and reported WS buffer
        across all current subscribers. Stale heartbeats (older than
        :data:`H264_STATS_FRESH_SECS`) are ignored so a viewer that quietly
        wandered off doesn't pin the bit-rate down forever."""
        max_qd = 0
        for q in list(self._subs):
            try:
                qs = q.qsize()
            except NotImplementedError:  # pragma: no cover — defensive
                qs = 0
            if qs > max_qd:
                max_qd = qs
        max_buf = 0.0
        now = time.time()
        for st in list(self._sub_stats.values()):
            if not st["last_report"]:
                continue
            if now - st["last_report"] > H264_STATS_FRESH_SECS:
                continue
            if st["buffered_amount"] > max_buf:
                max_buf = st["buffered_amount"]
        return {"queue": float(max_qd), "buffered": max_buf}

    async def _adjust_bitrate_loop(self) -> None:
        """Periodic feedback loop: every :data:`H264_ADJUST_INTERVAL`
        seconds, look at the worst subscriber's queue depth + reported WS
        ``bufferedAmount``, classify the link as pressured / clean /
        neutral, and after enough consecutive samples either halve or
        +50% the target bit-rate. ``adjust_bitrate`` decides whether the
        change is large enough to actually restart screenrecord."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(),
                                        timeout=H264_ADJUST_INTERVAL)
                return  # _stop fired
            except asyncio.TimeoutError:
                pass
            if self._pinned_bitrate is not None:
                # User pinned the bit-rate from the viewer toolbar —
                # respect their choice and skip the adapt cycle entirely.
                # Re-apply on each tick so the encoder stays at the
                # pinned value even if some other code path nudged the
                # target (defensive: today nothing else does, but this
                # is the only place that owns the rate).
                if self.bitrate != self._pinned_bitrate:
                    self.adjust_bitrate(self._pinned_bitrate)
                continue
            if not self._subs:
                # No viewers — let the streaks decay so the next viewer
                # starts from a neutral state rather than a stale verdict.
                self._pressure_streak = max(0, self._pressure_streak - 1)
                self._headroom_streak = max(0, self._headroom_streak - 1)
                continue
            sample = self._sample_link_pressure()
            high = (sample["queue"] >= H264_PRESSURE_QUEUE
                    or sample["buffered"] >= H264_PRESSURE_BUFFERED)
            low = (sample["queue"] <= H264_HEADROOM_QUEUE
                   and sample["buffered"] <= H264_HEADROOM_BUFFERED)
            if high:
                self._pressure_streak += 1
                self._headroom_streak = 0
            elif low:
                self._headroom_streak += 1
                self._pressure_streak = 0
            else:
                self._pressure_streak = max(0, self._pressure_streak - 1)
                self._headroom_streak = max(0, self._headroom_streak - 1)

            if self._pressure_streak >= H264_PRESSURE_STREAK:
                # Halve, but don't drop below the floor; reset the
                # streak so we don't keep clobbering the encoder.
                new_target = max(H264_BITRATE_FLOOR,
                                 int(self._target_bitrate * 0.5))
                self._pressure_streak = 0
                if new_target != self._target_bitrate:
                    self.adjust_bitrate(new_target)
            elif self._headroom_streak >= H264_HEADROOM_STREAK:
                new_target = min(H264_BITRATE_CEILING,
                                 int(self._target_bitrate * 1.5))
                self._headroom_streak = 0
                if new_target != self._target_bitrate:
                    self.adjust_bitrate(new_target)

    # ------------- PNG mode -------------

    async def _capture_once(self) -> Optional[bytes]:
        """Run one `adb exec-out screencap -p` and return the PNG bytes."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "exec-out", "screencap", "-p",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self.last_error = "adb is not installed"
            return None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SCREENCAP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            self.last_error = "screencap timed out"
            return None
        if proc.returncode != 0 or not stdout:
            msg = stderr.decode("utf-8", errors="replace").strip()
            self.last_error = (msg or "screencap failed")[:200]
            return None
        if len(stdout) > MAX_FRAME_BYTES:
            self.last_error = "frame too large"
            return None
        # Quick PNG-magic sanity check; older adb shells used to mangle
        # CR/LF in `exec-out` output. Modern adb avoids that, but it costs
        # us nothing to drop frames that aren't valid PNGs.
        if not stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            self.last_error = "screencap output is not a PNG"
            return None
        return stdout

    async def _run_png(self) -> None:
        if not shutil.which("adb"):
            self.last_error = "adb is not installed"
            return
        backoff = 0.0
        while not self._stop.is_set():
            interval = 1.0 / self.fps
            t0 = time.monotonic()
            png = await self._capture_once()
            if png is not None:
                self.frame_count += 1
                self.last_frame_at = time.time()
                self.last_error = ""
                self._latest = png
                backoff = 0.0
                self._fanout(png)
            else:
                self.error_count += 1
                # Exponential backoff up to 2s on consecutive errors so we
                # don't hammer adb after the phone disconnected.
                backoff = min(2.0, max(0.5, backoff * 2 if backoff else 0.5))
            elapsed = time.monotonic() - t0
            sleep_t = max(backoff, interval - elapsed)
            if sleep_t > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_t)
                except asyncio.TimeoutError:
                    pass

    # ------------- H.264 mode -------------

    async def _run_h264(self) -> None:
        if not shutil.which("adb"):
            self.last_error = "adb is not installed"
            return
        backoff = 0.0
        while not self._stop.is_set():
            ok = await self._run_screenrecord_once()
            if ok:
                # Successful run hit the time-limit cap and exited
                # cleanly; restart immediately so playback continues.
                backoff = 0.0
            else:
                self.error_count += 1
                backoff = min(2.0, max(0.5, backoff * 2 if backoff else 0.5))
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff or 0.05)
            except asyncio.TimeoutError:
                pass

    async def _run_screenrecord_once(self) -> bool:
        """Spawn one screenrecord invocation and pump its stdout into
        access units. Returns True on a clean exit (so caller can
        immediately restart), False on error / stall."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "exec-out", "screenrecord",
                "--output-format=h264",
                f"--time-limit={SCREENRECORD_RESTART_SECS}",
                f"--bit-rate={self.bitrate}",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self.last_error = "adb is not installed"
            return False

        # Expose the process so the adaptive-bitrate loop can terminate
        # it to apply a new --bit-rate. Termination is treated like the
        # normal time-limit exit (clean_exit=True) so the outer loop
        # restarts immediately with the updated value.
        self._current_proc = proc
        leftover = b""
        pending_au: List[bytes] = []
        clean_exit = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(H264_READ_CHUNK),
                        timeout=H264_STALL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    self.last_error = "screenrecord stalled"
                    break
                if not chunk:
                    # screenrecord exited — that's the normal time-limit
                    # path. Flush any pending AU and signal clean restart.
                    if pending_au:
                        self._emit_au(pending_au)
                        pending_au.clear()
                    clean_exit = True
                    break
                buf = leftover + chunk
                # Cap absolute buffer size to protect against runaway input.
                if len(buf) > MAX_LEFTOVER_BYTES:
                    self.last_error = "h264 buffer overrun"
                    break
                nals, leftover = _split_annexb(buf)
                for nal in nals:
                    self._handle_nal(nal, pending_au)
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
            # Drain stderr (best effort) for diagnostic info.
            if not clean_exit and proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(proc.stderr.read(),
                                                  timeout=0.2)
                    if err:
                        msg = err.decode("utf-8", errors="replace").strip()
                        if msg:
                            self.last_error = msg[:200]
                except asyncio.TimeoutError:
                    pass
            self._current_proc = None
        return clean_exit

    def _handle_nal(self, nal: bytes, pending_au: List[bytes]) -> None:
        """Dispatch a single NAL: cache parameter sets, batch into
        access units, and emit each AU as soon as it's complete."""
        t = _nal_type(nal)
        if t == 7:  # SPS
            self._sps = nal
            cs = _derive_codec_string(nal)
            if cs:
                self._codec_string = cs
            pending_au.append(nal)
        elif t == 8:  # PPS
            self._pps = nal
            pending_au.append(nal)
        elif t in (1, 5):  # non-IDR slice / IDR slice
            pending_au.append(nal)
            self._emit_au(pending_au)
            pending_au.clear()
        elif t == 9:  # AUD: explicit access-unit boundary
            if pending_au:
                self._emit_au(pending_au)
                pending_au.clear()
            # Don't include AUD itself in the next AU (it's just a marker).
        else:
            # SEI (6), filler (12), other — keep with current AU.
            pending_au.append(nal)

    def _emit_au(self, nals: List[bytes]) -> None:
        if not nals:
            return
        au = NAL_START_CODE + (NAL_START_CODE.join(nals))
        if len(au) > MAX_AU_BYTES:
            return
        is_keyframe = any(_nal_type(n) == 5 for n in nals)
        self.frame_count += 1
        self.last_frame_at = time.time()
        self.last_error = ""
        if is_keyframe:
            self._latest_keyframe_au = au
        self._fanout(au)

    # ------------- Shared fan-out -------------

    def _fanout(self, payload: bytes) -> None:
        """Push ``payload`` to every subscriber. Drops the oldest queued
        item when a queue is full so one slow viewer can't stall the
        capture pipeline for the others."""
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


# ---------- Module-level registry ----------

_sessions: Dict[str, CaptureSession] = {}
_registry_lock = asyncio.Lock()


async def start_session(serial: str, fps: int = DEFAULT_FPS,
                         mode: str = MODE_PNG,
                         bitrate: int = DEFAULT_H264_BITRATE) -> CaptureSession:
    """Get-or-create a capture session for the given serial.

    If an existing session is running with a different mode (PNG ⇄ H.264)
    we tear it down and spin up a fresh one — the two pipelines own
    incompatible on-device resources (screencap vs screenrecord) and
    can't share state.
    """
    if mode not in VALID_MODES:
        mode = MODE_PNG
    async with _registry_lock:
        existing = _sessions.get(serial)
        if existing and existing.is_running() and existing.mode == mode:
            existing.set_fps(fps)
            return existing
        old = _sessions.pop(serial, None) if existing else None
    if old is not None:
        try:
            await old.stop()
        except Exception:  # noqa: BLE001
            pass
    sess = CaptureSession(serial, fps=fps, mode=mode, bitrate=bitrate)
    async with _registry_lock:
        _sessions[serial] = sess
    await sess.start()
    return sess


async def stop_session(serial: str) -> bool:
    async with _registry_lock:
        sess = _sessions.pop(serial, None)
    if sess is None:
        return False
    await sess.stop()
    return True


async def stop_all() -> None:
    async with _registry_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for sess in sessions:
        try:
            await sess.stop()
        except Exception:  # noqa: BLE001
            pass


def get_session(serial: str) -> Optional[CaptureSession]:
    return _sessions.get(serial)


def list_sessions() -> List[Dict]:
    return [s.status() for s in _sessions.values()]
