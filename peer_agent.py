"""
``python run.py peer`` — secondary-laptop agent.

Pairs once against the main bridge using its rotating 8-digit pair
code and user-verified TLS fingerprint, stores the long-lived per-peer secret in
``~/.phone-bridge/peer_link.json`` (mode 600), then maintains a
heartbeat WebSocket to the main bridge so it shows up as "online" in
``GET /api/peers``. Re-runs without ``--pair-code`` reuse the saved
secret.

This task only wires up the foundation handshake. Subsequent tasks
will add screen / input / file proxying that piggybacks on the same
peer secret — the routes layer on the main bridge is already
forward-compatible (unknown WS message types are ignored, not
rejected).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import platform
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import websockets

STATE_DIR = Path.home() / ".phone-bridge"
# Same filename the main bridge uses on its end ("peers.json"). The
# two never share a filesystem (they run on different laptops), and
# matching the name keeps the spec / docs consistent across both
# sides of the link.
LINK_FILE = STATE_DIR / "peers.json"
HEARTBEAT_SECONDS = 5.0
RECONNECT_INITIAL = 1.0
RECONNECT_MAX = 30.0
# Default port for the peer's local HTTPS server (screen + input). The
# main bridge proxies through this; the browser never hits it directly.
DEFAULT_PEER_PORT = 8766


def _fetch_main_certificate(main_url: str) -> Tuple[str, str]:
    """Fetch a certificate without credentials; caller must verify its pin."""
    parsed = urlparse(main_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("Peer pairing requires an HTTPS main-bridge URL.")
    port = parsed.port or 443
    ctx = ssl._create_unverified_context()
    try:
        raw = socket.create_connection((parsed.hostname, port), timeout=8.0)
        with ctx.wrap_socket(raw, server_hostname=parsed.hostname) as tls:
            der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise SystemExit(f"Could not read the main bridge certificate: {exc}")
    if not der:
        raise SystemExit("The main bridge did not present a TLS certificate.")
    return ssl.DER_cert_to_PEM_cert(der), hashlib.sha256(der).hexdigest()


def _pin_main_certificate(main_url: str, expected: str) -> str:
    pem, actual = _fetch_main_certificate(main_url)
    expected_hex = "".join(c for c in expected.lower() if c in "0123456789abcdef")
    if len(expected_hex) != 64 or not hmac.compare_digest(actual, expected_hex):
        raise SystemExit("Main bridge TLS fingerprint mismatch; refusing to send pairing credentials.")
    return pem


def _ssl_ctx_for_main(main_url: str, pinned_cert_pem: Optional[str] = None):
    """Use system CA verification or trust only the user-pinned self-signed cert."""
    if not main_url.startswith("https://"):
        raise SystemExit("Peer connections require HTTPS/WSS; plain HTTP is not supported.")
    if pinned_cert_pem:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=pinned_cert_pem)
        ctx.verify_mode = ssl.CERT_REQUIRED
        # The exact certificate is trusted by its fingerprint, even when the
        # user connects through a LAN IP not present in its DNS SAN entries.
        ctx.check_hostname = False
        return ctx
    return ssl.create_default_context()


def _normalise_main(url: str) -> str:
    u = url.strip().rstrip("/")
    if "://" not in u:
        u = "https://" + u
    return u


def _atomic_write_link(data: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = LINK_FILE.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, LINK_FILE)
    os.chmod(LINK_FILE, 0o600)


def _load_link() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(LINK_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _http_post(url: str, body: Dict[str, Any], timeout: float = 10.0,
               ssl_ctx: Optional[ssl.SSLContext] = None) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method="POST",
                  headers={"Content-Type": "application/json"})
    if not url.startswith("https://"):
        raise SystemExit("Pairing credentials may only be sent over HTTPS.")
    with urlopen(req, timeout=timeout,
                 context=ssl_ctx or ssl.create_default_context()) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _ws_url(main_url: str) -> str:
    """Translate http(s)://host:port → ws(s)://host:port/ws/peer."""
    u = urlparse(main_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    netloc = u.netloc or u.path
    return f"{scheme}://{netloc}/ws/peer"


def _pair(main_url: str, code: str, peer_port: int,
          fingerprint: Optional[str] = None) -> Dict[str, Any]:
    pinned_cert_pem = (_pin_main_certificate(main_url, fingerprint)
                       if fingerprint else None)
    ssl_ctx = _ssl_ctx_for_main(main_url, pinned_cert_pem)
    body = {
        "code": code,
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}".strip(),
        "peer_port": peer_port,
    }
    try:
        resp = _http_post(f"{main_url}/api/peers/pair", body, ssl_ctx=ssl_ctx)
    except HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", e.reason)
        except Exception:  # noqa: BLE001
            detail = e.reason
        raise SystemExit(f"Pairing failed ({e.code}): {detail}")
    except URLError as e:
        raise SystemExit(f"Couldn't reach main bridge at {main_url}: {e.reason}")

    if "id" not in resp or "secret" not in resp:
        raise SystemExit(f"Main bridge gave unexpected response: {resp!r}")
    link = {
        "main": main_url,
        "id": resp["id"],
        "secret": resp["secret"],
        "hostname": resp.get("hostname", socket.gethostname()),
        "peer_port": peer_port,
        "paired_at": time.time(),
    }
    if pinned_cert_pem:
        link["main_cert_pem"] = pinned_cert_pem
    _atomic_write_link(link)
    return link


