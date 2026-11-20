"""
Local HTTPS/WSS server that runs on a *peer* laptop.

The main bridge proxies remote-control traffic to this server using
the per-peer secret it was given at pairing time. The server requires
HTTPS/WSS; its certificate is announced over the main bridge's
certificate-verified heartbeat and pinned by the main proxy.

This module only runs in the peer agent process — never on the main
bridge. It re-uses ``server.routes.screen._capture_frame`` and the
input dispatch logic from ``server.routes.input`` so the peer's
screen and the main laptop's screen behave identically.
"""
from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import pty
import secrets as _secrets
import signal
import struct
import sys
import termios
import time
from typing import Optional

import mss
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from PIL import Image
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Button, Controller as MouseController

# Mirror of the main-side defaults; kept in sync with server.routes.screen.
DEFAULT_FPS = 10
DEFAULT_QUALITY = 60
MAX_FPS = 30
MIN_QUALITY = 20
MAX_QUALITY = 95

_BUTTON_MAP = {"left": Button.left, "right": Button.right,
               "middle": Button.middle}
_KEY_MAP = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab,
    "Escape": Key.esc, "Delete": Key.delete,
    "ArrowUp": Key.up, "ArrowDown": Key.down,
    "ArrowLeft": Key.left, "ArrowRight": Key.right,
    "Home": Key.home, "End": Key.end,
    "PageUp": Key.page_up, "PageDown": Key.page_down,
    "F1": Key.f1, "F2": Key.f2, "F3": Key.f3, "F4": Key.f4,
    "F5": Key.f5, "F6": Key.f6, "F7": Key.f7, "F8": Key.f8,
    "F9": Key.f9, "F10": Key.f10, "F11": Key.f11, "F12": Key.f12,
    "Control": Key.ctrl, "Alt": Key.alt, "Shift": Key.shift,
    "Meta": Key.cmd, " ": Key.space,
}

_PEER_SECRET: str = ""
_primary_monitor: Optional[dict] = None
# Capabilities the most recently built app advertises. Computed in
# ``build_app`` based on which optional routers (files / stats /
# clipboard) actually mounted, plus the always-built-in features
# (screen / control / terminal). ``peer_agent`` reads this after
# starting the server and sends it on the heartbeat hello so the
# main bridge's picker can grey out tabs we can't serve.
_CAPABILITIES: list = []


def capabilities() -> list:
    """Snapshot of the features this peer agent currently exposes.
    Always returns a fresh copy so callers can't mutate the global.
    Probes import-availability on first call so callers (the
    heartbeat loop) don't have to wait for the FastAPI app to be
    built before knowing what to advertise."""
    if not _CAPABILITIES:
        caps = ["screen", "control", "terminal"]
        try:
            import server.deps  # noqa: F401
            import server.routes.clipboard  # noqa: F401
            import server.routes.files  # noqa: F401
            import server.routes.stats  # noqa: F401
        except ImportError:
            pass
        else:
            caps.extend(["files", "stats", "clipboard"])
        return caps
    return list(_CAPABILITIES)


def _check(secret: Optional[str]) -> None:
    """Constant-time secret check; raises 401 on mismatch. Routes use
    this rather than reading the global directly so the comparison
    can't accidentally short-circuit on length."""
    if not secret or not _secrets.compare_digest(_PEER_SECRET, secret):
        raise HTTPException(status_code=401, detail="Bad peer secret")


def _capture_frame(quality: int = DEFAULT_QUALITY,
                   scale: float = 1.0) -> bytes:
    """Identical to server.routes.screen._capture_frame, copied so the
    peer agent can run without importing the heavyweight server
    package on machines that only act as peers."""
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


def _get_primary_monitor() -> dict:
    global _primary_monitor
    if _primary_monitor is None:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            _primary_monitor = {
                "left": int(mon["left"]), "top": int(mon["top"]),
                "width": int(mon["width"]), "height": int(mon["height"]),
            }
    return _primary_monitor


