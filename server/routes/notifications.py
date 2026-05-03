"""
Notification mirroring routes.

REST under /api/notifications/* for snapshot + dismiss/open actions.
WebSocket at /ws/notifications for live updates.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, \
    WebSocketDisconnect
from fastapi import Query

from .. import audit, notifications, security
from ..auth import verify_token
from ..deps import require_auth

router = APIRouter()
ws_router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


@router.get("/notifications")
async def get_notifications(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Current snapshot of phone notifications."""
    return await notifications.get_snapshot()


@router.post("/notifications/dismiss")
async def dismiss_notification(request: Request,
                               _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    key = str(body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    result = await notifications.dismiss(key)
    audit.log("control", "notif_dismiss", ip=_client_ip(request),
              outcome="ok" if result.get("ok") else "err",
              detail={"key": key[:120], **{k: v for k, v in result.items() if k != "ok"}})
    return result


@router.post("/notifications/open")
async def open_notification(request: Request,
                            _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    package = str(body.get("package") or "").strip()
    if not package:
        raise HTTPException(status_code=400, detail="package required")
    result = await notifications.open_on_phone(package)
    audit.log("control", "notif_open", ip=_client_ip(request),
              outcome="ok" if result.get("ok") else "err",
              detail={"package": package[:120],
                      **{k: v for k, v in result.items() if k != "ok"}})
    return result


@ws_router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket, token: str = Query(None)):
    """Push the latest notification snapshot whenever it changes."""
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        audit.log("control", "notif_ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/notifications"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("control", "notif_ws", ip=ip, outcome="connected")
    queue = await notifications.subscribe()

    async def _send() -> None:
        try:
            while True:
                snap = await queue.get()
                await websocket.send_text(json.dumps(snap))
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            return

    async def _drain() -> None:
        # The client doesn't send data — but we still need to detect
        # disconnects, so consume any inbound text/ping frames.
        try:
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            return

    try:
        await asyncio.gather(
            _send(),
            _drain(),
            security.ws_session_watchdog(websocket, token, "control",
                                         "notif_ws", ip),
        )
    finally:
        await notifications.unsubscribe(queue)
        audit.log("control", "notif_ws", ip=ip, outcome="disconnected")
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