def _print_banner(link: Dict[str, Any]) -> None:
    print("\n" + "=" * 52)
    print("  🔗  Phone-Bridge Peer Agent")
    print("=" * 52)
    print(f"\n  Main bridge : {link['main']}")
    print(f"  Peer ID     : {link['id']}")
    print(f"  Hostname    : {link['hostname']}")
    print(f"  Peer port   : {link.get('peer_port', DEFAULT_PEER_PORT)} "
          f"(HTTPS for screen + input)")
    print(f"  Link file   : {LINK_FILE}")
    print("\n  Heartbeating once every 5 s. Browser-control requests")
    print("  flow through the main bridge — nothing else can hit the")
    print("  peer port without the per-peer secret.")
    print("  Press Ctrl+C to disconnect.\n")
    print("=" * 52 + "\n")


async def _heartbeat_loop(link: Dict[str, Any], peer_cert_pem: str) -> None:
    """Maintain the heartbeat WS forever, reconnecting with backoff."""
    url = _ws_url(link["main"])
    ssl_ctx = _ssl_ctx_for_main(link["main"], link.get("main_cert_pem"))
    delay = RECONNECT_INITIAL
    # Imported lazily so unit-testing the heartbeat doesn't drag in
    # mss/pynput. ``None`` (NOT an empty list) means "couldn't
    # determine"; we omit the field entirely from hello in that
    # case so the main bridge's registry treats it as
    # legacy/unknown rather than "supports nothing".
    try:
        from peer_server import capabilities as _peer_caps
        caps = _peer_caps()
    except Exception:  # noqa: BLE001
        caps = None
    while True:
        try:
            print(f"[peer] connecting to {url} …", flush=True)
            async with websockets.connect(url, ssl=ssl_ctx,
                                          ping_interval=None) as ws:
                hello_msg = {
                    "type": "hello",
                    "id": link["id"],
                    "secret": link["secret"],
                    # The main bridge stores this certificate from the
                    # authenticated WSS channel and pins it for proxy traffic.
                    "peer_cert": peer_cert_pem,
                    # Re-announce our peer-server port on every hello
                    # so the main bridge's registry stays in sync if
                    # we restart on a different port.
                    "peer_port": int(link.get("peer_port") or DEFAULT_PEER_PORT),
                }
                if caps is not None:
                    # Advertise which proxied features this peer can
                    # actually serve so the picker can grey out tabs
                    # whose router didn't mount (e.g. an old install
                    # without server.routes.files). Omit entirely if
                    # we couldn't probe — the main treats absence as
                    # "legacy / unknown" and won't disable rows.
                    hello_msg["capabilities"] = caps
                await ws.send(json.dumps(hello_msg))
                hello_ack = await asyncio.wait_for(ws.recv(), timeout=10.0)
                try:
                    parsed = json.loads(hello_ack)
                except json.JSONDecodeError:
                    parsed = {}
                if parsed.get("type") != "hello-ok":
                    raise RuntimeError(f"unexpected hello reply: {hello_ack!r}")
                print("[peer] linked — online.", flush=True)
                delay = RECONNECT_INITIAL  # reset backoff after a clean link

                while True:
                    await ws.send(json.dumps({"type": "ping"}))
                    try:
                        reply = await asyncio.wait_for(ws.recv(),
                                                       timeout=HEARTBEAT_SECONDS + 2.0)
                    except asyncio.TimeoutError:
                        # No pong in time — main bridge probably gone.
                        raise RuntimeError("heartbeat timeout")
                    # We don't really care about the pong contents;
                    # any other message types are forward-compat for
                    # later tasks. Sleep then ping again.
                    await asyncio.sleep(HEARTBEAT_SECONDS)
        except (OSError, asyncio.TimeoutError, RuntimeError,
                websockets.exceptions.WebSocketException) as e:
            print(f"[peer] disconnected: {e}", flush=True)
        except asyncio.CancelledError:
            raise

        print(f"[peer] retrying in {int(delay)} s …", flush=True)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        delay = min(RECONNECT_MAX, delay * 2)


