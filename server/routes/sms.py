"""
SMS inbox + OTP auto-copy routes.

REST under /api/sms/* and a WebSocket at /ws/sms that pushes:
  - {"type": "snapshot", connected, serial, messages, settings, ...}
  - {"type": "otp", id, code, from, copied, auto_copy_enabled, ts}
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, \
    WebSocketDisconnect

from .. import audit, security, sms
from ..auth import consume_connection_ticket
from ..deps import require_auth

router = APIRouter()
ws_router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


@router.get("/sms/recent")
async def recent_sms(request: Request,
                     _token: str = Depends(require_auth)) -> Dict[str, Any]:
    snap = await sms.get_snapshot()
    audit.log("sms", "read", ip=_client_ip(request), outcome="ok",
              detail={"count": len(snap.get("messages", []))})
    return snap


@router.get("/sms/settings")
async def get_sms_settings(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return sms.get_settings()


@router.post("/sms/settings")
async def set_sms_settings(request: Request,
                           _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    auto_copy = body.get("auto_copy")
    if auto_copy is None:
        raise HTTPException(status_code=400, detail="auto_copy required")
    settings = await sms.update_settings(auto_copy=bool(auto_copy))
    audit.log("sms", "settings", ip=_client_ip(request), outcome="ok",
              detail={"auto_copy": settings.get("auto_copy")})
    return settings


@ws_router.websocket("/ws/sms")
async def sms_ws(websocket: WebSocket, ticket: str = Query(None)):
    ip = security.ws_client_ip(websocket)
    session_token = consume_connection_ticket(ticket, websocket.url.path)
    if not session_token:
        audit.log("sms", "ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/sms"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("sms", "ws", ip=ip, outcome="connected")
    queue = await sms.subscribe()

    async def _send() -> None:
        try:
            while True:
                payload = await queue.get()
                await websocket.send_text(json.dumps(payload))
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            return

    async def _drain() -> None:
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            return

    try:
        await asyncio.gather(
            _send(),
            _drain(),
            security.ws_session_watchdog(websocket, session_token, "sms", "ws", ip),
        )
    finally:
        await sms.unsubscribe(queue)
        audit.log("sms", "ws", ip=ip, outcome="disconnected")
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
