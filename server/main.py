import asyncio
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import audio_capture, audit, auth, mirror_recording, notifications, peer_pair_code, phone_capture, photos, scrcpy_watcher, security, sms, webrtc_config
from .deps import require_auth
from .routes import clipboard, files, input as input_route, notifications as notifications_route, peer_proxy as peer_proxy_route, peers as peers_route, photos as photos_route, screen, scrcpy, signaling, sms as sms_route, stats, terminal

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="Laptop-Phone Bridge", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def _bucket_for(path: str) -> Optional[str]:
    """Map a path to a rate-limit bucket. None = no limiting."""
    if path.startswith("/api/auth/"):
        return "auth"
    if path.startswith("/api/files"):
        return "files"
    if path.startswith("/api/clipboard"):
        return "clipboard"
    if path.startswith("/api/"):
        return "default"
    return None


def _audit_action_for(method: str, path: str) -> Optional[tuple[str, str]]:
    """Return (category, action) to audit-log, or None to skip."""
    if path.startswith("/api/auth/"):
        return None  # logged explicitly with success/failure
    if path.startswith("/api/files/upload"):
        return ("files", "upload")
    if path.startswith("/api/files/download-folder"):
        return ("files", "download_folder")
    if path.startswith("/api/files/download-zip"):
        return ("files", "download_zip")
    if path.startswith("/api/files/download"):
        return ("files", "download")
    if path == "/api/files" and method == "DELETE":
        return ("files", "delete")
    if path == "/api/clipboard" and method == "GET":
        return ("clipboard", "read")
    if path == "/api/clipboard" and method == "POST":
        return ("clipboard", "write")
    if path == "/api/activity":
        return None  # don't pollute the log with reads of itself
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        bucket = _bucket_for(path)
        if bucket is not None:
            ip = _client_ip(request)
            if not security.check_rate(ip, bucket):
                audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                          detail={"path": path, "bucket": bucket})
                raise HTTPException(status_code=429, detail="Too many requests")
        return await call_next(request)


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        info = _audit_action_for(request.method, request.url.path)
        response = await call_next(request)
        if info is not None:
            category, action = info
            outcome = "ok" if response.status_code < 400 else f"err_{response.status_code}"
            audit.log(
                category,
                action,
                ip=_client_ip(request),
                outcome=outcome,
                detail={"path": request.url.path},
            )
        return response


app.add_middleware(AuditMiddleware)
app.add_middleware(RateLimitMiddleware)


def _get_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.query_params.get("token")


@app.post("/api/auth/login")
async def login(request: Request):
    ip = _client_ip(request)
    locked, remaining = security.is_locked(ip)
    if locked:
        audit.log("auth", "login", ip=ip, outcome="locked",
                  detail={"retry_after_s": remaining})
        raise HTTPException(
            status_code=429,
            detail=f"Too many wrong PIN attempts. Try again in {remaining // 60 + 1} min.",
        )

    # Throttle: after a few recent failures, deliberately slow each new
    # request from this IP before even checking the PIN. This makes online
    # brute-force impractical without disturbing legitimate users (who only
    # hit the delay once they've already mistyped several times).
    delay = security.next_failure_delay(ip)
    if delay > 0:
        await asyncio.sleep(delay)

    body = await request.json()
    pin = str(body.get("pin", ""))

    if not auth.verify_pin(pin):
        now_locked, attempts_left = security.record_failure(ip)
        if now_locked:
            audit.log("auth", "lockout", ip=ip, outcome="locked",
                      detail={"window_s": security.LOCKOUT_SECONDS})
            print(
                f"\n[security] !! {ip} LOCKED OUT for "
                f"{security.LOCKOUT_SECONDS // 60} min after "
                f"{security.MAX_PIN_FAILS} wrong PIN attempts !!\n"
            )
            raise HTTPException(
                status_code=429,
                detail=f"Too many wrong PIN attempts. Locked out for {security.LOCKOUT_SECONDS // 60} minutes.",
            )
        audit.log("auth", "login", ip=ip, outcome="bad_pin",
                  detail={"attempts_left": attempts_left})
        print(
            f"[security] wrong PIN from {ip} "
            f"({attempts_left} attempt{'s' if attempts_left != 1 else ''} left before lockout)"
        )
        raise HTTPException(status_code=401, detail="Invalid PIN")

    security.record_success(ip)
    token = auth.create_session()
    audit.log("auth", "login", ip=ip, outcome="ok")
    return {"token": token}


@app.post("/api/auth/logout")
async def logout(request: Request):
    token = _get_token(request)
    if token:
        auth.invalidate_session(token)
        audit.log("auth", "logout", ip=_client_ip(request), outcome="ok")
    return {"ok": True}


@app.get("/api/auth/check")
async def auth_check(request: Request):
    token = _get_token(request)
    if not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"ok": True, "inactivity_timeout_s": int(auth.get_inactivity_timeout())}


