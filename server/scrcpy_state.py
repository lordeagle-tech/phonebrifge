"""
State for the scrcpy / adb one-click mirror feature.

Holds:
- a small JSON-backed list of "recent devices" (serial + friendly name)
  persisted at ~/.phone-bridge/scrcpy.json so reconnecting is one click
- the currently running scrcpy subprocess (one at a time) plus a small
  ring buffer of its most recent stdout/stderr lines for the UI poll
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import link_probe, sndcpy_state

STATE_DIR = Path.home() / ".phone-bridge"
STATE_FILE = STATE_DIR / "scrcpy.json"
MAX_RECENT = 8
LOG_RING = 200

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_proc_serial: Optional[str] = None
_proc_preset: Optional[str] = None
_proc_started: float = 0.0
_log_ring: Deque[str] = deque(maxlen=LOG_RING)
_reader_thread: Optional[threading.Thread] = None


def _ensure_dir() -> None:
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass


def _load() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"recent": []}


def _save(data: Dict[str, Any]) -> None:
    _ensure_dir()
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def list_recent() -> List[Dict[str, Any]]:
    return list(_load().get("recent", []))


def remember_device(serial: str, name: str = "") -> None:
    """Push a device to the front of the recent list (deduped).

    If `name` is blank and the device is already remembered with a
    friendly name, the existing name is preserved (we never overwrite
    "Pixel 8" with the raw serial just because the caller didn't pass
    a label). Auto-launch settings (`auto`, `auto_preset`) and watcher
    bookkeeping fields are also preserved on update.
    """
    data = _load()
    existing = next((r for r in data.get("recent", []) if r.get("serial") == serial), None)
    if not name and existing:
        name = existing.get("name", "")
    entry: Dict[str, Any] = {
        "serial": serial,
        "name": name or serial,
        "last_used": int(time.time()),
        "auto": bool(existing.get("auto", False)) if existing else False,
        "auto_preset": (existing.get("auto_preset") if existing else None) or "default",
        "last_attempt": existing.get("last_attempt") if existing else None,
        "last_outcome": existing.get("last_outcome") if existing else None,
        "failures": int(existing.get("failures", 0)) if existing else 0,
        # "audio" controls whether launching this device's mirror also starts
        # sndcpy to route phone audio to the laptop. Preserved across updates
        # like the auto-launch flag.
        "audio": bool(existing.get("audio", False)) if existing else False,
        # "quality_override" pins the auto-tuner: "auto" (default) probes
        # the link before launch and picks low/medium/high; "low"/"medium"/
        # "high" force that profile and skip the probe. "last_quality" is
        # the most recent tuning result the UI shows as a "Tuned ·" pill.
        "quality_override": (existing.get("quality_override")
                             if existing else None) or "auto",
        "last_quality": existing.get("last_quality") if existing else None,
    }
    recent = [r for r in data.get("recent", []) if r.get("serial") != serial]
    recent.insert(0, entry)
    data["recent"] = recent[:MAX_RECENT]
    _save(data)


def set_auto(serial: str, enabled: bool, preset: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Toggle the watcher's auto-launch flag for one remembered device.

    Toggling auto on/off resets the failure counter so the user always
    gets a fresh, immediate retry after flipping the switch.
    """
    data = _load()
    target = None
    for r in data.get("recent", []):
        if r.get("serial") == serial:
            prev = bool(r.get("auto", False))
            r["auto"] = bool(enabled)
            if preset:
                r["auto_preset"] = preset
            if prev != bool(enabled):
                r["failures"] = 0
            target = r
            break
    if target is not None:
        _save(data)
    return target


def set_audio(serial: str, enabled: bool) -> Optional[Dict[str, Any]]:
    """Toggle the "send audio to laptop" preference for one remembered device.

    Returns the updated entry, or None if the serial isn't remembered yet.
    Toggling does NOT touch a currently-running mirror — the change applies
    on the next launch (manual or via the auto-watcher).
    """
    data = _load()
    target = None
    for r in data.get("recent", []):
        if r.get("serial") == serial:
            r["audio"] = bool(enabled)
            target = r
            break
    if target is not None:
        _save(data)
    return target


_QUALITY_VALUES = ("auto", "low", "medium", "high")


