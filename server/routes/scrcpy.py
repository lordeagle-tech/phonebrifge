"""
One-click scrcpy / adb mirror endpoints.

All routes require the bridge PIN session (`require_auth`). They are intended
for the laptop browser (the `/mirror-setup` page), but the auth gate is the
same as every other API route — there is no implicit "laptop only" check.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from .. import (audio_capture, audit, mirror_recording, phone_capture,
                 phone_input, ring, scrcpy_state, scrcpy_watcher, security,
                 sndcpy_state)
from ..auth import consume_connection_ticket
from ..deps import require_auth

router = APIRouter()
ws_router = APIRouter()

ADB_TIMEOUT = 8.0          # adb commands should be quick on a healthy host
ADB_PAIR_TIMEOUT = 20.0    # pairing rounds-trip can be a bit slower
QR_FIND_PAIR_TIMEOUT = 90.0   # how long to wait for the phone to advertise after scan
QR_FIND_CONN_TIMEOUT = 30.0   # how long to wait for the post-pair connect service

# adb serials are alphanumerics + a few separators (USB serials, IPs, ".local")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
PAIR_CODE_RE = re.compile(r"^\d{4,8}$")


async def _run(cmd: List[str], *, timeout: float = ADB_TIMEOUT,
               input_text: Optional[str] = None) -> Dict[str, Any]:
    """Run a subprocess, capturing stdout/stderr/return code with a timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e),
                "missing": cmd[0]}
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "code": -1, "stdout": "",
                "stderr": f"Timed out after {timeout:.0f}s"}
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


