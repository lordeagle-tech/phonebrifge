"""
Quick Wi-Fi link probe used by the scrcpy auto-tuner.

Given an adb serial, this resolves the phone's IPv4 address (directly for
wireless `host:port` serials, via `adb shell ip ...` for USB serials) and
measures the round-trip latency + packet loss to it. If ICMP is restricted
(common in hardened Wi-Fi networks) it falls back to timing TCP handshakes
to the adb-over-TCP port.

Designed to return in well under a second on a healthy LAN so we don't add
visible lag to the mirror launch flow. All operations are synchronous so
the function can be called from inside `scrcpy_state.launch()`; the caller
should run it via `asyncio.to_thread` from async contexts.
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, Optional

PING_BIN = "ping"
PING_TIMEOUT = 2.5     # overall budget for the ping subprocess
PING_COUNT = 3         # 3 echoes is enough to estimate loss / mean RTT
TCP_TIMEOUT = 0.8      # per-attempt budget for the TCP fallback
TCP_ATTEMPTS = 2       # two TCP handshakes for the average
# Hard wall-clock budget for the whole probe (resolve + ping + TCP). Aligned
# with the task intent of "<1s on a healthy LAN" — degraded paths bail out
# at this budget instead of stacking subprocess timeouts on top of each
# other and adding visible launch lag.
TOTAL_BUDGET_S = 1.5

_LOSS_RE = re.compile(r"(\d+(?:\.\d+)?)% packet loss")
_RTT_RE = re.compile(r"rtt [^=]*=\s*[\d.]+/([\d.]+)/[\d.]+/")
# Extract the first IPv4 from `inet 192.168.1.42/24 ...` lines emitted by
# Android's `ip -f inet addr show`.
_IPV4_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")
# Match `host:port` style adb serials (TCP transport). USB serials don't
# contain a colon.
_WIRELESS_RE = re.compile(r"^([A-Za-z0-9._-]{1,253}):(\d{1,5})$")


def _parse_host_port(serial: str) -> Optional[tuple[str, int]]:
    m = _WIRELESS_RE.match(serial or "")
    if not m:
        return None
    try:
        port = int(m.group(2))
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return m.group(1), port


def _ip_for_usb_serial(serial: str) -> Optional[str]:
    """Best-effort: ask the device for its wlan0 IPv4 via adb shell."""
    if not shutil.which("adb"):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - args list, not shell
            ["adb", "-s", serial, "shell", "ip", "-f", "inet", "addr",
             "show", "wlan0"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    m = _IPV4_RE.search(proc.stdout or "")
    return m.group(1) if m else None


def resolve_ip(serial: str) -> Optional[str]:
    """Return the phone's IPv4 address, or None if unknown."""
    hp = _parse_host_port(serial)
    if hp:
        return hp[0]
    return _ip_for_usb_serial(serial)


def _ping(ip: str) -> Optional[Dict[str, Any]]:
    """Run a short ICMP burst; return {rtt_ms, loss_pct, source} or None."""
    if not shutil.which(PING_BIN):
        return None
    # `-c N` count, `-i 0.2` 200ms spacing, `-W 1` per-reply 1s, `-w 2`
    # overall deadline 2s. -n skips reverse-DNS (fast + no surprises).
    cmd = [PING_BIN, "-c", str(PING_COUNT), "-i", "0.2", "-W", "1",
           "-w", "2", "-n", ip]
    try:
        proc = subprocess.run(  # noqa: S603 - args list, not shell
            cmd, capture_output=True, text=True, timeout=PING_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = proc.stdout or ""
    loss_m = _LOSS_RE.search(out)
    rtt_m = _RTT_RE.search(out)
    if not loss_m:
        # Couldn't parse stats — treat as unusable so we fall back to TCP.
        return None
    loss_pct = float(loss_m.group(1))
    rtt_ms: Optional[float] = float(rtt_m.group(1)) if rtt_m else None
    if rtt_ms is None and loss_pct >= 100.0:
        # 100% loss with no RTT line — the host is unreachable via ICMP.
        return None
    return {
        "rtt_ms": rtt_ms if rtt_ms is not None else 9999.0,
        "loss_pct": loss_pct,
        "source": "ping",
    }


def _tcp_probe(ip: str, port: int) -> Optional[Dict[str, Any]]:
    """Time TCP handshakes as an ICMP-blocked fallback."""
    samples: list[float] = []
    failures = 0
    for _ in range(TCP_ATTEMPTS):
        t0 = time.monotonic()
        try:
            with socket.create_connection((ip, port), timeout=TCP_TIMEOUT):
                samples.append((time.monotonic() - t0) * 1000.0)
        except OSError:
            failures += 1
    total = len(samples) + failures
    if total == 0:
        return None
    if not samples:
        return {"rtt_ms": 9999.0, "loss_pct": 100.0, "source": "tcp"}
    avg = sum(samples) / len(samples)
    loss = (failures / total) * 100.0
    return {"rtt_ms": avg, "loss_pct": loss, "source": "tcp"}


def probe(serial: str) -> Optional[Dict[str, Any]]:
    """Measure RTT + loss to the phone behind `serial`.

    Returns {"rtt_ms": float, "loss_pct": float, "source": "ping"|"tcp",
             "ip": str} or None if the phone IP cannot be determined / no
    measurement could be made. Bounded by `TOTAL_BUDGET_S`: if resolving
    the IP already exceeded the budget, the TCP fallback is skipped so the
    launch flow doesn't accumulate visible lag.
    """
    started = time.monotonic()
    ip = resolve_ip(serial)
    if not ip:
        return None
    result = _ping(ip)
    if result is None and (time.monotonic() - started) < TOTAL_BUDGET_S:
        # Pick the right TCP port — for wireless serials use the adb port we
        # already know about; for USB fall back to the standard 5555.
        hp = _parse_host_port(serial)
        port = hp[1] if hp else 5555
        result = _tcp_probe(ip, port)
    if result is None:
        return None
    result["ip"] = ip
    return result
