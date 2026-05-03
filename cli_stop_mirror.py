"""
`phone-bridge stop-mirror` — close the native scrcpy window.

Mirror image of ``cli_mirror``: talks to the locally-running bridge over
loopback and POSTs to ``/api/scrcpy/stop``. Reports whether anything was
actually running so the user knows if the call was a no-op.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from typing import List, Optional

from cli_api import load_token, request as _request, resolve_base as _resolve_base


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phone-bridge stop-mirror",
        description="Close the native scrcpy window opened by `phone-bridge "
                    "mirror` (or the /mirror-setup page).",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bridge host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765,
                        help="Bridge port (default: 8765).")
    args = parser.parse_args(argv)

    token = load_token()
    base = _resolve_base(args.host, args.port, token)

    # Capture the pre-stop status so we can tell the user whether
    # there was actually a window to close. /scrcpy/stop returns the
    # post-stop status (running=False), which on its own can't
    # distinguish "stopped it for you" from "nothing was running".
    try:
        before = _request("GET", f"{base}/api/scrcpy/status", token)
    except urllib.error.HTTPError as e:
        print(f"Failed to read mirror status: {e.code} {e.reason}",
              file=sys.stderr)
        return 1
    was_running = bool(before.get("running"))
    serial = before.get("serial")

    try:
        _request("POST", f"{base}/api/scrcpy/stop", token, body={})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:  # noqa: BLE001
            detail = e.reason
        print(f"Stop failed: {e.code} {detail}", file=sys.stderr)
        return 1

    if was_running:
        if serial:
            print(f"Closed scrcpy window for {serial}.")
        else:
            print("Closed scrcpy window.")
    else:
        print("No scrcpy window was running — nothing to stop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