def build_app(secret: str) -> FastAPI:
    """Create the peer's FastAPI app. The secret is captured into a
    module global so route handlers (which can't take captured locals
    as path-operation deps) can reach it."""
    global _PEER_SECRET
    _PEER_SECRET = secret
    app = FastAPI(title="Phone-Bridge Peer Agent",
                  docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        # Anonymous: just a liveness probe for the main bridge. No
        # secret leak, no stats — only a bare boolean.
        return {"ok": True}

    @app.get("/screen/frame")
    async def screen_frame(
        request: Request,
        quality: int = Query(DEFAULT_QUALITY, ge=MIN_QUALITY, le=MAX_QUALITY),
    ):
        _check(request.headers.get("x-peer-secret"))
        data = await asyncio.to_thread(_capture_frame, quality, 1.0)
        return Response(content=data, media_type="image/jpeg")

    @app.get("/screen.mjpeg")
    async def screen_mjpeg(
        request: Request,
        fps: int = Query(DEFAULT_FPS, ge=1, le=MAX_FPS),
        quality: int = Query(DEFAULT_QUALITY, ge=20, le=MAX_QUALITY),
    ):
        _check(request.headers.get("x-peer-secret"))
        interval = 1.0 / fps

        async def gen():
            while True:
                t0 = time.monotonic()
                try:
                    frame = await asyncio.to_thread(_capture_frame, quality, 1.0)
                except Exception:  # noqa: BLE001
                    break
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
                elapsed = time.monotonic() - t0
                sleep_t = max(0.0, interval - elapsed)
                if sleep_t > 0:
                    await asyncio.sleep(sleep_t)

        return StreamingResponse(
            gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.websocket("/input")
    async def input_ws(ws: WebSocket):
        # Can't raise HTTPException from a WS handler; do the manual
        # check + close on bad secret.
        secret = ws.headers.get("x-peer-secret")
        if not secret or not _secrets.compare_digest(_PEER_SECRET, secret):
            await ws.close(code=4001)
            return
        await ws.accept()
        mouse = MouseController()
        keyboard = KeyboardController()
        try:
            while True:
                try:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                except (WebSocketDisconnect, json.JSONDecodeError):
                    break
                kind = msg.get("type")
                if kind == "ping":
                    # Echoed back so the browser can compute RTT
                    # through the main bridge.
                    await ws.send_text(json.dumps({
                        "type": "pong",
                        "ts": msg.get("ts"),
                    }))
                elif kind == "move":
                    mouse.move(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
                elif kind == "move_to":
                    # Browser sends normalized (x,y) in 0..1 over the
                    # primary monitor — same convention as click_at —
                    # so the cursor warps without us needing to know
                    # the peer's resolution on the main bridge side.
                    try:
                        nx = float(msg.get("x", 0.0))
                        ny = float(msg.get("y", 0.0))
                    except (TypeError, ValueError):
                        continue
                    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                        continue
                    mon = _get_primary_monitor()
                    mouse.position = (
                        int(mon["left"] + nx * mon["width"]),
                        int(mon["top"] + ny * mon["height"]),
                    )
                elif kind == "click":
                    btn = _BUTTON_MAP.get(msg.get("button", "left"), Button.left)
                    mouse.click(btn)
                elif kind == "click_at":
                    try:
                        nx = float(msg.get("x", 0.0))
                        ny = float(msg.get("y", 0.0))
                    except (TypeError, ValueError):
                        continue
                    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
                        continue
                    btn = _BUTTON_MAP.get(msg.get("button", "left"), Button.left)
                    mon = _get_primary_monitor()
                    abs_x = int(mon["left"] + nx * mon["width"])
                    abs_y = int(mon["top"] + ny * mon["height"])
                    mouse.position = (abs_x, abs_y)
                    mouse.click(btn)
                elif kind == "scroll":
                    dx = float(msg.get("dx", 0))
                    dy = float(msg.get("dy", 0))
                    mouse.scroll(int(dx), int(-dy))
                elif kind == "key":
                    key_name = msg.get("key", "")
                    k = _KEY_MAP.get(key_name)
                    if k:
                        keyboard.press(k); keyboard.release(k)
                    elif len(key_name) == 1:
                        keyboard.press(key_name); keyboard.release(key_name)
                elif kind == "type":
                    text = msg.get("text", "")
                    if text:
                        keyboard.type(text)
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Phone-targeting endpoints — re-use the main bridge's HTTP routers
    # (files, stats, clipboard) so the phone's existing API contract
    # works unchanged when it points at this peer. We swap the
    # PIN-token dependency for a per-peer-secret check; the routers
    # themselves don't need to know which kind of bridge they're
    # mounted on.
    # ------------------------------------------------------------------
    async def _peer_secret_dep(request: Request) -> str:
        # Imported lazily so a peer-only deployment doesn't have to
        # import every main-bridge dependency at import time.
        secret = request.headers.get("x-peer-secret")
        if not secret or not _secrets.compare_digest(_PEER_SECRET, secret):
            raise HTTPException(status_code=401,
                                detail="Bad peer secret")
        return secret

    # Always-built-in features. ``terminal`` lives on /ws/terminal,
    # ``screen`` on /screen.*, ``control`` on /input — all defined
    # above unconditionally, so they're advertised even when the main
    # ``server`` package isn't importable.
    mounted = ["screen", "control", "terminal"]
    try:
        from server.deps import require_auth as _main_require_auth
        from server.routes import clipboard as _clipboard_route
        from server.routes import files as _files_route
        from server.routes import stats as _stats_route
        from server.request_limits import RequestBodyLimitMiddleware
    except ImportError:
        # Peers installed without the main server package can still
        # serve screen+input — file/stats/clipboard targeting just
        # won't be available from the phone picker.
        _main_require_auth = None
    else:
        app.dependency_overrides[_main_require_auth] = _peer_secret_dep
        app.include_router(_files_route.router, prefix="/api")
        app.include_router(_stats_route.router, prefix="/api")
        app.include_router(_clipboard_route.router, prefix="/api")
        mounted.extend(["files", "stats", "clipboard"])
        app.add_middleware(
            RequestBodyLimitMiddleware,
            path="/api/files/upload",
            max_bytes=_files_route.MAX_UPLOAD_BYTES + 1024 * 1024,
        )

    global _CAPABILITIES
    _CAPABILITIES = mounted

    @app.websocket("/ws/terminal")
    async def terminal_ws(ws: WebSocket):
        secret = ws.headers.get("x-peer-secret")
        if not secret or not _secrets.compare_digest(_PEER_SECRET, secret):
            await ws.close(code=4001)
            return
        await ws.accept()

        pid, fd = pty.fork()
        if pid == 0:
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["HOME"] = os.path.expanduser("~")
            try:
                os.chdir(env["HOME"])
            except OSError:
                pass
            os.execvpe("bash", ["bash", "--login"], env)
            sys.exit(1)

        def _set_size(rows: int, cols: int) -> None:
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0))
            except Exception:  # noqa: BLE001
                pass

        _set_size(24, 80)

        async def read_pty() -> None:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, fd, 4096)
                    if not data:
                        break
                    await ws.send_bytes(data)
                except (OSError, WebSocketDisconnect):
                    break

        async def write_pty() -> None:
            while True:
                try:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                except (WebSocketDisconnect, json.JSONDecodeError):
                    break
                except Exception:  # noqa: BLE001
                    break
                kind = msg.get("type")
                if kind == "input":
                    try:
                        os.write(fd, str(msg.get("data", "")).encode("utf-8"))
                    except OSError:
                        break
                elif kind == "resize":
                    _set_size(int(msg.get("rows", 24)),
                              int(msg.get("cols", 80)))

        try:
            await asyncio.gather(read_pty(), write_pty())
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    return app


async def serve(secret: str, host: str = "0.0.0.0", port: int = 8766,
                certfile: str = "", keyfile: str = "") -> None:
    """Run the peer server forever. Imported and awaited by
    ``peer_agent.py`` alongside the heartbeat loop."""
    import uvicorn
    app = build_app(secret)
    if not certfile or not keyfile:
        raise RuntimeError("Peer server requires a TLS certificate and private key")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, ssl_certfile=certfile,
                            ssl_keyfile=keyfile)
    server = uvicorn.Server(config)
    await server.serve()
