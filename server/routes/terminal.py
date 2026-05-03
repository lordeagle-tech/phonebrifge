import asyncio
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from .. import audit, security
from ..auth import verify_token

router = APIRouter()

_ADB_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def _first_adb_device() -> str | None:
    """Return the serial of the first ready adb device, or None."""
    adb = shutil.which("adb")
    if not adb:
        return None
    try:
        out = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=4
        ).stdout
    except Exception:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


@router.websocket("/ws/terminal")
async def terminal_ws(
    websocket: WebSocket,
    token: str = Query(None),
    mode: str = Query("laptop"),
    serial: str | None = Query(None),
):
    ip = security.ws_client_ip(websocket)
    if not verify_token(token):
        audit.log("control", "terminal_ws", ip=ip, outcome="auth_fail")
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/terminal"})
        await websocket.close(code=4029)
        return

    await websocket.accept()
    audit.log("control", "terminal_ws", ip=ip, outcome="connected")

    loop = asyncio.get_event_loop()

    pid, fd = pty.fork()

    if pid == 0:
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = os.path.expanduser("~")
        os.chdir(env["HOME"])
        if mode == "phone":
            adb = shutil.which("adb")
            if not adb:
                os.write(1, b"\r\n\x1b[31madb is not installed on this laptop.\x1b[0m\r\n")
                os.write(1, b"Install with: sudo apt install adb\r\n")
                sys.exit(1)
            target = serial if (serial and _ADB_SERIAL_RE.match(serial)) else _first_adb_device()
            if not target:
                os.write(1, b"\r\n\x1b[33mNo Android device detected over adb.\x1b[0m\r\n")
                os.write(1, b"Connect your phone (USB or wifi-pair) and try again.\r\n")
                sys.exit(1)
            os.write(1, f"\x1b[36m[ Connected to {target} via adb ]\x1b[0m\r\n".encode())
            os.execvpe(adb, [adb, "-s", target, "shell"], env)
            sys.exit(1)
        os.execvpe("bash", ["bash", "--login"], env)
        sys.exit(1)

    _set_pty_size(fd, 24, 80)

    async def read_from_pty() -> None:
        while True:
            try:
                data = await asyncio.to_thread(os.read, fd, 4096)
                await websocket.send_bytes(data)
            except OSError:
                break

    async def write_to_pty() -> None:
        while True:
            try:
                raw = await websocket.receive_text()
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "input":
                    data = msg.get("data", "")
                    os.write(fd, data.encode("utf-8"))
                elif kind == "resize":
                    rows = int(msg.get("rows", 24))
                    cols = int(msg.get("cols", 80))
                    _set_pty_size(fd, rows, cols)
            except WebSocketDisconnect:
                break
            except Exception:
                break

    try:
        await asyncio.gather(
            read_from_pty(),
            write_to_pty(),
            security.ws_session_watchdog(websocket, token, "control", "terminal_ws", ip),
        )
    finally:
        audit.log("control", "terminal_ws", ip=ip, outcome="disconnected")
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
            await websocket.close()
        except Exception:
            pass
