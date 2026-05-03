"""
`phone-bridge mirror-status` — quick status check for the scrcpy window.

Sibling to ``cli_mirror`` and ``cli_stop_mirror``: hits ``/api/scrcpy/status``
over loopback with the same CLI token and prints a one-line summary.

Exit code 0 when a scrcpy window is running, non-zero otherwise so the
command composes nicely in shell scripts:

    if phone-bridge mirror-status >/dev/null; then ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from typing import List, Optional

from cli_api import load_token, request as _request, resolve_base as _resolve_base


def _format_uptime(started_at: Optional[float]) -> str:
    if not started_at:
        return "00:00:00"
    secs = max(0, int(time.time() - float(started_at)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _friendly_name(base: str, token: str, serial: str) -> Optional[str]:
    """Look up the human-friendly device label from the recents list."""
    try:
        resp = _request("GET", f"{base}/api/scrcpy/recent", token, timeout=5.0)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None
    for entry in resp.get("recent") or []:
        if entry.get("serial") == serial:
            name = entry.get("name")
            if name and name != serial:
                return str(name)
            return None
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phone-bridge mirror-status",
        description="Print whether a scrcpy window is currently running, "
                    "for which device, and for how long.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bridge host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765,
                        help="Bridge port (default: 8765).")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Emit the raw status payload as a single-line "
                             "JSON object (running, serial, pid, started_at, "
                             "uptime_seconds) for scripting.")
    args = parser.parse_args(argv)

    token = load_token()
    base = _resolve_base(args.host, args.port, token)

    try:
        st = _request("GET", f"{base}/api/scrcpy/status", token)
    except urllib.error.HTTPError as e:
        if args.as_json:
            print(json.dumps({
                "running": False,
                "serial": None,
                "pid": None,
                "started_at": None,
                "uptime_seconds": 0,
                "error": f"{e.code} {e.reason}",
            }))
        else:
            print(f"Failed to read mirror status: {e.code} {e.reason}",
                  file=sys.stderr)
        return 1

    running = bool(st.get("running"))
    serial = st.get("serial")
    pid = st.get("pid")
    started_at = st.get("started_at")
    uptime_seconds = (
        max(0, int(time.time() - float(started_at))) if started_at else 0
    )

    if args.as_json:
        payload = {
            "running": running,
            "serial": serial,
            "pid": pid,
            "started_at": started_at,
            "uptime_seconds": uptime_seconds,
        }
        print(json.dumps(payload))
        return 0 if running else 1

    if not running:
        print("No scrcpy window running.")
        return 1

    serial_str = serial or "?"
    uptime = _format_uptime(started_at)
    name = _friendly_name(base, token, serial_str) if serial_str != "?" else None
    label = f"{name} (serial {serial_str})" if name else serial_str
    pid_part = f"pid {pid}" if pid is not None else "pid ?"
    print(f"Mirroring {label} — {pid_part}, up {uptime}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
