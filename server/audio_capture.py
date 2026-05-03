"""
Headless phone audio capture for the browser-based mirror viewer.

Reuses the sndcpy server APK (https://github.com/rom1v/sndcpy) to capture
the phone's *output* audio stream and fan it out to WebSocket subscribers
in raw 16-bit signed little-endian 48 kHz stereo PCM. Each device serial
gets at most one AudioCaptureSession.

The pipeline is the same one sndcpy's shell launcher uses, just expressed
in Python so we can intercept the PCM stream:

  1. `adb push <sndcpy.apk> /data/local/tmp/sndcpy.jar`
  2. `adb forward tcp:0 localabstract:sndcpy`           (capture port)
  3. `adb shell CLASSPATH=/data/local/tmp/sndcpy.jar app_process / com.rom1v.sndcpy.Server`
  4. open a TCP connection to localhost:<port>, read PCM, fan out

Mutex: the phone exposes the audio stream on a single abstract Unix
socket (`localabstract:sndcpy`) — only one consumer can attach at a
time. So this module is mutually exclusive with `sndcpy_state` (the
"play audio on laptop speakers" toggle). The HTTP layer enforces the
mutex with a 409 in either direction.

Independent of `phone_capture.py` so audio and video sessions can run
side by side or alone.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Set

# sndcpy's server emits raw signed 16-bit little-endian PCM, stereo, 48 kHz.
SAMPLE_RATE = 48000
CHANNELS = 2
BIT_DEPTH = 16
BYTES_PER_SAMPLE = (BIT_DEPTH // 8) * CHANNELS

# Read in ~40 ms chunks so latency stays low without churning the WS.
READ_CHUNK_BYTES = int(SAMPLE_RATE * BYTES_PER_SAMPLE * 0.04)  # 7680 bytes
MAX_SUBSCRIBERS = 4
QUEUE_HIGHWATER = 8  # ~320 ms of buffered audio before drop-oldest
ABSTRACT_SOCKET = "sndcpy"
DEVICE_JAR = "/data/local/tmp/sndcpy.jar"
ADB_TIMEOUT = 8.0
SERVER_BOOT_WAIT = 2.0  # mirror sndcpy.sh: it sleeps ~2 s before nc

_PORT_LINE_RE = re.compile(r"^\s*(\d{1,5})\s*$")


def _locate_sndcpy_apk() -> Optional[str]:
    """Find sndcpy.apk next to the sndcpy launcher script.

    sndcpy is distributed as a shell script + APK that live in the same
    directory; the script does `DIR=$(dirname "$(readlink -f "$0")")`
    and pushes `$DIR/sndcpy.apk`. We mirror that lookup so we don't have
    to ship our own copy of the APK.
    """
    sn = shutil.which("sndcpy")
    if not sn:
        return None
    sn_real = os.path.realpath(sn)
    apk = os.path.join(os.path.dirname(sn_real), "sndcpy.apk")
    if os.path.isfile(apk):
        return apk
    return None


def deps() -> Dict[str, Any]:
    """Probe what's needed to run a browser-audio session."""
    apk = _locate_sndcpy_apk()
    return {
        "adb": bool(shutil.which("adb")),
        "sndcpy_apk": apk,
        "ready": bool(apk and shutil.which("adb")),
        "install_hint": (
            "Install sndcpy from https://github.com/rom1v/sndcpy/releases "
            "and put both `sndcpy` and `sndcpy.apk` somewhere on $PATH "
            "(e.g. /usr/local/bin/) so they sit side-by-side."
        ),
    }