def set_quality(serial: str, override: str) -> Optional[Dict[str, Any]]:
    """Pin (or unpin) the auto-tuner for one device.

    `override` must be one of "auto" / "low" / "medium" / "high". "auto"
    re-enables the link probe; the others force that profile on every
    launch and skip the probe. Returns the updated entry or None if the
    serial isn't remembered yet.
    """
    if override not in _QUALITY_VALUES:
        raise ValueError(f"quality must be one of {_QUALITY_VALUES}")
    data = _load()
    target = None
    for r in data.get("recent", []):
        if r.get("serial") == serial:
            r["quality_override"] = override
            target = r
            break
    if target is not None:
        _save(data)
    return target


def _record_last_quality(serial: str, info: Dict[str, Any]) -> None:
    """Persist the tuning result on the device entry for the UI pill."""
    data = _load()
    for r in data.get("recent", []):
        if r.get("serial") == serial:
            r["last_quality"] = info
            _save(data)
            return


def update_attempt(serial: str, outcome: str, success: bool = False) -> None:
    """Record the watcher's most recent attempt against a device.

    `success=True` resets the per-device failure counter so future scans
    don't sit in backoff just because the device flaked once. Anything
    else increments it.
    """
    data = _load()
    for r in data.get("recent", []):
        if r.get("serial") == serial:
            r["last_attempt"] = int(time.time())
            r["last_outcome"] = outcome[:200]
            if success:
                r["failures"] = 0
            else:
                r["failures"] = int(r.get("failures", 0)) + 1
            _save(data)
            return


def forget_device(serial: str) -> None:
    data = _load()
    data["recent"] = [r for r in data.get("recent", []) if r.get("serial") != serial]
    _save(data)


# ---------- Quiet hours (auto-launch suppression window) ----------

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _normalize_hhmm(value: Any, default: str) -> str:
    """Coerce to HH:MM 24h string; fall back to `default` if invalid."""
    if isinstance(value, str) and _TIME_RE.match(value):
        return value
    return default


def get_quiet_hours() -> Dict[str, Any]:
    """Return the current quiet-hours setting.

    Shape: {"enabled": bool, "start": "HH:MM", "end": "HH:MM"}.
    The window is interpreted in the server's local time. If start == end the
    window is treated as empty (never quiet). If end < start, the window wraps
    around midnight (e.g. 22:00 → 07:00).
    """
    data = _load()
    raw = data.get("quiet_hours") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "start": _normalize_hhmm(raw.get("start"), "22:00"),
        "end": _normalize_hhmm(raw.get("end"), "07:00"),
    }


def set_quiet_hours(enabled: bool, start: str, end: str) -> Dict[str, Any]:
    """Persist quiet-hours config. Raises ValueError on bad time strings."""
    if not _TIME_RE.match(start or ""):
        raise ValueError("start must be HH:MM (24h)")
    if not _TIME_RE.match(end or ""):
        raise ValueError("end must be HH:MM (24h)")
    data = _load()
    data["quiet_hours"] = {
        "enabled": bool(enabled),
        "start": start,
        "end": end,
    }
    _save(data)
    return get_quiet_hours()


