"""
HTTP + WebSocket routes for peer-laptop linking.

Three surfaces:
  * ``POST /api/peers/pair`` — open endpoint (strictly rate-limited via
    the pairing bucket). A peer presents the 8-digit pair code shown on
    the main laptop's banner; on success the registry mints a record
    and returns ``{id, secret}`` once.
  * ``GET /api/peers`` and ``GET /api/peers/pair-code`` — PIN-auth.
    Used by the laptop UI / phone UI / future proxy code to know
    which peers exist and what the current pair code is without
    re-reading the terminal.
  * ``WS /ws/peer`` — peer-secret-auth. Paired peers maintain this
    connection for as long as they're "online"; while it's open the
    registry's online flag flips true. Peers send a heartbeat every
    5 s; the main side replies with a pong so the peer can detect
    silent network drops too.

Browser PIN sessions never see peer secrets — those leave the
registry exactly once at pairing time.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from .. import audit, peer_pair_code, peer_registry
from ..deps import require_auth

router = APIRouter()
ws_router = APIRouter()

PEER_ID_RE = re.compile(r"^[0-9a-f]{12}$")
HOSTNAME_RE = re.compile(r"^[\w.\-]{1,64}$")  # forgiving; we just store it
# Peer pings every 5 s. We drop the WS after 12 s of silence so a
# silently half-closed socket is detected within ~12 s and the
# registry's online flag flips false within the 15-s target window.
HEARTBEAT_TIMEOUT = 12.0


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


@router.get("/peers")
async def list_peers(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """List every paired peer plus its current online flag."""
    return {"peers": peer_registry.list_public()}


@router.get("/peers/pair-code")
async def pair_code(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Current 8-digit code + seconds until it rotates. PIN-auth so
    only an already-trusted browser session can read it; the code
    itself is what authorises the next peer to join."""
    code, remaining = peer_pair_code.get()
    return {"code": code, "expires_in_s": remaining,
            "rotates_every_s": peer_pair_code.ROTATE_SECONDS}


@router.delete("/peers/{peer_id}")
async def forget_peer(peer_id: str, request: Request,
                      _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Revoke a peer's secret. The peer's WS will close on its next
    heartbeat (auth fails)."""
    if not PEER_ID_RE.match(peer_id):
        raise HTTPException(status_code=400, detail="Invalid peer id")
    if not peer_registry.remove(peer_id):
        raise HTTPException(status_code=404, detail="Unknown peer")
    audit.log("peer", "forget", ip=_client_ip(request), outcome="ok",
              detail={"peer_id": peer_id})
    return {"ok": True}


@router.post("/peers/pair")
async def pair(request: Request) -> Dict[str, Any]:
    """Pairing endpoint. NOT PIN-authed — the 8-digit code is the
    auth. Body: ``{code, hostname, os}``. The peer's IP is captured
    from the request itself (more reliable than asking the peer)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    code = str(body.get("code", "")).strip()
    hostname = str(body.get("hostname", "")).strip()
    os_name = str(body.get("os", "")).strip()
    try:
        peer_port = int(body.get("peer_port") or 0)
    except (TypeError, ValueError):
        peer_port = 0
    if not (0 <= peer_port <= 65535):
        peer_port = 0
    ip = _client_ip(request)

    if not HOSTNAME_RE.match(hostname):
        raise HTTPException(status_code=400, detail="Invalid hostname")
    if len(os_name) > 64:
        raise HTTPException(status_code=400, detail="Invalid os")
    if not peer_pair_code.consume(code):
        audit.log("peer", "pair", ip=ip, outcome="bad_code")
        raise HTTPException(status_code=401,
                            detail="Pairing code is wrong, expired, or already used")

    rec = peer_registry.add_peer(hostname=hostname, os_name=os_name,
                                 local_ip=ip, peer_port=peer_port)
    audit.log("peer", "pair", ip=ip, outcome="ok",
              detail={"peer_id": rec["id"], "hostname": hostname})
    return {
        "id": rec["id"],
        "secret": rec["secret"],
        "hostname": rec["hostname"],
    }


@ws_router.websocket("/ws/peer")
async def peer_ws(ws: WebSocket) -> None:
    """Heartbeat channel. The peer connects once per session and sends
    a JSON ``{"type":"hello", id, secret}`` immediately, then a
    ``{"type":"ping"}`` every 5 s. We answer pings with ``{"type":
    "pong"}`` and drop the connection if no message arrives for
    ``HEARTBEAT_TIMEOUT`` seconds.

    All other message types are reserved for future features
    (screen-frame proxy, input forwarding) and are silently ignored
    here so adding new ones doesn't break the foundation handshake.
    """
    await ws.accept()
    peer_id = ""
    try:
        # Hello must arrive within the heartbeat window; if not the
        # client is broken or hostile.
        try:
            raw = await asyncio.wait_for(ws.receive_text(),
                                         timeout=HEARTBEAT_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(code=4000, reason="hello timeout")
            return
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError:
            await ws.close(code=4000, reason="bad hello")
            return
        if hello.get("type") != "hello":
            await ws.close(code=4000, reason="expected hello")
            return
        peer_id = str(hello.get("id", ""))
        secret = str(hello.get("secret", ""))
        if not PEER_ID_RE.match(peer_id) or not peer_registry.verify(peer_id, secret):
            await ws.close(code=4001, reason="bad credentials")
            return

        if "peer_cert" in hello:
            if not peer_registry.update_certificate(peer_id, hello["peer_cert"]):
                await ws.close(code=4000, reason="bad peer certificate")
                return

        client_ip = ws.client.host if ws.client else None
        # Peer agents that exposed an HTTP server (for screen + input
        # proxying) re-announce the port on every hello so a peer-side
        # restart with a different port is transparent to the main.
        try:
            announced_port = int(hello.get("peer_port") or 0)
        except (TypeError, ValueError):
            announced_port = 0
        if 0 < announced_port <= 65535:
            peer_registry.update_port(peer_id, announced_port)
        # Capability list is optional — old peer agents won't include
        # it. Pass through whatever the peer sent (None if absent) so
        # the registry can distinguish "advertised nothing" from
        # "didn't advertise at all".
        raw_caps = hello.get("capabilities")
        if raw_caps is None:
            peer_registry.update_capabilities(peer_id, None)
        elif isinstance(raw_caps, list):
            peer_registry.update_capabilities(peer_id, raw_caps)
        peer_registry.mark_connect(peer_id)
        peer_registry.touch(peer_id, local_ip=client_ip)
        await ws.send_text(json.dumps({"type": "hello-ok",
                                       "server": "phone-bridge"}))

        # Heartbeat loop. We trust the timeout to catch silent drops;
        # an explicit pong on every ping keeps both sides honest.
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(),
                                             timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                # Half-closed: registry will mark offline within
                # ONLINE_GRACE_SECONDS once we drop the conn count.
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "ping":
                # Re-verify the secret on every ping so a revoked peer
                # is kicked the moment its next heartbeat arrives,
                # rather than waiting for the heartbeat-timeout drop.
                if not peer_registry.verify(peer_id, secret):
                    await ws.close(code=4001, reason="revoked")
                    return
                peer_registry.touch(peer_id, local_ip=client_ip)
                await ws.send_text(json.dumps({"type": "pong"}))
            elif mtype == "bye":
                break
            # Unknown types are silently ignored (forward-compat with
            # downstream features that will reuse this channel).
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        # Don't let a single peer's bug take down the server.
        pass
    finally:
        if peer_id:
            peer_registry.mark_disconnect(peer_id)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