class AudioCaptureSession:
    """One running audio-capture pipeline bound to a single device serial."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.started_at = time.time()
        self.last_chunk_at: float = 0.0
        self.bytes_streamed: int = 0
        self.last_error: str = ""
        self.local_port: Optional[int] = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()  # set once the TCP stream is open
        self._startup_failed = False
        self._task: Optional[asyncio.Task] = None
        self._server_proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._subs: Set["asyncio.Queue[bytes]"] = set()
        self._sub_lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "serial": self.serial,
            "started_at": self.started_at,
            "last_chunk_at": self.last_chunk_at,
            "bytes_streamed": self.bytes_streamed,
            "subscribers": self.subscriber_count,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "bit_depth": BIT_DEPTH,
            "format": "pcm_s16le",
            "local_port": self.local_port,
            "last_error": self.last_error,
        }

    async def add_subscriber(self) -> Optional["asyncio.Queue[bytes]"]:
        async with self._sub_lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                return None
            q: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=QUEUE_HIGHWATER)
            self._subs.add(q)
        return q

    async def remove_subscriber(self, q: "asyncio.Queue[bytes]") -> None:
        async with self._sub_lock:
            self._subs.discard(q)

    # ---------- pipeline setup ----------

    async def _adb(self, *args: str, timeout: float = ADB_TIMEOUT,
                   capture: bool = True) -> Dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, *args,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return {"ok": False, "stdout": "", "stderr": str(e)}
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"ok": False, "stdout": "",
                    "stderr": f"adb {args[0]} timed out"}
        return {
            "ok": proc.returncode == 0,
            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
        }

    async def _push_apk(self, apk_path: str) -> bool:
        r = await self._adb("push", apk_path, DEVICE_JAR, timeout=20.0)
        if not r["ok"]:
            self.last_error = (r["stderr"] or r["stdout"] or "adb push failed").strip()[:300]
            return False
        return True

    async def _setup_forward(self) -> Optional[int]:
        # `adb forward tcp:0 localabstract:sndcpy` allocates a free port
        # and prints it on stdout. Anything else is an error.
        r = await self._adb("forward", "tcp:0", f"localabstract:{ABSTRACT_SOCKET}")
        if not r["ok"]:
            self.last_error = (r["stderr"] or r["stdout"] or "adb forward failed").strip()[:300]
            return None
        line = r["stdout"].strip().splitlines()[-1] if r["stdout"].strip() else ""
        m = _PORT_LINE_RE.match(line)
        if not m:
            self.last_error = f"could not parse forwarded port from: {line!r}"
            return None
        try:
            port = int(m.group(1))
        except ValueError:
            self.last_error = "invalid forwarded port"
            return None
        if not (1 <= port <= 65535):
            self.last_error = f"forwarded port out of range: {port}"
            return None
        return port

    async def _remove_forward(self) -> None:
        # Best effort — never raise.
        try:
            await self._adb("forward", "--remove",
                            f"tcp:{self.local_port}", timeout=4.0)
        except Exception:  # noqa: BLE001
            pass

    async def _spawn_server(self) -> bool:
        """Spawn the device-side sndcpy server via `adb shell app_process`."""
        try:
            self._server_proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.serial, "shell",
                f"CLASSPATH={DEVICE_JAR}",
                "app_process", "/", "com.rom1v.sndcpy.Server",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            self.last_error = f"adb missing: {e}"
            return False
        # Don't await — the server runs until killed. Just give it a
        # moment to bind the abstract socket before we connect.
        await asyncio.sleep(SERVER_BOOT_WAIT)
        if self._server_proc.returncode is not None:
            # Server died immediately — likely user denied the permission
            # prompt on the phone, or the APK isn't compatible.
            try:
                out = (await self._server_proc.stdout.read()).decode(
                    "utf-8", errors="replace") if self._server_proc.stdout else ""
            except Exception:  # noqa: BLE001
                out = ""
            self.last_error = (out.strip() or
                "sndcpy server exited immediately. Tap Allow on the phone "
                "permission prompt and try again."
            )[:300]
            return False
        return True

    # ---------- main loop ----------

    async def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"audio-capture:{self.serial}",
        )

    async def stop(self) -> None:
        # The _run() task is responsible for tearing down its own
        # resources via the finally-block; we just signal it and wait.
        # Belt-and-braces: also close the TCP socket to unblock any
        # pending read, and unblock any subscriber stuck in queue.get().
        self._stop.set()
        # Make sure callers waiting on _ready don't deadlock if we're
        # stopped before the stream ever opened.
        self._ready.set()
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        # Sentinel after the task has finished its own cleanup so any
        # WS subscribers still parked on queue.get() unblock and close.
        await self._notify_subscribers_ended()

    async def _notify_subscribers_ended(self) -> None:
        """Push the empty-sentinel to every subscriber so the WS handler
        can flush its `ended` JSON message and close. Drains queues that
        are already full so the sentinel is never silently dropped."""
        for q in list(self._subs):
            # Make room for the sentinel even if the queue is full.
            while q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                q.put_nowait(b"")
            except asyncio.QueueFull:
                pass

    async def _teardown(self) -> None:
        """Tear down everything _run() may have allocated. Idempotent
        and never raises — used in the finally-block of _run() so a
        failed startup or mid-stream exception still releases the
        on-device server, the adb forward, and any blocked subscribers.
        """
        # 1. Kill the on-device sndcpy server first so the abstract
        #    socket is freed before we drop the forward.
        if self._server_proc is not None and self._server_proc.returncode is None:
            try:
                self._server_proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._server_proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    self._server_proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._server_proc.wait(), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        # 2. Close the TCP read side so a blocked read() returns.
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        # 3. Drop the local-port forward.
        if self.local_port is not None:
            await self._remove_forward()
            self.local_port = None
        # 4. Wake every WS subscriber with the end sentinel so they emit
        #    a clean `ended` to the browser instead of stalling on get().
        await self._notify_subscribers_ended()
        # 5. Make sure nobody's left waiting on _ready (failed startup).
        self._ready.set()
        # 6. Drop ourselves from the module registry so a subsequent
        #    start_session() builds a fresh session instead of reusing
        #    a dead one.
        async with _registry_lock:
            if _sessions.get(self.serial) is self:
                _sessions.pop(self.serial, None)

    async def _run(self) -> None:
        try:
            if not shutil.which("adb"):
                self.last_error = "adb is not installed"
                self._startup_failed = True
                return
            apk = _locate_sndcpy_apk()
            if not apk:
                self.last_error = (
                    "sndcpy.apk not found on disk. Install sndcpy "
                    "(sndcpy + sndcpy.apk side-by-side on $PATH)."
                )
                self._startup_failed = True
                return
            if not await self._push_apk(apk):
                self._startup_failed = True
                return
            port = await self._setup_forward()
            if port is None:
                self._startup_failed = True
                return
            self.local_port = port
            if not await self._spawn_server():
                self._startup_failed = True
                return
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=5.0,
                )
            except (asyncio.TimeoutError, OSError) as e:
                self.last_error = f"could not connect to forwarded port {port}: {e}"
                self._startup_failed = True
                return
            # Stream is live — release any caller waiting on start().
            self._ready.set()
            # Pump bytes -> subscribers until socket closes or we're stopped.
            while not self._stop.is_set():
                try:
                    chunk = await self._reader.read(READ_CHUNK_BYTES)
                except (asyncio.CancelledError, ConnectionError):
                    break
                if not chunk:
                    break
                self.bytes_streamed += len(chunk)
                self.last_chunk_at = time.time()
                self.last_error = ""
                # Drop oldest if a viewer can't keep up — one stalled tab
                # can't be allowed to back up the capture pipeline.
                for q in list(self._subs):
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        pass
        finally:
            await self._teardown()


# ---------- Module-level registry ----------

_sessions: Dict[str, AudioCaptureSession] = {}
_registry_lock = asyncio.Lock()


def is_running(serial: Optional[str] = None) -> bool:
    """If `serial` is given, return whether *that* device has a session.
    Otherwise return whether *any* device has one."""
    if serial is not None:
        s = _sessions.get(serial)
        return bool(s and s.is_running())
    return any(s.is_running() for s in _sessions.values())


def active_serial() -> Optional[str]:
    for s in _sessions.values():
        if s.is_running():
            return s.serial
    return None


def get_session(serial: str) -> Optional[AudioCaptureSession]:
    return _sessions.get(serial)


def list_sessions() -> List[Dict[str, Any]]:
    return [s.status() for s in _sessions.values()]


async def start_session(serial: str) -> AudioCaptureSession:
    """Get-or-create an audio capture session for the given serial.

    Mutex: refuse if `sndcpy_state` already owns the on-device audio
    socket for any device — both consumers can't share the abstract
    socket. The caller surfaces the conflict as a 409.
    """
    # Late import to avoid a circular dependency with sndcpy_state.
    from . import sndcpy_state  # noqa: WPS433
    if sndcpy_state.is_running():
        raise RuntimeError(
            "Phone audio is already routed to laptop speakers (sndcpy is "
            "running). Turn off 'Send audio to laptop' for the device or "
            "stop the mirror first to use browser audio."
        )
    async with _registry_lock:
        existing = _sessions.get(serial)
        if existing is not None and existing.is_running():
            # Reuse: either it's fully online (return immediately
            # below) or it's still booting (wait on the same _ready
            # event below). Either way, do NOT spawn a second pipeline
            # for the same serial — they would race for the on-device
            # abstract socket and clobber each other.
            sess = existing
            spawned = False
        else:
            # Replace any stale session object that's no longer running
            # so callers don't get a half-dead handle back.
            if existing is not None:
                _sessions.pop(serial, None)
            sess = AudioCaptureSession(serial)
            _sessions[serial] = sess
            spawned = True
    if spawned:
        await sess.start()
    # Wait for the pipeline to either come fully online or fail. We
    # cap the wait at ~30 s — push + forward + server boot + first
    # connect typically takes 3-6 s, longer when the user has to tap
    # "Allow" on the phone-side capture prompt.
    try:
        await asyncio.wait_for(sess._ready.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        await sess.stop()
        raise RuntimeError(
            "Audio capture did not start in time. If the phone showed an "
            "'Allow audio capture' prompt, tap Allow and try again."
        )
    if sess._startup_failed or not sess.is_running():
        # _teardown() already cleaned up; surface the underlying error.
        msg = sess.last_error or "Audio capture failed to start."
        # Best-effort: make sure the registry is empty for this serial.
        async with _registry_lock:
            if _sessions.get(serial) is sess:
                _sessions.pop(serial, None)
        raise RuntimeError(msg)
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
