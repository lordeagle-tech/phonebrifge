"""
`phone-bridge mirror` — pop up the native scrcpy window from the terminal.

Talks to the locally-running bridge over loopback, reusing the same
``/api/scrcpy/devices`` and ``/api/scrcpy/launch`` endpoints the web UI
uses. Authenticates with the loopback CLI token the server writes to
``~/.phone-bridge/cli_token`` on startup, so no PIN re-entry is needed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

from cli_api import load_token, request as _request, resolve_base as _resolve_base

# Map the friendly --quality flag to (preset, override). The preset is
# what /scrcpy/launch accepts today; the override (when set) pins the
# auto-tuner via /scrcpy/quality so "high" actually means high, not
# "let the link probe pick something near high".
_QUALITY_MAP: Dict[str, Tuple[str, Optional[str]]] = {
    "auto":   ("default",     "auto"),
    "low":    ("low-bitrate", None),
    "medium": ("default",     "medium"),
    "high":   ("default",     "high"),
}


def _pick_device(devices: List[Dict[str, Any]],
                 wanted_serial: Optional[str]) -> Dict[str, Any]:
    ready = [d for d in devices if d.get("state") == "device"]
    if wanted_serial:
        match = next((d for d in devices if d.get("serial") == wanted_serial),
                     None)
        if match is None:
            raise SystemExit(
                f"No connected device with serial {wanted_serial!r}.\n"
                f"Connected: {[d.get('serial') for d in devices] or 'none'}"
            )
        if match.get("state") != "device":
            raise SystemExit(
                f"Device {wanted_serial} is in state {match.get('state')!r} "
                "— accept the USB-debugging prompt on the phone, or re-pair "
                "via the /mirror-setup page."
            )
        return match
    if not ready:
        raise SystemExit(
            "No phones are paired yet. Open https://<laptop-ip>:8765/mirror-setup "
            "in a browser and pair your phone first."
        )
    if len(ready) == 1:
        return ready[0]
    print("Multiple phones connected — pick one:")
    for i, d in enumerate(ready, 1):
        label = d.get("name") or d.get("serial")
        print(f"  [{i}] {label}  ({d.get('serial')})")
    while True:
        try:
            raw = input("Choose [1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nCancelled.")
        try:
            idx = int(raw)
            if 1 <= idx <= len(ready):
                return ready[idx - 1]
        except ValueError:
            pass
        print(f"Enter a number from 1 to {len(ready)}.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phone-bridge mirror",
        description="Pop up the native scrcpy window for a paired phone.",
    )
    parser.add_argument("serial", nargs="?", default=None,
                        help="ADB serial to mirror (optional; picks the only "
                             "paired phone, or prompts if multiple).")
    parser.add_argument("--quality", choices=list(_QUALITY_MAP.keys()),
                        default=None,
                        help="Pin a quality profile. Default: leave the "
                             "device's saved preference alone.")
    parser.add_argument("--record", action="store_true",
                        help="Record the session to ~/.phone-bridge/recordings/.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bridge host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765,
                        help="Bridge port (default: 8765).")
    args = parser.parse_args(argv)

    if not shutil.which("scrcpy"):
        print("scrcpy is not installed. Install with:\n"
              "  sudo apt install scrcpy", file=sys.stderr)
        return 2
    if not shutil.which("adb"):
        print("adb is not installed. Install with:\n"
              "  sudo apt install adb", file=sys.stderr)
        return 2

    token = load_token()
    base = _resolve_base(args.host, args.port, token)

    try:
        dev_resp = _request("GET", f"{base}/api/scrcpy/devices", token)
    except urllib.error.HTTPError as e:
        if e.code == 503:
            print("The bridge says adb isn't installed. Install with:\n"
                  "  sudo apt install adb", file=sys.stderr)
            return 2
        raise SystemExit(f"Failed to list devices: HTTP {e.code} {e.reason}")
    devices = dev_resp.get("devices") or []
    chosen = _pick_device(devices, args.serial)
    serial = chosen["serial"]
    name = chosen.get("name") or serial

    # If --quality was passed, push the override BEFORE launching so the
    # tuner picks it up (or, for "low", we just use the low-bitrate
    # preset directly which already matches what the web UI ships).
    preset = "default"
    override: Optional[str] = None
    if args.quality:
        preset, override = _QUALITY_MAP[args.quality]
    if args.record:
        preset = "record"
    quality_pending_first_launch = False
    if override is not None:
        try:
            _request("POST", f"{base}/api/scrcpy/quality", token,
                     body={"serial": serial, "override": override})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Device isn't in the remembered list yet — the launch
                # path will add it. We re-apply the override AFTER
                # launch so subsequent runs honour the flag, and warn
                # the user that the first run will use auto-tuning.
                quality_pending_first_launch = True
                print(f"Note: --quality {args.quality} will take effect on "
                      "the NEXT launch (this is the first time this device "
                      "is being mirrored, so auto-tuning runs once).",
                      file=sys.stderr)
            else:
                print(f"Note: couldn't pin quality ({e.code} {e.reason}). "
                      "Launching anyway.", file=sys.stderr)

    print(f"Launching scrcpy for {name} ({serial}) — preset={preset}…")
    try:
        launch_resp = _request("POST", f"{base}/api/scrcpy/launch", token,
                               body={"serial": serial, "preset": preset,
                                     "name": name})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = e.reason
        if e.code == 409:
            print(f"scrcpy is already running ({detail}). Close the existing "
                  "window first, or stop it from /mirror-setup.",
                  file=sys.stderr)
            return 1
        print(f"Launch failed: {e.code} {detail}", file=sys.stderr)
        return 1
    pid = launch_resp.get("pid")
    print(f"Native scrcpy window is up (pid {pid}). "
          "Click and scroll inside the window to drive the phone.")
    # First-launch quality push: the device exists in recents now, so
    # the override will stick for next time.
    if quality_pending_first_launch and override is not None:
        try:
            _request("POST", f"{base}/api/scrcpy/quality", token,
                     body={"serial": serial, "override": override})
        except urllib.error.HTTPError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
