"""
WebRTC signaling relay.

A pair of authenticated peers (phone = sender, laptop = viewer) connect to
the same room and exchange SDP offers/answers and ICE candidates through
this server. The bridge is *only* a signaling channel — once the peers have
exchanged SDP/ICE, the actual A/V stream flows directly peer-to-peer.

Protocol (JSON messages over WS at /ws/signal?ticket=...&room=...&role=...):
  Client -> Server:
    {"type": "signal", "data": <SDP or ICE blob>}     # forwarded to peer
    {"type": "bye"}                                   # session ended
  Server -> Client:
    {"type": "ready"}                                 # joined room ok
    {"type": "peer-joined", "role": "sender|viewer"}  # other side connected
    {"type": "peer-left"}                             # other side dropped
    {"type": "signal", "from": "...", "data": ...}    # relayed signal
    {"type": "error", "message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .. import audit, security
from ..auth import consume_connection_ticket

router = APIRouter()

ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ROLE_SENDER = "sender"
ROLE_VIEWER = "viewer"


MAX_SIGNAL_BYTES = 64 * 1024  # cap relayed payload to avoid memory abuse


class _Room:
    __slots__ = ("sender", "viewer")

    def __init__(self) -> None:
        self.sender: Optional[WebSocket] = None
        self.viewer: Optional[WebSocket] = None


# A single global lock protects the rooms dict AND every room's
# sender/viewer slots. Signaling traffic is tiny (a handful of messages per
# session), so contention is a non-issue and a single lock removes a class
# of races between room registration and room cleanup.
_rooms: Dict[str, _Room] = {}
_lock = asyncio.Lock()


@router.websocket("/ws/signal")
async def signal_ws(
    websocket: WebSocket,
    ticket: str = Query(None),
    room: str = Query("main"),
    role: str = Query(...),
):
    ip = security.ws_client_ip(websocket)

    session_token = consume_connection_ticket(ticket, websocket.url.path)
    if not session_token:
        audit.log("screen", "signal_ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if role not in (ROLE_SENDER, ROLE_VIEWER):
        await websocket.close(code=4400)
        return
    if not ROOM_RE.match(room):
        await websocket.close(code=4400)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/signal"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("screen", "signal_ws", ip=ip, outcome="connected",
              detail={"room": room, "role": role})

    # Register (creating the room if needed), kicking out any previous
    # occupant of the same role.
    async with _lock:
        rm = _rooms.get(room)
        if rm is None:
            rm = _Room()
            _rooms[room] = rm
        prev = rm.sender if role == ROLE_SENDER else rm.viewer
        if role == ROLE_SENDER:
            rm.sender = websocket
        else:
            rm.viewer = websocket
        peer = rm.viewer if role == ROLE_SENDER else rm.sender

    if prev is not None:
        try:
            await prev.send_text(json.dumps({"type": "error", "message": "replaced"}))
            await prev.close(code=4409)
        except Exception:
            pass

    await websocket.send_text(json.dumps({"type": "ready"}))
    if peer is not None:
        try:
            await peer.send_text(json.dumps({"type": "peer-joined", "role": role}))
            await websocket.send_text(json.dumps(
                {"type": "peer-joined",
                 "role": ROLE_VIEWER if role == ROLE_SENDER else ROLE_SENDER}))
        except Exception:
            pass

    async def watchdog():
        await security.ws_session_watchdog(
            websocket, session_token, "screen", "signal_ws", ip)

    watch_task = asyncio.create_task(watchdog())

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            if len(raw) > MAX_SIGNAL_BYTES:
                # Refuse to relay oversized payloads (protects the peer's
                # WS buffer from being filled by a misbehaving client).
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype not in ("signal", "bye"):
                continue

            async with _lock:
                target = rm.viewer if role == ROLE_SENDER else rm.sender
            if target is None:
                continue
            payload = {"type": mtype, "from": role}
            if mtype == "signal":
                payload["data"] = msg.get("data")
            try:
                await target.send_text(json.dumps(payload))
            except Exception:
                break
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):
            pass
        was_active = False
        async with _lock:
            if role == ROLE_SENDER and rm.sender is websocket:
                rm.sender = None
                was_active = True
            elif role == ROLE_VIEWER and rm.viewer is websocket:
                rm.viewer = None
                was_active = True
            other = rm.viewer if role == ROLE_SENDER else rm.sender
            # Atomically drop empty rooms so a late-arriving peer can't
            # register into an orphaned room.
            if rm.sender is None and rm.viewer is None and _rooms.get(room) is rm:
                _rooms.pop(room, None)
        # Only notify the remaining peer if WE were the active occupant
        # being torn down — a socket replaced earlier (kicked with 4409)
        # should not send a spurious peer-left to its successor.
        if was_active and other is not None:
            try:
                await other.send_text(json.dumps({"type": "peer-left"}))
            except Exception:
                pass
        audit.log("screen", "signal_ws", ip=ip, outcome="disconnected",
                  detail={"room": room, "role": role})
        try:
            await websocket.close()
        except Exception:
            pass
