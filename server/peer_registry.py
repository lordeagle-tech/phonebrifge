"""
Peer-laptop registry for the main bridge.

Stores one record per linked secondary laptop with its long-lived
shared secret, friendly metadata (hostname / OS / last-seen LAN IP)
and live-online flag. Persisted to ``~/.phone-bridge/peers.json`` with
mode 600 so other users on the box can't read the secrets. The
registry is the single source of truth other features will query
(``GET /api/peers``); higher-level features (screen, input, file
proxy) will look peers up by id.

This module deliberately does NOT speak HTTP/WebSocket itself —
that lives in ``server/routes/peers.py``. The split keeps state
management testable and lets the routes layer stay thin.
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_DIR = Path.home() / ".phone-bridge"
PEERS_FILE = STATE_DIR / "peers.json"

# A peer is considered online while it has an active heartbeat WS AND
# its last heartbeat was within this many seconds. Higher-level code
# uses ``online()`` rather than reading the field directly so policy
# stays in one place.
ONLINE_GRACE_SECONDS = 15.0

_lock = threading.RLock()
# id -> record (in-memory cache of the on-disk JSON, kept in sync).
_peers: Dict[str, Dict[str, Any]] = {}
# id -> count of currently-open heartbeat WS connections. A peer is
# only "live" when this is >0; the timestamp grace is a secondary
# liveness check that lets us mark peers offline within ~15 s if a WS
# half-closes silently.
_live_conn_counts: Dict[str, int] = {}
_loaded = False


def _atomic_write(data: Dict[str, Any]) -> None:
    """Write peers.json with mode 600, replacing atomically."""
    try:
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return
    tmp = PEERS_FILE.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, PEERS_FILE)
    try:
        os.chmod(PEERS_FILE, 0o600)
    except OSError:
        pass


def _persist_locked() -> None:
    # Caller holds _lock. Strip transient fields before persisting.
    payload = {
        "version": 1,
        "peers": {
            pid: {k: v for k, v in rec.items() if k != "_transient"}
            for pid, rec in _peers.items()
        },
    }
    try:
        _atomic_write(payload)
    except OSError:
        # Persistence failures shouldn't crash the server — peers will
        # just have to re-pair if the box reboots before the next
        # successful write.
        pass


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not PEERS_FILE.exists():
        return
    try:
        raw = json.loads(PEERS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for pid, rec in (raw.get("peers") or {}).items():
        if not isinstance(rec, dict) or "secret" not in rec:
            continue
        _peers[pid] = rec


def _ensure_loaded() -> None:
    with _lock:
        _load_locked()


def add_peer(hostname: str, os_name: str, local_ip: str,
             peer_port: int = 0) -> Dict[str, Any]:
    """Create a new peer record with a fresh secret. Caller is the
    pairing endpoint; the secret is returned to the peer ONCE and never
    leaves the registry afterwards.

    ``peer_port`` is the TCP port the peer agent's local HTTP server
    listens on (used by the screen + input proxy). 0 means the peer
    didn't expose one yet (foundation-only pairing); proxy routes
    return 503 in that case."""
    _ensure_loaded()
    pid = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    rec = {
        "id": pid,
        "secret": secret,
        "hostname": hostname[:64] or "unknown",
        "os": os_name[:64] or "unknown",
        "local_ip": local_ip[:64] or "",
        "peer_port": int(peer_port) if peer_port else 0,
        "paired_at": time.time(),
        "last_seen": 0.0,
    }
    with _lock:
        _peers[pid] = rec
        _persist_locked()
    return dict(rec)  # caller gets a copy including the secret


def get_endpoint(pid: str) -> Optional[Dict[str, Any]]:
    """Return everything the proxy needs to reach a peer: ip, port,
    secret. Used by ``server/peer_proxy.py``. Returns None for
    unknown peers OR peers that paired without a peer_port."""
    _ensure_loaded()
    with _lock:
        rec = _peers.get(pid)
        if not rec:
            return None
        if not rec.get("peer_port") or not rec.get("local_ip"):
            return None
        return {
            "id": pid,
            "ip": rec["local_ip"],
            "port": int(rec["peer_port"]),
            "secret": rec["secret"],
            "cert_pem": rec.get("cert_pem", ""),
            "hostname": rec.get("hostname", ""),
        }


def update_certificate(pid: str, cert_pem: str) -> bool:
    """Store a peer TLS certificate announced over its authenticated WSS link."""
    if not isinstance(cert_pem, str) or len(cert_pem) > 8192:
        return False
    try:
        ssl.PEM_cert_to_DER_cert(cert_pem)
    except (ValueError, ssl.SSLError):
        return False
    _ensure_loaded()
    with _lock:
        rec = _peers.get(pid)
        if rec is None:
            return False
        if rec.get("cert_pem") != cert_pem:
            rec["cert_pem"] = cert_pem
            _persist_locked()
    return True


def update_capabilities(pid: str, caps: Optional[List[str]]) -> None:
    """Record the feature list a peer announced on its last hello.
    ``None`` means the peer didn't send the field (legacy agent);
    we wipe any stale list so the picker shows the row as
    "capabilities unknown" rather than holding onto info from a
    different agent build."""
    _ensure_loaded()
    with _lock:
        rec = _peers.get(pid)
        if rec is None:
            return
        if caps is None:
            if "capabilities" in rec:
                rec.pop("capabilities", None)
                _persist_locked()
            return
        # Defensive: only keep short string entries so a hostile or
        # buggy peer can't bloat the registry with megabytes of junk.
        clean = sorted({str(c)[:32] for c in caps if isinstance(c, str)})
        if rec.get("capabilities") != clean:
            rec["capabilities"] = clean
            _persist_locked()


def update_port(pid: str, peer_port: int) -> None:
    """Bump the peer's HTTP-server port (the peer agent reports it on
    every reconnect so a restart-with-new-port is transparent)."""
    _ensure_loaded()
    if not peer_port:
        return
    with _lock:
        rec = _peers.get(pid)
        if rec is None:
            return
        if int(rec.get("peer_port") or 0) != int(peer_port):
            rec["peer_port"] = int(peer_port)
            _persist_locked()


def verify(pid: str, secret: str) -> bool:
    """Constant-time check of (id, secret). Used by the heartbeat WS."""
    _ensure_loaded()
    with _lock:
        rec = _peers.get(pid)
    if rec is None:
        return False
    return secrets.compare_digest(rec.get("secret", ""), secret or "")


def touch(pid: str, *, local_ip: Optional[str] = None) -> None:
    """Bump the last-seen timestamp (called on every heartbeat). If the
    peer's apparent LAN IP changed (DHCP lease, Wi-Fi roam) we update
    that too."""
    _ensure_loaded()
    now = time.time()
    with _lock:
        rec = _peers.get(pid)
        if rec is None:
            return
        rec["last_seen"] = now
        if local_ip and local_ip != rec.get("local_ip"):
            rec["local_ip"] = local_ip[:64]
            _persist_locked()


def mark_connect(pid: str) -> None:
    with _lock:
        _live_conn_counts[pid] = _live_conn_counts.get(pid, 0) + 1


def mark_disconnect(pid: str) -> None:
    with _lock:
        n = _live_conn_counts.get(pid, 0) - 1
        if n <= 0:
            _live_conn_counts.pop(pid, None)
        else:
            _live_conn_counts[pid] = n


def online(pid: str, *, now: Optional[float] = None) -> bool:
    """Live = at least one WS connected AND last heartbeat is fresh.
    The double check protects against silently half-closed sockets
    (Wi-Fi drop where TCP RST never arrives)."""
    _ensure_loaded()
    if now is None:
        now = time.time()
    with _lock:
        rec = _peers.get(pid)
        if rec is None:
            return False
        if _live_conn_counts.get(pid, 0) <= 0:
            return False
        return (now - rec.get("last_seen", 0.0)) < ONLINE_GRACE_SECONDS


def list_public() -> List[Dict[str, Any]]:
    """Return the registry as a list of dicts safe to expose over the
    PIN-authed API. Secrets are stripped; an ``online`` flag is
    computed on the fly so callers don't have to."""
    _ensure_loaded()
    now = time.time()
    out: List[Dict[str, Any]] = []
    with _lock:
        for pid, rec in _peers.items():
            out.append({
                "id": pid,
                "hostname": rec.get("hostname"),
                "os": rec.get("os"),
                "local_ip": rec.get("local_ip"),
                "peer_port": int(rec.get("peer_port") or 0),
                "controllable": bool(rec.get("peer_port") and rec.get("local_ip")),
                "last_seen": rec.get("last_seen", 0.0),
                "online": (
                    _live_conn_counts.get(pid, 0) > 0
                    and (now - rec.get("last_seen", 0.0)) < ONLINE_GRACE_SECONDS
                ),
                # Either the list a recent agent advertised, or null
                # if the peer hasn't told us yet (legacy agent that
                # predates the capabilities field).
                "capabilities": rec.get("capabilities"),
            })
    # Most-recently-seen first; helps the UI put fresh peers up top.
    out.sort(key=lambda r: r.get("last_seen", 0.0), reverse=True)
    return out


def remove(pid: str) -> bool:
    """Forget a peer entirely (revokes its secret). Returns True if a
    record was removed."""
    _ensure_loaded()
    with _lock:
        if pid not in _peers:
            return False
        _peers.pop(pid, None)
        _live_conn_counts.pop(pid, None)
        _persist_locked()
    return True