@app.get("/api/webrtc/config")
async def webrtc_cfg(_token: str = Depends(require_auth)):
    """ICE server configuration for WebRTC peers (sender and viewer)."""
    return {"iceServers": webrtc_config.get_ice_servers()}


@app.get("/api/activity")
async def activity(
    limit: int = Query(50, ge=1, le=200),
    category: Optional[str] = Query(None),
    _token: str = Depends(require_auth),
):
    return {"events": audit.recent(limit=limit, category=category)}


# REST API routes
app.include_router(files.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(clipboard.router, prefix="/api")
app.include_router(scrcpy.router, prefix="/api")
# Browser-based mirror viewer (separate from the native scrcpy launch):
# REST endpoints already live under /api via scrcpy.router; the WebSocket
# `/ws/mirror` lives at root level like /ws/screen.
app.include_router(scrcpy.ws_router)

# Screen routes: REST under /api, WebSocket at /ws/screen
app.include_router(screen.http_router, prefix="/api")
app.include_router(screen.ws_router)

# Notification mirroring: REST under /api, WebSocket at /ws/notifications
app.include_router(notifications_route.router, prefix="/api")
app.include_router(notifications_route.ws_router)

# SMS inbox + OTP auto-copy: REST under /api, WebSocket at /ws/sms
app.include_router(sms_route.router, prefix="/api")
app.include_router(sms_route.ws_router)

# Auto-import phone photos to a laptop folder
app.include_router(photos_route.router, prefix="/api")

# WebSocket-only routes at root level
app.include_router(terminal.router)
app.include_router(input_route.router)
app.include_router(signaling.router)

# Peer-laptop linking: REST under /api/peers, heartbeat WS at /ws/peer
app.include_router(peers_route.router, prefix="/api")
app.include_router(peers_route.ws_router)
# Browser-facing proxy to a peer laptop (screen MJPEG + input WS).
# REST under /api, WS at /ws/peers/{id}/input.
app.include_router(peer_proxy_route.http_router, prefix="/api")
app.include_router(peer_proxy_route.ws_router)


@app.get("/viewer", include_in_schema=False)
async def viewer_alias():
    """Friendly /viewer alias for the laptop screen-share viewer page."""
    return RedirectResponse(url="/viewer.html", status_code=307)


@app.get("/mirror-setup", include_in_schema=False)
async def mirror_setup_alias():
    """Friendly /mirror-setup alias for the laptop scrcpy launcher page."""
    return RedirectResponse(url="/mirror-setup.html", status_code=307)


@app.get("/mirror-view", include_in_schema=False)
async def mirror_view_alias():
    """Friendly /mirror-view alias for the browser-based phone-mirror viewer."""
    return RedirectResponse(url="/mirror-view.html", status_code=307)


@app.get("/peer-control", include_in_schema=False)
async def peer_control_alias():
    """Friendly /peer-control alias for the remote peer-laptop viewer."""
    return RedirectResponse(url="/peer-control.html", status_code=307)


@app.on_event("startup")
async def _start_background_workers() -> None:
    # Auto-launches the mirror when an opted-in remembered device returns.
    scrcpy_watcher.start()
    # Polls the active device for live notifications.
    notifications.start()
    # Polls the active device for SMS inbox + OTP auto-copy.
    sms.start()
    # Auto-imports new phone photos into the user-chosen laptop folder.
    photos.start()


@app.on_event("shutdown")
async def _stop_background_workers() -> None:
    # Stop the watchers first so they can't try to talk to adb during
    # teardown, then make sure we don't leave an orphan scrcpy window.
    try:
        await scrcpy_watcher.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await notifications.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await sms.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        await photos.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import ring as _ring  # local import keeps startup ordering simple
        await _ring.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        scrcpy.scrcpy_state.stop()
    except Exception:  # noqa: BLE001
        pass
    # Finalise any in-flight recordings BEFORE we kill the capture
    # loops — the recorder needs to drain the last few queued frames
    # and run ffmpeg to mux the MP4. If we tear down phone_capture
    # first, the recorder's video pump sees an empty queue + sentinel
    # and finishes with whatever it had.
    try:
        await mirror_recording.stop_all()
    except Exception:  # noqa: BLE001
        pass
    # Tear down any active browser-mirror capture loops so the adb child
    # processes don't outlive the server.
    try:
        await phone_capture.stop_all()
    except Exception:  # noqa: BLE001
        pass
    # And the parallel browser-audio pipelines (separate adb children
    # for the on-device sndcpy server + the local TCP forwards).
    try:
        await audio_capture.stop_all()
    except Exception:  # noqa: BLE001
        pass


class _NoCacheStaticFiles(StaticFiles):
    """Static file server that disables browser caching for HTML/CSS/JS,
    so iterative UI changes show up on phones without a hard-refresh."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        try:
            if path.endswith((".html", ".css", ".js", ".mjs")) or path in ("", "/"):
                resp.headers["Cache-Control"] = "no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
        except Exception:
            pass
        return resp


app.mount("/", _NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
