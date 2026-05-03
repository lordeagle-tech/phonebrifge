"""
Remote input route — mouse and keyboard control via WebSocket.

Protocol (JSON messages from client):
  Mouse move:        {"type": "move",     "dx": float, "dy": float}
  Left click:        {"type": "click",    "button": "left"}
  Right click:       {"type": "click",    "button": "right"}
  Click at position: {"type": "click_at", "x": 0..1, "y": 0..1, "button": "left"|"right"}
  Scroll:            {"type": "scroll",   "dx": float, "dy": float}
  Key press:         {"type": "key",      "key": "<key_name>"}
  Type text:         {"type": "type",     "text": "<string>"}

`click_at` x/y are normalized (0..1) coordinates within the primary monitor —
the client sends the position relative to the screen-mirror image and the
server scales it to the laptop's actual screen resolution.

NOTE: pynput is optional. If unavailable (e.g., no display), mouse/keyboard
control will be disabled gracefully.
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from .. import audit, security
from ..auth import verify_token

router = APIRouter()

try:
    import mss as _mss
    import mss
    _MSS_AVAILABLE = True
except Exception:
    _MSS_AVAILABLE = False

try:
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Controller as KeyboardController, Key
    _PYNPUT_AVAILABLE = True

    _BUTTON_MAP = {
        "left": Button.left,
        "right": Button.right,
        "middle": Button.middle,
    }

    _KEY_MAP = {
        "Enter": Key.enter,
        "Backspace": Key.backspace,
        "Tab": Key.tab,
        "Escape": Key.esc,
        "Delete": Key.delete,
        "ArrowUp": Key.up,
        "ArrowDown": Key.down,
        "ArrowLeft": Key.left,
        "ArrowRight": Key.right,
        "Home": Key.home,
        "End": Key.end,
        "PageUp": Key.page_up,
        "PageDown": Key.page_down,
        "F1": Key.f1, "F2": Key.f2, "F3": Key.f3, "F4": Key.f4,
        "F5": Key.f5, "F6": Key.f6, "F7": Key.f7, "F8": Key.f8,
        "F9": Key.f9, "F10": Key.f10, "F11": Key.f11, "F12": Key.f12,
        "Control": Key.ctrl, "Alt": Key.alt, "Shift": Key.shift,
        "Meta": Key.cmd,
        " ": Key.space,
    }
except Exception:
    _PYNPUT_AVAILABLE = False
    _BUTTON_MAP = {}
    _KEY_MAP = {}


_primary_monitor: dict | None = None


def _get_primary_monitor() -> dict:
    """Return cached primary monitor geometry (left, top, width, height)."""
    global _primary_monitor
    if _primary_monitor is None:
        if _MSS_AVAILABLE:
            try:
                with mss.mss() as sct:
                    mon = sct.monitors[1]
                    _primary_monitor = {
                        "left": int(mon["left"]),
                        "top": int(mon["top"]),
                        "width": int(mon["width"]),
                        "height": int(mon["height"]),
                    }
                return _primary_monitor
            except Exception:
                pass
        _primary_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    return _primary_monitor


@router.websocket("/ws/input")
async def input_ws(websocket: WebSocket, token: str = Query(None)):
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        audit.log("control", "input_ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/input"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("control", "input_ws", ip=ip, outcome="connected")

    if not _PYNPUT_AVAILABLE:
        await websocket.send_text('{"error": "Mouse/keyboard control not available in this environment"}')
        await websocket.close()
        return

    mouse = MouseController()
    keyboard = KeyboardController()

    async def watchdog():
        await security.ws_session_watchdog(websocket, token, "control", "input_ws", ip)

    watch_task = asyncio.create_task(watchdog())

    try:
        while True:
            try:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
            except (WebSocketDisconnect, Exception):
                break

            kind = msg.get("type")

            if kind == "move":
                dx = float(msg.get("dx", 0))
                dy = float(msg.get("dy", 0))
                mouse.move(dx, dy)

            elif kind == "click":
                btn_name = msg.get("button", "left")
                btn = _BUTTON_MAP.get(btn_name, Button.left)
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
                max_x = mon["left"] + mon["width"] - 1
                max_y = mon["top"] + mon["height"] - 1
                abs_x = min(max_x, max(mon["left"], int(mon["left"] + nx * mon["width"])))
                abs_y = min(max_y, max(mon["top"], int(mon["top"] + ny * mon["height"])))
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
                    keyboard.press(k)
                    keyboard.release(k)
                elif len(key_name) == 1:
                    keyboard.press(key_name)
                    keyboard.release(key_name)

            elif kind == "type":
                text = msg.get("text", "")
                if text:
                    keyboard.type(text)

    finally:
        watch_task.cancel()
        audit.log("control", "input_ws", ip=ip, outcome="disconnected")
        try:
            await websocket.close()
        except Exception:
            pass