@router.get("/scrcpy/deps")
async def deps(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Probe scrcpy / adb installation. Used by the setup page on load."""
    scrcpy_path = shutil.which("scrcpy")
    adb_path = shutil.which("adb")
    adb_version = None
    if adb_path:
        r = await _run([adb_path, "version"])
        if r["ok"]:
            first = r["stdout"].splitlines()[0] if r["stdout"] else ""
            adb_version = first.strip()
    scrcpy_version = None
    if scrcpy_path:
        r = await _run([scrcpy_path, "--version"])
        # scrcpy prints version on the first stdout line
        if r["stdout"]:
            scrcpy_version = r["stdout"].splitlines()[0].strip()
    # sndcpy / vlc are optional — only used when the user opts a device into
    # "play phone audio through laptop speakers". We surface them in the
    # same payload so the UI can render install hints in one place.
    audio = sndcpy_state.deps()
    return {
        "scrcpy": {"installed": bool(scrcpy_path), "path": scrcpy_path,
                   "version": scrcpy_version},
        "adb": {"installed": bool(adb_path), "path": adb_path,
                "version": adb_version},
        "sndcpy": audio["sndcpy"],
        "vlc": audio["vlc"],
        "install_hint": "sudo apt install scrcpy adb",
        "audio_install_hint": audio["install_hint"],
    }


def _parse_devices(stdout: str) -> List[Dict[str, str]]:
    """Parse `adb devices -l` into a list of {serial, state, model, ...}."""
    devices: List[Dict[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info: Dict[str, str] = {"serial": serial, "state": state}
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                info[k] = v
        # Friendly name: prefer model, else IP-style serial, else serial
        name = info.get("model") or info.get("device") or serial
        info["name"] = name.replace("_", " ")
        info["wireless"] = ":" in serial  # adb-over-tcp serials look like ip:port
        devices.append(info)
    return devices


@router.get("/scrcpy/devices")
async def devices(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    r = await _run(["adb", "devices", "-l"])
    if not r["ok"]:
        raise HTTPException(status_code=500,
                            detail=r["stderr"] or "adb devices failed")
    return {"devices": _parse_devices(r["stdout"])}


def _validate_host_port(host: str, port: int) -> None:
    if not HOST_RE.match(host or ""):
        raise HTTPException(status_code=400, detail="Invalid host")
    if not (1 <= int(port) <= 65535):
        raise HTTPException(status_code=400, detail="Invalid port")


@router.post("/scrcpy/pair")
async def pair(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    host = str(body.get("host", "")).strip()
    port = int(body.get("port") or 0)
    code = str(body.get("code", "")).strip()
    _validate_host_port(host, port)
    if not PAIR_CODE_RE.match(code):
        raise HTTPException(status_code=400,
                            detail="Pairing code must be 4-8 digits")
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")

    # `adb pair host:port` then prompts for the code on stdin
    r = await _run(["adb", "pair", f"{host}:{port}"],
                   input_text=code + "\n", timeout=ADB_PAIR_TIMEOUT)
    audit.log("scrcpy", "pair", ip=_client_ip(request),
              outcome="ok" if r["ok"] else "fail",
              detail={"host": host, "port": port})
    if not r["ok"]:
        msg = (r["stderr"] or r["stdout"] or "Pairing failed").strip()
        raise HTTPException(status_code=400, detail=msg[:500])
    return {"ok": True, "output": r["stdout"].strip()}


@router.post("/scrcpy/connect")
async def connect(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    host = str(body.get("host", "")).strip()
    port = int(body.get("port") or 0)
    _validate_host_port(host, port)
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    r = await _run(["adb", "connect", f"{host}:{port}"])
    audit.log("scrcpy", "connect", ip=_client_ip(request),
              outcome="ok" if r["ok"] else "fail",
              detail={"host": host, "port": port})
    out = (r["stdout"] or r["stderr"]).strip()
    if not r["ok"] or "failed" in out.lower() or "cannot" in out.lower():
        raise HTTPException(status_code=400, detail=out[:500] or "connect failed")
    return {"ok": True, "output": out, "serial": f"{host}:{port}"}


@router.post("/scrcpy/disconnect")
async def disconnect(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    r = await _run(["adb", "disconnect", serial])
    audit.log("scrcpy", "disconnect", ip=_client_ip(request),
              outcome="ok" if r["ok"] else "fail", detail={"serial": serial})
    return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip()}


@router.post("/scrcpy/launch")
async def launch(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    preset = str(body.get("preset", "default")).strip() or "default"
    name = str(body.get("name", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if preset not in scrcpy_state.PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {preset}")
    # Remember the friendly name BEFORE launch so the recents list keeps the
    # nice device label even if scrcpy itself fails to start.
    if name:
        scrcpy_state.remember_device(serial, name)
    try:
        # launch() runs a short link probe (ping/TCP, ~1-2s on a degraded
        # link) before spawning scrcpy, so push it off the event loop.
        st = await asyncio.to_thread(
            scrcpy_state.launch, serial, preset, None, name or None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.log("scrcpy", "launch", ip=_client_ip(request), outcome="ok",
              detail={"serial": serial, "preset": preset, "name": name or None,
                      "tuning": st.get("tuning")})
    return st


# ---------- QR-code wireless pairing (Android 11+ "Pair device with QR code") ----------
#
# Android 11+ exposes a native "Pair device with QR code" workflow under
# Wireless debugging. The QR encodes a Wi-Fi-style payload:
#     WIFI:T:ADB;S:<service-name>;P:<password>;;
# When the phone scans it, the phone advertises an mDNS service of type
# `_adb-tls-pairing._tcp` named "<service-name>". We then call `adb pair`
# against the discovered host:port (using the password) and, once paired,
# wait for the matching `_adb-tls-connect._tcp` service to appear and
# `adb connect` to it. The result is a fully wireless-connected serial in
# `host:port` form, ready for `scrcpy`.

_qr_lock = asyncio.Lock()
_qr_state: Dict[str, Any] = {
    "active": False,
    "service": None,
    "code": None,
    "qr_payload": None,
    "started_at": None,
    "status": "idle",   # idle | waiting_scan | pairing | connecting | paired | error | cancelled
    "message": "",
    "error": "",
    "paired_serial": None,
}
_qr_task: Optional[asyncio.Task] = None


def _qr_snapshot() -> Dict[str, Any]:
    s = dict(_qr_state)
    # Don't leak the raw password back over the wire after the first read —
    # it's only useful when generating the QR.
    if s["status"] != "waiting_scan":
        s["code"] = None
        s["qr_payload"] = None
    return s


async def _adb_mdns_services() -> List[Dict[str, str]]:
    """Parse `adb mdns services` into a list of {name, type, host, port}."""
    r = await _run(["adb", "mdns", "services"], timeout=5.0)
    services: List[Dict[str, str]] = []
    if not r["ok"]:
        return services
    # Each non-header line looks roughly like:
    #   <name>  <type>  <host>:<port>
    line_re = re.compile(r"^(\S+)\s+(\S+)\s+([0-9A-Fa-f.:\-]+):(\d+)\s*$")
    for line in r["stdout"].splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        services.append({
            "name": m.group(1),
            "type": m.group(2),
            "host": m.group(3),
            "port": m.group(4),
        })
    return services


async def _qr_pair_worker(service: str, code: str) -> None:
    """Background coroutine that waits for the phone, pairs, and connects."""
    try:
        _qr_state["status"] = "waiting_scan"
        _qr_state["message"] = "Waiting for the phone to scan the QR code…"

        # Step 1 — discover the pairing service the phone advertises.
        deadline = time.monotonic() + QR_FIND_PAIR_TIMEOUT
        pair_addr: Optional[tuple[str, str]] = None
        while time.monotonic() < deadline:
            for s in await _adb_mdns_services():
                if s["name"] == service and "pairing" in s["type"]:
                    pair_addr = (s["host"], s["port"])
                    break
            if pair_addr:
                break
            await asyncio.sleep(1.0)

        if not pair_addr:
            _qr_state["status"] = "error"
            _qr_state["error"] = (
                "Did not see the phone on the network. Make sure both devices "
                "are on the same Wi-Fi and that you scanned the QR from "
                "Wireless debugging → Pair device with QR code."
            )
            return

        # Step 2 — pair using the password from the QR.
        _qr_state["status"] = "pairing"
        _qr_state["message"] = f"Pairing with {pair_addr[0]}:{pair_addr[1]}…"
        r = await _run(
            ["adb", "pair", f"{pair_addr[0]}:{pair_addr[1]}"],
            input_text=code + "\n",
            timeout=ADB_PAIR_TIMEOUT,
        )
        if not r["ok"]:
            _qr_state["status"] = "error"
            _qr_state["error"] = (
                (r["stderr"] or r["stdout"]).strip() or "adb pair failed"
            )[:500]
            return

        # Step 3 — wait for the post-pair connect service from the same host.
        _qr_state["status"] = "connecting"
        _qr_state["message"] = "Pairing succeeded. Connecting over ADB…"
        deadline = time.monotonic() + QR_FIND_CONN_TIMEOUT
        connect_addr: Optional[tuple[str, str]] = None
        while time.monotonic() < deadline:
            for s in await _adb_mdns_services():
                if "connect" in s["type"] and s["host"] == pair_addr[0]:
                    connect_addr = (s["host"], s["port"])
                    break
            if connect_addr:
                break
            await asyncio.sleep(1.0)

        if not connect_addr:
            _qr_state["status"] = "error"
            _qr_state["error"] = (
                "Paired, but the phone never advertised an ADB connect port. "
                "Open the Connected devices section and try Detect devices."
            )
            return

        cr = await _run(["adb", "connect", f"{connect_addr[0]}:{connect_addr[1]}"])
        out = (cr["stdout"] or cr["stderr"]).strip()
        if not cr["ok"] or "failed" in out.lower() or "cannot" in out.lower():
            _qr_state["status"] = "error"
            _qr_state["error"] = out[:500] or "adb connect failed"
            return

        serial = f"{connect_addr[0]}:{connect_addr[1]}"
        scrcpy_state.remember_device(serial, name=serial)
        _qr_state["status"] = "paired"
        _qr_state["paired_serial"] = serial
        _qr_state["message"] = f"Connected as {serial}"
    except asyncio.CancelledError:
        _qr_state["status"] = "cancelled"
        _qr_state["message"] = "Cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        _qr_state["status"] = "error"
        _qr_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _qr_state["active"] = False


@router.post("/scrcpy/pair-qr/start")
async def pair_qr_start(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Generate a one-time QR payload for Android's Wireless-debug QR pairing."""
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    global _qr_task
    async with _qr_lock:
        if _qr_state["active"] and _qr_state["status"] in {
            "waiting_scan", "pairing", "connecting"
        }:
            raise HTTPException(
                status_code=409,
                detail="A QR pairing is already in progress. Cancel it first.",
            )
        # Random service name + 6-digit code. The service-name suffix avoids
        # collisions if the user retries quickly.
        service = f"bridge-{secrets.token_hex(3)}"
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        payload = f"WIFI:T:ADB;S:{service};P:{code};;"
        _qr_state.update({
            "active": True,
            "service": service,
            "code": code,
            "qr_payload": payload,
            "started_at": int(time.time()),
            "status": "waiting_scan",
            "message": "Waiting for the phone to scan the QR code…",
            "error": "",
            "paired_serial": None,
        })
        _qr_task = asyncio.create_task(_qr_pair_worker(service, code))
    audit.log("scrcpy", "pair_qr_start", ip=_client_ip(request), outcome="ok",
              detail={"service": service})
    # Return the payload + code only on this initial call so the UI can
    # render the QR and (optionally) display the code as a fallback.
    return {
        "qr_payload": payload,
        "service": service,
        "code": code,
        "status": _qr_state["status"],
    }


@router.get("/scrcpy/pair-qr/status")
async def pair_qr_status(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return _qr_snapshot()


@router.post("/scrcpy/pair-qr/cancel")
async def pair_qr_cancel(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    global _qr_task
    async with _qr_lock:
        if _qr_task and not _qr_task.done():
            _qr_task.cancel()
        _qr_state["active"] = False
        if _qr_state["status"] in {"waiting_scan", "pairing", "connecting"}:
            _qr_state["status"] = "cancelled"
            _qr_state["message"] = "Cancelled"
    audit.log("scrcpy", "pair_qr_cancel", ip=_client_ip(request), outcome="ok")
    return _qr_snapshot()


@router.post("/scrcpy/stop")
async def stop(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    before = scrcpy_state.status()
    st = scrcpy_state.stop()
    if before.get("running") and before.get("serial"):
        restore = await phone_input.device_action(before["serial"], "rotate_restore")
        st["rotation_restore"] = restore
    audit.log("scrcpy", "stop", ip=_client_ip(request), outcome="ok")
    return st


@router.get("/scrcpy/status")
async def status(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return scrcpy_state.status()


@router.post("/scrcpy/device-action")
async def device_action(request: Request,
                        _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Send a fixed phone-control action to the selected ADB device."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    action = str(body.get("action", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if action not in {
        "home", "back", "recents", "rotate", "rotate_restore",
        "awake_on", "awake_off",
    }:
        raise HTTPException(status_code=400, detail="Unsupported phone action")
    result = await phone_input.device_action(serial, action)
    audit.log("control", "phone_action", ip=_client_ip(request),
              outcome="ok" if result.get("ok") else "fail",
              detail={"serial": serial, "action": action})
    if not result.get("ok"):
        raise HTTPException(status_code=502,
                            detail=str(result.get("error") or "Phone action failed")[:300])
    return result


@router.get("/scrcpy/recent")
async def recent(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return {
        "recent": scrcpy_state.list_recent(),
        "watcher": scrcpy_watcher.status(),
    }


@router.post("/scrcpy/forget")
async def forget(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    scrcpy_state.forget_device(serial)
    return {"ok": True}


@router.get("/scrcpy/quiet-hours")
async def get_quiet_hours(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return {**scrcpy_state.get_quiet_hours(),
            "now_quiet": scrcpy_state.in_quiet_hours()["quiet"]}


@router.post("/scrcpy/quiet-hours")
async def set_quiet_hours(request: Request,
                          _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    start = str(body.get("start", "")).strip()
    end = str(body.get("end", "")).strip()
    try:
        cfg = scrcpy_state.set_quiet_hours(enabled, start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log("scrcpy", "quiet_hours_set", ip=_client_ip(request), outcome="ok",
              detail={"enabled": enabled, "start": start, "end": end})
    return {**cfg, "now_quiet": scrcpy_state.in_quiet_hours()["quiet"]}


@router.post("/scrcpy/ring/start")
async def ring_start(request: Request,
                     _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Make the phone ring loudly (alarm stream) for ~30 s."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    duration = body.get("duration", ring.DEFAULT_DURATION)
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration must be a number")
    result = await ring.start(serial, duration=duration)
    outcome = "ok" if result.get("ok") else "err"
    audit.log("scrcpy", "ring_start", ip=_client_ip(request), outcome=outcome,
              detail={"serial": serial, "duration": duration,
                      "extended": bool(result.get("extended")),
                      "error": result.get("error")})
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "ring failed"))
    return result


@router.post("/scrcpy/ring/stop")
async def ring_stop(request: Request,
                    _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    result = await ring.stop(serial)
    audit.log("scrcpy", "ring_stop", ip=_client_ip(request),
              outcome="ok" if result.get("ok") else "err",
              detail={"serial": serial, "stopped": bool(result.get("stopped"))})
    return result


@router.get("/scrcpy/ring/status")
async def ring_status(serial: str = "",
                      _token: str = Depends(require_auth)) -> Dict[str, Any]:
    serial = serial.strip()
    if serial and not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    return await ring.get_status(serial or None)


@router.post("/scrcpy/audio")
async def set_audio(request: Request,
                    _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Toggle "play phone audio through laptop speakers" for a remembered
    device. Takes effect on the next mirror launch — we deliberately do not
    start/stop sndcpy here so flipping the toggle doesn't surprise the user
    with a sudden audio popup mid-session."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    enabled = bool(body.get("enabled", False))
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    updated = scrcpy_state.set_audio(serial, enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="Device is not in the recents list")
    audit.log("scrcpy", "audio_set", ip=_client_ip(request), outcome="ok",
              detail={"serial": serial, "enabled": enabled})
    return {"ok": True, "device": updated, "audio": sndcpy_state.status()}


@router.post("/scrcpy/quality")
async def set_quality(request: Request,
                      _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Pin the auto-tuner choice for one remembered device.

    Body: {"serial": "<adb serial>", "override": "auto"|"low"|"medium"|"high"}.
    "auto" re-enables the link probe; the others force that profile and
    skip the probe on subsequent launches.
    """
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    override = str(body.get("override", "")).strip().lower()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    try:
        updated = scrcpy_state.set_quality(serial, override)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404,
                            detail="Device not in recents — launch it once first")
    audit.log("scrcpy", "quality", ip=_client_ip(request), outcome="ok",
              detail={"serial": serial, "override": override})
    return {"ok": True, "device": updated}


@router.post("/scrcpy/auto")
async def set_auto(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Toggle "auto-launch when this device comes online" for a remembered device."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    enabled = bool(body.get("enabled", False))
    preset = str(body.get("preset", "")).strip() or None
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if preset and preset not in scrcpy_state.PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {preset}")
    updated = scrcpy_state.set_auto(serial, enabled, preset)
    if updated is None:
        raise HTTPException(status_code=404, detail="Device is not in the recents list")
    audit.log("scrcpy", "auto_set", ip=_client_ip(request), outcome="ok",
              detail={"serial": serial, "enabled": enabled, "preset": preset})
    return {"ok": True, "device": updated, "watcher": scrcpy_watcher.status()}


# ---------- Browser-based mirror viewer ---------------------------------
#
# A second, browser-only viewer that streams periodic screencap PNGs over a
# WebSocket so any device on the LAN can watch the phone screen without
# installing scrcpy. Completely independent of the native scrcpy launch
# above — both can run side by side. The capture itself lives in
# `phone_capture.py` so it can be reused later for recording.

@router.post("/scrcpy/viewer/start")
async def viewer_start(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    fps = int(body.get("fps") or phone_capture.DEFAULT_FPS)
    # mode: "png" (legacy screencap) or "h264" (screenrecord +
    # WebCodecs). Browsers without WebCodecs send "png".
    mode = str(body.get("mode") or phone_capture.MODE_PNG).strip().lower()
    if mode not in phone_capture.VALID_MODES:
        raise HTTPException(status_code=400,
                            detail=f"Invalid mode (use one of {phone_capture.VALID_MODES})")
    bitrate = int(body.get("bitrate") or phone_capture.DEFAULT_H264_BITRATE)
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    sess = await phone_capture.start_session(serial, fps=fps, mode=mode,
                                             bitrate=bitrate)
    audit.log("scrcpy", "viewer_start", ip=_client_ip(request), outcome="ok",
              detail={"serial": serial, "fps": sess.fps, "mode": sess.mode})
    return {"ok": True, "session": sess.status()}


@router.post("/scrcpy/viewer/stop")
async def viewer_stop(request: Request, _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    stopped = await phone_capture.stop_session(serial)
    audit.log("scrcpy", "viewer_stop", ip=_client_ip(request),
              outcome="ok" if stopped else "noop",
              detail={"serial": serial})
    return {"ok": True, "stopped": stopped}


@router.get("/scrcpy/viewer/status")
async def viewer_status(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return {"sessions": phone_capture.list_sessions()}


@router.post("/scrcpy/viewer/bitrate")
async def viewer_bitrate(request: Request,
                          _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Pin the H.264 encoder to a fixed bit-rate, or return to auto.

    Body: ``{"serial": "...", "bitrate": <int bps> | null}``.
    A null / missing / 0 ``bitrate`` clears the pin and re-enables the
    adaptive loop. Server clamps positive values to the same
    floor/ceiling the adaptive loop uses, so a bad client value can't
    break the encoder."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    raw = body.get("bitrate", None)
    target: Optional[int]
    if raw is None or raw == 0 or raw == "":
        target = None
    else:
        try:
            target = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="bitrate must be an integer (bps) or null")
        if target < 0:
            raise HTTPException(status_code=400, detail="bitrate must be >= 0")
    sess = phone_capture.get_session(serial)
    if sess is None or not sess.is_running():
        raise HTTPException(status_code=404,
                            detail="No mirror capture is running for this device.")
    if sess.mode != phone_capture.MODE_H264:
        raise HTTPException(status_code=409,
                            detail="Quality pinning is only available in H.264 mode.")
    info = sess.pin_bitrate(target)
    audit.log("scrcpy", "viewer_pin_bitrate", ip=_client_ip(request),
              outcome="ok", detail={"serial": serial, "bitrate": target})
    return {"ok": True, "session": info}


@ws_router.websocket("/ws/mirror")
async def mirror_ws(websocket: WebSocket) -> None:
    """Stream PNG frames for one device's capture session.

    Query params: `serial` (required), `ticket` (one-use connection ticket).
    Each binary message is a single PNG frame; an empty payload is the
    "stream ended" sentinel so the browser can close cleanly.
    """
    serial = (websocket.query_params.get("serial") or "").strip()
    ticket = websocket.query_params.get("ticket")
    ip = security.ws_client_ip(websocket)
    if not SERIAL_RE.match(serial):
        await websocket.close(code=4400)
        return
    session_token = consume_connection_ticket(ticket, websocket.url.path)
    if not session_token:
        audit.log("scrcpy", "mirror_ws", ip=ip, outcome="auth_fail",
                  detail={"serial": serial})
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/mirror"})
        await websocket.close(code=4029)
        return

    sess = phone_capture.get_session(serial)
    if sess is None or not sess.is_running():
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": "Capture not running. Click 'Start browser viewer' on the setup page.",
        })
        await websocket.close(code=4404)
        return

    queue = await sess.add_subscriber()
    if queue is None:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": f"Too many viewers (max {phone_capture.MAX_SUBSCRIBERS}).",
        })
        await websocket.close(code=4429)
        return

    await websocket.accept()
    audit.log("scrcpy", "mirror_ws", ip=ip, outcome="connected",
              detail={"serial": serial})
    # Send a tiny hello so the client can flip its status pill to "live"
    # before the first frame arrives. For h264 mode we also include the
    # decoder hints (codec string + base64 SPS/PPS) when we already have
    # them — the client can configure VideoDecoder before the first AU
    # lands and start decoding immediately. If we haven't seen an SPS
    # yet, the client extracts it from the first received AU instead.
    hello = {
        "type": "hello",
        "serial": serial,
        "fps": sess.fps,
        "mode": sess.mode,
    }
    if sess.mode == phone_capture.MODE_H264:
        cfg = sess.codec_config()
        if cfg:
            hello["codec"] = cfg
    try:
        await websocket.send_json(hello)
    except Exception:  # noqa: BLE001
        pass

    async def send_frames() -> None:
        while True:
            try:
                frame = await queue.get()
            except asyncio.CancelledError:
                return
            if not frame:
                # Sentinel: capture session stopped.
                try:
                    await websocket.send_json({"type": "ended"})
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                await websocket.send_bytes(frame)
            except (WebSocketDisconnect, Exception):
                return

    async def recv_control() -> None:
        # Inbound JSON messages drive the phone via `adb shell input`.
        # Coordinates are sent in *phone pixels* — the browser already
        # knows the capture frame's pixel size (it sets the canvas to
        # img.naturalWidth/Height) so we don't need to re-derive the
        # device resolution server-side.
        #
        #   {"type": "tap",   "x": int, "y": int}
        #   {"type": "swipe", "x1": int, "y1": int, "x2": int, "y2": int,
        #                     "duration": int}    # ms, optional
        #   {"type": "key",   "key": "Enter" | "Backspace" | ...}
        #   {"type": "text",  "text": "..."}
        #
        # Errors are reported back as {"type": "input_error", ...} so the
        # UI can surface them; we never close the socket on a bad event.
        import json as _json
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, Exception):
                return
            try:
                msg = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            kind = msg.get("type") if isinstance(msg, dict) else None
            if not kind:
                continue
            try:
                if kind == "tap":
                    result = await phone_input.tap(serial,
                                                   msg.get("x"), msg.get("y"))
                elif kind == "swipe":
                    result = await phone_input.swipe(
                        serial,
                        msg.get("x1"), msg.get("y1"),
                        msg.get("x2"), msg.get("y2"),
                        duration_ms=int(msg.get("duration") or 200),
                    )
                elif kind == "key":
                    result = await phone_input.keyevent(
                        serial, str(msg.get("key", "")),
                    )
                elif kind == "text":
                    result = await phone_input.type_text(
                        serial, str(msg.get("text", "")),
                    )
                elif kind == "ping":
                    # Cheap keepalive that some browsers emit; ignore.
                    continue
                elif kind == "stats":
                    # Adaptive-bitrate heartbeat from the viewer. We
                    # accept a single field today (`bufferedAmount` from
                    # the WebSocket) but tolerate extras so future
                    # signals (RTT, decode lag) don't need a protocol
                    # bump. Never replied to or surfaced as an error —
                    # the sender just keeps us informed.
                    try:
                        ba = int(msg.get("bufferedAmount") or 0)
                    except (TypeError, ValueError):
                        ba = 0
                    sess.report_subscriber_stats(queue, buffered_amount=ba)
                    # Reply with the current encoder stats so the
                    # viewer can show the live bit-rate (and detect
                    # adaptive step-ups / step-downs by watching the
                    # bitrate_changes counter). Only meaningful in
                    # h264 mode — in PNG mode these fields are None
                    # and the client will simply ignore them.
                    if sess.mode == phone_capture.MODE_H264:
                        st = sess.status()
                        try:
                            await websocket.send_json({
                                "type": "stats",
                                "bitrate": st.get("bitrate"),
                                "target_bitrate": st.get("target_bitrate"),
                                "bitrate_changes": st.get("bitrate_changes"),
                            })
                        except Exception:  # noqa: BLE001
                            return
                    continue
                else:
                    result = {"ok": False, "error": f"unknown event: {kind}"}
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            if not result.get("ok"):
                try:
                    await websocket.send_json({
                        "type": "input_error",
                        "kind": kind,
                        "error": str(result.get("error") or "input failed"),
                    })
                except Exception:  # noqa: BLE001
                    return
                audit.log("scrcpy", "mirror_input", ip=ip, outcome="fail",
                          detail={"serial": serial, "kind": kind,
                                  "error": result.get("error")})

    try:
        await asyncio.gather(
            send_frames(),
            recv_control(),
            security.ws_session_watchdog(websocket, session_token, "scrcpy",
                                         "mirror_ws", ip),
            return_exceptions=True,
        )
    finally:
        await sess.remove_subscriber(queue)
        audit.log("scrcpy", "mirror_ws", ip=ip, outcome="disconnected",
                  detail={"serial": serial})
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# ---------- Browser-based audio viewer ----------------------------------
#
# Streams the phone's *output* audio over a WebSocket so the same
# /mirror-view tab can hear what's playing on the phone. Mutually
# exclusive with the existing per-device "Send audio to laptop"
# (sndcpy_state) toggle since they share an abstract Unix socket on the
# device — the HTTP layer enforces that with a 409.

@router.get("/scrcpy/audio-viewer/deps")
async def audio_viewer_deps(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return audio_capture.deps()


@router.post("/scrcpy/audio-viewer/start")
async def audio_viewer_start(request: Request,
                              _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    if not shutil.which("adb"):
        raise HTTPException(status_code=503, detail="adb is not installed")
    d = audio_capture.deps()
    if not d["ready"]:
        raise HTTPException(status_code=503, detail=d["install_hint"])
    try:
        sess = await audio_capture.start_session(serial)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.log("scrcpy", "audio_viewer_start", ip=_client_ip(request),
              outcome="ok", detail={"serial": serial})
    return {"ok": True, "session": sess.status()}


@router.post("/scrcpy/audio-viewer/stop")
async def audio_viewer_stop(request: Request,
                             _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    stopped = await audio_capture.stop_session(serial)
    audit.log("scrcpy", "audio_viewer_stop", ip=_client_ip(request),
              outcome="ok" if stopped else "noop",
              detail={"serial": serial})
    return {"ok": True, "stopped": stopped}


@router.get("/scrcpy/audio-viewer/status")
async def audio_viewer_status(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return {"sessions": audio_capture.list_sessions()}


# ---------- Mirror-stream recording -------------------------------------
#
# Tees the live H.264 mirror (and the matching audio capture, if it
# happens to be running for the same device) into an MP4 on the
# laptop's recordings folder. Writes raw .h264 / .pcm files during
# capture and post-muxes with ffmpeg on stop — see mirror_recording.py
# for the rationale.

@router.post("/scrcpy/recorder/start")
async def recorder_start(request: Request,
                          _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    # Optional max-length safety net. 0 / missing = unlimited; negative
    # is rejected; positive values are clamped server-side to
    # mirror_recording.MAX_RECORDING_SECONDS so a hostile client can't
    # ask for a 10-day recording.
    raw_max = body.get("max_seconds", 0)
    try:
        max_seconds = int(raw_max)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_seconds must be an integer")
    if max_seconds < 0:
        raise HTTPException(status_code=400, detail="max_seconds must be >= 0")
    if max_seconds > mirror_recording.MAX_RECORDING_SECONDS:
        max_seconds = mirror_recording.MAX_RECORDING_SECONDS
    try:
        rec = await mirror_recording.start_recording(
            serial, max_seconds=max_seconds or None)
    except RuntimeError as e:
        # 409 covers both "already recording" and "no capture / no
        # ffmpeg / no slot" — the UI just shows the message verbatim.
        raise HTTPException(status_code=409, detail=str(e))
    audit.log("scrcpy", "recorder_start", ip=_client_ip(request),
              outcome="ok", detail={"serial": serial,
                                    "has_audio": rec.status()["has_audio"],
                                    "max_seconds": rec.status()["max_seconds"]})
    return {"ok": True, "recording": rec.status()}


@router.post("/scrcpy/recorder/extend")
async def recorder_extend(request: Request,
                           _token: str = Depends(require_auth)) -> Dict[str, Any]:
    """Push the auto-stop deadline back. Used by the 80%-warning
    "Keep recording" button — gives the user a one-click way to keep
    a long-running session going without aborting and restarting."""
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    raw = body.get("extra_seconds", 0)
    try:
        extra_seconds = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="extra_seconds must be an integer")
    # Strict route-level semantics: "extend" means push the deadline
    # back by a positive amount. Removing the cap entirely is a
    # different operation and should require an explicit gesture so
    # we don't accidentally turn a 5-minute recording into an
    # unlimited one because of a UI bug. The underlying
    # Recording.extend() still supports clear-cap for callers that
    # want it (e.g. future "make unlimited" button).
    if extra_seconds <= 0:
        raise HTTPException(status_code=400,
                            detail="extra_seconds must be > 0")
    if extra_seconds > mirror_recording.MAX_RECORDING_SECONDS:
        extra_seconds = mirror_recording.MAX_RECORDING_SECONDS
    info = await mirror_recording.extend_recording(serial, extra_seconds)
    if info is None:
        raise HTTPException(status_code=404,
                            detail="No recording in progress for this device.")
    audit.log("scrcpy", "recorder_extend", ip=_client_ip(request),
              outcome="ok", detail={"serial": serial,
                                    "extra_seconds": extra_seconds,
                                    "max_seconds": info.get("max_seconds")})
    return {"ok": True, "recording": info}


@router.post("/scrcpy/recorder/stop")
async def recorder_stop(request: Request,
                         _token: str = Depends(require_auth)) -> Dict[str, Any]:
    body = await request.json()
    serial = str(body.get("serial", "")).strip()
    if not SERIAL_RE.match(serial):
        raise HTTPException(status_code=400, detail="Invalid serial")
    info = await mirror_recording.stop_recording(serial)
    if info is None:
        return {"ok": True, "stopped": False, "recording": None}
    audit.log("scrcpy", "recorder_stop", ip=_client_ip(request),
              outcome="ok" if info.get("finalized") else "fail",
              detail={"serial": serial, "mp4_path": info.get("mp4_path"),
                      "mp4_size": info.get("mp4_size"),
                      "duration_sec": info.get("duration_sec"),
                      "error": info.get("last_error") or None})
    return {"ok": True, "stopped": True, "recording": info}


@router.get("/scrcpy/recorder/status")
async def recorder_status(serial: str = "",
                           _token: str = Depends(require_auth)) -> Dict[str, Any]:
    serial = serial.strip()
    if serial:
        if not SERIAL_RE.match(serial):
            raise HTTPException(status_code=400, detail="Invalid serial")
        rec = mirror_recording.get_recording(serial)
        return {"recording": rec.status() if rec is not None else None}
    return {"recordings": mirror_recording.list_recordings()}


# ---------- Saved-recording library (laptop-side MP4 browser) -----------
#
# These three endpoints back the "Recordings" card on the mirror-setup
# page: list every finalised MP4 in `~/.phone-bridge/recordings/`,
# stream one back so a `<video>` element can scrub it inline, and
# delete one by name.

@router.get("/scrcpy/recordings/list")
async def recordings_list(_token: str = Depends(require_auth)) -> Dict[str, Any]:
    return {"recordings": await mirror_recording.list_saved()}


@router.get("/scrcpy/recordings/file/{name}")
async def recordings_file(name: str,
                          _token: str = Depends(require_auth)):
    target = mirror_recording.saved_path(name)
    if target is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    # FileResponse handles Range requests so HTML5 video can seek/scrub
    # without downloading the whole file first.
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(target),
        media_type="video/mp4",
        filename=target.name,
    )


@router.delete("/scrcpy/recordings/file/{name}")
async def recordings_delete(name: str, request: Request,
                            _token: str = Depends(require_auth)) -> Dict[str, Any]:
    target = mirror_recording.saved_path(name)
    if target is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    ok = mirror_recording.delete_saved(name)
    audit.log("scrcpy", "recording_delete", ip=_client_ip(request),
              outcome="ok" if ok else "fail",
              detail={"name": name})
    if not ok:
        raise HTTPException(status_code=500, detail="Could not delete recording")
    return {"ok": True}


@ws_router.websocket("/ws/mirror-audio")
async def mirror_audio_ws(websocket: WebSocket) -> None:
    """Stream raw 16-bit LE PCM audio chunks for one device.

    Query params: `serial` (required), `ticket` (one-use connection ticket).
    First message is a JSON `hello` with sample rate / channels / bit
    depth. Subsequent binary messages are PCM byte chunks. An empty
    binary payload OR a JSON `ended` message means the capture stopped.
    """
    serial = (websocket.query_params.get("serial") or "").strip()
    ticket = websocket.query_params.get("ticket")
    ip = security.ws_client_ip(websocket)
    if not SERIAL_RE.match(serial):
        await websocket.close(code=4400)
        return
    session_token = consume_connection_ticket(ticket, websocket.url.path)
    if not session_token:
        audit.log("scrcpy", "mirror_audio_ws", ip=ip, outcome="auth_fail",
                  detail={"serial": serial})
        await websocket.close(code=4001)
        return
    if not security.check_rate(ip, "default"):
        audit.log("system", "rate_limit", ip=ip, outcome="blocked",
                  detail={"path": "/ws/mirror-audio"})
        await websocket.close(code=4029)
        return

    sess = audio_capture.get_session(serial)
    if sess is None or not sess.is_running():
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": "Audio capture is not running. Click the speaker "
                       "button to start it.",
        })
        await websocket.close(code=4404)
        return

    queue = await sess.add_subscriber()
    if queue is None:
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "message": f"Too many audio listeners (max {audio_capture.MAX_SUBSCRIBERS}).",
        })
        await websocket.close(code=4429)
        return

    await websocket.accept()
    audit.log("scrcpy", "mirror_audio_ws", ip=ip, outcome="connected",
              detail={"serial": serial})
    try:
        await websocket.send_json({
            "type": "hello",
            "serial": serial,
            "sample_rate": audio_capture.SAMPLE_RATE,
            "channels": audio_capture.CHANNELS,
            "bit_depth": audio_capture.BIT_DEPTH,
            "format": "pcm_s16le",
        })
    except Exception:  # noqa: BLE001
        pass

    async def send_chunks() -> None:
        while True:
            try:
                chunk = await queue.get()
            except asyncio.CancelledError:
                return
            if not chunk:
                try:
                    await websocket.send_json({"type": "ended"})
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                await websocket.send_bytes(chunk)
            except (WebSocketDisconnect, Exception):
                return

    async def recv_control() -> None:
        # Drain client messages so the recv buffer doesn't grow. The
        # protocol is one-way for now; future work (mute toggling,
        # gain control) would extend this.
        while True:
            try:
                await websocket.receive_text()
            except (WebSocketDisconnect, Exception):
                return

    try:
        await asyncio.gather(
            send_chunks(),
            recv_control(),
            security.ws_session_watchdog(websocket, session_token, "scrcpy",
                                         "mirror_audio_ws", ip),
            return_exceptions=True,
        )
    finally:
        await sess.remove_subscriber(queue)
        audit.log("scrcpy", "mirror_audio_ws", ip=ip, outcome="disconnected",
                  detail={"serial": serial})
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