def _hhmm_to_minutes(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(now: Optional[time.struct_time] = None) -> Dict[str, Any]:
    """Are we currently inside the configured quiet window?

    Returns {"quiet": bool, "enabled": bool, "start": str, "end": str}.
    Always safe to call — if the setting is missing or disabled, "quiet"
    is False.
    """
    cfg = get_quiet_hours()
    out = {"quiet": False, **cfg}
    if not cfg["enabled"]:
        return out
    start_m = _hhmm_to_minutes(cfg["start"])
    end_m = _hhmm_to_minutes(cfg["end"])
    if start_m == end_m:
        return out  # empty window
    lt = now or time.localtime()
    cur_m = lt.tm_hour * 60 + lt.tm_min
    if start_m < end_m:
        out["quiet"] = start_m <= cur_m < end_m
    else:
        # Window wraps midnight (e.g. 22:00 → 07:00)
        out["quiet"] = cur_m >= start_m or cur_m < end_m
    return out


# ---------- scrcpy subprocess management ----------

# Quality profiles applied AFTER the preset's flags so that, for repeated
# options like --max-size and --video-bit-rate, scrcpy uses the auto-tuned
# value (it honours the last occurrence on the command line). Tuning is
# only applied to the "default" preset; "low-bitrate" and "record" are
# explicit user choices we leave alone.
QUALITY_PROFILES: Dict[str, List[str]] = {
    "high":   ["--max-size=1920", "--video-bit-rate=12M", "--max-fps=60"],
    "medium": ["--max-size=1280", "--video-bit-rate=6M",  "--max-fps=45"],
    "low":    ["--max-size=800",  "--video-bit-rate=2M",  "--max-fps=30"],
}


def pick_quality(rtt_ms: Optional[float], loss_pct: Optional[float]) -> str:
    """Map a probe result to a quality profile name.

    Pure function — easy to unit-test independently of the probe. Thresholds
    are conservative on purpose: we'd rather drop a notch and stay smooth
    than push for HD on a marginal link.
    """
    if rtt_ms is None or loss_pct is None:
        return "medium"
    if loss_pct >= 5.0 or rtt_ms >= 100.0:
        return "low"
    if loss_pct == 0.0 and rtt_ms < 40.0:
        return "high"
    return "medium"


# Presets that auto-tune accepts. Other presets (record, low-bitrate) bypass
# tuning entirely so the user gets exactly what they asked for.
_TUNABLE_PRESETS = {"default", "fullscreen"}


PRESETS: Dict[str, List[str]] = {
    # Full Android display on the laptop; forward audio + clipboard.
    "fullscreen": [
        "--fullscreen",
        "--audio-codec=opus",
        "--max-size=1280",
        "--video-bit-rate=8M",
    ],
    # Sensible windowed defaults: forward audio + clipboard, fit to window.
    "default": [
        "--audio-codec=opus",
        "--max-size=1280",
        "--video-bit-rate=8M",
    ],
    # Slow-network preset
    "low-bitrate": [
        "--no-audio",
        "--max-size=800",
        "--video-bit-rate=2M",
        "--max-fps=20",
    ],
    # Record to file (timestamped under ~/.phone-bridge/recordings/)
    "record": [
        "--audio-codec=opus",
        "--max-size=1280",
        "--video-bit-rate=8M",
    ],
}


def _drain_output(proc: subprocess.Popen) -> None:
    """Collect interleaved stdout/stderr into the ring buffer until exit."""
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
            _log_ring.append(f"[scrcpy exited with code {rc}]")


def is_running() -> bool:
    with _lock:
        return _proc is not None and _proc.poll() is None


def current_serial() -> Optional[str]:
    # NB: must not call is_running() here — it would re-acquire _lock.
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return _proc_serial
        return None


def status() -> Dict[str, Any]:
    with _lock:
        running = _proc is not None and _proc.poll() is None
        snapshot = {
            "running": running,
            "pid": _proc.pid if running and _proc else None,
            "serial": _proc_serial if running else None,
            "preset": _proc_preset if running else None,
            "started_at": _proc_started if running else None,
            "log_tail": list(_log_ring)[-40:],
        }
    # Read audio status outside the scrcpy lock to avoid nested locking
    # (sndcpy_state has its own lock).
    snapshot["audio"] = sndcpy_state.status()
    return snapshot


def launch(serial: str, preset: str = "default",
           extra_args: Optional[List[str]] = None,
           friendly_name: Optional[str] = None) -> Dict[str, Any]:
    """Spawn scrcpy for the given device. Returns status dict.

    `friendly_name` (optional) is stored alongside the serial in the recent
    devices file so the UI can show a human label like "Pixel 8" instead of
    the raw `192.168.1.42:5555`.
    """
    global _proc, _proc_serial, _proc_preset, _proc_started, _reader_thread
    if is_running():
        raise RuntimeError("scrcpy is already running; stop it first.")
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")

    args: List[str] = ["scrcpy", "--serial", serial]
    args.extend(PRESETS[preset])
    if preset == "record":
        rec_dir = STATE_DIR / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        out = rec_dir / f"phone-{int(time.time())}.mp4"
        args.extend(["--record", str(out)])

    # ---- Auto-tune quality based on link conditions (or override) ----
    # Look up the persisted override BEFORE remember_device() rewrites the
    # entry — that way an explicit "low" set from the UI wins this launch.
    pre_entry = next(
        (r for r in _load().get("recent", []) if r.get("serial") == serial),
        None,
    )
    override = (pre_entry or {}).get("quality_override") or "auto"
    tuning: Optional[Dict[str, Any]] = None
    # Defer log lines until after _log_ring.clear() below; otherwise the
    # clear in the launching block would wipe them out.
    tuning_logs: List[str] = []
    if preset in _TUNABLE_PRESETS:
        if override in ("low", "medium", "high"):
            tuning = {
                "profile": override,
                "rtt_ms": None,
                "loss_pct": None,
                "source": "override",
                "override": True,
            }
        else:
            try:
                probed = link_probe.probe(serial)
            except Exception as e:  # noqa: BLE001 - probe must never crash launch
                probed = None
                tuning_logs.append(f"[tune] probe error: {e!r}")
            if probed:
                profile = pick_quality(probed.get("rtt_ms"),
                                       probed.get("loss_pct"))
                tuning = {
                    "profile": profile,
                    "rtt_ms": probed.get("rtt_ms"),
                    "loss_pct": probed.get("loss_pct"),
                    "source": probed.get("source"),
                    "override": False,
                }
            else:
                # Probe couldn't reach the phone (USB-only with no wlan IP,
                # or ICMP+TCP both blocked). Fall back to "medium" so the
                # mirror still launches with sensible defaults.
                tuning = {
                    "profile": "medium",
                    "rtt_ms": None,
                    "loss_pct": None,
                    "source": "fallback",
                    "override": False,
                }
        args.extend(QUALITY_PROFILES[tuning["profile"]])
        rtt_str = (f"{tuning['rtt_ms']:.0f}ms"
                   if tuning["rtt_ms"] is not None else "n/a")
        loss_str = (f"{tuning['loss_pct']:.0f}%"
                    if tuning["loss_pct"] is not None else "n/a")
        tuning_logs.append(
            f"[tune] profile={tuning['profile']} rtt={rtt_str} "
            f"loss={loss_str} src={tuning['source']}"
        )

    if extra_args:
        args.extend(extra_args)

    with _lock:
        _log_ring.clear()
        for line in tuning_logs:
            _log_ring.append(line)
        _log_ring.append(f"[launching] {shlex.join(args)}")
        try:
            _proc = subprocess.Popen(  # noqa: S603 - args list, not shell
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "scrcpy is not installed. Install with: sudo apt install scrcpy"
            ) from e
        _proc_serial = serial
        _proc_preset = preset
        _proc_started = time.time()
        _reader_thread = threading.Thread(
            target=_drain_output, args=(_proc,), daemon=True
        )
        _reader_thread.start()

    remember_device(serial, name=friendly_name or "")
    # Persist the tuning result so /scrcpy/recent can render the
    # "Tuned · medium · RTT 45ms" pill on the per-device row even after
    # the user closes and reopens the page.
    if tuning is not None:
        _record_last_quality(serial, {**tuning, "ts": int(time.time())})

    # If the device opted into audio forwarding, kick off sndcpy alongside
    # scrcpy. Failures here (sndcpy or VLC missing, port already in use,
    # etc.) are intentionally non-fatal: the silent mirror is still useful,
    # so we just log the reason into the scrcpy ring buffer where the UI's
    # log tail will show it.
    entry = next(
        (r for r in _load().get("recent", []) if r.get("serial") == serial),
        None,
    )
    if entry and entry.get("audio"):
        try:
            sndcpy_state.start(serial)
            with _lock:
                _log_ring.append("[audio] sndcpy launched")
        except RuntimeError as e:
            with _lock:
                _log_ring.append(f"[audio] sndcpy not started: {e}")

    # Surface the tuning result on the launch response so the caller (the
    # mirror-setup page) can flash a toast and update the pill immediately
    # without waiting for the next /scrcpy/recent poll.
    snap = status()
    if tuning is not None:
        snap["tuning"] = tuning
    return snap


def stop(timeout: float = 3.0) -> Dict[str, Any]:
    """Terminate scrcpy cleanly; SIGKILL if it ignores SIGTERM. Also stops
    sndcpy if it was started for this mirror — the two are paired for the
    lifetime of one mirror session."""
    global _proc, _proc_serial
    # Always attempt to stop sndcpy first so the audio stream stops cleanly
    # even if scrcpy already exited (e.g. user closed the scrcpy window).
    try:
        sndcpy_state.stop(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass
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
        _log_ring.append("[scrcpy stopped]")
        _proc = None
        _proc_serial = None
    return status()