async def _run_all(link: Dict[str, Any]) -> None:
    """Run the heartbeat + the peer HTTPS/WSS server concurrently. If
    either crashes irrecoverably we cancel both so the user sees the
    error and the agent restarts cleanly."""
    from peer_server import serve as _serve_peer
    from server.tls import ensure_cert

    cert_file, key_file, _ = ensure_cert()
    peer_cert_pem = Path(cert_file).read_text(encoding="ascii")
    port = int(link.get("peer_port") or DEFAULT_PEER_PORT)
    tasks = [
        asyncio.create_task(_heartbeat_loop(link, peer_cert_pem), name="heartbeat"),
        asyncio.create_task(_serve_peer(link["secret"], host="0.0.0.0",
                                        port=port, certfile=str(cert_file),
                                        keyfile=str(key_file)),
                            name="peer-server"),
    ]
    try:
        done, pending = await asyncio.wait(tasks,
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        # Surface whichever exited first so the user sees the cause.
        for t in done:
            try:
                t.result()
            except asyncio.CancelledError:
                pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phone-bridge peer",
        description="Run this laptop as a peer of a main phone-bridge.",
    )
    parser.add_argument("--main", default=None,
                        help="Main bridge URL, e.g. https://192.168.1.42:8765 "
                             "(required on first run; remembered after).")
    parser.add_argument("--pair-code", default=None,
                        help="8-digit pair code shown on the main bridge "
                             "banner (required on first run).")
    parser.add_argument("--fingerprint", default=None,
                        help="SHA-256 fingerprint shown by the main bridge; "
                             "required when pairing to its self-signed HTTPS certificate.")
    parser.add_argument("--peer-port", type=int, default=DEFAULT_PEER_PORT,
                        help=f"Local TCP port for screen + input HTTPS "
                             f"(default: {DEFAULT_PEER_PORT}). The main "
                             f"bridge proxies through this port.")
    parser.add_argument("--re-pair", action="store_true",
                        help="Forget the saved link and pair again. Use this "
                             "if the main bridge revoked the peer or you want "
                             "to repoint at a different one.")
    args = parser.parse_args(argv)

    saved = None if args.re_pair else _load_link()

    if saved is None:
        if not args.main or not args.pair_code:
            parser.error("first-time pairing needs --main URL and --pair-code CODE")
        link = _pair(_normalise_main(args.main), args.pair_code,
                     args.peer_port, args.fingerprint)
    else:
        link = saved
        changed = False
        if args.main:
            # Allow updating the URL without re-pairing if the main
            # bridge just moved IP — secret is unchanged.
            new_main = _normalise_main(args.main)
            if new_main != link.get("main"):
                link["main"] = new_main
                changed = True
        if args.fingerprint:
            link["main_cert_pem"] = _pin_main_certificate(
                link["main"], args.fingerprint)
            changed = True
        elif not link.get("main_cert_pem"):
            print("[security] no stored TLS pin; if the main bridge uses its "
                  "self-signed certificate, re-run with --fingerprint from "
                  "the main bridge banner.", flush=True)
        if args.peer_port and int(link.get("peer_port") or 0) != args.peer_port:
            link["peer_port"] = int(args.peer_port)
            changed = True
        if changed:
            _atomic_write_link(link)

    _print_banner(link)

    try:
        asyncio.run(_run_all(link))
    except KeyboardInterrupt:
        print("\n[peer] stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
