#!/usr/bin/env python3
"""
Laptop-Phone Bridge
Run this on your Parrot OS laptop, then connect from your Android phone.
"""

import argparse
import os
import random
import socket
import sys

import qrcode
import uvicorn

from server import auth, cli_token, peer_pair_code, webrtc_config


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_remote_tunnel(port: int, domain: str | None = None):
    """Start an ngrok tunnel and return (public_https_url, tunnel_handle).

    If `domain` is provided, the tunnel is bound to that reserved ngrok
    domain (e.g. "mybridge.ngrok-free.app") so the public URL stays the
    same across restarts and saved phone bookmarks keep working.
    """
    try:
        from pyngrok import conf, ngrok
    except ImportError:
        print(
            "Error: --remote requires pyngrok. Install with:\n"
            "  pip install pyngrok",
            file=sys.stderr,
        )
        sys.exit(1)

    authtoken = os.environ.get("NGROK_AUTHTOKEN")
    if authtoken:
        conf.get_default().auth_token = authtoken

    # Normalize a user-supplied domain: accept "https://x.ngrok.app",
    # "x.ngrok.app/", or a bare hostname.
    if domain:
        d = domain.strip()
        if "://" in d:
            d = d.split("://", 1)[1]
        d = d.strip("/")
        if not d:
            print("Error: --remote-domain is empty.", file=sys.stderr)
            sys.exit(1)
        domain = d

    connect_kwargs = {"bind_tls": True}
    if domain:
        connect_kwargs["domain"] = domain
        if not authtoken:
            print(
                "Error: --remote-domain requires NGROK_AUTHTOKEN to be set "
                "(reserved domains are tied to your ngrok account).",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        tunnel = ngrok.connect(port, "http", **connect_kwargs)
    except Exception as e:
        print(f"Error: failed to start ngrok tunnel: {e}", file=sys.stderr)
        if domain:
            print(
                f"\nHint: make sure '{domain}' is reserved on your ngrok\n"
                "account at https://dashboard.ngrok.com/cloud-edge/domains\n"
                "and that NGROK_AUTHTOKEN matches that account.",
                file=sys.stderr,
            )
        elif not authtoken:
            print(
                "\nHint: ngrok requires a free auth token. Get one at\n"
                "  https://dashboard.ngrok.com/get-started/your-authtoken\n"
                "then set it with:\n"
                "  export NGROK_AUTHTOKEN=<your-token>",
                file=sys.stderr,
            )
        sys.exit(1)

    public_url = tunnel.public_url
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[len("http://"):]
    return public_url, tunnel


def print_banner(url: str, pin: str, *, remote: bool, tls: bool,
                 fingerprint: str | None, inactivity_min: int) -> None:
    print("\n" + "=" * 52)
    print("  📱  Laptop-Phone Bridge")
    print("=" * 52)
    print(f"\n  Server URL : {url}")
    print(f"  Access PIN : {pin}")
    if remote:
        print("  Mode       : REMOTE (secure public tunnel)")
    else:
        print("  Mode       : LOCAL (same Wi-Fi only)")
    print(f"  Encryption : {'🔒 HTTPS enabled' if tls else '⚠️  HTTP (not encrypted)'}")
    print(f"  Inactivity : auto-logout after {inactivity_min} min idle")
    print(f"  Audit log  : ~/.phone-bridge/audit.log")
    if tls and fingerprint:
        print(f"\n  Cert SHA-256 fingerprint:")
        print(f"    {fingerprint}")
        print("  (Verify this on first connect to detect tampering.)")
    print(f"  🪞 Phone mirror: open {url}/mirror-setup")

    # Show the rotating peer pair code so the user can read it off the
    # banner when bringing a second / third laptop online. Code rotates
    # every few minutes — re-read the banner if the code expires.
    pair_code, expires_in = peer_pair_code.get()
    print(f"  🔗 Peer pair code : {pair_code}  "
          f"(rotates in {expires_in // 60}m {expires_in % 60:02d}s)")
    print("     Run on another laptop:")
    print(f"       python run.py peer --main {url} --pair-code {pair_code}")

    print("\n  Scan the QR code with your phone:\n")

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    if remote:
        print("\n  This URL works from anywhere — Wi-Fi or mobile data.")
        print("  Keep the PIN private; anyone with the URL + PIN can connect.")
    else:
        print("\n  Make sure your phone is on the same Wi-Fi network.")
    if tls:
        print("  Your browser will warn about the self-signed cert on first")
        print("  visit — tap 'Advanced' then 'Proceed' to trust this device.")
    print("  Press Ctrl+C to stop the server.\n")
    print("=" * 52 + "\n")


def main() -> None:
    # Subcommand dispatch: `python run.py mirror [...]` shells out to the
    # CLI helper that pops up the native scrcpy window. Anything else
    # (including no subcommand) falls through to the existing server-
    # launch flow so old invocations keep working unchanged.
    if len(sys.argv) >= 2 and sys.argv[1] == "mirror":
        from cli_mirror import main as mirror_main
        sys.exit(mirror_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "stop-mirror":
        from cli_stop_mirror import main as stop_mirror_main
        sys.exit(stop_mirror_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "mirror-status":
        from cli_mirror_status import main as mirror_status_main
        sys.exit(mirror_status_main(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "peer":
        # Run this laptop as a peer of a main bridge instead of as a
        # main bridge itself. No PIN, no QR, no uvicorn — just the
        # heartbeat agent.
        from peer_agent import main as peer_main
        sys.exit(peer_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(description="Laptop-Phone Bridge Server")
    parser.add_argument("--pin", type=str, default=None, help="Access PIN (6 digits). Auto-generated if not set.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Expose the server via a secure public tunnel (ngrok) so you can connect from any network.",
    )
    parser.add_argument(
        "--remote-domain",
        type=str,
        default=None,
        help="Reserved ngrok domain to bind the tunnel to (e.g. mybridge.ngrok-free.app). "
             "Reuse the same URL across restarts so phone bookmarks keep working. "
             "Implies --remote. Requires NGROK_AUTHTOKEN.",
    )
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="Disable HTTPS and serve plain HTTP (NOT recommended; only for trusted local debugging).",
    )
    parser.add_argument(
        "--session-timeout",
        type=int,
        default=30,
        help="Auto-logout after this many minutes of inactivity (default: 30; 0 disables).",
    )
    parser.add_argument(
        "--stun",
        action="append",
        default=None,
        help="STUN server URL for WebRTC (e.g. stun:stun.example.com:3478). "
             "May be passed multiple times. Default: stun:stun.l.google.com:19302.",
    )
    parser.add_argument(
        "--no-stun",
        action="store_true",
        help="Disable STUN entirely; rely on direct LAN connectivity only "
             "(use when the laptop and phone are on the same Wi-Fi).",
    )
    args = parser.parse_args()

    pin = args.pin or str(random.randint(100000, 999999))
    if not pin.isdigit() or len(pin) < 4:
        print("Error: PIN must be at least 4 digits.", file=sys.stderr)
        sys.exit(1)

    auth.set_pin(pin)
    auth.set_inactivity_timeout(args.session_timeout * 60)
    # Mint a loopback-only token so `python run.py mirror` (and any
    # other terminal helpers) can call the local API without the user
    # re-typing the PIN. File is mode 600 under ~/.phone-bridge/.
    cli_tok = cli_token.install()

    if args.no_stun:
        webrtc_config.set_ice_servers([])
    elif args.stun:
        webrtc_config.set_ice_servers([{"urls": s} for s in args.stun])

    if args.remote_domain and not args.remote:
        args.remote = True  # --remote-domain implies --remote

    tunnel = None
    if args.remote:
        url, tunnel = start_remote_tunnel(args.port, domain=args.remote_domain)
    else:
        scheme = "http" if args.no_tls else "https"
        local_ip = get_local_ip()
        url = f"{scheme}://{local_ip}:{args.port}"

    # Set up TLS unless disabled or using ngrok (which terminates TLS itself).
    cert_file = key_file = None
    fingerprint = None
    tls_enabled = not args.no_tls and not args.remote
    if tls_enabled:
        try:
            from server.tls import ensure_cert
            local_ip = get_local_ip()
            cert_path, key_path, fingerprint = ensure_cert(extra_hosts=[local_ip])
            cert_file = str(cert_path)
            key_file = str(key_path)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            print("Run with --no-tls to start in plain HTTP mode (not recommended).",
                  file=sys.stderr)
            sys.exit(1)

    print_banner(
        url, pin,
        remote=args.remote,
        tls=tls_enabled or args.remote,  # ngrok also terminates TLS
        fingerprint=fingerprint,
        inactivity_min=args.session_timeout,
    )

    from server.main import app

    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
    finally:
        # Always remove the CLI token on shutdown so a stale file can't
        # be reused after the server exits (the in-memory session is
        # gone too, but the file should follow).
        cli_token.uninstall(cli_tok)
        if tunnel is not None:
            try:
                from pyngrok import ngrok
                ngrok.disconnect(tunnel.public_url)
            except Exception:
                pass


if __name__ == "__main__":
    main()
