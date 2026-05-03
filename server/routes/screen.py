"""
Screen capture routes.

http_router: REST endpoints (register with prefix="/api")
  GET /screen/frame  - single JPEG frame
  GET /screen/stream - MJPEG stream (token via query param for <img> src usage)

ws_router: WebSocket (register at root, no prefix)
  WS /ws/screen - binary JPEG frame stream with control messages

NOTE: Screen capture requires an X11/display environment. When unavailable
(e.g., headless server), endpoints return 503 gracefully.
"""
import asyncio
import io
import time

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from PIL import Image

from .. import audit, security
from ..auth import verify_token
from ..deps import require_auth

http_router = APIRouter()
ws_router = APIRouter()

DEFAULT_FPS = 10
DEFAULT_QUALITY = 60
MAX_FPS = 30
MIN_QUALITY = 20
MAX_QUALITY = 95

try:
    import mss as _mss_module
    _MSS_AVAILABLE = True
except Exception:
    _MSS_AVAILABLE = False


def _capture_frame(quality: int = DEFAULT_QUALITY, scale: float = 0.5) -> bytes:
    """Capture primary monitor and return JPEG bytes."""
    import mss
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        if scale != 1.0:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        return buf.getvalue()


def _no_display_response() -> Response:
    return Response(
        content='{"detail": "Screen capture not available (no display)"}',
        status_code=503,
        media_type="application/json",
    )


@http_router.get("/screen/frame")
async def screen_frame(
    quality: int = Query(DEFAULT_QUALITY),
    _token: str = Depends(require_auth),
):
    """Single JPEG frame."""
    q = max(MIN_QUALITY, min(MAX_QUALITY, quality))
    try:
        data = await asyncio.to_thread(_capture_frame, q)
        return Response(content=data, media_type="image/jpeg")
    except Exception:
        return _no_display_response()


@http_router.get("/screen/stream")
async def screen_mjpeg(
    fps: int = Query(DEFAULT_FPS),
    quality: int = Query(DEFAULT_QUALITY),
    token: str = Query(None),
):
    """MJPEG stream — can be used as an <img> src."""
    if not verify_token(token):
        return Response(status_code=401)

    fps = max(1, min(MAX_FPS, fps))
    quality = max(MIN_QUALITY, min(MAX_QUALITY, quality))
    interval = 1.0 / fps

    async def generate():
        while True:
            t0 = time.monotonic()
            try:
                frame = await asyncio.to_thread(_capture_frame, quality)
            except Exception:
                break
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
            elapsed = time.monotonic() - t0
            sleep_t = max(0.0, interval - elapsed)
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@ws_router.websocket("/ws/screen")
async def screen_ws(
    websocket: WebSocket,
    fps: int = Query(DEFAULT_FPS),
    quality: int = Query(DEFAULT_QUALITY),
    token: str = Query(None),
):
    """WebSocket binary frame stream with fps/quality control."""
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        audit.log("screen", "screen_ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/screen"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("screen", "screen_ws", ip=ip, outcome="connected")

    fps = max(1, min(MAX_FPS, fps))
    quality = max(MIN_QUALITY, min(MAX_QUALITY, quality))
    interval = 1.0 / fps

    async def send_frames() -> None:
        nonlocal fps, quality, interval
        while True:
            t0 = time.monotonic()
            try:
                frame = await asyncio.to_thread(_capture_frame, quality)
                await websocket.send_bytes(frame)
            except (WebSocketDisconnect, Exception):
                break
            elapsed = time.monotonic() - t0
            sleep_t = max(0.0, interval - elapsed)
            if sleep_t > 0:
                await asyncio.sleep(sleep_t)

    async def recv_control() -> None:
        nonlocal fps, quality, interval
        while True:
            try:
                msg = await websocket.receive_json()
                if "fps" in msg:
                    fps = max(1, min(MAX_FPS, int(msg["fps"])))
                    interval = 1.0 / fps
                if "quality" in msg:
                    quality = max(MIN_QUALITY, min(MAX_QUALITY, int(msg["quality"])))
            except (WebSocketDisconnect, Exception):
                break

    try:
        await asyncio.gather(
            send_frames(),
            recv_control(),
            security.ws_session_watchdog(websocket, token, "screen", "screen_ws", ip),
        )
    finally:
        audit.log("screen", "screen_ws", ip=ip, outcome="disconnected")
        try:
            await websocket.close()
        except Exception:
            pass
