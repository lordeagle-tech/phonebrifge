"""
Shared helpers for the `phone-bridge` terminal subcommands.

Both ``cli_mirror`` (start scrcpy) and ``cli_stop_mirror`` (close it) talk
to the locally-running bridge over loopback using the CLI token the
server writes to ``~/.phone-bridge/cli_token`` on startup. The HTTP
plumbing — TLS context, auth header, scheme probing — is identical, so
it lives here.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from server import cli_token


def _ssl_ctx() -> ssl.SSLContext:
    # The bridge's TLS cert is self-signed. We're talking to loopback,
    # so the auth gate is the CLI token, not the cert.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(method: str, url: str, token: str,
            body: Optional[Dict[str, Any]] = None,
            timeout: float = 15.0) -> Dict[str, Any]:
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = _ssl_ctx() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _try_base_urls(host: str, port: int) -> List[str]:
    # Default install is HTTPS; --no-tls flips to HTTP. Try HTTPS first
    # then fall back so the CLI works either way without flags.
    return [f"https://{host}:{port}", f"http://{host}:{port}"]


def resolve_base(host: str, port: int, token: str) -> str:
    """Pick whichever scheme the running server actually answers on."""
    last_err: Optional[Exception] = None
    for base in _try_base_urls(host, port):
        try:
            request("GET", f"{base}/api/scrcpy/deps", token, timeout=3.0)
            return base
        except urllib.error.HTTPError as e:
            # 401/403 still proves the port is up; surface auth issue.
            if e.code in (401, 403):
                raise SystemExit(
                    "CLI token rejected by the bridge. The server may have "
                    "restarted since the token was written — restart the "
                    "bridge and try again."
                )
            last_err = e
        except (urllib.error.URLError, OSError) as e:
            last_err = e
    raise SystemExit(
        f"Couldn't reach the bridge on {host}:{port}. Is `python run.py` "
        f"running on this laptop?\n  Last error: {last_err}"
    )


def load_token() -> str:
    """Read the loopback CLI token or exit with a helpful message."""
    token = cli_token.read()
    if not token:
        print(
            "No CLI token found at ~/.phone-bridge/cli_token.\n"
            "Start the bridge with `python run.py` first — the token is "
            "written automatically on startup.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token
