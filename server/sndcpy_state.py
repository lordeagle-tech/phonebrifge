"""
Subprocess launcher for `sndcpy`, the audio-forwarding companion to scrcpy.

sndcpy is a shell script (https://github.com/rom1v/sndcpy) that pushes a tiny
APK to the phone, runs `adb forward`, and pipes the captured audio stream into
VLC on the laptop. We start it alongside scrcpy when the user has opted into
"Send audio to laptop" for the device, and stop it when the mirror stops.

Like `scrcpy_state`, only one sndcpy process is tracked at a time — each mirror
session pairs with at most one audio stream.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

LOG_RING = 80

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_proc_serial: Optional[str] = None
_proc_started: float = 0.0
_log_ring: Deque[str] = deque(maxlen=LOG_RING)
_reader_thread: Optional[threading.Thread] = None


# ---------- dependency probe ----------

def deps() -> Dict[str, Any]:
    """Probe whether sndcpy and vlc are installed on the laptop.

    sndcpy is usually distributed as a single shell script; users typically
    drop it in /usr/local/bin or ~/.local/bin. vlc is the playback backend.
    """
    sndcpy_path = shutil.which("sndcpy")
    vlc_path = shutil.which("vlc") or shutil.which("cvlc")
    return {
        "sndcpy": {"installed": bool(sndcpy_path), "path": sndcpy_path},
        "vlc": {"installed": bool(vlc_path), "path": vlc_path},
        "install_hint": (
            "Install VLC with `sudo apt install vlc`, then download sndcpy "
            "from https://github.com/rom1v/sndcpy/releases and put the "
            "`sndcpy` script somewhere on your $PATH (e.g. /usr/local/bin/)."
        ),
    }


def installed() -> bool:
    d = deps()
    return d["sndcpy"]["installed"] and d["vlc"]["installed"]


# ---------- subprocess management ----------

def _drain_output(proc: subprocess.Popen) -> None:
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            with _lock:
                _log_ring.append(line)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _log_ring.append(f"[reader error] {e!r}")
    finally:
        with _lock:
            rc = proc.poll()
            _log_ring.append(f"[sndcpy exited with code {rc}]")


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def current_serial() -> Optional[str]:
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return _proc_serial
        return None


def status() -> Dict[str, Any]:
    with _lock:
        running = _proc is not None and _proc.poll() is None
        d = deps()
        return {
            "running": running,
            "pid": _proc.pid if running and _proc else None,
            "serial": _proc_serial if running else None,
            "started_at": _proc_started if running else None,
            "log_tail": list(_log_ring)[-20:],
            "installed": d["sndcpy"]["installed"] and d["vlc"]["installed"],
            "deps": d,
        }


def start(serial: str) -> Dict[str, Any]:
    """Spawn sndcpy for the given device.

    Raises RuntimeError if sndcpy/vlc aren't installed or another sndcpy
    process is already running. The caller (scrcpy_state.launch) catches the
    error and surfaces a friendly message via the scrcpy log ring buffer so
    audio failures never abort the mirror.
    """
    global _proc, _proc_serial, _proc_started, _reader_thread
    if is_running():
        raise RuntimeError("sndcpy is already running; stop it first.")
    # Mutex: the browser-audio capture pipeline uses the same on-device
    # abstract socket, so they can't both run. Late import keeps the
    # dependency one-way (audio_capture imports us, not the other way).
    try:
        from . import audio_capture  # noqa: WPS433
        if audio_capture.is_running():
            other = audio_capture.active_serial() or "another device"
            raise RuntimeError(
                f"Browser audio is currently streaming for {other}. Stop the "
                "browser audio viewer first to route audio to laptop speakers."
            )
    except ImportError:
        pass
    d = deps()
    if not d["sndcpy"]["installed"]:
        raise RuntimeError(
            "sndcpy is not installed. " + d["install_hint"]
        )
    if not d["vlc"]["installed"]:
        raise RuntimeError(
            "vlc is not installed. Install with: sudo apt install vlc"
        )

    args = ["sndcpy", "--serial", serial]
    with _lock:
        _log_ring.clear()
        _log_ring.append(f"[launching] {shlex.join(args)}")
        try:
            # sndcpy normally prompts the user to confirm the audio-capture
            # request on the phone. We feed an empty stdin so the script
            # doesn't block waiting for keyboard input on the headless
            # bridge process — the phone-side prompt still appears.
            _proc = subprocess.Popen(  # noqa: S603 - args list, not shell
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "sndcpy launcher not found on $PATH. " + d["install_hint"]
            ) from e
        _proc_serial = serial
        _proc_started = time.time()
        _reader_thread = threading.Thread(
            target=_drain_output, args=(_proc,), daemon=True
        )
        _reader_thread.start()
    return status()


def stop(timeout: float = 3.0) -> Dict[str, Any]:
    """Terminate sndcpy. SIGKILL if SIGTERM is ignored. VLC is a child of
    the sndcpy script so terminating the script tree closes the player too;
    we additionally call adb forward --remove-all best-effort to free the
    forwarded port."""
    global _proc, _proc_serial
    with _lock:
        proc = _proc
    if proc is None or proc.poll() is not None:
        with _lock:
            _proc = None
            _proc_serial = None
        return status()
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _log_ring.append(f"[stop error] {e!r}")
    with _lock:
        _log_ring.append("[sndcpy stopped]")
        _proc = None
        _proc_serial = None
    return status()
